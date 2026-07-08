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

# Locks to prevent double-clicks/spam on the mode picker buttons
MODE_PICK_LOCKS = set()


def _is_stale_command(update) -> bool:
    """
    Returns True if this command message is older than 30 seconds.

    Why: When Koyeb restarts the bot, Telegram re-delivers all queued
    updates (commands sent while the bot was offline). These stale commands
    would be re-processed as if sent fresh — causing ghost challenge banners
    to appear. Any legitimate fresh command is always <2 seconds old, so
    a 30-second cutoff safely drops only re-queued stale commands.

    NOTE: This only applies to text command handlers — button callbacks
    (Join Game, Draw Player, etc.) are NOT affected by this check.
    """
    try:
        msg_age = time.time() - update.effective_message.date.timestamp()
        if msg_age > 30:
            logger.info(
                f"Dropping stale command from user {update.effective_user.id} "
                f"(age={msg_age:.0f}s > 30s) — likely a post-restart replay."
            )
            return True
    except Exception:
        pass
    return False


async def _expire_challenge(ch_key: str, owner_id: int, chat_id: int, message_id: int, bot):
    """After 2 minutes, expire the challenge if not yet joined."""
    await asyncio.sleep(120)  # 2 minutes
    # Remove from tracking (in-memory + DB)
    _pending_challenges.pop(ch_key, None)
    try:
        # ch_key = "{owner_id}_{mode}_{message_id}" — extract only mode (middle segment)
        _parts = ch_key.split('_')
        _mode = _parts[1] if len(_parts) >= 3 else (_parts[1] if len(_parts) == 2 else None)
        await delete_pending_challenge(owner_id, _mode)
    except Exception:
        pass
    EXPIRED_TEXT = "⏰ <b>Challenge Expired</b>\nNo one joined in time. Start a new one with /challenge odi or /challengeipl."
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
    EXPIRED_TEXT = "⏰ <b>Challenge Expired</b>\nNo one joined in time. Start a new one with /challenge odi or /challengeipl."
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


async def _check_match_limit(user_id: int, mode_of_reply) -> bool:
    """
    Returns True if user can start/join a match.
    If at limit (>=2), sends a descriptive message and returns False.
    mode_of_reply: an Update.message or a CallbackQuery object.
    """
    from database import get_user_active_matches_info
    from telegram.helpers import escape_markdown
    def _esc(t): return escape_markdown(str(t), version=1)
    matches = await get_user_active_matches_info(user_id)
    if len(matches) < 2:
        return True
    # Build descriptive block message
    lines = ["⛔ *You are already playing 2 matches!*", "Your active matches:"]
    for doc in matches:
        sd = doc.get("state_data", doc)  # handle both wrapped and unwrapped
        mode  = sd.get("mode", "?")
        ta    = sd.get("team_a", {})
        tb    = sd.get("team_b", {})
        opp   = tb.get("owner_name", "?") if ta.get("owner_id") == user_id else ta.get("owner_name", "?")
        # Count filled slots
        filled = sum(
            1 for v in list(ta.get("slots", {}).values()) + list(tb.get("slots", {}).values())
            if v is not None
        )
        status = sd.get("state", "?")
        status_label = "🟡 Ready Check" if status == "READY_CHECK" else "🟢 Drafting"
        lines.append(f"\u2022 {mode} vs {_esc(opp)} — {filled} picks done  {status_label}")
    lines.append("\n_Finish one of your matches first to start/join a new challenge._")
    msg = "\n".join(lines)
    try:
        if hasattr(mode_of_reply, 'answer'):  # It's a CallbackQuery
            await mode_of_reply.answer("⛔ You are already playing 2 matches! Finish one before joining.", show_alert=True)
        elif hasattr(mode_of_reply, 'reply_text'):
            await mode_of_reply.reply_text(msg, parse_mode="Markdown")
    except Exception:
        pass
    return False

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
    if _is_stale_command(update): return  # Drop replayed command from before bot restart
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # ─ Match limit check ─────────────────────────────────────
    _reply_obj = getattr(update, 'effective_message', None) or getattr(update, 'callback_query', None)
    if not await _check_match_limit(owner_id, _reply_obj):
        return
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
    _ch_key = f"{owner_id}_IPL_{msg.message_id}"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "IPL")
    except Exception:
        pass


