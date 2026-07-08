# handlers/draft.py
import dataclasses
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
import logging
from game.state import load_match_state, save_match_state, draw_player_for_turn, switch_turn, evict_match_cache
from game.models import Match, Player
from database import get_player
from utils.validators import validate_draft_action
from config import MAX_REDRAWS, POSITIONS_T20, POSITIONS_TEST, POSITIONS_FIFA, POSITIONS_WWE, DRAFT_BANNER_URL, DRAFT_BANNER_ODI, DRAFT_BANNER_INTL, DRAFT_BANNER_IPL, DRAFT_BANNER_TEST, DRAFT_BANNER_FIFA, DRAFT_BANNER_WWE
from utils.banners import get_banner_for_match, get_banner_for_mode
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)


logger = logging.getLogger(__name__)

# Cache for Banner File ID to prevent re-uploads
CACHED_BANNERS = {}

# Concurrency Control
PROCESSING_LOCKS = set()

# ── AFK Forfeit System ──────────────────────────────────────────────────
AFK_TASKS: dict = {}  # match_id -> asyncio.Task
AFK_TIMEOUT = 600    # 10 minutes

async def _afk_forfeit(match_id: str, expected_turn: int, bot, chat_id: int):
    """Fires after AFK_TIMEOUT seconds if the same player still hasn't moved."""
    await asyncio.sleep(AFK_TIMEOUT)
    try:
        match = await load_match_state(match_id)
        if not match or match.state != "DRAFTING":
            return
        if int(match.current_turn) != int(expected_turn):
            return  # Player moved before timeout

        afk_team = match.team_a if int(match.team_a.owner_id) == int(expected_turn) else match.team_b
        opp_team  = match.team_b if afk_team is match.team_a else match.team_a

        # Record loss for AFK player only; opponent gets no win/loss
        from database import update_user_stats, get_db
        try:
            await update_user_stats(afk_team.owner_id, afk_team.owner_name, "L", mode=match.mode)
        except Exception as e:
            logger.error(f"AFK forfeit stats update failed: {e}")

        match.state = "FINISHED"
        import time as _t
        match.finished_at = _t.time()
        await save_match_state(match)
        evict_match_cache(match_id)
        from utils.rate_limit import debouncer
        debouncer.cancel_updates(chat_id, match.draft_message_id)

        msg = f"💤 *{esc(afk_team.owner_name)} forfeited due to being AFK for 10 mins.*"
        try:
            if match.draft_message_id:
                try:
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=match.draft_message_id,
                        caption=msg,
                        reply_markup=None,
                        parse_mode="Markdown"
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=match.draft_message_id,
                            text=msg,
                            reply_markup=None,
                            parse_mode="Markdown"
                        )
                    except Exception:
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception:
            pass
        # Cleanup pinned board and DB record
        try:
            if getattr(match, 'pinned_message_id', None):
                await bot.unpin_chat_message(chat_id=chat_id, message_id=match.pinned_message_id)
        except Exception:
            pass
        try:
            db = get_db()
            await db.matches.delete_one({"match_id": match_id})
        except Exception:
            pass
    except Exception as e:
        logger.error(f"_afk_forfeit error for {match_id}: {e}")
    finally:
        AFK_TASKS.pop(match_id, None)

def _reset_afk_timer(match: Match, bot, chat_id: int):
    """Cancel any existing AFK task and start a fresh 10-min timer for current turn."""
    old = AFK_TASKS.pop(match.match_id, None)
    if old and not old.done():
        old.cancel()
    match.turn_deadline = time.time() + AFK_TIMEOUT
    asyncio.create_task(save_match_state(match))
    task = asyncio.create_task(
        _afk_forfeit(match.match_id, int(match.current_turn), bot, chat_id)
    )
    AFK_TASKS[match.match_id] = task

