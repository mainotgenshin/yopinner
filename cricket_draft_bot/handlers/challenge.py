# handlers/challenge.py
import html
import asyncio
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import ChatMigrated
from game.state import create_match_state
from telegram.helpers import escape_markdown
from database import save_pending_challenge, delete_pending_challenge

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)

# In-memory dict tracking active pending challenges
# key = owner_id, value = {task, chat_id, message_id}
_pending_challenges: dict = {}

async def _expire_challenge(ch_key: str, owner_id: int, chat_id: int, message_id: int, bot):
    """After 2 minutes, expire the challenge if not yet joined."""
    await asyncio.sleep(120)  # 2 minutes
    # Remove from tracking (in-memory + DB)
    _pending_challenges.pop(ch_key, None)
    try:
        # ch_key = "{owner_id}_{mode}" — extract mode for compound DB delete
        _mode = ch_key.split('_', 1)[1] if '_' in ch_key else None
        await delete_pending_challenge(owner_id, _mode)
    except Exception:
        pass
    EXPIRED_TEXT = "⏰ <b>Challenge Expired</b>\nNo one joined in time. Start a new one with /challenge intl or /challengeipl."
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id,
            caption=EXPIRED_TEXT, parse_mode="HTML"
        )
    except Exception:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=EXPIRED_TEXT, parse_mode="HTML"
            )
        except Exception:
            pass  # Message already gone or edited

async def _replace_old_challenge(ch_key: str, bot):
    """Cancel the previous pending challenge for this user+mode and clean up its message."""
    old = _pending_challenges.pop(ch_key, None)
    if not old:
        return
    task = old.get('task')
    if task and not task.done():
        task.cancel()
    # Clean DB
    try:
        parts = ch_key.split('_', 1)
        await delete_pending_challenge(int(parts[0]), parts[1] if len(parts) > 1 else None)
    except Exception:
        pass
    # Silently mark old message as expired (no one joined)
    old_chat = old.get('chat_id')
    old_msg  = old.get('message_id')
    if not old_chat or not old_msg:
        return
    EXPIRED_TEXT = "⏰ <b>Challenge Expired</b>\nNo one joined in time. Start a new one with /challenge intl or /challengeipl."
    try:
        await bot.edit_message_caption(
            chat_id=old_chat, message_id=old_msg,
            caption=EXPIRED_TEXT, parse_mode="HTML"
        )
    except Exception:
        try:
            await bot.edit_message_text(
                chat_id=old_chat, message_id=old_msg,
                text=EXPIRED_TEXT, parse_mode="HTML"
            )
        except Exception:
            pass


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
        f"🏏 *{esc(mode)} Challenge Sent!*\n\nWho wants to play against {html.escape(update.effective_user.first_name)}?",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def challenge_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = f"join_IPL_{owner_id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    name = html.escape(update.effective_user.first_name)
    caption = f"🏏 <b>IPL Challenge!</b>\nUser: {name}\nMode: IPL\nWaiting for opponent... <i>(expires in 2 min)</i>"
    banner = await get_banner_for_mode("ipl")
    msg = None
    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=banner, caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except ChatMigrated as e:
        chat_id = e.migrate_to_chat_id
        try:
            msg = await context.bot.send_photo(
                chat_id=chat_id, photo=banner, caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id, text=caption + "\n<i>(Enable media permissions to see banners)</i>",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            return
    if not msg: return
    _ch_key = f"{owner_id}_IPL"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "IPL")
    except Exception:
        pass


async def challenge_intl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = f"join_INTL_{owner_id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    name = html.escape(update.effective_user.first_name)
    caption = f"🏏 <b>International Challenge!</b>\nUser: {name}\nMode: International\nWaiting for opponent... <i>(expires in 2 min)</i>"
    banner = await get_banner_for_mode("intl")
    msg = None
    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=banner, caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except ChatMigrated as e:
        chat_id = e.migrate_to_chat_id
        try:
            msg = await context.bot.send_photo(
                chat_id=chat_id, photo=banner, caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id, text=caption + "\n<i>(Enable media permissions to see banners)</i>",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            return
    if not msg: return
    _ch_key = f"{owner_id}_International"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "International")
    except Exception:
        pass