async def challenge_odi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_stale_command(update): return  # Drop replayed command from before bot restart
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # ─ Match limit check
    _reply_obj = getattr(update, 'effective_message', None) or getattr(update, 'callback_query', None)
    if not await _check_match_limit(owner_id, _reply_obj):
        return
    key = f"join_ODI_{owner_id}"
    keyboard = [[InlineKeyboardButton("\u2694\ufe0f Join Game", callback_data=key)]]
    name = html.escape(update.effective_user.first_name)
    caption = f"\U0001f3cf <b>ODI Challenge!</b>\nUser: {name}\nMode: ODI\nWaiting for opponent... <i>(expires in 2 min)</i>"
    banner = await get_banner_for_mode("odi")
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
    _ch_key = f"{owner_id}_ODI_{msg.message_id}"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "ODI")
    except Exception:
        pass

# Backward-compat alias — /challengeintl still works
challenge_intl = challenge_odi


async def challenge_fifa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_stale_command(update): return  # Drop replayed command from before bot restart
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # ─ Match limit check
    _reply_obj = getattr(update, 'effective_message', None) or getattr(update, 'callback_query', None)
    if not await _check_match_limit(owner_id, _reply_obj):
        return
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
    _ch_key = f"{owner_id}_FIFA_{msg.message_id}"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "FIFA")
    except Exception:
        pass


