# handlers/draft.py
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
import logging
from game.state import load_match_state, save_match_state, draw_player_for_turn, switch_turn
from game.models import Match
from utils.validators import validate_draft_action
from config import MAX_REDRAWS, POSITIONS_T20, POSITIONS_TEST, POSITIONS_FIFA, DRAFT_BANNER_URL, DRAFT_BANNER_INTL, DRAFT_BANNER_IPL, DRAFT_BANNER_FIFA
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)


logger = logging.getLogger(__name__)

# Cache for Banner File ID to prevent re-uploads
CACHED_BANNERS = {}

# Concurrency Control
PROCESSING_LOCKS = set()

async def handle_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    parts = data.split('_')
    action = parts[0]
    
    logger.info(f"DEBUG: Processing Callback. Data={data} Parts={parts}")
    
    # Parsing ID logic
    if action == "assign":
        slot = parts[-1]
        match_id = "_".join(parts[1:-1])
    elif action == "replace":
        sub = parts[1]
        if sub == "exec":
             slot = parts[-1]
             match_id = "_".join(parts[2:-1])
        else:
             match_id = "_".join(parts[2:])
    else:
        # draw / redraw
        match_id = "_".join(parts[1:])
        
    logger.info(f"DEBUG: Parsed MatchID={match_id} Action={action}")
    
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
            await safe_answer(f"Match ended or expired. ({match_id})", alert=True)
            return
            
        # Check turn
        if query.from_user.id != match.current_turn:
            await safe_answer("Turn passed! Board updating...", alert=True)
            return
    
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


def format_draft_board(match: Match) -> str:
    """Creates the text for the draft board (Static UI Rule 1)."""
    def format_team(team):
        lines = [f"🔵 {esc(team.owner_name)}" if team == match.team_a else f"🔴 {esc(team.owner_name)}"]
        for slot, player in team.slots.items():
            val = esc(player.name) if player else ". . ."
            lines.append(f"• {slot}: {val}")
        return "\n".join(lines)

    board = f"🏁 *Drafting Phase*\n\n"
    board += format_team(match.team_a) + "\n\n"
    board += format_team(match.team_b) + "\n\n"
    
    current_name = match.team_a.owner_name if match.current_turn == match.team_a.owner_id else match.team_b.owner_name
    board += f"🎯 *Turn:* {esc(current_name)}"

    return board

import asyncio
from telegram.error import RetryAfter
from utils.rate_limit import debouncer

