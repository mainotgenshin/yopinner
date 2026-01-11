# handlers/draft.py
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
import logging
from game.state import load_match_state, save_match_state, draw_player_for_turn, switch_turn
from game.models import Match
from utils.validators import validate_draft_action
from config import MAX_REDRAWS, POSITIONS_T20, POSITIONS_TEST, DRAFT_BANNER_URL


logger = logging.getLogger(__name__)

# Cache for Banner File ID to prevent re-uploads
# Cache for Banner File ID to prevent re-uploads
CACHED_BANNER_ID = None

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
    
    try:
        match = load_match_state(match_id)
        if not match:
            logger.error(f"DEBUG: Match not found! ID: {match_id}")
            await query.answer(f"Match ended or expired. ({match_id})", show_alert=True)
            return
            
        # Check turn
        if query.from_user.id != match.current_turn:
            await query.answer("Not your turn!", show_alert=True)
            return
    
        if action == "draw":
            await handle_draw(update, context, match)
        
        elif action == "assign":
            if not match.pending_player_id:
                # Double-check state in case of race?
                await query.answer("No player drawn! Click Draw first.", show_alert=True)
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
        lines = [f"🔵 {team.owner_name}" if team == match.team_a else f"🔴 {team.owner_name}"]
        for slot, player in team.slots.items():
            val = player.name if player else ". . ."
            lines.append(f"• {slot}: {val}")
        return "\n".join(lines)

    board = f"🏁 **Drafting Phase**\n\n"
    board += format_team(match.team_a) + "\n\n"
    board += format_team(match.team_b) + "\n\n"
    
    current_name = match.team_a.owner_name if match.current_turn == match.team_a.owner_id else match.team_b.owner_name
    board += f"🎯 **Turn:** {current_name}"

    return board

import asyncio
from telegram.error import RetryAfter

