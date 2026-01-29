
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
import logging
from game.state import load_match_state, save_match_state
from game.models import Match, Player
from database import get_player
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

def esc(t):
    return escape_markdown(str(t), version=1)

# Helper to get squad buttons
def get_squad_buttons(team, callback_prefix, exclude_id=None):
    keyboard = []
    # Sort slots?
    for slot_name, player in team.slots.items():
        if not player: continue
        if exclude_id and player.player_id == exclude_id: continue
        
        btn_text = f"{player.name} ({slot_name})"
        cb = f"{callback_prefix}_{player.player_id}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb)])
    return keyboard

async def handle_trade_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    match_id = "_".join(query.data.split('_')[2:])
    user_id = query.from_user.id
    
    match = load_match_state(match_id)
    if not match: return
    
    # 1. Validation
    if getattr(match.team_a, 'trades_used', 0) + getattr(match.team_b, 'trades_used', 0) >= 1:
        await query.answer("Trades used up!", show_alert=True)
        return
        
    # Check if a trade is already active
    if match.trade_offer and match.trade_offer.get('step') != 'DONE':
        await query.answer("A trade is already in progress!", show_alert=True)
        return

    # Identify Initiator
    is_team_a = (user_id == match.team_a.owner_id)
    initiator = match.team_a if is_team_a else match.team_b
    opponent = match.team_b if is_team_a else match.team_a
    
    if user_id != initiator.owner_id:
        await query.answer("You are not part of this match.", show_alert=True)
        return

    # INIT TRADE STATE
    match.trade_offer = {
        'initiator_id': user_id,
        'step': 'PICK_TARGET',
        'target_msg_id': None,
        'picks': {} # 'initiator_gets', 'opponent_gets'
    }
    save_match_state(match)
    
    # Show Opponent Squad to Initiator
    # "Select a player to TAKE from opponent"
    buttons = get_squad_buttons(opponent, f"tradetarget_{match_id}")
    buttons.append([InlineKeyboardButton("❌ Cancel Trade", callback_data=f"tradecancel_{match_id}")])
    
    text = f"🔄 **Trade Initiated!**\n\nSelect a player from {esc(opponent.owner_name)}'s squad that you want to **TAKE**."
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def handle_trade_target_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    # data: tradetarget_{match_id}_{player_id}
    # split: tradetarget, match, id... ID might contain underscores? 
    # Player IDs are usually "PL_NAME". 
    parts = data.split('_')
    # tradetarget, match, pl, name...
    # Safe parsing: match_id is usually UUID (no underscores? wait, user ID included?).
    
    # Let's rely on stored match state if possible or robust split
    # Actually match_id in this codebase is ChatID_UniqueString. 
    # Let's assume match_id has underscores.
    # Player ID starts with 'PL_'.
    
    # Robust find:
    pl_idx = -1
    for i, p in enumerate(parts):
        if p == 'PL':
            pl_idx = i
            break
            
    if pl_idx == -1: return 
    
    match_id = "_".join(parts[1:pl_idx])
    player_id = "_".join(parts[pl_idx:])
    
    match = load_match_state(match_id)
    if not match or not match.trade_offer: return
    
    user_id = query.from_user.id
    if user_id != match.trade_offer['initiator_id']: return

    # Store Pick
    match.trade_offer['picks']['initiator_gets'] = player_id
    match.trade_offer['step'] = 'WAIT_ACCEPT'
    save_match_state(match)
    
    # Notify Opponent
    player_obj = get_player(player_id)
    p_name = player_obj.get('name', 'Unknown')
    
    initiator_team = match.team_a if match.team_a.owner_id == user_id else match.team_b
    
    text = (
        f"🔄 **Trade Offer!**\n\n"
        f"👤 {esc(initiator_team.owner_name)} wants to trade for **{esc(p_name)}**.\n\n"
        "Do you accept?"
    )
    
    buttons = [
        [InlineKeyboardButton("✅ Accept & Pick Counter", callback_data=f"tradeaccept_{match_id}")],
        [InlineKeyboardButton("❌ Reject", callback_data=f"tradereject_{match_id}")]
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def handle_trade_respond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split('_')[0] # tradeaccept / tradereject
    match_id = "_".join(query.data.split('_')[1:])
    
    match = load_match_state(match_id)
    if not match or not match.trade_offer: return
    
    user_id = query.from_user.id
    initiator_id = match.trade_offer['initiator_id']
    
    # Ensure responder is NOT initiator
    if user_id == initiator_id:
        await query.answer("Waiting for opponent...", show_alert=True)
        return

    if action == "tradereject":
        match.trade_offer = None
        save_match_state(match)
        
        # Reset Dashboard
        # Reuse logic from ready.py or simple text
        await query.message.edit_text("❌ **Trade Rejected.** returning to dashboard...", parse_mode="Markdown")
        # Trigger Ready Refresh?
        from handlers.ready import handle_ready
        # Trigger Ready Refresh
        from handlers.ready import handle_ready
        # Hack Fix: Use override
        await handle_ready(update, context, match_id_override=match_id) # This refreshes the dashboard with Trade button back
        return

    if action == "tradeaccept":
        match.trade_offer['step'] = 'PICK_COUNTER'
        save_match_state(match)
        
        # Show Initiator's Squad to Responder
        # "Select a player to TAKE from Initiator"
        
        # Determine Initiator Team
        initiator_team = match.team_a if match.team_a.owner_id == initiator_id else match.team_b
        
        buttons = get_squad_buttons(initiator_team, f"tradecounter_{match_id}")
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"tradereject_{match_id}")]) # Reuse reject to cancel
        
        text = f"🔄 **Trade Accepted!**\n\nNow select a player from {esc(initiator_team.owner_name)}'s squad to **TAKE** in return."
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def handle_trade_counter_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # Parse ID robustly again
    parts = data.split('_')
    pl_idx = -1
    for i, p in enumerate(parts):
        if p == 'PL':
            pl_idx = i
            break
    if pl_idx == -1: return 
    
    match_id = "_".join(parts[1:pl_idx])
    player_id = "_".join(parts[pl_idx:])
    
    match = load_match_state(match_id)
    if not match or not match.trade_offer: return
    
    # Store Pick
    match.trade_offer['picks']['opponent_gets'] = player_id
    match.trade_offer['step'] = 'CONFIRM'
    match.trade_offer['confirms'] = [] # List of IDs who confirmed
    save_match_state(match)
    
    # Show Final Summary
    p1_id = match.trade_offer['picks']['initiator_gets']
    p2_id = match.trade_offer['picks']['opponent_gets']
    
    p1 = get_player(p1_id)
    p2 = get_player(p2_id)
    
    initiator_id = match.trade_offer['initiator_id']
    initiator_name = match.team_a.owner_name if match.team_a.owner_id == initiator_id else match.team_b.owner_name
    opponent_name = match.team_b.owner_name if match.team_a.owner_id == initiator_id else match.team_a.owner_name
    
    text = (
        "🔄 **CONFIRM TRADE**\n\n"
        f"🔹 {esc(initiator_name)} gets: **{esc(p1['name'])}**\n"
        f"🔹 {esc(opponent_name)} gets: **{esc(p2['name'])}**\n\n"
        "Both players must confirm."
    )
    
    buttons = [
        [InlineKeyboardButton("✅ Confirm (0/2)", callback_data=f"tradeconfirm_{match_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"tradecancel_{match_id}")]
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def handle_trade_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    match_id = "_".join(query.data.split('_')[1:])
    user_id = query.from_user.id
    
    match = load_match_state(match_id)
    if not match or not match.trade_offer: return
    
    confirms = match.trade_offer.get('confirms', [])
    
    if user_id in confirms:
        await query.answer("You already confirmed!", show_alert=True)
        return
        
    confirms.append(user_id)
    match.trade_offer['confirms'] = confirms
    save_match_state(match)
    
    # Update Button Text (1/2 or 2/2)
    count = len(confirms)
    
    if count < 2:
        # Edit Keyboard only
        buttons = [
            [InlineKeyboardButton(f"✅ Confirm ({count}/2)", callback_data=f"tradeconfirm_{match_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"tradecancel_{match_id}")]
        ]
        try:
             await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        except: pass
    else:
        # EXECUTE TRADE
        await execute_trade_swap(match, query, context, update)

async def handle_trade_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    match_id = "_".join(query.data.split('_')[1:])
    
    match = load_match_state(match_id)
    if match:
        match.trade_offer = None
        save_match_state(match)
        
    await query.message.edit_text("❌ **Trade Cancelled.**", parse_mode="Markdown")
    # Return to dashboard
    from handlers.ready import handle_ready
    # Return to dashboard
    from handlers.ready import handle_ready
    await handle_ready(update, context, match_id_override=match_id)

async def execute_trade_swap(match, query, context, update):
    try:
        # Get IDs
        initiator_id = match.trade_offer['initiator_id']
        id_i_gets = match.trade_offer['picks']['initiator_gets'] # Player ID
        id_o_gets = match.trade_offer['picks']['opponent_gets'] # Player ID
        
        team_i = match.team_a if match.team_a.owner_id == initiator_id else match.team_b
        team_o = match.team_b if match.team_a.owner_id == initiator_id else match.team_a
        
        # SWAP LOGIC: Find slot keys
        slot_i = None # Slot where 'id_o_gets' is currently (in Initiator Team)
        slot_o = None # Slot where 'id_i_gets' is currently (in Opponent Team)
        
        # Initiator gives 'id_o_gets'
        for k, p in team_i.slots.items():
            if p and p.player_id == id_o_gets:
                slot_i = k
                break
                
        # Opponent gives 'id_i_gets'
        for k, p in team_o.slots.items():
            if p and p.player_id == id_i_gets:
                slot_o = k
                break
                
        if not slot_i or not slot_o:
            await query.message.edit_text("❌ Error: Player not found in slot. Trade failed.")
            match.trade_offer = None
            save_match_state(match)
            return
            
        # Perform Swap
        p_for_initiator = team_o.slots[slot_o]
        p_for_opponent = team_i.slots[slot_i]
        
        # Move P_FOR_INITIATOR (Virat) -> TEAM_I [SLOT_I] (Takes Dhoni's place)
        team_i.slots[slot_i] = p_for_initiator
        
        # Move P_FOR_OPPONENT (Dhoni) -> TEAM_O [SLOT_O] (Takes Virat's place)
        team_o.slots[slot_o] = p_for_opponent
        
        # Mark used
        team_i.trades_used += 1

        match.trade_offer = None # Clear
        save_match_state(match)
        
        await query.message.edit_text("✅ **Trade Successful!** Players Swapped.", parse_mode="Markdown")
        
        # Return to Dashboard
        from handlers.ready import handle_ready
        # Pass match_id explicitly to avoid setting query.data (Forbidden)
        await handle_ready(update, context, match_id_override=match.match_id)
        
    except Exception as e:
        logger.error(f"Trade Execution Failed: {e}")
        await query.message.edit_text("❌ Critical Error during swap.")
