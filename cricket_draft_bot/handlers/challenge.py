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
        
    # Track Group for Broadcasts
    from database import save_chat
    await save_chat(update.effective_chat.id)
        
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
    
    await update.effective_message.reply_text(
        f"🏏 **{mode} Challenge Sent!**\n\nWho wants to play against {update.effective_user.first_name}?",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def challenge_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    from telegram.error import BadRequest
    key = f"join_IPL_{update.effective_user.id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    caption = f"🏏 **IPL Challenge!**\nUser: {update.effective_user.first_name}\nMode: IPL\nWaiting for opponent..."
    banner = await get_banner_for_mode("ipl")
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=banner,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption + "\n*(Enable media permissions in this chat to see banners)*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def challenge_intl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    key = f"join_INTL_{update.effective_user.id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    caption = f"🏏 **International Challenge!**\nUser: {update.effective_user.first_name}\nMode: International\nWaiting for opponent..."
    banner = await get_banner_for_mode("intl")
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=banner,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption + "\n*(Enable media permissions in this chat to see banners)*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def challenge_fifa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    key = f"join_FIFA_{update.effective_user.id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    caption = f"⚽ **FIFA Challenge!**\nUser: {update.effective_user.first_name}\nMode: FIFA\nWaiting for opponent..."
    banner = await get_banner_for_mode("fifa")
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=banner,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption + "\n*(Enable media permissions in this chat to see banners)*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def challenge_unified(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /challenge intl - Start Intl Draft
    """
    if not context.args:
        await update.effective_message.reply_text("Usage: `/challenge intl`", parse_mode="Markdown")
        return

    mode_arg = context.args[0].lower()
    
    from utils.banners import get_banner_for_mode
    banner = await get_banner_for_mode("intl")  # default

    if mode_arg == 'test':
        await update.effective_message.reply_text("🚧 Test Mode is temporarily disabled. Please use `intl`.")
        return
    elif mode_arg in ['t20', 'ipl']:
        from database import get_db
        db = get_db()
        config = await db.system_config.find_one({"key": "ipl_mode"})
        if config and config.get("enabled"):
             real_mode = "IPL"
             banner = await get_banner_for_mode("ipl")
        else:
             await update.effective_message.reply_text("🚧 IPL Mode is currently disabled/under development.")
             return
    elif mode_arg in ['intl', 'international']:
        real_mode = "International"
    elif mode_arg in ['fifa', 'football']:
        real_mode = "FIFA"
        banner = await get_banner_for_mode("fifa")
    else:
        await update.effective_message.reply_text(f"❌ Unknown mode: {mode_arg}\nUse `intl`, `t20`, `test`, `fifa`.")
        return
    
    # Check for Reply (Targeted Challenge)
    target_user = None
    if update.effective_message.reply_to_message:
        target_user = update.effective_message.reply_to_message.from_user
        if target_user.id == update.effective_user.id:
            await update.effective_message.reply_text("You can't challenge yourself!")
            return
            
    # Callback Data
    key = f"join_{real_mode}_{update.effective_user.id}"
    if target_user:
        key += f"_{target_user.id}"
        
    keyboard = [[InlineKeyboardButton("⚔️ Accept Challenge", callback_data=key)]]
    
    from telegram.helpers import escape_markdown
    def esc(t): return escape_markdown(t, version=1)

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
        
    try:
        await update.effective_message.reply_photo(photo=banner, caption=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        await update.effective_message.reply_text(f"{msg}\n*(Enable media permissions in this chat to see banners)*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # Don't delete! We will edit it.
    # await query.answer() 
    # Actually answer() is needed to stop loading spinner, but doesn't affect message.
    await query.answer()
    
    parts = query.data.split('_') # join, MODE, OWNER_ID, [TARGET_ID]
    mode = parts[1]
    owner_id = int(parts[2])
    
    # Check for Targeted Challenge
    if len(parts) > 3:
        target_id = int(parts[3])
        if query.from_user.id != target_id:
            await query.answer("⛔ This challenge is not for you!", show_alert=True)
            return

    # Check Self-Join
    if query.from_user.id == owner_id:
        await query.answer("⛔ You cannot play against yourself!", show_alert=True)
        return
        
    # Start Match
    # Verify Owner Name (from DB or context? We don't have it easily here if stateless)
    # We'll use "Player 1" if unknown, but better to fetch.
    # Actually create_match_state usually takes ID and Name.
    # We can get names from User objects if we had them.
    # The challenger's name is in the caption, but parsing it is brittle.
    # Let's use "Challenger" / "Acceptor" or fetch from TG API (get_chat_member)
    
    try:
        # Extract challenger name from message caption — no extra API call needed
        text = query.message.caption or query.message.text or ""
        challenger_name = "Player 1"
        for line in text.split('\n'):
            line_clean = line.strip().strip('*')
            if line_clean.startswith('User: ') or line_clean.startswith('From: '):
                challenger_name = line_clean.split(': ', 1)[1].strip()
                break
    except Exception:
        challenger_name = "Player 1"
        
    joiner_name = query.from_user.first_name
    
    # Initialize Match
    # Initialize Match
    match = await create_match_state(
        chat_id=update.effective_chat.id,
        mode=mode, 
        owner_id=owner_id, 
        challenger_id=query.from_user.id,
        owner_name=challenger_name, # In state.py owner_name is param 5
        challenger_name=joiner_name # In state.py challenger_name is param 6
    )
    
    # CRITICAL: Reuse Message ID
    match.draft_message_id = query.message.message_id
    
    # Save Initial State
    from game.state import save_match_state
    await save_match_state(match)
    
    # Start Draft (Update the message)
    from handlers.draft import format_draft_board, update_draft_message
    from utils.banners import get_banner_for_mode

    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]

    if "IPL" in mode:
        banner = await get_banner_for_mode("ipl")
    elif mode == "FIFA":
        banner = await get_banner_for_mode("fifa")
    else:
        banner = await get_banner_for_mode("intl")

    # Edit the existing message into the draft board
    await update_draft_message(update, context, match, board_text, keyboard, media=banner)

    # Pin the draft board — must be done here because draft_message_id was
    # pre-set above (reusing challenge message), so update_draft_message skips
    # its own pin block (which only fires when sending a brand new message).
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=query.message.message_id,
            disable_notification=True
        )
        match.pinned_message_id = query.message.message_id
        await save_match_state(match)
    except Exception:
        pass  # Bot not admin — skip silently