async def update_draft_message(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, caption: str, keyboard: list, media=None):
    """
    Unified handler to update the draft message.
    Logic:
    - If `media` is None -> Board View (Text Message)
    - If `media` is URL -> Card View (Photo Message)
    - If type changes (Text <-> Photo), Delete & Resend.
    - If type same, Edit.
    """
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Validation
    if not match.draft_message_id:
        # Fallback: Send new
        if media:
             msg = await context.bot.send_photo(chat_id=match.chat_id, photo=media, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
        else:
             msg = await context.bot.send_message(chat_id=match.chat_id, text=caption, reply_markup=reply_markup, parse_mode="Markdown")
        
        try:
            await context.bot.pin_chat_message(chat_id=match.chat_id, message_id=msg.message_id)
        except:
             pass
        match.draft_message_id = msg.message_id
        save_match_state(match)
        return

    # Attempt to Edit
    
    try:
        if media:
             # Want Photo
             # Try Media Edit (assuming previous was Photo)
             await context.bot.edit_message_media(
                chat_id=match.chat_id,
                message_id=match.draft_message_id,
                media=InputMediaPhoto(media=media, caption=caption, parse_mode="Markdown"),
                reply_markup=reply_markup
             )
        else:
             # Want Text
             # Try Text Edit (assuming previous was Text)
             await context.bot.edit_message_text(
                chat_id=match.chat_id,
                message_id=match.draft_message_id,
                text=caption, # 'text' not 'caption'
                reply_markup=reply_markup,
                parse_mode="Markdown"
             )
             
    except Exception as e:
        err = str(e)
        logger.warning(f"Draft Message Update Error: {err}")
        
        # Check if type mismatch OR invalid file ID ("Wrong file identifier")
        is_type_mismatch = (
            "not a text message" in err 
            or "not a media message" in err 
            or "no caption" in err 
            or "photo" in err
            or "There is no text" in err
            or "Wrong file identifier" in err
            or "Media_empty" in err
            or "Bad Request" in err
        )
        
        if is_type_mismatch or "not found" in err:
             logger.info(f"Switching Message Type or Recovering from Error (Error: {err})")
             # Delete Old
             try:
                 await context.bot.delete_message(chat_id=match.chat_id, message_id=match.draft_message_id)
             except:
                 pass
             
             # Send New - with Safety Fallback
             try:
                 if media and str(media).strip(): # Ensure media is not empty string
                      try:
                          msg = await context.bot.send_photo(chat_id=match.chat_id, photo=media, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
                      except Exception as media_err:
                          logger.error(f"Failed to send media: {media_err}. Falling back to Text.")
                          msg = await context.bot.send_message(chat_id=match.chat_id, text=caption + "\n⚠️ Image failed to load.", reply_markup=reply_markup, parse_mode="Markdown")
                 else:
                      msg = await context.bot.send_message(chat_id=match.chat_id, text=caption, reply_markup=reply_markup, parse_mode="Markdown")
             except Exception as final_err:
                 logger.error(f"CRITICAL: Failed to recover draft message: {final_err}")
                 return

             match.draft_message_id = msg.message_id
             
             try:
                 await context.bot.pin_chat_message(chat_id=match.chat_id, message_id=msg.message_id)
             except:
                 pass
             save_match_state(match)
        else:
             logger.error(f"Failed to update draft message: {e}")



async def handle_draw(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    # Prevent double-draw if already pending
    player = None
    if match.pending_player_id:
        from database import get_player
        p_data = get_player(match.pending_player_id)
        if p_data:
            player = p_data
    
    if not player:
        player = draw_player_for_turn(match)
        
    if not player:
        await update.callback_query.answer("No eligible players left!", show_alert=True)
        return
        
    match.pending_player_id = player['player_id']
    save_match_state(match)
    
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    # UI: Show Player Card in the same message
    # Rule 2: Strict Caption Format
    # ✨ ⚔️ <CurrentPlayerName>'s turn
    # Pulled: <Cricketer Name>
    # Assign a position:
    card_caption = f"✨ ⚔️ {current_team.owner_name}'s turn\nPulled: {player['name']}\nAssign a position:"
    
    # Buttons for Card
    keyboard = []
    
    active_positions = POSITIONS_TEST if "Test" in match.mode else POSITIONS_T20
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
    p_data = get_player(player['player_id'])
    media = p_data.get('image_file_id', DRAFT_BANNER_URL)
    
    # Update the single message to show the card
    await update_draft_message(update, context, match, card_caption, keyboard, media=media)


async def handle_assign(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, player_id: str, slot: str):
    from database import get_player
    from game.models import Player
    
    p_data = get_player(player_id)
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    # Assign
    current_team.slots[slot] = Player(**p_data)
    match.pending_player_id = None
    
    # Check Complete
    if match.team_a.is_complete() and match.team_b.is_complete():
        match.state = "READY_CHECK"
        save_match_state(match)
        
        board_text = format_draft_board(match)
        # Final Board Update
        keyboard = [[InlineKeyboardButton("🚀 READY", callback_data=f"ready_{match.match_id}")]]
        await update_draft_message(update, context, match, f"{board_text}\n\n✅ **Draft Complete!** Waiting for Ready...", keyboard)
        return

    # Switch Turn
    switch_turn(match)
    save_match_state(match)
    
    # Update Board for Next Turn (Restore Draw Button and Banner)
    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
    # Fix: Use Banner Image to keep message type as Photo (avoids delete/resend flicker)
    await update_draft_message(update, context, match, board_text, keyboard, media=DRAFT_BANNER_URL)


async def handle_redraw(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    if current_team.redraws_remaining > 0:
        current_team.redraws_remaining -= 1
        
        # Permanent Discard Logic
        if match.pending_player_id:
            # Remove from pool if present
            if match.pending_player_id in match.draft_pool:
                match.draft_pool.remove(match.pending_player_id)
                logger.info(f"DEBUG: Permanently discarded {match.pending_player_id} from pool.")
            
            match.pending_player_id = None
        
        # Switch Turn
        switch_turn(match)
        save_match_state(match)
        
        # Update Board (Restore Banner)
        board_text = format_draft_board(match)
        keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
        
        # Fix: Use Banner Image
        await update_draft_message(update, context, match, f"{board_text}\n\n🗑 {current_team.owner_name} used Skip! Turn Consumed.", keyboard, media=DRAFT_BANNER_URL)
        
    else:
        await update.callback_query.answer("No skips left!", show_alert=True)

async def handle_replace_start(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    if current_team.replacements_remaining <= 0:
        await update.callback_query.answer("No replacements left!", show_alert=True)
        return

    # Check if we have a pending player (should be there)
    if not match.pending_player_id:
        await update.callback_query.answer("No player drawn!", show_alert=True)
        return
        
    # Get Player Data
    from database import get_player
    player = get_player(match.pending_player_id)
    
    # UI: Show Filled Slots to Replace
    card_caption = f"♻️ **Replacing Player**\nNew Player: {player['name']}\n\nSelect a position to replace:"
    
    keyboard = []
    
    # Show active filled positions
    active_positions = POSITIONS_TEST if "Test" in match.mode else POSITIONS_T20
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
    media = player.get('image_file_id', DRAFT_BANNER_URL)
    
    await update_draft_message(update, context, match, card_caption, keyboard, media=media)

async def handle_replace_exec(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match, slot: str):
    current_team = match.team_a if match.team_a.owner_id == match.current_turn else match.team_b
    
    # Validation
    if current_team.replacements_remaining <= 0:
        await update.callback_query.answer("No replacements left!", show_alert=True)
        return
        
    old_player = current_team.slots.get(slot)
    if not old_player:
        await update.callback_query.answer("Slot is empty! Cannot replace.", show_alert=True)
        return
        
    from database import get_player
    from game.models import Player
    
    new_player_data = get_player(match.pending_player_id)
    new_player = Player(**new_player_data)
    
    # Execute Replace
    current_team.slots[slot] = new_player
    current_team.replacements_remaining -= 1
    match.pending_player_id = None
    
    # Switch Turn
    switch_turn(match)
    save_match_state(match)
    
    # Update Board
    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]
    
    await update_draft_message(update, context, match, f"{board_text}\n\n♻️ {current_team.owner_name} replaced {old_player.name} with {new_player.name}!", keyboard, media=DRAFT_BANNER_URL)

async def handle_replace_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, match: Match):
    # Just go back to draw view
    await handle_draw(update, context, match)