async def challenge_fifa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = f"join_FIFA_{owner_id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    name = html.escape(update.effective_user.first_name)
    caption = f"⚽ <b>FIFA Challenge!</b>\nUser: {name}\nMode: FIFA\nWaiting for opponent... <i>(expires in 2 min)</i>"
    banner = await get_banner_for_mode("fifa")
    msg = None
    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=banner, caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except ChatMigrated as e:
        chat_id = e.migrate_to_chat_id
        try:
            msg = await context.bot.send_photo(
                chat_id=chat_id, photo=banner, caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id, text=caption + "\n<i>(Enable media permissions to see banners)</i>",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            return
    if not msg: return
    _ch_key = f"{owner_id}_FIFA"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "FIFA")
    except Exception:
        pass


async def challenge_wwe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = f"join_WWE_{owner_id}"
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    name = html.escape(update.effective_user.first_name)
    caption = f"🤼 <b>WWE Challenge!</b>\nUser: {name}\nMode: WWE\nWaiting for opponent... <i>(expires in 2 min)</i>"
    banner = await get_banner_for_mode("wwe")
    msg = None
    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=banner, caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except ChatMigrated as e:
        chat_id = e.migrate_to_chat_id
        try:
            msg = await context.bot.send_photo(
                chat_id=chat_id, photo=banner, caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id, text=caption + "\n<i>(Enable media permissions to see banners)</i>",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception:
            return
    if not msg: return
    _ch_key = f"{owner_id}_WWE"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "WWE")
    except Exception:
        pass


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
    elif mode_arg in ['wwe', 'wrestling']:
        real_mode = "WWE"
        banner = await get_banner_for_mode("wwe")
    else:
        await update.effective_message.reply_text(f"❌ Unknown mode: {mode_arg}\nUse `intl`, `t20`, `fifa`, `wwe`.")
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
        
    owner_id = update.effective_user.id
    chat_id  = update.effective_chat.id
    sent_msg = None
    try:
        sent_msg = await update.effective_message.reply_photo(
            photo=banner, caption=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    except Exception:
        try:
            sent_msg = await update.effective_message.reply_text(
                f"{msg}\n*(Enable media permissions in this chat to see banners)*",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
        except Exception:
            return

    if not sent_msg:
        return

    # Cancel + edit old challenge message (if any) before starting new one
    _ch_key = f"{owner_id}_{real_mode}"

    # Start 2-min expiry task
    _ch_key = f"{owner_id}_{real_mode}"
    task = asyncio.create_task(
        _expire_challenge(_ch_key, owner_id, chat_id, sent_msg.message_id, context.bot)
    )
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': sent_msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, sent_msg.message_id, real_mode)
    except Exception:
        pass

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

    # Check Self-Join FIRST — before touching the timer
    if query.from_user.id == owner_id:
        await query.answer("⛔ You cannot play against yourself!", show_alert=True)
        return

    # Cancel expiry task — only reached if a different user is joining
    _ch_key = f"{owner_id}_{mode}"
    pending = _pending_challenges.pop(_ch_key, None)
    if pending:
        task = pending.get('task')
        if task and not task.done():
            task.cancel()
    # Remove from DB — pass mode so compound (owner_id, mode) key is matched
    try:
        await delete_pending_challenge(owner_id, mode)
    except Exception:
        pass

        
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
    elif mode == "WWE":
        banner = await get_banner_for_mode("wwe")
    else:
        banner = await get_banner_for_mode("intl")

    # Edit the existing message into the draft board
    await update_draft_message(update, context, match, board_text, keyboard, media=banner)

    # Pin the draft board — must be done here because draft_message_id was
    # pre-set above (reusing challenge message), so update_draft_message skips
    # its own pin block (which only fires when sending a brand new message).
    pinned_msg_id = query.message.message_id
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=pinned_msg_id,
            disable_notification=True
        )
        match.pinned_message_id = pinned_msg_id
        await save_match_state(match)
    except Exception:
        pass  # Bot not admin — skip silently

    # Start 30-min abandon timeout (always, regardless of pin success)
    _chat_id = update.effective_chat.id
    _match_id = match.match_id
    _bot = context.bot

    async def _abandon_timeout_live(bot, chat_id, msg_id, match_id, delay=1800):
        await asyncio.sleep(delay)
        from game.state import load_match_state as _load
        m = await _load(match_id)
        if not m or m.state in ("DRAFTING", "READY_CHECK"):
            try:
                await bot.unpin_chat_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            if m:
                try:
                    from database import get_db
                    await get_db().matches.delete_one({"match_id": match_id})
                except Exception:
                    pass

    asyncio.create_task(_abandon_timeout_live(_bot, _chat_id, pinned_msg_id, _match_id))