def start_forfeit_timer_on_startup(match: Match, bot):
    """Reschedule the AFK forfeit timer on bot startup based on remaining turn_deadline."""
    if match.state != "DRAFTING":
        return
    now = time.time()
    if match.turn_deadline > 0:
        remaining = match.turn_deadline - now
        if remaining <= 0:
            # Already expired, trigger forfeit immediately
            asyncio.create_task(_afk_forfeit(match.match_id, int(match.current_turn), bot, match.chat_id))
            return
    else:
        remaining = AFK_TIMEOUT
        match.turn_deadline = now + AFK_TIMEOUT
        asyncio.create_task(save_match_state(match))

    old = AFK_TASKS.pop(match.match_id, None)
    if old and not old.done():
        old.cancel()

    async def _afk_forfeit_startup():
        await asyncio.sleep(remaining)
        try:
            m = await load_match_state(match.match_id)
            if m and m.state == "DRAFTING" and int(m.current_turn) == int(match.current_turn):
                await _afk_forfeit(match.match_id, int(match.current_turn), bot, match.chat_id)
        except Exception:
            pass
    AFK_TASKS[match.match_id] = asyncio.create_task(_afk_forfeit_startup())


async def handle_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    parts = data.split('_')
    action = parts[0]
    

    
    # Parsing ID logic — use | as separator between match_id and slot
    # to safely handle slot names with spaces (e.g. "All Rounder", "High Flyer")
    # Backward-compat: old matches before the | fix still send _ separator
    if action == "assign":
        # format: assign_{match_id}|{slot}  (new)
        # format: assign_{match_id}_{slot}  (old, slot has no spaces in this path)
        if '|' in data:
            pipe_idx = data.index('|')
            slot = data[pipe_idx + 1:]
            match_id = data[len('assign_'):pipe_idx]
        else:
            # Old format fallback: last underscore-separated token is the slot
            # match_id = ownerid_timestamp (2 parts), slot is the rest
            match_id = f"{parts[1]}_{parts[2]}"
            slot = "_".join(parts[3:])
    elif action == "replace":
        sub = parts[1]
        if sub == "exec":
            # format: replace_exec_{match_id}|{slot}  (new)
            # format: replace_exec_{match_id}_{slot}  (old)
            if '|' in data:
                pipe_idx = data.index('|')
                slot = data[pipe_idx + 1:]
                match_id = data[len('replace_exec_'):pipe_idx]
            else:
                match_id = f"{parts[2]}_{parts[3]}"
                slot = "_".join(parts[4:])
        else:
            match_id = "_".join(parts[2:])
    else:
        # draw / redraw
        match_id = "_".join(parts[1:])
        

    
    # Locking
    if match_id in PROCESSING_LOCKS:
        logger.warning(f"DEBUG: Locked request ignored for {match_id}")
        await query.answer("⏳ Processing previous action...", show_alert=False)
        return
        
    PROCESSING_LOCKS.add(match_id)

    async def safe_answer(text, alert=True):
        try:
            await query.answer(text, show_alert=alert)
        except Exception:
            pass # Ignore expiry
    
    try:
        match = await load_match_state(match_id)
        if not match:
            logger.error(f"DEBUG: Match not found! ID: {match_id}")
            await safe_answer("⚠️ Match ended or expired (Admin reset or maintenance).", alert=True)
            return
            
        # Check turn — cast both to int to guard against str/int type mismatch from MongoDB
        if int(query.from_user.id) != int(match.current_turn):
            await safe_answer("Turn passed! Board updating...", alert=True)
            return

        # Turn is correct — answer immediately to stop spinner
        try:
            await query.answer()
        except Exception:
            pass
    
        if action == "draw":
            await handle_draw(update, context, match)
        
        elif action == "assign":
            if not match.pending_player_id:
                # Double-check state in case of race?
                await safe_answer("Player already assigned! Please wait...", alert=True)
                return
            await handle_assign(update, context, match, match.pending_player_id, slot)
            
        elif action == "redraw":
            await handle_redraw(update, context, match)
            
        elif action == "replace":
            sub = parts[1]
            if sub == "start":
                await handle_replace_start(update, context, match)
            elif sub == "exec":
                await handle_replace_exec(update, context, match, slot)
            elif sub == "cancel":
                await handle_replace_cancel(update, context, match)
            
    except Exception as e:
        logger.error(f"Error in draft handler: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if match_id in PROCESSING_LOCKS:
            PROCESSING_LOCKS.remove(match_id)


def format_draft_board(match: Match, include_turn: bool = True) -> str:
    """Creates the text for the draft board (Static UI Rule 1)."""
    def format_team(team):
        lines = [f"🔵 {esc(team.owner_name)}" if team == match.team_a else f"🔴 {esc(team.owner_name)}"]
        for slot, player in team.slots.items():
            val = esc(player.name) if player else ". . ."
            lines.append(f"• {slot}: {val}")
        return "\n".join(lines)

    board = f"🏁 *Drafting Phase*\n\n"
    board += format_team(match.team_a) + "\n\n"
    board += format_team(match.team_b)

    if include_turn:
        current_name = match.team_a.owner_name if match.current_turn == match.team_a.owner_id else match.team_b.owner_name
        board += f"\n\n🎯 *Turn:* {esc(current_name)}"

    return board

import asyncio
from telegram.error import RetryAfter
from utils.rate_limit import debouncer

async def update_draft_message(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, caption: str, keyboard: list, media=None, synchronous: bool = False):
    """
    Unified handler to update the draft message using the Rate Limiter (Debouncer).
    Logic:
    - If no message exists, send a new one synchronously.
    - If message exists and synchronous=True, edit it immediately to prevent race conditions.
    - If message exists and synchronous=False, push the update to the Debouncer queue to prevent Error 429.
    """
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 1. Initial Creation (Synchronous)
    if not match.draft_message_id:
        if media:
             msg = await context.bot.send_photo(chat_id=match.chat_id, photo=media, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
        else:
             msg = await context.bot.send_message(chat_id=match.chat_id, text=caption, reply_markup=reply_markup, parse_mode="Markdown")
        
        match.draft_message_id = msg.message_id
        # Auto-pin the draft board in background
        async def _bg_pin():
            try:
                await context.bot.pin_chat_message(
                    chat_id=match.chat_id,
                    message_id=msg.message_id,
                    disable_notification=True
                )
                from game.state import load_match_state as _bg_load, save_match_state as _bg_save
                m = await _bg_load(match.match_id)
                if m:
                    m.pinned_message_id = msg.message_id
                    await _bg_save(m)
            except Exception:
                pass
        import asyncio
        asyncio.create_task(_bg_pin())
        
        # Start abandon timeout
        async def _abandon_timeout(bot, chat_id, msg_id, match_id, delay=1800):
            await asyncio.sleep(delay)
            from game.state import load_match_state
            m = await load_match_state(match_id)
            if not m or m.state in ["DRAFTING", "READY_CHECK"]:
                try:
                    await bot.unpin_chat_message(chat_id=chat_id, message_id=msg_id)
                    if m:
                        from database import get_db
                        await get_db().matches.delete_one({"match_id": match_id})
                except Exception:
                    pass
        asyncio.create_task(_abandon_timeout(context.bot, match.chat_id, msg.message_id, match.match_id))
        await save_match_state(match)
        return

    # 2. Synchronous Edit
    if synchronous:
        try:
            if media:
                await context.bot.edit_message_media(
                    chat_id=match.chat_id,
                    message_id=match.draft_message_id,
                    media=InputMediaPhoto(media=media, caption=caption, parse_mode="Markdown"),
                    reply_markup=reply_markup
                )
            else:
                await context.bot.edit_message_caption(
                    chat_id=match.chat_id,
                    message_id=match.draft_message_id,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning(f"Synchronous draft board edit failed: {e}. Falling back to recreation...")
            await debouncer._recreate_message(match, context.bot, caption, reply_markup, media, "Markdown")
        return

    # 3. Batched Editing (Asynchronous)
    await debouncer.schedule_update(match, context.bot, caption, reply_markup, media=media, parse_mode="Markdown")



async def handle_draw(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    # Prevent double-draw if already pending
    player = None
    if match.pending_player_id:
        p_data = await get_player(match.pending_player_id)
        if p_data:
            # logger.info(f"DEBUG: Draw Request Idempotency...")
            player = p_data
    
    if not player:
        player = await draw_player_for_turn(match)
        
    if not player:
        try:
            await update.callback_query.answer("No eligible players left!", show_alert=True)
        except: pass
        return
        
    match.pending_player_id = player['player_id']
    # Background the DB save — user doesn't need to wait for it.
    import asyncio as _aio
    _aio.create_task(save_match_state(match))
    # NOTE: AFK timer is reset AFTER update_draft_message below,
    # ensuring the player sees the assign buttons before the 10-min clock starts.
    
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    # UI: Show Player Card in the same message
    # Rule 2: Strict Caption Format
    # ✨ ⚔️ <CurrentPlayerName>'s turn
    # Pulled: <Cricketer Name>
    # Assign a position:
    card_caption = f"✨ ⚔️ {esc(current_team.owner_name)}'s turn\nPulled: {esc(player['name'])}\nAssign a position:"
    
    # Buttons for Card
    keyboard = []
    
    if match.mode == "FIFA":
        active_positions = POSITIONS_FIFA
    elif "WWE" in match.mode:
        active_positions = POSITIONS_WWE
    elif "Test" in match.mode:
        active_positions = POSITIONS_TEST
    else:
        active_positions = POSITIONS_T20
        
    row = []
    for pos in active_positions:
        if not current_team.slots.get(pos):
            # Unfilled -> Enabled
            row.append(InlineKeyboardButton(f"🟢 {pos}", callback_data=f"assign_{match.match_id}|{pos}"))
        # else: Do not append (Hidden)
             
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    # Footer Actions (Skip & Replace)
    footer_row = []
    if current_team.redraws_remaining > 0:
        footer_row.append(InlineKeyboardButton(f"🗑 Skip ({current_team.redraws_remaining})", callback_data=f"redraw_{match.match_id}"))
    
    if current_team.replacements_remaining > 0 and any(current_team.slots.values()):
        footer_row.append(InlineKeyboardButton(f"♻️ Replace ({current_team.replacements_remaining})", callback_data=f"replace_start_{match.match_id}"))
        
    if footer_row:
        keyboard.append(footer_row)
    
    # Get Player Image — reuse already-fetched player data (no duplicate DB call)
    p_data = player
    
    # Image Key Logic
    if match.mode == "FIFA":
        img_key = 'fifa_image_url' 
        # Prefer file_id if available (updated manually)
        if p_data.get('image_file_id'):
            img_key = 'image_file_id'
        default_banner = DRAFT_BANNER_FIFA
    elif "WWE" in match.mode:
        img_key = 'wwe_image_url'
        if p_data.get('image_file_id'):
            img_key = 'image_file_id'
        default_banner = DRAFT_BANNER_WWE
    elif match.mode == "Test":
        img_key = 'test_image_url'
        if not p_data.get(img_key):
            img_key = 'image_file_id'
        default_banner = DRAFT_BANNER_TEST
    else:
        img_key = 'ipl_image_file_id' if "IPL" in match.mode else 'image_file_id'
        # Fallback to normal image if IPL image missing
        if "IPL" in match.mode and not p_data.get(img_key):
            img_key = 'image_file_id'
        if "IPL" in match.mode:
            default_banner = DRAFT_BANNER_IPL
        else:
            default_banner = DRAFT_BANNER_ODI
    
    media = p_data.get(img_key) or default_banner
    
    # Update the single message to show the card
    await update_draft_message(update, context, match, card_caption, keyboard, media=media)
    # Reset AFK timer AFTER UI is queued — player has 10 min to assign
    _reset_afk_timer(match, context.bot, match.chat_id)


async def handle_assign(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, player_id: str, slot: str):
    
    p_data = await get_player(player_id)
    if not p_data:
        # Rare edge case: player deleted from DB while match was in progress.
        # Return without changing match state so the player can retry by clicking again.
        logger.error(f"handle_assign: player {player_id} not found in DB — match {match.match_id}")
        try:
            await update.callback_query.answer(
                "⚠️ Error loading player data. Please click the position again to retry.",
                show_alert=True
            )
        except Exception:
            pass
        return
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b

    # Filter p_data to only known fields
    known_fields = {f.name for f in dataclasses.fields(Player)}
    filtered_data = {k: v for k, v in p_data.items() if k in known_fields}
    
    # Assign
    current_team.slots[slot] = Player(**filtered_data)

    # REMOVE FROM POOL
    if player_id in match.draft_pool:
        match.draft_pool.remove(player_id)
        match.draft_pool_removed.append(player_id)  # Delta tracking
    else:
        logger.warning(f"DEBUG: {player_id} was assigned but not found in pool!")

    match.pending_player_id = None
    
    # Check Complete
    if match.team_a.is_complete() and match.team_b.is_complete():
        import time
        match.state = "READY_CHECK"
        match.draft_completed_at = time.time()  # Timestamp for 5-min auto-ready
        await save_match_state(match)

        # Start 5-min auto-simulate timer (live — not just on startup recovery)
        async def _auto_ready_live(bot, match_id, chat_id):
            await asyncio.sleep(300)  # 5 minutes
            from game.state import load_match_state, save_match_state as _save
            from game.simulation import run_simulation
            m = await load_match_state(match_id)
            if not m or m.state != "READY_CHECK":
                return  # Already simulated or cancelled
            try:
                import time as _t
                m.state = "SIMULATING"
                m.team_a.is_ready = True
                m.team_b.is_ready = True
                await _save(m)
                result_text = await run_simulation(m)  # async, returns str
                m.state = "FINISHED"
                m.finished_at = _t.time()
                await _save(m)
                msg = f"⏰ *Auto-Ready triggered (5min timeout)*\n\n{result_text}"
                try:
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception:
                    try:
                        await bot.send_message(chat_id=chat_id, text=msg)
                    except Exception:
                        pass
                pinned = getattr(m, 'pinned_message_id', None)
                if pinned:
                    try:
                        await bot.unpin_chat_message(chat_id=chat_id, message_id=pinned)
                    except Exception:
                        pass
                try:
                    from database import get_db
                    await get_db().matches.delete_one({"match_id": match_id})
                    evict_match_cache(match_id)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Auto-ready live failed for {match_id}: {e}")


        asyncio.create_task(_auto_ready_live(context.bot, match.match_id, match.chat_id))

        # Draft done — cancel AFK timer (no more turns)
        old_afk = AFK_TASKS.pop(match.match_id, None)
        if old_afk and not old_afk.done():
            old_afk.cancel()

        board_text = format_draft_board(match)
        # Final Board Update
        keyboard = [[InlineKeyboardButton("🚀 READY", callback_data=f"ready_{match.match_id}")]]
        
        # Add Swap button as a direct DM deep-link (each team gets 1 swap)
        a_swaps = getattr(match.team_a, 'swaps_used', 0)
        b_swaps = getattr(match.team_b, 'swaps_used', 0)
        if a_swaps < 1 or b_swaps < 1:
            bot_uname = context.bot.username
            swap_url = f"https://t.me/{bot_uname}?start=swap_{match.match_id}"
            keyboard.append([InlineKeyboardButton("🔀 Swap Positions (1 Left)", url=swap_url)])
        banner = await get_banner_for_match(match)
        await update_draft_message(update, context, match, f"{format_draft_board(match, include_turn=False)}\n\n✅ *Draft Complete!* Waiting for Ready...", keyboard, media=banner)
        return

    # Switch Turn
    await switch_turn(match)
    
    # Update Board for Next Turn (Restore Draw Button and Banner)
    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
    
    banner = await get_banner_for_match(match)
    await update_draft_message(update, context, match, board_text, keyboard, media=banner)
    # Reset AFK timer AFTER UI is queued — ensures next player sees Draw button before clock starts
    _reset_afk_timer(match, context.bot, match.chat_id)


async def handle_redraw(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    if current_team.redraws_remaining > 0:
        current_team.redraws_remaining -= 1
        
        # Permanent Discard Logic
        if match.pending_player_id:
            if match.pending_player_id in match.draft_pool:
                match.draft_pool.remove(match.pending_player_id)
                match.draft_pool_removed.append(match.pending_player_id)  # Delta tracking
            match.pending_player_id = None
        
        # Switch Turn
        await switch_turn(match)
        
        # Update Board (Restore Banner)
        board_text = format_draft_board(match)
        keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
        
        banner = await get_banner_for_match(match)
        await update_draft_message(update, context, match, f"{board_text}\n\n⏩ {esc(current_team.owner_name)} Skipped! Turn Consumed.", keyboard, media=banner)
        # Reset AFK timer AFTER UI is queued — ensures next player sees Draw button before clock starts
        _reset_afk_timer(match, context.bot, match.chat_id)
        
    else:
        try:
            await update.callback_query.answer("No skips left!", show_alert=True)
        except: pass

async def handle_replace_start(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    if current_team.replacements_remaining <= 0:
        try:
            await update.callback_query.answer("No replacements left!", show_alert=True)
        except: pass
        return

    # Check if we have a pending player (should be there)
    if not match.pending_player_id:
        try:
            await update.callback_query.answer("No player drawn!", show_alert=True)
        except: pass
        return
        
    # Get Player Data
    player = await get_player(match.pending_player_id)
    
    # UI: Show Filled Slots to Replace
    card_caption = f"♻️ *Replacing Player*\nNew Player: {esc(player['name'])}\n\nSelect a position to replace:"
    
    keyboard = []
    
    # Show active filled positions
    if match.mode == "FIFA":
        active_positions = POSITIONS_FIFA
    elif "WWE" in match.mode:
        active_positions = POSITIONS_WWE
    elif "Test" in match.mode:
        active_positions = POSITIONS_TEST
    else:
        active_positions = POSITIONS_T20
        
    row = []
    for pos in active_positions:
        if current_team.slots.get(pos):
             # Filled -> Eligible for replace
             # Show who is currently there? "Pos: PlayerName"
             current_p = current_team.slots.get(pos)
             btn_text = f"🔴 {pos}: {current_p.name}"
             row.append(InlineKeyboardButton(btn_text, callback_data=f"replace_exec_{match.match_id}|{pos}"))
             
        if len(row) == 1: # 1 per row for readability since names can be long
             keyboard.append(row)
             row = []
    if row: keyboard.append(row)
    
    # Cancel Button
    keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data=f"replace_cancel_{match.match_id}")])
    
    # Reuse media (banner or player card)
    # We should probably show the player card of the NEW player to keep context
    
    if "IPL" in match.mode:
        img_key = 'ipl_image_file_id'
        if not player.get(img_key): img_key = 'image_file_id'
    elif match.mode == "FIFA":
        img_key = 'fifa_image_url'
        # Prefer file_id if manually updated
        if player.get('image_file_id'):
            img_key = 'image_file_id'
    elif "WWE" in match.mode:
        img_key = 'wwe_image_url'
        if player.get('image_file_id'):
            img_key = 'image_file_id'
    elif match.mode == "Test":
        img_key = 'test_image_url'
        if not player.get(img_key):
            img_key = 'image_file_id'
    else:
        img_key = 'image_file_id'
        

        
    if "IPL" in match.mode:
        default_banner = DRAFT_BANNER_IPL
    elif match.mode == "FIFA":
        default_banner = DRAFT_BANNER_FIFA
    elif "WWE" in match.mode:
        default_banner = DRAFT_BANNER_WWE
    elif match.mode == "Test":
        default_banner = DRAFT_BANNER_TEST
    else:  # ODI
        default_banner = DRAFT_BANNER_ODI
    media = player.get(img_key) or default_banner
    
    await update_draft_message(update, context, match, card_caption, keyboard, media=media)

async def handle_replace_exec(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, slot: str):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    # Validation
    if current_team.replacements_remaining <= 0:
        try:
            await update.callback_query.answer("No replacements left!", show_alert=True)
        except: pass
        return
        
    old_player = current_team.slots.get(slot)
    if not old_player:
        try:
            await update.callback_query.answer("Slot is empty! Cannot replace.", show_alert=True)
        except: pass
        return
        
    new_player_data = await get_player(match.pending_player_id)
    if not new_player_data:
        try:
             await update.callback_query.answer("Error: Pending player lost. Please redraw.", show_alert=True)
             # Should probably reset state or redraw?
        except: pass
        return
    
    import dataclasses
    known_fields = {f.name for f in dataclasses.fields(Player)}
    filtered_data = {k: v for k, v in new_player_data.items() if k in known_fields}
    
    new_player = Player(**filtered_data)
    
    # Execute Replace

    current_team.slots[slot] = new_player
    current_team.replacements_remaining -= 1

    # REMOVE OLD PENDING FROM POOL (The new player)
    if match.pending_player_id in match.draft_pool:
        match.draft_pool.remove(match.pending_player_id)
        match.draft_pool_removed.append(match.pending_player_id)  # Delta tracking
        logger.info(f"DEBUG: Removed {match.pending_player_id} from pool on Replace.")
    
    match.pending_player_id = None
    
    # Switch Turn
    await switch_turn(match)
    await save_match_state(match)
    
    # Update Board
    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
    

    
    
    banner = await get_banner_for_match(match)
    await update_draft_message(update, context, match, f"{board_text}\n\n♻️ {esc(current_team.owner_name)} replaced {esc(old_player.name)} with {esc(new_player.name)}!", keyboard, media=banner)
    # Reset AFK timer AFTER UI is queued — ensures next player sees Draw button before clock starts
    _reset_afk_timer(match, context.bot, match.chat_id)

async def handle_replace_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    # Just go back to draw view
    await handle_draw(update, context, match)
