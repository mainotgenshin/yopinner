"""
handlers/mini_cric.py
Tactical 1v1 Mini-Cricket Handlers and Gameplay Loop.
Features:
- /mini_cric [1-5] group challenge & acceptance
- /mini_cric cancel (challenger pending cancel)
- /end mutual match termination in DM
- Clean DM in-place message editing (zero chat flood)
- 5-minute per-turn AFK timer with forfeit (0 coins to both)
- Mutex anti-spam locks on every turn
- Dynamic Mystery Ball button hiding (max 1 per over)
- Auto-resume recovery for bot restarts
"""
import asyncio
import logging
import random
import time
import html
import re
import uuid
from typing import Dict, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from game.mini_cric_engine import (
    simulate_ball,
    resolve_mystery_ball,
    check_milestone,
    calculate_rewards,
    DELIVERIES,
    SHOTS,
    WINNER_REWARDS,
)

logger = logging.getLogger(__name__)

def esc(t):
    return escape_markdown(str(t), version=1)

def esc_html(t):
    return html.escape(str(t or ''))

# In-memory match locks and active lookup
_MATCH_LOCKS: Dict[str, asyncio.Lock] = {}
_ACTIVE_USER_MATCH: Dict[int, str] = {}  # user_id -> match_id
_USER_COOLDOWNS: Dict[int, float] = {}   # user_id -> timestamp
_ACTIVE_TIMERS: Dict[str, asyncio.Task] = {}  # match_id -> timer_task

def _get_lock(match_id: str) -> asyncio.Lock:
    if match_id not in _MATCH_LOCKS:
        _MATCH_LOCKS[match_id] = asyncio.Lock()
    return _MATCH_LOCKS[match_id]

async def _safe_answer_query(query, text: Optional[str] = None, show_alert: bool = False):
    """Safely answers callback query; suppresses 'Query is too old' and connection timeouts."""
    if not query:
        return
    try:
        if text:
            await query.answer(text, show_alert=show_alert)
        else:
            await query.answer()
    except Exception as e:
        logger.debug(f"query.answer safely suppressed error: {e}")


def is_user_in_mini_cric(user_id: int) -> bool:
    """Check if a user is currently engaged in an active mini-cricket match."""
    return user_id in _ACTIVE_USER_MATCH


# ── Database Helpers ──────────────────────────────────────────────────────────

async def _save_match(match: dict):
    from database import get_db
    db = get_db()
    await db.mini_cric_matches.update_one(
        {"match_id": match["match_id"]},
        {"$set": match},
        upsert=True
    )

async def _get_match(match_id: str) -> Optional[dict]:
    from database import get_db
    db = get_db()
    return await db.mini_cric_matches.find_one({"match_id": match_id})


# ── Challenge Commands ────────────────────────────────────────────────────────