async def update_draft_message(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, caption: str, keyboard: list, media=None):
    """
    Unified handler to update the draft message using the Rate Limiter (Debouncer).
    Logic:
    - If no message exists, send a new one synchronously.
    - If message exists, push the update to the Debouncer queue to prevent Error 429.
    """
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 1. Initial Creation (Synchronous)
    if not match.draft_message_id:
        if media:
             msg = await context.bot.send_photo(chat_id=match.chat_id, photo=media, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
        else:
             msg = await context.bot.send_message(chat_id=match.chat_id, text=caption, reply_markup=reply_markup, parse_mode="Markdown")
        
        try:
            await context.bot.pin_chat_message(chat_id=match.chat_id, message_id=msg.message_id)
        except:
             pass
        match.draft_message_id = msg.message_id
        await save_match_state(match)
        return

    # 2. Batched Editing (Asynchronous)
    await debouncer.schedule_update(match, context.bot, caption, reply_markup, media=media, parse_mode="Markdown")



async def handle_draw(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    # Prevent double-draw if already pending
    player = None
    if match.pending_player_id:
        from database import get_player
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
    await save_match_state(match)
    
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
    elif "Test" in match.mode:
        active_positions = POSITIONS_TEST
    else:
        active_positions = POSITIONS_T20
        
    row = []
    for pos in active_positions:
        if not current_team.slots.get(pos):
            # Unfilled -> Enabled
            row.append(InlineKeyboardButton(f"🟢 {pos}", callback_data=f"assign_{match.match_id}_{pos}"))
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
    
    # Get Player Image
    from database import get_player
    p_data = await get_player(player['player_id'])
    
    # Image Key Logic
    if match.mode == "FIFA":
        img_key = 'fifa_image_url' 
        # Prefer file_id if available (updated manually)
        if p_data.get('image_file_id'):
            img_key = 'image_file_id'
        
        default_banner = DRAFT_BANNER_FIFA
        media = p_data.get(img_key, default_banner)
    else:
        img_key = 'ipl_image_file_id' if "IPL" in match.mode else 'image_file_id'
        # Fallback to normal image if IPL image missing
        if "IPL" in match.mode and not p_data.get(img_key):
            img_key = 'image_file_id'
        default_banner = DRAFT_BANNER_IPL if "IPL" in match.mode else DRAFT_BANNER_INTL
    
    media = p_data.get(img_key, default_banner)
    
    # Update the single message to show the card
    await update_draft_message(update, context, match, card_caption, keyboard, media=media)


async def handle_assign(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, player_id: str, slot: str):
    from database import get_player
    from game.models import Player
    
    p_data = await get_player(player_id)
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    # Filter p_data to only known fields
    import dataclasses
    known_fields = {f.name for f in dataclasses.fields(Player)}
    filtered_data = {k: v for k, v in p_data.items() if k in known_fields}
    
    # Assign
    current_team.slots[slot] = Player(**filtered_data)

    # REMOVE FROM POOL
    if player_id in match.draft_pool:
        match.draft_pool.remove(player_id)
        logger.info(f"DEBUG: Removed {player_id} from pool on Assignment.")
    else:
        logger.warning(f"DEBUG: {player_id} was assigned but not found in pool!")

    match.pending_player_id = None
    
    # Check Complete
    if match.team_a.is_complete() and match.team_b.is_complete():
        match.state = "READY_CHECK"
        await save_match_state(match)
        
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
        # Banner for Final Screen (To keep Single Pin)
        # Determine Banner
        if "IPL" in match.mode:
            banner = DRAFT_BANNER_IPL
        elif match.mode == "FIFA":
            banner = DRAFT_BANNER_FIFA
        else:
            banner = DRAFT_BANNER_INTL
            
        await update_draft_message(update, context, match, f"{board_text}\n\n✅ **Draft Complete!** Waiting for Ready...", keyboard, media=banner)
        return

    # Switch Turn
    await switch_turn(match)
    await save_match_state(match)
    
    # Update Board for Next Turn (Restore Draw Button and Banner)
    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
    
    # Determine Banner
    if "IPL" in match.mode:
        banner = DRAFT_BANNER_IPL
    elif match.mode == "FIFA":
        banner = DRAFT_BANNER_FIFA
    else:
        banner = DRAFT_BANNER_INTL
    
    # Update
    await update_draft_message(update, context, match, board_text, keyboard, media=banner)


async def handle_redraw(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    if current_team.redraws_remaining > 0:
        current_team.redraws_remaining -= 1
        
        # Permanent Discard Logic
        if match.pending_player_id:
            # Debug Logs
            logger.info(f"DEBUG: Skipping Player {match.pending_player_id}. Pool Size Before: {len(match.draft_pool)}")
            
            # Remove from pool if present
            if match.pending_player_id in match.draft_pool:
                match.draft_pool.remove(match.pending_player_id)
                logger.info(f"DEBUG: Permanently discarded {match.pending_player_id} from pool. New Size: {len(match.draft_pool)}")
            else:
                 logger.warning(f"DEBUG: Skipped Player {match.pending_player_id} NOT found in Draft Pool!")
            
            match.pending_player_id = None
        
        # Switch Turn
        await switch_turn(match)
        await save_match_state(match)
        
        # Update Board (Restore Banner)
        board_text = format_draft_board(match)
        keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
        
        # Determine Banner
        if "IPL" in match.mode:
            banner = DRAFT_BANNER_IPL
        elif match.mode == "FIFA":
            banner = DRAFT_BANNER_FIFA
        else:
            banner = DRAFT_BANNER_INTL

        # Update Board (Restore Banner)
        await update_draft_message(update, context, match, f"{board_text}\n\n⏩ {esc(current_team.owner_name)} Skipped! Turn Consumed.", keyboard, media=banner)
        
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
    from database import get_player
    player = await get_player(match.pending_player_id)
    
    # UI: Show Filled Slots to Replace
    card_caption = f"♻️ *Replacing Player*\nNew Player: {esc(player['name'])}\n\nSelect a position to replace:"
    
    keyboard = []
    
    # Show active filled positions
    if match.mode == "FIFA":
        active_positions = POSITIONS_FIFA
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
             row.append(InlineKeyboardButton(btn_text, callback_data=f"replace_exec_{match.match_id}_{pos}"))
             
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
    else:
        img_key = 'image_file_id'
        

        
    default_banner = DRAFT_BANNER_IPL if "IPL" in match.mode else (DRAFT_BANNER_FIFA if match.mode == "FIFA" else DRAFT_BANNER_INTL)
    media = player.get(img_key, default_banner)
    
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
        
    from database import get_player
    from game.models import Player
    
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
        logger.info(f"DEBUG: Removed {match.pending_player_id} from pool on Replace.")
    
    match.pending_player_id = None
    
    # Switch Turn
    await switch_turn(match)
    await save_match_state(match)
    
    # Update Board
    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
    

    
    
    if "IPL" in match.mode:
        banner = DRAFT_BANNER_IPL
    elif match.mode == "FIFA":
        banner = DRAFT_BANNER_FIFA
    else:
        banner = DRAFT_BANNER_INTL
    
    await update_draft_message(update, context, match, f"{board_text}\n\n♻️ {esc(current_team.owner_name)} replaced {esc(old_player.name)} with {esc(new_player.name)}!", keyboard, media=banner)

async def handle_replace_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    # Just go back to draw view
    await handle_draw(update, context, match)