async def send_wwe_gender_selector(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int):
    """
    Replies to the user with 2 buttons (Men / Women) to choose WWE challenge mode.
    """
    target_id = 0
    if not update.callback_query and update.effective_message and update.effective_message.reply_to_message:
        replied_user = update.effective_message.reply_to_message.from_user
        if replied_user and replied_user.id != owner_id:
            target_id = replied_user.id

    keyboard = [
        [
            InlineKeyboardButton("♂️ Men (WWE)", callback_data=f"wwe_pick_men_{owner_id}_{target_id}"),
            InlineKeyboardButton("♀️ Women (WWE)", callback_data=f"wwe_pick_women_{owner_id}_{target_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = "🤼 <b>WWE Challenge!</b>\nChoose the gender mode for this challenge:"
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(
            msg_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    else:
        await update.effective_message.reply_text(
            msg_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

async def handle_wwe_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')  # ["wwe", "pick", gender, owner_id, target_id]
    if len(parts) < 4:
        return
    gender = parts[2]
    try:
        owner_id = int(parts[3])
    except ValueError:
        return
    target_id = int(parts[4]) if len(parts) >= 5 else 0

    # Owner check
    if query.from_user.id != owner_id:
        await query.answer("⛔ Not for you! Only the person who sent the challenge can select the mode.", show_alert=True)
        return

    # Anti-spam lock
    msg_id = query.message.message_id if query.message else None
    if msg_id:
        if msg_id in MODE_PICK_LOCKS:
            await query.answer("Processing your selection...", show_alert=False)
            return
        MODE_PICK_LOCKS.add(msg_id)

    await query.answer()

    mode = "WWE" if gender == "men" else "WWE Women"
    
    # Delete selector message
    try:
        await query.message.delete()
    except Exception:
        pass

    # Start WWE challenge
    await challenge_wwe_start(update, context, owner_id, mode, target_id)

async def challenge_wwe_start(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int, mode: str, target_id: int = 0):
    from utils.banners import get_banner_for_mode
    chat_id = update.effective_chat.id
    
    # Re-verify match limit
    _reply_obj = getattr(update, 'effective_message', None) or getattr(update, 'callback_query', None)
    if not await _check_match_limit(owner_id, _reply_obj):
        # Remove from locks if limit hit
        msg_id = update.callback_query.message.message_id if update.callback_query and update.callback_query.message else None
        if msg_id:
            MODE_PICK_LOCKS.discard(msg_id)
        return

    target_user = None
    if target_id > 0:
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=target_id)
            target_user = member.user
        except Exception:
            pass

    # Avoid spaces in callback mode string
    ch_mode_key = "WWEWomen" if mode == "WWE Women" else "WWE"
    key = f"join_{ch_mode_key}_{owner_id}"
    if target_id > 0:
        key += f"_{target_id}"
        
    keyboard = [[InlineKeyboardButton("⚔️ Join Game", callback_data=key)]]
    challenger_name = update.effective_user.first_name
    banner = await get_banner_for_mode("wwe" if mode == "WWE" else "wwe_women")
    
    from telegram.helpers import escape_markdown
    def _esc(t): return escape_markdown(t, version=1)
    
    if target_user:
        msg_text = (
            f"🤼 *{mode} Challenge!*\n"
            f"From: {_esc(challenger_name)}\n"
            f"To: {_esc(target_user.first_name)}\n\n"
            f"Waiting for {_esc(target_user.first_name)} to accept..."
        )
    else:
        msg_text = (
            f"🤼 *{mode} Challenge!*\n"
            f"User: {_esc(challenger_name)}\n"
            f"Waiting for opponent... _(expires in 2 min)_"
        )
        
    sent_msg = None
    try:
        sent_msg = await context.bot.send_photo(
            chat_id=chat_id, photo=banner, caption=msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    except Exception:
        try:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=msg_text + "\n*(Enable media permissions in this chat to see banners)*",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
        except Exception:
            return
            
    if not sent_msg:
        return
        
    _ch_key = f"{owner_id}_{ch_mode_key}_{sent_msg.message_id}"
    task = asyncio.create_task(
        _expire_challenge(_ch_key, owner_id, chat_id, sent_msg.message_id, context.bot)
    )
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': sent_msg.message_id}
    
    try:
        await save_pending_challenge(owner_id, chat_id, sent_msg.message_id, ch_mode_key)
    except Exception:
        pass

async def challenge_wwe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_stale_command(update): return  # Drop replayed command from before bot restart
    owner_id = update.effective_user.id
    # ─ Match limit check
    _reply_obj = getattr(update, 'effective_message', None) or getattr(update, 'callback_query', None)
    if not await _check_match_limit(owner_id, _reply_obj):
        return
    await send_wwe_gender_selector(update, context, owner_id)

async def challenge_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_stale_command(update): return  # Drop replayed command from before bot restart
    from utils.banners import get_banner_for_mode
    owner_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # ─ Match limit check
    _reply_obj = getattr(update, 'effective_message', None) or getattr(update, 'callback_query', None)
    if not await _check_match_limit(owner_id, _reply_obj):
        return
    key = f"join_Test_{owner_id}"
    keyboard = [[InlineKeyboardButton("\u2694\ufe0f Join Game", callback_data=key)]]
    name = html.escape(update.effective_user.first_name)
    caption = f"\U0001f3cf <b>Test Challenge!</b>\nUser: {name}\nMode: Test\nWaiting for opponent... <i>(expires in 2 min)</i>"
    banner = await get_banner_for_mode("test")
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
    _ch_key = f"{owner_id}_Test_{msg.message_id}"
    task = asyncio.create_task(_expire_challenge(_ch_key, owner_id, chat_id, msg.message_id, context.bot))
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, msg.message_id, "Test")
    except Exception:
        pass


async def challenge_unified(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /challenge [mode] — Start a draft challenge.
    With no args: shows mode picker buttons (IPL, ODI, Test, FIFA, WWE).
    """
    if _is_stale_command(update): return  # Drop replayed command from before bot restart
    owner_id = update.effective_user.id
    # ─ Match limit check for all /challenge entries (with or without args)
    if not await _check_match_limit(owner_id, update.effective_message):
        return

    if not context.args:
        keyboard = [
            [
                InlineKeyboardButton("\U0001f3cf IPL",  callback_data=f"challenge_pick_IPL_{owner_id}"),
                InlineKeyboardButton("\U0001f30d ODI",  callback_data=f"challenge_pick_ODI_{owner_id}"),
                InlineKeyboardButton("\U0001f3df Test", callback_data=f"challenge_pick_Test_{owner_id}"),
            ],
            [
                InlineKeyboardButton("\u26bd FIFA", callback_data=f"challenge_pick_FIFA_{owner_id}"),
                InlineKeyboardButton("\U0001f93c WWE",  callback_data=f"challenge_pick_WWE_{owner_id}"),
            ]
        ]
        try:
            await update.effective_message.reply_text(
                "\U0001f3ae <b>Choose a game mode to challenge:</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception:
            pass  # Bot has no send rights in this chat
        return

    mode_arg = context.args[0].lower()
    from utils.banners import get_banner_for_mode

    if mode_arg in ('odi', 'intl', 'international'):
        real_mode = "ODI"
        banner = await get_banner_for_mode("odi")
    elif mode_arg == 'test':
        real_mode = "Test"
        banner = await get_banner_for_mode("test")
    elif mode_arg in ('t20', 'ipl'):
        real_mode = "IPL"
        banner = await get_banner_for_mode("ipl")
    elif mode_arg in ('fifa', 'football'):
        real_mode = "FIFA"
        banner = await get_banner_for_mode("fifa")
    elif mode_arg in ('wwe', 'wrestling'):
        await send_wwe_gender_selector(update, context, owner_id)
        return
    else:
        await update.effective_message.reply_text(
            f"\u274c Unknown mode: {mode_arg}\nUse: `odi`, `test`, `ipl`, `fifa`, `wwe`.",
            parse_mode="Markdown"
        )
        return

    # Check for targeted challenge (reply)
    target_user = None
    if update.effective_message.reply_to_message:
        target_user = update.effective_message.reply_to_message.from_user
        if target_user.id == update.effective_user.id:
            await update.effective_message.reply_text("You can't challenge yourself!")
            return

    key = f"join_{real_mode}_{update.effective_user.id}"
    if target_user:
        key += f"_{target_user.id}"

    keyboard = [[InlineKeyboardButton("\u2694\ufe0f Accept Challenge", callback_data=key)]]

    from telegram.helpers import escape_markdown
    def _esc(t): return escape_markdown(t, version=1)

    if target_user:
        msg_text = (
            f"\U0001f3cf *{real_mode} Challenge!*\n"
            f"From: {_esc(update.effective_user.first_name)}\n"
            f"To: {_esc(target_user.first_name)}\n\n"
            f"Waiting for {_esc(target_user.first_name)} to accept..."
        )
    else:
        msg_text = (
            f"\U0001f3cf *{real_mode} Challenge!*\n"
            f"User: {_esc(update.effective_user.first_name)}\n"
            f"Waiting for opponent..."
        )

    owner_id = update.effective_user.id
    chat_id  = update.effective_chat.id
    sent_msg = None
    try:
        sent_msg = await update.effective_message.reply_photo(
            photo=banner, caption=msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    except Exception:
        try:
            sent_msg = await update.effective_message.reply_text(
                f"{msg_text}\n*(Enable media permissions in this chat to see banners)*",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
        except Exception:
            return

    if not sent_msg:
        return

    _ch_key = f"{owner_id}_{real_mode}_{sent_msg.message_id}"
    task = asyncio.create_task(
        _expire_challenge(_ch_key, owner_id, chat_id, sent_msg.message_id, context.bot)
    )
    _pending_challenges[_ch_key] = {'task': task, 'chat_id': chat_id, 'message_id': sent_msg.message_id}
    try:
        await save_pending_challenge(owner_id, chat_id, sent_msg.message_id, real_mode)
    except Exception:
        pass


async def handle_mode_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles challenge_pick_MODE_OWNERID inline button from /challenge mode picker.
    Only the user who sent /challenge can interact with the mode buttons.
    """
    query = update.callback_query
    # Format: challenge_pick_{MODE}_{owner_id}
    parts = query.data.split('_')  # ["challenge", "pick", MODE, owner_id]
    if len(parts) >= 4:
        mode = parts[2]
        try:
            owner_id = int(parts[3])
        except (ValueError, IndexError):
            owner_id = None
    else:
        # Backward-compat: old format without owner_id
        mode = query.data.split('_', 2)[2]
        owner_id = None

    # Owner check — only the challenger can pick a mode
    if owner_id and query.from_user.id != owner_id:
        await query.answer("\u274c Not for you! Only the person who sent /challenge can pick a mode.", show_alert=True)
        return

    # Check lock to prevent rapid double-clicks (spam)
    msg_id = query.message.message_id if query.message else None
    if msg_id:
        if msg_id in MODE_PICK_LOCKS:
            await query.answer("Processing your selection...", show_alert=False)
            return
        MODE_PICK_LOCKS.add(msg_id)

    await query.answer()
    if mode != "WWE":
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        if msg_id:
            MODE_PICK_LOCKS.discard(msg_id)
    dispatch = {
        "IPL": challenge_ipl,
        "ODI": challenge_odi,
        "Test": challenge_test,
        "FIFA": challenge_fifa,
        "WWE": challenge_wwe,
    }
    fn = dispatch.get(mode)
    if fn:
        await fn(update, context)

async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    parts = query.data.split('_') # join, MODE, OWNER_ID, [TARGET_ID]
    mode = parts[1]
    real_mode = "WWE Women" if mode == "WWEWomen" else mode
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

    # ─ Match limit checks ────────────────────────────────────────
    if not await _check_match_limit(query.from_user.id, query):
        return  # Joiner is at limit
    if not await _check_match_limit(owner_id, query):
        await query.answer("⛔ The challenger already has 2 active matches.", show_alert=True)
        return  # Owner somehow also at limit

    # ─ Atomic check and claim of the challenge ──────────────────
    from database import find_and_delete_pending_challenge
    claimed = await find_and_delete_pending_challenge(owner_id, mode)
    if not claimed:
        # Challenge is already accepted or expired.
        await query.answer("⚠️ Challenge has already been accepted or expired!", show_alert=True)
        try:
            # CRITICAL: Only strip the button if this message is NOT a live draft board.
            # If the challenge was already accepted, the same message_id is now the
            # draft board with "Draw Player" button. Calling edit_reply_markup(None)
            # here would silently wipe the Draw button from the active match.
            # We check: is there a live DRAFTING match that owns this message_id,
            # or does either player in this chat currently have an active match?
            from database import get_db as _gdb
            _db = _gdb()
            _msg_id = query.message.message_id if query.message else None
            _is_live_draft = False
            if _msg_id:
                _live = await _db.matches.find_one({
                    "chat_id": query.message.chat.id if query.message else None,
                    "state_data.state": {"$in": ["DRAFTING", "READY_CHECK"]},
                    "$or": [
                        {"state_data.draft_message_id": _msg_id},
                        {"state_data.team_a.owner_id": owner_id},
                        {"state_data.team_b.owner_id": owner_id},
                        {"state_data.team_a.owner_id": query.from_user.id},
                        {"state_data.team_b.owner_id": query.from_user.id}
                    ]
                })
                _is_live_draft = bool(_live)
            if not _is_live_draft:
                # Safe to remove button — this is a genuinely expired/stale challenge
                await query.message.edit_reply_markup(reply_markup=None)
            # If _is_live_draft is True: leave the message alone — it's the draft board
        except Exception:
            pass
        return

    # All checks passed — answer callback query to stop loading spinner
    await query.answer()

    # Cancel expiry task for THIS specific message — only reached if a different user is joining
    _joined_msg_id = query.message.message_id if query.message else None
    _ch_key = f"{owner_id}_{mode}_{_joined_msg_id}"
    pending = _pending_challenges.pop(_ch_key, None)
    if pending:
        task = pending.get('task')
        if task and not task.done():
            task.cancel()

        
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
    match = await create_match_state(
        chat_id=update.effective_chat.id,
        mode=real_mode, 
        owner_id=owner_id, 
        challenger_id=query.from_user.id,
        owner_name=challenger_name, # In state.py owner_name is param 5
        challenger_name=joiner_name, # In state.py challenger_name is param 6
        draft_message_id=query.message.message_id
    )
    
    # Start Draft (Update the message)
    from handlers.draft import format_draft_board, update_draft_message
    from utils.banners import get_banner_for_mode

    board_text = format_draft_board(match)
    keyboard = [[InlineKeyboardButton("🎲 Draw Player", callback_data=f"draw_{match.match_id}")]]

    if "IPL" in mode:
        banner = await get_banner_for_mode("ipl")
    elif mode == "FIFA":
        banner = await get_banner_for_mode("fifa")
    elif mode in ("WWE", "WWEWomen"):
        banner = await get_banner_for_mode("wwe" if mode == "WWE" else "wwe_women")
    elif mode == "Test":
        banner = await get_banner_for_mode("test")
    else:  # ODI (and legacy International)
        banner = await get_banner_for_mode("odi")

    # Edit the existing message into the draft board synchronously to bypass debouncer delay
    await update_draft_message(update, context, match, board_text, keyboard, media=banner, synchronous=True)

    # Start the 10-minute AFK forfeit timer for the first player's turn!
    from handlers.draft import _reset_afk_timer
    _reset_afk_timer(match, context.bot, update.effective_chat.id)

    # Pin the draft board — run in background task to avoid blocking the user
    pinned_msg_id = query.message.message_id
    async def _bg_pin():
        try:
            await context.bot.pin_chat_message(
                chat_id=update.effective_chat.id,
                message_id=pinned_msg_id,
                disable_notification=True
            )
            from game.state import load_match_state as _bg_load, save_match_state as _bg_save
            m = await _bg_load(match.match_id)
            if m:
                m.pinned_message_id = pinned_msg_id
                await _bg_save(m)
        except Exception:
            pass
    asyncio.create_task(_bg_pin())

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

