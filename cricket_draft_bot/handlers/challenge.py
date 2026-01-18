# handlers/challenge.py
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from game.state import create_match_state
import logging
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)

async def challenge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """
    Generic handler for /challenge_ipl (mode="IPL") or /challenge_intl (mode="International")
    """
    if not update.message:
        return

    # Check mention
    if not update.message.mentions:
        await update.message.reply_text("⚠ Usage: /challenge_ipl @username")
        return
        
    # Get entities
    # Assuming first mention is opponent
    # entities = update.message.parse_entities(["mention", "text_mention"])
    # Simplified: Get first mention
    
    # We need the user ID of the mentioned user.
    # Telegram bots can't easily resolve @username to ID unless they have seen the user.
    # But message.reply_to_message might work, or we force users to start bot first.
    # The prompt implies "@username".
    # Since we can't reliably get ID from username without interaction, we'll store the username
    # and ask the user to click "Join".
    
    challenger_name = "Waiting..."
    # We don't have ID yet.
    
    # Actually, proper flow:
    # A sends Challenge. Button "Join". B clicks Join. Match starts.
    
    keyboard = [
        [InlineKeyboardButton("⚔️ Join Challenge", callback_data=f"join_{mode}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🏏 **{mode} Challenge Sent!**\n\nWho wants to play against {update.effective_user.first_name}?",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def join_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    mode = data.split('_')[1] # join_IPL
    
    challenger = query.from_user
    owner_name = query.message.reply_to_message.from_user.first_name if query.message.reply_to_message else "Player A"
    owner_id = query.message.reply_to_message.from_user.id if query.message.reply_to_message else 0
    
    if owner_id == 0:
        # If original message lost or something? 
        # Actually join button is on the bot's message.
        # The bot's message "reply_to" might not be available if not specifically set.
        # But we can't easily track the original sender unless stored in callback data or global state.
        # Alternative: The challenge text has the name.
        # Better: Store pending challenge in context? No, stateless preferable.
        # Simplest: Anyone can click join, but create match assigns sender of the command as Owner?
        # But we don't know who sent the command from the callback query on the BOT's message.
        # Wait, the bot's message is a reply to the command? No, usually just a new message.
        pass

    # To fix this: encode owner_id in callback data? "join_IPL_12345"
    # But data limit is small (64 bytes).
    # Let's hope create_task is fine. 
    # Let's update the original code to encode ID.
    pass

async def challenge_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = f"join_IPL_{update.effective_user.id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    await update.message.reply_text(
        f"🏏 **IPL Challenge!**\nUser: {update.effective_user.first_name}\nMode: IPL\nWaiting for opponent...",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def challenge_intl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = f"join_INTL_{update.effective_user.id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    await update.message.reply_text(
        f"🏏 **International Challenge!**\nUser: {update.effective_user.first_name}\nMode: International\nWaiting for opponent...",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def challenge_unified(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /challenge intl - Start Intl Draft
    /challenge t20  - Coming Soon
    /challenge test - Coming Soon
    Supports replying to a user to target them.
    """
    if not context.args:
        await update.message.reply_text("Usage: `/challenge intl`", parse_mode="Markdown")
        return

    mode_arg = context.args[0].lower()
    
    if mode_arg == 'test':
        # real_mode = "Test"
        await update.message.reply_text("🚧 Test Mode is temporarily disabled. Please use `intl`.")
        return
    elif mode_arg in ['t20', 'ipl']:
        # Check if enabled
        from database import get_db
        db = get_db()
        config = db.system_config.find_one({"key": "ipl_mode"})
        
        if config and config.get("enabled"):
             real_mode = "IPL"
        else:
             await update.message.reply_text("🚧 IPL Mode is currently disabled/under development.")
             return
    elif mode_arg in ['intl', 'international']:
        real_mode = "International"
    else:
        await update.message.reply_text(f"❌ Unknown mode: {mode_arg}\nUse `intl`, `t20`, `test`.")
        return
    
    # Check for Reply (Targeted Challenge)
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user.id == update.effective_user.id:
            await update.message.reply_text("You can't challenge yourself!")
            return
            
    # Callback Data
    # Standard: join_International_OWNERID
    # Targeted: join_International_OWNERID_TARGETID
    
    key = f"join_{real_mode}_{update.effective_user.id}"
    if target_user:
        key += f"_{target_user.id}"
        
    keyboard = [[InlineKeyboardButton("⚔️ Accept Challenge", callback_data=key)]]
    
    from telegram.helpers import escape_markdown
    
    # helper to escape name for V1
    def esc(t):
        return escape_markdown(t, version=1)

    if target_user:
        msg = (
            f"🏏 *{real_mode} Challenge!*\n"
            f"From: {esc(update.effective_user.first_name)}\n"
            f"To: {esc(target_user.first_name)}\n\n"
            f"Waiting for {esc(target_user.first_name)} to accept..."
        )
    else:
        msg = (
            f"🏏 *{real_mode} Challenge!*\n"
            f"User: {esc(update.effective_user.first_name)}\n"
            f"Waiting for opponent..."
        )
        
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split('_') # join, MODE, OWNER_ID, [TARGET_ID]
    mode = parts[1]
    owner_id = int(parts[2])
    
    # Check for Targeted Challenge
    if len(parts) > 3:
        target_id = int(parts[3])
        if query.from_user.id != target_id:
            await query.answer("This challenge is not for you!", show_alert=True)
            return
    
    if query.from_user.id == owner_id:
        await query.answer("You can't play against yourself!", show_alert=True)
        return
        
    chat_id = update.effective_chat.id
    
    # Create Match
    # We need names
    # Owner Name? We only have ID. 
    # We can fetch via get_chat_member or just use "Player 1" if not cached.
    # Actually, we can get name from get_chat(owner_id) if same chat?
    try:
        owner = await context.bot.get_chat_member(chat_id, owner_id)
        owner_name = owner.user.first_name
    except:
        owner_name = "Player 1"
        
    challenger_name = query.from_user.first_name
    
    match = create_match_state(chat_id, mode, owner_id, query.from_user.id, owner_name, challenger_name)
    

    # Start Draft UI
    # Show first drafter
    current_name = owner_name if match.current_turn == owner_id else challenger_name
    board_text = f"✅ **Match Started!**\nMod: {mode}\n{esc(owner_name)} vs {esc(challenger_name)}\n\n👉 **Turn:** {esc(current_name)}"
    
    keyboard = [[InlineKeyboardButton("🃏 Draw Player", callback_data=f"draw_{match.match_id}")]]
    
    # Switch to Text Message (No Banner)
    try:
        await query.delete_message()
    except:
        pass

    msg = await context.bot.send_message(
        chat_id=chat_id, 
        text=board_text,  # Text only
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode="Markdown"
    )
    
    # Pin the message
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception as e:
        logger.warning(f"Failed to pin message: {e}")
        
    match.draft_message_id = msg.message_id
    from game.state import save_match_state
    save_match_state(match)