async def handle_mini_cric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mini_cric [overs] @opponent
    Starts a 1v1 tactical cricket challenge in group chat.
    """
    try:
        msg = update.effective_message
        user = update.effective_user
        chat = update.effective_chat

        if not msg or not user or not chat:
            return

        logger.info(f"=== [MINI_CRIC] Triggered by {user.id} ({user.first_name}) in chat {chat.id} ({chat.type}) with args={context.args} ===")

        # Must be in group or supergroup
        chat_type_str = str(chat.type).lower()
        if "private" in chat_type_str:
            if await _try_refresh_dm_turn(context, user.id, msg):
                return
            await msg.reply_text(
                "Mini Cricket challenges must be sent in a group chat!\n"
                "Add me to a group to play.\n\n"
                "(If you are in an active match, send /refresh to restore your turn buttons.)"
            )
            return

        # Check cancel subcommand first (e.g. /mini_cric cancel)
        args = context.args or []
        if args and args[0].lower() == "cancel":
            await handle_mini_cric_cancel(update, context)
            return

        # Cooldown check (3s)
        now = time.time()
        if now - _USER_COOLDOWNS.get(user.id, 0.0) < 3.0:
            await msg.reply_text("Please wait a moment before sending another command.")
            return
        _USER_COOLDOWNS[user.id] = now

        # Check if challenger already has a pending challenge
        from database import get_db
        db = get_db()
        existing_pending = await db.mini_cric_matches.find_one({
            "challenger_id": user.id,
            "status": "pending"
        })
        if existing_pending:
            await msg.reply_text("You already have a pending challenge! Wait for it to be accepted/declined, or type /mini_cric cancel to abort it.")
            return

        # Check user active mini cric
        if is_user_in_mini_cric(user.id):
            await msg.reply_text("You are already playing a Mini Cricket match! Finish it first.")
            return

        # Cross-mode check: active draft matches
        from database import get_user_active_matches_info
        user_drafts = await get_user_active_matches_info(user.id)
        if user_drafts:
            await msg.reply_text("You are currently in an active draft match. Finish it before playing Mini Cricket!")
            return

        # Parse overs argument
        overs = 1
        for a in args:
            if a.isdigit():
                val = int(a)
                if 1 <= val <= 5:
                    overs = val
                break

        # Parse target opponent
        target_user = None
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target_user = msg.reply_to_message.from_user
            logger.info(f"=== [MINI_CRIC] Opponent from reply: {target_user.id} ({target_user.first_name}) ===")
        elif getattr(msg, "entities", None):
            for ent in msg.entities:
                if getattr(ent, "type", "") == "text_mention" and getattr(ent, "user", None):
                    target_user = ent.user
                    logger.info(f"=== [MINI_CRIC] Opponent from text_mention: {target_user.id} ({target_user.first_name}) ===")
                    break

        if not target_user:
            logger.info("=== [MINI_CRIC] No opponent found; replying with usage instructions ===")
            win_reward = WINNER_REWARDS.get(overs, 3)
            ov_label = f"{overs} Over" if overs == 1 else f"{overs} Overs"
            usage_html = (
                f"<b>Tactical Mini-Cricket (1-Wicket Match)</b>\n\n"
                f"Usage: <code>/mini_cric [1-5]</code> (in reply to opponent's message)\n"
                f"Example: <code>/mini_cric 2</code> (replying to your opponent)\n"
                f"• Overs: <b>{ov_label}</b> | Winner: <b>+{win_reward}🪙</b> | Loser: <b>0🪙</b>"
            )
            try:
                await msg.reply_text(usage_html, parse_mode="HTML")
            except Exception:
                await msg.reply_text(
                    f"Tactical Mini-Cricket (1-Wicket Match)\n\n"
                    f"Usage: /mini_cric [1-5] (in reply to opponent's message)\n"
                    f"Example: /mini_cric 2 (replying to your opponent)\n"
                    f"• Overs: {ov_label} | Winner: +{win_reward}🪙 | Loser: 0🪙"
                )
            return

        if target_user.is_bot or target_user.id == user.id:
            await msg.reply_text("You cannot challenge a bot or yourself.")
            return

        # Target active check
        if is_user_in_mini_cric(target_user.id):
            t_name = esc_html(target_user.first_name)
            await msg.reply_text(f"<b>{t_name}</b> is already in a Mini Cricket match.", parse_mode="HTML")
            return

        target_drafts = await get_user_active_matches_info(target_user.id)
        if target_drafts:
            t_name = esc_html(target_user.first_name)
            await msg.reply_text(f"<b>{t_name}</b> is currently playing a draft match.", parse_mode="HTML")
            return

        # Create challenge
        match_id = str(uuid.uuid4())[:8]
        match_doc = {
            "match_id": match_id,
            "chat_id": chat.id,
            "challenger_id": user.id,
            "challenger_name": user.first_name,
            "opponent_id": target_user.id,
            "opponent_name": target_user.first_name,
            "overs": overs,
            "total_balls": overs * 6,
            "status": "pending",
            "created_at": now,
            "innings": 1,
            "innings_1_score": 0,
            "innings_1_wickets": 0,
            "innings_1_balls": 0,
            "innings_1_reel": [],
            "innings_2_score": 0,
            "innings_2_wickets": 0,
            "innings_2_balls": 0,
            "innings_2_reel": [],
            "target": 0,
            "mystery_used_over": {},
            "end_requested_by": [],
            "group_message_id": msg.message_id,
        }

        await _save_match(match_doc)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Accept Match", callback_data=f"mc_acc|{match_id}|{target_user.id}"),
            InlineKeyboardButton("Decline", callback_data=f"mc_dec|{match_id}|{target_user.id}")
        ]])

        c_name = esc_html(user.first_name)
        t_name = esc_html(target_user.first_name)
        win_reward = WINNER_REWARDS.get(overs, 3)
        ov_label = f"{overs} Over" if overs == 1 else f"{overs} Overs"

        challenge_text = (
            f"<b>Mini-Cricket Challenge!</b> (1-Wicket Match)\n\n"
            f"<b>{c_name}</b> challenges <b>{t_name}</b>!\n"
            f"• Overs: <b>{ov_label}</b> ({overs*6} balls max)\n"
            f"• Winner: <b>+{win_reward}🪙</b> | Loser: <b>0🪙</b>\n\n"
            f"<b>{t_name}</b>, do you accept?"
        )

        try:
            sent_msg = await msg.reply_text(
                challenge_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception as _e1:
            logger.warning(f"HTML challenge send failed: {_e1}, sending plain text...")
            sent_msg = await msg.reply_text(
                f"Mini-Cricket Challenge! (1-Wicket Match)\n\n"
                f"{user.first_name} challenges {target_user.first_name}!\n"
                f"• Overs: {ov_label} ({overs*6} balls max)\n"
                f"• Winner: +{win_reward}🪙 | Loser: 0🪙\n\n"
                f"{target_user.first_name}, do you accept?",
                reply_markup=keyboard
            )

        match_doc["challenge_msg_id"] = sent_msg.message_id
        await _save_match(match_doc)
        logger.info(f"=== [MINI_CRIC] Challenge created: match_id={match_id} ===")

    except Exception as err:
        logger.error(f"=== [MINI_CRIC ERROR] Unhandled exception in handle_mini_cric: {err}", exc_info=True)
        try:
            await update.effective_message.reply_text(f"Mini-Cricket Error: {err}")
        except Exception:
            pass


async def handle_mini_cric_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mini_cric cancel — Allows challenger to cancel their specific pending challenge."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    from database import get_db
    db = get_db()
    match = await db.mini_cric_matches.find_one({
        "challenger_id": user.id,
        "status": "pending"
    })
    if not match:
        await update.effective_message.reply_text("You don't have any pending Mini Cricket challenge to cancel.")
        return

    match["status"] = "cancelled"
    await _save_match(match)

    challenge_msg_id = match.get("challenge_msg_id")
    group_chat_id = match.get("chat_id")
    if challenge_msg_id and group_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=group_chat_id,
                message_id=challenge_msg_id,
                text=f"<b>Challenge cancelled by {esc_html(user.first_name)}</b>.",
                parse_mode="HTML"
            )
        except Exception:
            try:
                await context.bot.edit_message_text(
                    chat_id=group_chat_id,
                    message_id=challenge_msg_id,
                    text=f"Challenge cancelled by {user.first_name}."
                )
            except Exception:
                pass

    try:
        await update.effective_message.reply_text("<b>Your Mini Cricket challenge has been cancelled.</b>", parse_mode="HTML")
    except Exception:
        await update.effective_message.reply_text("Your Mini Cricket challenge has been cancelled.")


async def handle_mini_cric_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /refresh command in DM:
    Immediately restores turn buttons and current scoreboard if buttons ever disappear.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    await _try_refresh_dm_turn(context, user.id, msg)


async def _try_refresh_dm_turn(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_msg) -> bool:
    """Finds active match for user and re-sends current turn prompt with interactive buttons."""
    from database import get_db
    db = get_db()

    match_id = _ACTIVE_USER_MATCH.get(user_id)
    match = None
    if match_id:
        match = await _get_match(match_id)
    if not match:
        match = await db.mini_cric_matches.find_one({
            "$or": [{"challenger_id": user_id}, {"opponent_id": user_id}],
            "status": {"$in": ["toss", "decision", "in_progress"]}
        })

    if not match or match.get("status") not in ("toss", "decision", "in_progress"):
        if reply_msg:
            await reply_msg.reply_text("You are not in an active Mini Cricket match.")
        return False

    m_id = match["match_id"]
    _ACTIVE_USER_MATCH[match["challenger_id"]] = m_id
    _ACTIVE_USER_MATCH[match["opponent_id"]] = m_id
    status = match.get("status")

    if status == "toss":
        caller_id = match.get("toss_caller_id")
        if user_id == caller_id:
            toss_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Heads", callback_data=f"mc_toss|{m_id}|{caller_id}|heads"),
                InlineKeyboardButton("Tails", callback_data=f"mc_toss|{m_id}|{caller_id}|tails"),
            ]])
            sent = await reply_msg.reply_text("<b>Toss Time!</b>\nCall the coin toss:", reply_markup=toss_kb, parse_mode="HTML")
            match["caller_dm_msg_id"] = sent.message_id
            await _save_match(match)
        else:
            await reply_msg.reply_text("<i>Waiting for opponent to call the toss...</i>", parse_mode="HTML")
        return True

    elif status == "decision":
        winner_id = match.get("toss_winner_id")
        if user_id == winner_id:
            decision_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Bat First", callback_data=f"mc_decide|{m_id}|{winner_id}|bat"),
                InlineKeyboardButton("Bowl First", callback_data=f"mc_decide|{m_id}|{winner_id}|bowl"),
            ]])
            sent = await reply_msg.reply_text("<b>You won the toss!</b>\nChoose to Bat or Bowl:", reply_markup=decision_kb, parse_mode="HTML")
            match["caller_dm_msg_id"] = sent.message_id
            await _save_match(match)
        else:
            await reply_msg.reply_text("<i>Waiting for toss winner to choose Bat or Bowl...</i>", parse_mode="HTML")
        return True

    elif status == "in_progress":
        bowler_id = match["bowler_id"]
        batter_id = match["batter_id"]
        innings = match["innings"]
        overs = match["overs"]
        total_balls = match["total_balls"]
        balls_bowled = match["innings_1_balls"] if innings == 1 else match["innings_2_balls"]
        over_num = (balls_bowled // 6) + 1
        ball_in_over = (balls_bowled % 6) + 1
        score = match["innings_1_score"] if innings == 1 else match["innings_2_score"]
        wkts = match["innings_1_wickets"] if innings == 1 else match["innings_2_wickets"]
        reel = match["innings_1_reel"] if innings == 1 else match["innings_2_reel"]
        reel_text = _build_over_reel_text(reel, over_num)
        ov_str = f"{balls_bowled // 6}.{balls_bowled % 6}"
        ov_label = f"{overs} Over" if overs == 1 else f"{overs} Overs"

        c_name = match["challenger_name"]
        o_name = match["opponent_name"]
        bat_name = c_name if batter_id == match["challenger_id"] else o_name
        bowl_name = o_name if batter_id == match["challenger_id"] else c_name

        target_line = ""
        if innings == 2:
            target = match["target"]
            needed = max(0, target - score)
            balls_left = max(0, total_balls - balls_bowled)
            rrr = (needed / (balls_left / 6.0)) if balls_left > 0 else 0.0
            run_term = "run" if needed == 1 else "runs"
            ball_term = "ball" if balls_left == 1 else "balls"
            target_line = f"Target: <b>{target}</b> (Need <b>{needed} {run_term}</b> from <b>{balls_left} {ball_term}</b> | RRR: <b>{rrr:.1f}</b>)\n"

        if user_id == bowler_id:
            if match.get("pending_delivery"):
                delivery = match["pending_delivery"]
                text = (
                    f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Batting: <b>{esc_html(bat_name)}</b>\n"
                    f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
                    f"{target_line}"
                    f"Over {over_num}: [ {reel_text} ]\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Delivery Locked In: <b>{delivery}</b>\n"
                    f"<i>Waiting for <b>{esc_html(bat_name)}</b> to play their shot...</i>"
                )
                sent = await reply_msg.reply_text(text, parse_mode="HTML")
                match["bowler_dm_msg_id"] = sent.message_id
            else:
                bowl_kb = _build_bowler_keyboard(match, over_num)
                text = (
                    f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Batting: <b>{esc_html(bat_name)}</b>\n"
                    f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
                    f"{target_line}"
                    f"Over {over_num}: [ {reel_text} ]\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Last Ball: <i>{esc_html(match['last_ball_commentary'])}</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Your Turn to Bowl! Pick delivery (Ball {ball_in_over}/6):"
                )
                sent = await reply_msg.reply_text(text, reply_markup=bowl_kb, parse_mode="HTML")
                match["bowler_dm_msg_id"] = sent.message_id
            await _save_match(match)
            return True

        elif user_id == batter_id:
            if match.get("pending_delivery"):
                bat_kb = _build_batter_keyboard(match)
                del_shown = "Mystery Ball" if match["pending_delivery"] == "Mystery" else match["pending_delivery"]
                text = (
                    f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Batting: <b>{esc_html(bat_name)}</b>\n"
                    f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
                    f"{target_line}"
                    f"Over {over_num}: [ {reel_text} ]\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Bowler bowled: <b>{del_shown}</b>!\n"
                    f"Choose your shot:"
                )
                sent = await reply_msg.reply_text(text, reply_markup=bat_kb, parse_mode="HTML")
                match["batter_dm_msg_id"] = sent.message_id
            else:
                text = (
                    f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Batting: <b>{esc_html(bat_name)}</b>\n"
                    f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
                    f"{target_line}"
                    f"Over {over_num}: [ {reel_text} ]\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Last Ball: <i>{esc_html(match['last_ball_commentary'])}</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Waiting for <b>{esc_html(bowl_name)}</b> to bowl...</i>"
                )
                sent = await reply_msg.reply_text(text, parse_mode="HTML")
                match["batter_dm_msg_id"] = sent.message_id
            await _save_match(match)
            return True

    return False


# ── Clean HTML & In-Place DM Editing Helpers ─────────────────────────────────

_USER_CLICK_COOLDOWN: dict[int, float] = {}

def _check_click_rate_limit(user_id: int) -> bool:
    """Returns True if user is allowed to click; False if spamming within 2 seconds."""
    now = time.time()
    last = _USER_CLICK_COOLDOWN.get(user_id, 0.0)
    if now - last < 2.0:
        return False
    _USER_CLICK_COOLDOWN[user_id] = now
    return True

def _strip_html_tags(text: str) -> str:
    return re.sub(r'<[^>]+>', '', str(text or ''))

async def _send_or_edit_dm(bot, user_id: int, msg_id: Optional[int], text: str, reply_markup=None, parse_mode="HTML") -> int:
    """Edits active message in-place to prevent chat flood, falling back to sending a new message."""
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
            return msg_id
        except Exception:
            try:
                # Plain-text fallback for edit in case formatting fails
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=msg_id,
                    text=_strip_html_tags(text),
                    reply_markup=reply_markup,
                    parse_mode=None
                )
                return msg_id
            except Exception:
                pass
    try:
        sent = await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
        return sent.message_id
    except Exception:
        sent = await bot.send_message(
            chat_id=user_id,
            text=_strip_html_tags(text),
            reply_markup=reply_markup,
            parse_mode=None
        )
        return sent.message_id


# ── Challenge Accept / Decline Callbacks ───────────────────────────────────────

async def cb_mc_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, target_str = parts

    if str(query.from_user.id) != target_str:
        await _safe_answer_query(query, "This challenge is not for you!", show_alert=True)
        return

    if not _check_click_rate_limit(query.from_user.id):
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    async with lock:
        await _safe_answer_query(query)
        match = await _get_match(match_id)
        if not match or match["status"] != "pending":
            await query.edit_message_text("Challenge expired or already processed.")
            return

        # Double check active states
        from database import get_user_active_matches_info
        c_drafts = await get_user_active_matches_info(match["challenger_id"])
        o_drafts = await get_user_active_matches_info(match["opponent_id"])
        if c_drafts or o_drafts:
            await query.edit_message_text("One of the players entered a draft match. Challenge cancelled.")
            return

        # Update state to toss
        match["status"] = "toss"
        c_id = match["challenger_id"]
        o_id = match["opponent_id"]

        _ACTIVE_USER_MATCH[c_id] = match_id
        _ACTIVE_USER_MATCH[o_id] = match_id

        # Random caller for toss
        caller_id = random.choice([c_id, o_id])
        waiting_id = o_id if caller_id == c_id else c_id
        caller_name = match["challenger_name"] if caller_id == c_id else match["opponent_name"]

        match["toss_caller_id"] = caller_id
        match["toss_waiting_id"] = waiting_id
        await _save_match(match)

        bot_username = context.bot.username or "Bot"
        # Edit group message to announce DM start and remove challenge buttons
        try:
            await query.edit_message_text(
                f"<b>Challenge Accepted!</b>\n\n"
                f"<b>{esc_html(match['challenger_name'])}</b> vs <b>{esc_html(match['opponent_name'])}</b>\n"
                f"• Match is taking place in Bot DMs!\n"
                f"• <a href=\"https://t.me/{bot_username}\">Click here to open DM</a>",
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            await query.edit_message_text(
                f"Challenge Accepted!\n\n"
                f"{match['challenger_name']} vs {match['opponent_name']}\n"
                f"• Match is taking place in Bot DMs!\n"
                f"• https://t.me/{bot_username}",
                disable_web_page_preview=True
            )

        # Send Toss UI in DMs
        await _send_toss_ui(context, match)


async def cb_mc_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, target_str = parts

    if str(query.from_user.id) != target_str:
        await _safe_answer_query(query, "Not your challenge!", show_alert=True)
        return

    if not _check_click_rate_limit(query.from_user.id):
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    await _safe_answer_query(query)
    match = await _get_match(match_id)
    if match and match["status"] == "pending":
        match["status"] = "declined"
        await _save_match(match)
        try:
            await query.edit_message_text(f"<b>{esc_html(query.from_user.first_name)} declined the challenge.</b>", parse_mode="HTML")
        except Exception:
            await query.edit_message_text(f"{query.from_user.first_name} declined the challenge.")


# ── Toss Flow in DMs ──────────────────────────────────────────────────────────

async def _send_toss_ui(context: ContextTypes.DEFAULT_TYPE, match: dict):
    caller_id = match["toss_caller_id"]
    waiting_id = match["toss_waiting_id"]
    match_id = match["match_id"]

    toss_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Heads", callback_data=f"mc_toss|{match_id}|{caller_id}|heads"),
        InlineKeyboardButton("Tails", callback_data=f"mc_toss|{match_id}|{caller_id}|tails"),
    ]])

    ov_label = f"{match['overs']} Over" if match['overs'] == 1 else f"{match['overs']} Overs"
    caller_text = (
        f"<b>Toss Time!</b> (Mini-Cricket <b>{ov_label}</b>)\n\n"
        f"Call the coin toss:"
    )
    match["caller_dm_msg_id"] = await _send_or_edit_dm(
        context.bot, caller_id, match.get("caller_dm_msg_id"), caller_text, reply_markup=toss_kb
    )

    caller_name = match["challenger_name"] if caller_id == match["challenger_id"] else match["opponent_name"]
    waiting_text = (
        f"<b>Toss Time!</b> (Mini-Cricket <b>{ov_label}</b>)\n\n"
        f"<i>Waiting for <b>{esc_html(caller_name)}</b> to call the toss...</i>"
    )
    match["waiting_dm_msg_id"] = await _send_or_edit_dm(
        context.bot, waiting_id, match.get("waiting_dm_msg_id"), waiting_text, reply_markup=None
    )

    _reset_turn_timer(context, match_id, caller_id)
    await _save_match(match)


async def cb_mc_toss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, caller_str, call = parts

    if str(query.from_user.id) != caller_str:
        await _safe_answer_query(query, "Not your toss call!", show_alert=True)
        return

    if not _check_click_rate_limit(query.from_user.id):
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    async with lock:
        await _safe_answer_query(query)
        match = await _get_match(match_id)
        if not match or match["status"] != "toss":
            return

        caller_id = int(caller_str)
        waiting_id = match["toss_waiting_id"]

        coin_flip = random.choice(["heads", "tails"])
        won = (call == coin_flip)
        toss_winner_id = caller_id if won else waiting_id
        toss_loser_id = waiting_id if won else caller_id

        winner_name = match["challenger_name"] if toss_winner_id == match["challenger_id"] else match["opponent_name"]

        match["status"] = "decision"
        match["toss_winner_id"] = toss_winner_id
        match["toss_loser_id"] = toss_loser_id
        match["coin_flip"] = coin_flip
        await _save_match(match)

        decision_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Bat First", callback_data=f"mc_decide|{match_id}|{toss_winner_id}|bat"),
            InlineKeyboardButton("Bowl First", callback_data=f"mc_decide|{match_id}|{toss_winner_id}|bowl"),
        ]])

        winner_text = (
            f"Coin showed <b>{coin_flip.title()}</b>!\n"
            f"<b>You won the toss!</b>\n\n"
            f"Choose to Bat or Bowl:"
        )
        loser_text = (
            f"Coin showed <b>{coin_flip.title()}</b>.\n"
            f"<i><b>{esc_html(winner_name)} won the toss</b> and is choosing to Bat or Bowl...</i>"
        )

        if caller_id == toss_winner_id:
            # Caller won: edit caller's message in-place to decision buttons (removes Heads/Tails!)
            match["caller_dm_msg_id"] = await _send_or_edit_dm(
                context.bot, caller_id, query.message.message_id, winner_text, reply_markup=decision_kb
            )
            # Edit waiting player's message in-place
            match["waiting_dm_msg_id"] = await _send_or_edit_dm(
                context.bot, waiting_id, match.get("waiting_dm_msg_id"), loser_text, reply_markup=None
            )
        else:
            # Caller lost: edit caller's message in-place (removes Heads/Tails!)
            match["caller_dm_msg_id"] = await _send_or_edit_dm(
                context.bot, caller_id, query.message.message_id, loser_text, reply_markup=None
            )
            # Edit waiting player's message in-place to decision buttons
            match["waiting_dm_msg_id"] = await _send_or_edit_dm(
                context.bot, waiting_id, match.get("waiting_dm_msg_id"), winner_text, reply_markup=decision_kb
            )

        await _save_match(match)
        _reset_turn_timer(context, match_id, toss_winner_id)


async def cb_mc_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, winner_str, choice = parts

    if str(query.from_user.id) != winner_str:
        await _safe_answer_query(query, "Not your decision!", show_alert=True)
        return

    if not _check_click_rate_limit(query.from_user.id):
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    async with lock:
        await _safe_answer_query(query)
        match = await _get_match(match_id)
        if not match or match["status"] != "decision":
            return

        winner_id = int(winner_str)
        loser_id = match["toss_loser_id"]
        caller_id = match["toss_caller_id"]
        winner_name = match["challenger_name"] if winner_id == match["challenger_id"] else match["opponent_name"]

        if choice == "bat":
            batting_first_id = winner_id
            bowling_first_id = loser_id
        else:
            batting_first_id = loser_id
            bowling_first_id = winner_id

        other_choice = "Bowl" if choice == "bat" else "Bat"

        # Edit winner message in-place (removes Bat/Bowl buttons!)
        winner_msg_id = query.message.message_id
        await _send_or_edit_dm(
            context.bot,
            winner_id,
            winner_msg_id,
            f"<b>You chose to {choice.title()} First!</b>\n<i>Preparing pitch & match ball...</i>",
            reply_markup=None
        )

        # Edit loser message in-place
        loser_dm_msg_id = match["waiting_dm_msg_id"] if winner_id == caller_id else match["caller_dm_msg_id"]
        await _send_or_edit_dm(
            context.bot,
            loser_id,
            loser_dm_msg_id,
            f"<b>{esc_html(winner_name)} chose to {choice.title()} First.</b> You will {other_choice} First!\n<i>Preparing pitch & match ball...</i>",
            reply_markup=None
        )

        match["batting_first_id"] = batting_first_id
        match["bowling_first_id"] = bowling_first_id
        match["batter_id"] = batting_first_id
        match["bowler_id"] = bowling_first_id
        match["status"] = "in_progress"
        match["innings"] = 1
        match["current_ball_num"] = 0
        match["pending_delivery"] = None
        match["pending_delivery_actual"] = None
        match["last_ball_commentary"] = "Match beginning! Innings 1 underway."

        # Assign in-progress DM message IDs
        if choice == "bat":
            match["batter_dm_msg_id"] = winner_msg_id
            match["bowler_dm_msg_id"] = loser_dm_msg_id
        else:
            match["bowler_dm_msg_id"] = winner_msg_id
            match["batter_dm_msg_id"] = loser_dm_msg_id

        await _save_match(match)

        # Launch ball 1 (directly edits messages in-place!)
        await _start_new_ball(context, match)


# ── Ball-by-Ball Gameplay Loop ────────────────────────────────────────────────

def _format_reel_ball(b: str) -> str:
    s = str(b)
    if s == "4":
        return "4️⃣"
    if s == "6":
        return "6️⃣"
    if s.upper() == "W":
        return "🔴"
    return s


def _build_over_reel_text(reel: list, over_num: int) -> str:
    start_idx = max(0, (over_num - 1) * 6)
    over_balls = reel[start_idx : start_idx + 6]
    display = [_format_reel_ball(b) for b in over_balls]
    while len(display) < 6:
        display.append(".")
    return " · ".join(display)


def _build_bowler_keyboard(match: dict, over_num: int) -> InlineKeyboardMarkup:
    match_id = match["match_id"]
    bowler_id = match["bowler_id"]
    used = match.get("mystery_used_over", {}).get(str(over_num), False)

    row1 = [
        InlineKeyboardButton("Bouncer", callback_data=f"mc_bowl|{match_id}|{bowler_id}|Bouncer"),
        InlineKeyboardButton("Yorker",  callback_data=f"mc_bowl|{match_id}|{bowler_id}|Yorker"),
    ]
    row2 = [
        InlineKeyboardButton("Fuller",  callback_data=f"mc_bowl|{match_id}|{bowler_id}|Fuller"),
        InlineKeyboardButton("Good",    callback_data=f"mc_bowl|{match_id}|{bowler_id}|Good"),
    ]
    rows = [row1, row2]
    if not used:
        rows.append([InlineKeyboardButton("Mystery Ball (1/over)", callback_data=f"mc_bowl|{match_id}|{bowler_id}|Mystery")])
    return InlineKeyboardMarkup(rows)


def _build_batter_keyboard(match: dict) -> InlineKeyboardMarkup:
    match_id = match["match_id"]
    batter_id = match["batter_id"]
    row1 = [
        InlineKeyboardButton("Drive",  callback_data=f"mc_shot|{match_id}|{batter_id}|Drive"),
        InlineKeyboardButton("Cut",    callback_data=f"mc_shot|{match_id}|{batter_id}|Cut"),
        InlineKeyboardButton("Pull",   callback_data=f"mc_shot|{match_id}|{batter_id}|Pull"),
    ]
    row2 = [
        InlineKeyboardButton("Defend", callback_data=f"mc_shot|{match_id}|{batter_id}|Defend"),
        InlineKeyboardButton("Loft",   callback_data=f"mc_shot|{match_id}|{batter_id}|Loft"),
    ]
    return InlineKeyboardMarkup([row1, row2])


async def _start_new_ball(context: ContextTypes.DEFAULT_TYPE, match: dict):
    """Sets up the turn for the next ball: bowler picks delivery."""
    match_id = match["match_id"]
    bowler_id = match["bowler_id"]
    batter_id = match["batter_id"]

    match["pending_delivery"] = None
    match["pending_delivery_actual"] = None
    await _save_match(match)

    innings = match["innings"]
    overs = match["overs"]
    total_balls = match["total_balls"]

    if innings == 1:
        score = match["innings_1_score"]
        wkts = match["innings_1_wickets"]
        balls_bowled = match["innings_1_balls"]
        reel = match["innings_1_reel"]
        target_line = ""
    else:
        score = match["innings_2_score"]
        wkts = match["innings_2_wickets"]
        balls_bowled = match["innings_2_balls"]
        reel = match["innings_2_reel"]
        target = match["target"]
        needed = max(0, target - score)
        balls_left = max(0, total_balls - balls_bowled)
        rrr = (needed / (balls_left / 6.0)) if balls_left > 0 else 0.0
        run_term = "run" if needed == 1 else "runs"
        ball_term = "ball" if balls_left == 1 else "balls"
        target_line = f"Target: <b>{target}</b> (Need <b>{needed} {run_term}</b> from <b>{balls_left} {ball_term}</b> | RRR: <b>{rrr:.1f}</b>)\n"

    over_num = (balls_bowled // 6) + 1
    ball_in_over = (balls_bowled % 6) + 1
    reel_text = _build_over_reel_text(reel, over_num)
    ov_str = f"{balls_bowled // 6}.{balls_bowled % 6}"
    ov_label = f"{overs} Over" if overs == 1 else f"{overs} Overs"

    # Names
    c_name = match["challenger_name"]
    o_name = match["opponent_name"]
    bat_name = c_name if batter_id == match["challenger_id"] else o_name
    bowl_name = o_name if batter_id == match["challenger_id"] else c_name

    bowler_text = (
        f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Batting: <b>{esc_html(bat_name)}</b>\n"
        f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
        f"{target_line}"
        f"Over {over_num}: [ {reel_text} ]\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Last Ball: <i>{esc_html(match['last_ball_commentary'])}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Your Turn to Bowl! Pick delivery (Ball {ball_in_over}/6):"
    )

    batter_text = (
        f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Batting: <b>{esc_html(bat_name)}</b>\n"
        f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
        f"{target_line}"
        f"Over {over_num}: [ {reel_text} ]\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Last Ball: <i>{esc_html(match['last_ball_commentary'])}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Waiting for <b>{esc_html(bowl_name)}</b> to bowl...</i>"
    )

    bowler_kb = _build_bowler_keyboard(match, over_num)

    # Edit Bowler Message in-place
    match["bowler_dm_msg_id"] = await _send_or_edit_dm(
        context.bot, bowler_id, match.get("bowler_dm_msg_id"), bowler_text, reply_markup=bowler_kb
    )

    # Edit Batter Message in-place
    match["batter_dm_msg_id"] = await _send_or_edit_dm(
        context.bot, batter_id, match.get("batter_dm_msg_id"), batter_text, reply_markup=None
    )

    await _save_match(match)
    _reset_turn_timer(context, match_id, bowler_id)


# ── Delivery Selected Callback ────────────────────────────────────────────────

async def cb_mc_bowl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, bowler_str, delivery = parts

    if str(query.from_user.id) != bowler_str:
        await _safe_answer_query(query, "Not your bowling turn!", show_alert=True)
        return

    # 2-second rate limit to prevent spamming
    if not _check_click_rate_limit(query.from_user.id):
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    async with lock:
        match = await _get_match(match_id)
        if not match or match["status"] != "in_progress":
            await _safe_answer_query(query, "Match is not in progress.", show_alert=False)
            return

        # Double delivery check: if already locked in, do not re-send to batter!
        if match.get("pending_delivery") is not None:
            await _safe_answer_query(query, "Delivery already locked in! Waiting for batter...", show_alert=True)
            return

        await _safe_answer_query(query)

        bowler_id = int(bowler_str)
        batter_id = match["batter_id"]
        innings = match["innings"]
        overs = match["overs"]
        total_balls = match["total_balls"]

        if innings == 1:
            score = match["innings_1_score"]
            wkts = match["innings_1_wickets"]
            balls_bowled = match["innings_1_balls"]
            reel = match["innings_1_reel"]
            target_line = ""
        else:
            score = match["innings_2_score"]
            wkts = match["innings_2_wickets"]
            balls_bowled = match["innings_2_balls"]
            reel = match["innings_2_reel"]
            target = match["target"]
            needed = max(0, target - score)
            balls_left = max(0, total_balls - balls_bowled)
            rrr = (needed / (balls_left / 6.0)) if balls_left > 0 else 0.0
            run_term = "run" if needed == 1 else "runs"
            ball_term = "ball" if balls_left == 1 else "balls"
            target_line = f"Target: <b>{target}</b> (Need <b>{needed} {run_term}</b> from <b>{balls_left} {ball_term}</b> | RRR: <b>{rrr:.1f}</b>)\n"

        over_num = (balls_bowled // 6) + 1
        reel_text = _build_over_reel_text(reel, over_num)
        ov_str = f"{balls_bowled // 6}.{balls_bowled % 6}"
        ov_label = f"{overs} Over" if overs == 1 else f"{overs} Overs"

        c_name = match["challenger_name"]
        o_name = match["opponent_name"]
        bat_name = c_name if batter_id == match["challenger_id"] else o_name

        is_mystery = (delivery == "Mystery")
        actual_del = resolve_mystery_ball() if is_mystery else delivery

        if is_mystery:
            match.setdefault("mystery_used_over", {})[str(over_num)] = True

        match["pending_delivery"] = delivery
        match["pending_delivery_actual"] = actual_del
        await _save_match(match)

        # Update bowler UI (locked in, retaining scoreboard)
        bowler_locked_text = (
            f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Batting: <b>{esc_html(bat_name)}</b>\n"
            f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
            f"{target_line}"
            f"Over {over_num}: [ {reel_text} ]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Delivery Locked In: <b>{delivery}</b>\n"
            f"<i>Waiting for <b>{esc_html(bat_name)}</b> to play their shot...</i>"
        )
        match["bowler_dm_msg_id"] = await _send_or_edit_dm(
            context.bot, bowler_id, query.message.message_id, bowler_locked_text, reply_markup=None
        )

        # Update batter UI with shot choices
        batter_kb = _build_batter_keyboard(match)
        shown_del = "Mystery Ball" if is_mystery else delivery
        batter_prompt_text = (
            f"<b>Mini-Cricket ({ov_label}) — Innings {innings}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Batting: <b>{esc_html(bat_name)}</b>\n"
            f"Score: <b>{score}/{wkts}</b> ({ov_str} ov)\n"
            f"{target_line}"
            f"Over {over_num}: [ {reel_text} ]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bowler bowled: <b>{shown_del}</b>!\n"
            f"Choose your shot:"
        )
        match["batter_dm_msg_id"] = await _send_or_edit_dm(
            context.bot, batter_id, match.get("batter_dm_msg_id"), batter_prompt_text, reply_markup=batter_kb
        )

        await _save_match(match)
        _reset_turn_timer(context, match_id, batter_id)


# ── Shot Selected Callback & Outcome Resolution ───────────────────────────────

async def cb_mc_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, batter_str, shot = parts

    if str(query.from_user.id) != batter_str:
        await _safe_answer_query(query, "Not your batting turn!", show_alert=True)
        return

    # 2-second rate limit to prevent spamming
    if not _check_click_rate_limit(query.from_user.id):
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await _safe_answer_query(query, "Please wait 2 seconds...", show_alert=True)
        return

    async with lock:
        match = await _get_match(match_id)
        if not match or match["status"] != "in_progress":
            await _safe_answer_query(query, "Match is not in progress.", show_alert=False)
            return

        delivery = match.get("pending_delivery")
        actual_del = match.get("pending_delivery_actual")
        if not delivery:
            await _safe_answer_query(query, "Shot already played for this ball!", show_alert=True)
            return

        match["pending_delivery"] = None
        match["pending_delivery_actual"] = None

        await _safe_answer_query(query)

        # Update batter message ID
        match["batter_dm_msg_id"] = query.message.message_id

        is_mystery = (delivery == "Mystery")
        is_wicket, runs, comment = simulate_ball(delivery, shot, is_mystery, actual_del)

        innings = match["innings"]
        total_balls = match["total_balls"]

        if innings == 1:
            prev_score = match["innings_1_score"]
            match["innings_1_balls"] += 1
            balls = match["innings_1_balls"]
            if is_wicket:
                match["innings_1_wickets"] += 1
                match["innings_1_reel"].append("W")
            else:
                match["innings_1_score"] += runs
                match["innings_1_reel"].append(str(runs) if runs > 0 else "0")
            new_score = match["innings_1_score"]
            reel = match["innings_1_reel"]
        else:
            prev_score = match["innings_2_score"]
            match["innings_2_balls"] += 1
            balls = match["innings_2_balls"]
            if is_wicket:
                match["innings_2_wickets"] += 1
                match["innings_2_reel"].append("W")
            else:
                match["innings_2_score"] += runs
                match["innings_2_reel"].append(str(runs) if runs > 0 else "0")
            new_score = match["innings_2_score"]
            reel = match["innings_2_reel"]

        # Check milestone
        milestone = check_milestone(prev_score, new_score, balls)
        if milestone:
            comment = f"{comment}\n{milestone}"

        match["last_ball_delivery"] = delivery
        match["last_ball_shot"] = shot
        match["last_ball_commentary"] = comment
        await _save_match(match)

        # ── Innings 1 Completion Check ──
        if innings == 1:
            if match["innings_1_wickets"] >= 1 or match["innings_1_balls"] >= total_balls:
                await _end_innings_1(context, match)
                return
            else:
                await _start_new_ball(context, match)
                return

        # ── Innings 2 Completion Check ──
        target = match["target"]
        if new_score >= target:
            # Batter won early chase!
            await _finalize_match(context, match, winner_role="batter")
            return
        elif match["innings_2_wickets"] >= 1:
            # Wicket fell, bowler won!
            await _finalize_match(context, match, winner_role="bowler")
            return
        elif match["innings_2_balls"] >= total_balls:
            # Overs finished
            if new_score == target - 1:
                await _finalize_match(context, match, winner_role="tie")
            else:
                await _finalize_match(context, match, winner_role="bowler")
            return
        else:
            await _start_new_ball(context, match)
            return


# ── Innings Swap (3-Second Pause in Place) ────────────────────────────────────

async def _end_innings_1(context: ContextTypes.DEFAULT_TYPE, match: dict):
    match["status"] = "innings_break"
    target = match["innings_1_score"] + 1
    match["target"] = target

    c_name = match["challenger_name"]
    o_name = match["opponent_name"]
    b1_name = c_name if match["batting_first_id"] == match["challenger_id"] else o_name
    b2_name = o_name if match["batting_first_id"] == match["challenger_id"] else c_name

    score_1 = match["innings_1_score"]
    wkts_1 = match["innings_1_wickets"]
    balls_1 = match["innings_1_balls"]
    ov_1 = f"{balls_1 // 6}.{balls_1 % 6}"

    break_text = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"   <b>INNINGS 1 COMPLETE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{esc_html(b1_name)}</b>: <b>{score_1}/{wkts_1}</b> ({ov_1} ov)\n"
        f"Target for <b>{esc_html(b2_name)}</b>: <b>{target} runs</b> ({match['total_balls']} balls)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Last Ball: <i>{esc_html(match['last_ball_commentary'])}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Swapping roles... Innings 2 starts in 3 seconds!</i>"
    )

    p1 = match["challenger_id"]
    p2 = match["opponent_id"]

    # Edit both messages in-place with break screen
    match["bowler_dm_msg_id"] = await _send_or_edit_dm(
        context.bot, p1, match.get("bowler_dm_msg_id"), break_text, reply_markup=None
    )
    match["batter_dm_msg_id"] = await _send_or_edit_dm(
        context.bot, p2, match.get("batter_dm_msg_id"), break_text, reply_markup=None
    )

    await _save_match(match)

    # 3-Second pause
    await asyncio.sleep(3)

    # Swap roles
    match["innings"] = 2
    match["batter_id"] = match["bowling_first_id"]
    match["bowler_id"] = match["batting_first_id"]
    match["status"] = "in_progress"
    match["mystery_used_over"] = {}
    match["last_ball_commentary"] = f"Innings 2 begins! Target: {target} runs."

    # Swap message IDs to match new bowler and batter roles
    old_b_mid = match.get("bowler_dm_msg_id")
    match["bowler_dm_msg_id"] = match.get("batter_dm_msg_id")
    match["batter_dm_msg_id"] = old_b_mid

    await _save_match(match)

    await _start_new_ball(context, match)


# ── Match Finalization & Scaled Rewards ───────────────────────────────────────

async def _finalize_match(context: ContextTypes.DEFAULT_TYPE, match: dict, winner_role: str):
    match["status"] = "completed"
    _cancel_timer(match["match_id"])

    # Clear active locks
    c_id = match["challenger_id"]
    o_id = match["opponent_id"]
    _ACTIVE_USER_MATCH.pop(c_id, None)
    _ACTIVE_USER_MATCH.pop(o_id, None)
    _MATCH_LOCKS.pop(match["match_id"], None)

    c_name = match["challenger_name"]
    o_name = match["opponent_name"]

    is_draw = (winner_role == "tie")
    if is_draw:
        winner_id = c_id
        loser_id = o_id
        result_str_html = "<b>Match Tied!</b> Scores level."
    elif winner_role == "batter":
        winner_id = match["batter_id"]
        loser_id = match["bowler_id"]
        w_name = c_name if winner_id == c_id else o_name
        result_str_html = f"<b>{esc_html(w_name)} won by 1 wicket!</b>"
    else:  # bowler won
        winner_id = match["bowler_id"]
        loser_id = match["batter_id"]
        w_name = c_name if winner_id == c_id else o_name
        margin = match["target"] - match["innings_2_score"] - 1
        run_word = "run" if margin == 1 else "runs"
        result_str_html = f"<b>{esc_html(w_name)} won by {margin} {run_word}!</b>"

    # Coin rewards
    from database import add_card_coins
    rewards = calculate_rewards(match["overs"], winner_id, loser_id, is_draw, is_forfeit=False)
    win_coins = rewards.get(winner_id, 0)
    lose_coins = rewards.get(loser_id, 0)

    await add_card_coins(winner_id, win_coins)
    await add_card_coins(loser_id, lose_coins)

    s1 = f"{match['innings_1_score']}/{match['innings_1_wickets']} ({match['innings_1_balls']//6}.{match['innings_1_balls']%6} ov)"
    s2 = f"{match['innings_2_score']}/{match['innings_2_wickets']} ({match['innings_2_balls']//6}.{match['innings_2_balls']%6} ov)"
    b1_name = c_name if match["batting_first_id"] == c_id else o_name
    b2_name = o_name if match["batting_first_id"] == c_id else c_name

    last_del = match.get("last_ball_delivery") or match.get("pending_delivery", "Ball")
    last_shot = match.get("last_ball_shot", "Shot")

    # Decisive last ball over and outcome notation (e.g. W for wicket or runs on chase)
    balls_2 = match.get("innings_2_balls", 0)
    final_ov = f"{balls_2 // 6}.{balls_2 % 6}"
    reel_2 = match.get("innings_2_reel", [])
    ball_sym = reel_2[-1] if reel_2 else ""
    sym_str = f"<b>{ball_sym}</b> · " if ball_sym else ""

    # Rewards text for DM
    c_coins = rewards.get(c_id, 0)
    o_coins = rewards.get(o_id, 0)

    final_card = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"   <b>MATCH SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{esc_html(b1_name)}</b>: {s1}\n"
        f"<b>{esc_html(b2_name)}</b>: {s2}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{result_str_html}\n\n"
        f"Last Ball ({final_ov} ov): {sym_str}{esc_html(last_del)} vs {esc_html(last_shot)}\n\n"
        f"Rewards:\n"
        f"• <b>{esc_html(c_name)}</b>: +{c_coins}🪙\n"
        f"• <b>{esc_html(o_name)}</b>: +{o_coins}🪙\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    # Edit existing DM messages in-place for both players (no lingering buttons!)
    c_mid = match.get("bowler_dm_msg_id") if c_id == match.get("bowler_id") else match.get("batter_dm_msg_id")
    o_mid = match.get("bowler_dm_msg_id") if o_id == match.get("bowler_id") else match.get("batter_dm_msg_id")

    await _send_or_edit_dm(context.bot, c_id, c_mid, final_card, reply_markup=None)
    await _send_or_edit_dm(context.bot, o_id, o_mid, final_card, reply_markup=None)

    # Post result announcement in group chat
    ov_label = f"{match['overs']} Over" if match['overs'] == 1 else f"{match['overs']} Overs"
    if is_draw:
        reward_line = "Rewards: +1🪙 / +1🪙 Card Coins (Tie)"
    else:
        reward_line = f"Rewards: +{win_coins}🪙 / {lose_coins}🪙 Card Coins"

    try:
        group_summary = (
            f"<b>Mini-Cricket Result ({ov_label})</b>\n\n"
            f"<b>{esc_html(b1_name)}</b>: {s1} · <b>{esc_html(b2_name)}</b>: {s2}\n"
            f"{result_str_html}\n\n"
            f"Last Ball ({final_ov} ov): {sym_str}{esc_html(last_del)} vs {esc_html(last_shot)}\n"
            f"{reward_line}"
        )
        await context.bot.send_message(chat_id=match["chat_id"], text=group_summary, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not send group result announcement: {e}")

    await _save_match(match)


# ── AFK 5-Min Timer & Mutual /end Handling ────────────────────────────────────

def _cancel_timer(match_id: str):
    if match_id in _ACTIVE_TIMERS:
        _ACTIVE_TIMERS[match_id].cancel()
        _ACTIVE_TIMERS.pop(match_id, None)


def _reset_turn_timer(context: ContextTypes.DEFAULT_TYPE, match_id: str, active_user_id: int):
    _cancel_timer(match_id)

    async def _timer_coro():
        try:
            await asyncio.sleep(300)  # 5 minutes
        except asyncio.CancelledError:
            return
        lock = _get_lock(match_id)
        async with lock:
            match = await _get_match(match_id)
            if not match or match["status"] not in ("toss", "decision", "in_progress"):
                return
            # Forfeit due to AFK: both receive 0 coins
            match["status"] = "abandoned"
            c_id = match["challenger_id"]
            o_id = match["opponent_id"]
            _ACTIVE_USER_MATCH.pop(c_id, None)
            _ACTIVE_USER_MATCH.pop(o_id, None)
            _MATCH_LOCKS.pop(match_id, None)
        
            afk_name = match["challenger_name"] if active_user_id == c_id else match["opponent_name"]
            msg = f"<b>Match ended: {esc_html(afk_name)} went AFK (5 min timeout).</b> No coins awarded."

            c_mid = match.get("bowler_dm_msg_id") if c_id == match.get("bowler_id") else match.get("batter_dm_msg_id")
            o_mid = match.get("bowler_dm_msg_id") if o_id == match.get("bowler_id") else match.get("batter_dm_msg_id")

            await _send_or_edit_dm(context.bot, c_id, c_mid, msg, reply_markup=None)
            await _send_or_edit_dm(context.bot, o_id, o_mid, msg, reply_markup=None)
            await _save_match(match)

    _ACTIVE_TIMERS[match_id] = asyncio.create_task(_timer_coro())


async def handle_mini_cric_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /end
    Mutual termination command in DM. Both players must run /end to abort with 0 coins.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or str(chat.type).lower() != "private":
        return

    match_id = _ACTIVE_USER_MATCH.get(user.id)
    if not match_id:
        await update.effective_message.reply_text("You are not in an active Mini Cricket match.")
        return

    lock = _get_lock(match_id)
    async with lock:
        match = await _get_match(match_id)
        if not match or match["status"] not in ("toss", "decision", "in_progress", "innings_break"):
            await update.effective_message.reply_text("No active match to end.")
            return

        end_list = match.setdefault("end_requested_by", [])
        if user.id not in end_list:
            end_list.append(user.id)
            await _save_match(match)

        c_id = match["challenger_id"]
        o_id = match["opponent_id"]
        other_id = o_id if user.id == c_id else c_id

        if len(end_list) >= 2:
            # Both confirmed /end
            match["status"] = "aborted"
            _cancel_timer(match_id)
            _ACTIVE_USER_MATCH.pop(c_id, None)
            _ACTIVE_USER_MATCH.pop(o_id, None)
        
            abort_msg = "<b>Match mutually ended via /end.</b> No coins awarded to either player."
            c_mid = match.get("bowler_dm_msg_id") if c_id == match.get("bowler_id") else match.get("batter_dm_msg_id")
            o_mid = match.get("bowler_dm_msg_id") if o_id == match.get("bowler_id") else match.get("batter_dm_msg_id")

            await _send_or_edit_dm(context.bot, c_id, c_mid, abort_msg, reply_markup=None)
            await _send_or_edit_dm(context.bot, o_id, o_mid, abort_msg, reply_markup=None)
            await _save_match(match)
        else:
            try:
                await update.effective_message.reply_text(
                    "<b>You voted to end the match.</b>\nWaiting for your opponent to type /end in their DM to confirm termination.",
                    parse_mode="HTML"
                )
            except Exception:
                await update.effective_message.reply_text(
                    "You voted to end the match.\nWaiting for your opponent to type /end in their DM to confirm termination."
                )
            try:
                await context.bot.send_message(
                    chat_id=other_id,
                    text="<b>Your opponent requested to end the match.</b>\nType <code>/end</code> here if you agree to abort (0 coins).",
                    parse_mode="HTML"
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=other_id,
                    text="Your opponent requested to end the match.\nType /end here if you agree to abort (0 coins)."
                )


# ── Auto-Resume Hook for GitHub & Dyno Restarts ───────────────────────────────

async def resume_mini_cric_matches(bot):
    """
    Called from main.py _startup_recovery() on dyno restart or GitHub deploy.
    Scans for in-progress mini matches and resumes them in DMs.
    """
    from database import get_db
    db = get_db()
    now = time.time()

    cursor = db.mini_cric_matches.find({"status": {"$in": ["toss", "decision", "in_progress", "innings_break"]}})
    matches = await cursor.to_list(100)

    for m in matches:
        match_id = m["match_id"]
        age = now - m.get("created_at", now)

        # Expire matches older than 30 minutes
        if age > 1800:
            m["status"] = "expired"
            await _save_match(m)
            continue

        # Re-register memory locks
        c_id = m["challenger_id"]
        o_id = m["opponent_id"]
        _ACTIVE_USER_MATCH[c_id] = match_id
        _ACTIVE_USER_MATCH[o_id] = match_id
    
        # Send recovery notification & refresh interactive turn in DM
        resume_notice = "<b>Bot restarted — resuming your Mini Cricket match!</b>"
        try:
            await bot.send_message(chat_id=c_id, text=resume_notice, parse_mode="HTML")
        except Exception:
            pass
        try:
            await bot.send_message(chat_id=o_id, text=resume_notice, parse_mode="HTML")
        except Exception:
            pass

        if m["status"] == "in_progress":
            if m.get("pending_delivery"):
                b_kb = _build_batter_keyboard(m)
                shown_del = "Mystery Ball" if m["pending_delivery"] == "Mystery" else m["pending_delivery"]
                prompt = f"<b>Bowler bowled: {shown_del}!</b>\nChoose your shot:"
                try:
                    bat_msg = await bot.send_message(chat_id=m["batter_id"], text=prompt, reply_markup=b_kb, parse_mode="HTML")
                    m["batter_dm_msg_id"] = bat_msg.message_id
                except Exception:
                    pass
            else:
                balls_bowled = m["innings_1_balls"] if m["innings"] == 1 else m["innings_2_balls"]
                over_num = (balls_bowled // 6) + 1
                bowl_kb = _build_bowler_keyboard(m, over_num)
                ball_in_over = (balls_bowled % 6) + 1
                prompt = f"Your Turn to Bowl! Pick delivery (Ball {ball_in_over}/6):"
                try:
                    bowl_msg = await bot.send_message(chat_id=m["bowler_id"], text=prompt, reply_markup=bowl_kb, parse_mode="HTML")
                    m["bowler_dm_msg_id"] = bowl_msg.message_id
                except Exception:
                    pass
        await _save_match(m)
