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
)

logger = logging.getLogger(__name__)

def esc(t):
    return escape_markdown(str(t), version=1)

# In-memory match locks and active lookup
_MATCH_LOCKS: Dict[str, asyncio.Lock] = {}
_ACTIVE_USER_MATCH: Dict[int, str] = {}  # user_id -> match_id
_USER_COOLDOWNS: Dict[int, float] = {}   # user_id -> timestamp
_ACTIVE_TIMERS: Dict[str, asyncio.Task] = {}  # match_id -> timer_task

def _get_lock(match_id: str) -> asyncio.Lock:
    if match_id not in _MATCH_LOCKS:
        _MATCH_LOCKS[match_id] = asyncio.Lock()
    return _MATCH_LOCKS[match_id]

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
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not chat:
        return

    # Must be in group
    if chat.type == "private":
        await msg.reply_text("🏏 Mini Cricket challenges must be sent in a *group chat*!\nAdd me to a group to play.", parse_mode="Markdown")
        return

    # Check cancel subcommand first
    args = context.args or []
    if args and args[0].lower() == "cancel":
        await handle_mini_cric_cancel(update, context)
        return

    # Cooldown check (5s)
    now = time.time()
    if now - _USER_COOLDOWNS.get(user.id, 0.0) < 5.0:
        await msg.reply_text("⏳ Please wait a few seconds before challenging again.")
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
        await msg.reply_text("⚠️ You already have a pending challenge! Wait for it to be accepted/declined, or type `/mini_cric cancel` to abort it.", parse_mode="Markdown")
        return

    # Check user active mini cric
    if is_user_in_mini_cric(user.id):
        await msg.reply_text("❌ You are already playing a Mini Cricket match! Finish it first.")
        return

    # Cross-mode check: active draft matches
    from database import get_user_active_matches_info
    user_drafts = await get_user_active_matches_info(user.id)
    if user_drafts:
        await msg.reply_text("❌ You are currently in an active draft match. Finish it before playing Mini Cricket!")
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
    elif msg.entities:
        for ent in msg.entities:
            if getattr(ent, "type", "") == "text_mention" and getattr(ent, "user", None):
                target_user = ent.user
                break
    if not target_user:
        await msg.reply_text(
            f"🏏 *Tactical Mini-Cricket (1-Wicket Match)*\n\n"
            f"Usage: `/mini_cric [1-5]` (reply to opponent or mention @username)\n"
            f"Example: `/mini_cric 2` (in reply to your opponent)\n"
            f"• Overs: *{overs}* | Rewards: *+{overs*10}🪙* / *+{overs*5}🪙*",
            parse_mode="Markdown"
        )
        return

    if target_user.is_bot or target_user.id == user.id:
        await msg.reply_text("❌ You cannot challenge a bot or yourself.")
        return

    # Target active check
    if is_user_in_mini_cric(target_user.id):
        await msg.reply_text(f"❌ *{esc(target_user.first_name)}* is already in a Mini Cricket match.", parse_mode="Markdown")
        return

    target_drafts = await get_user_active_matches_info(target_user.id)
    if target_drafts:
        await msg.reply_text(f"❌ *{esc(target_user.first_name)}* is currently playing a draft match.", parse_mode="Markdown")
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
        InlineKeyboardButton("✅ Accept Match", callback_data=f"mc_acc|{match_id}|{target_user.id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"mc_dec|{match_id}|{target_user.id}")
    ]])

    sent_msg = await msg.reply_text(
        f"🏏 *Mini-Cricket Challenge!* (1-Wicket Match)\n\n"
        f"*{esc(user.first_name)}* challenges *{esc(target_user.first_name)}*!\n"
        f"• Overs: *{overs}* ({overs*6} balls max)\n"
        f"• Winner: *+{overs*10}🪙* | Loser: *+{overs*5}🪙*\n\n"
        f"*{esc(target_user.first_name)}*, do you accept?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    match_doc["challenge_msg_id"] = sent_msg.message_id
    await _save_match(match_doc)


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
        await update.effective_message.reply_text("❌ You don't have any pending Mini Cricket challenge to cancel.")
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
                text=f"❌ *Challenge cancelled by {esc(user.first_name)}*.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await update.effective_message.reply_text(f"✅ *Your Mini Cricket challenge has been cancelled.*", parse_mode="Markdown")


# ── Challenge Accept / Decline Callbacks ───────────────────────────────────────

async def cb_mc_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, target_str = parts

    if str(query.from_user.id) != target_str:
        await query.answer("⛔ This challenge is not for you!", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await query.answer("⏳ Processing...", show_alert=False)
        return

    async with lock:
        await query.answer()
        match = await _get_match(match_id)
        if not match or match["status"] != "pending":
            await query.edit_message_text("❌ Challenge expired or already processed.")
            return

        # Double check active states
        from database import get_user_active_matches_info
        c_drafts = await get_user_active_matches_info(match["challenger_id"])
        o_drafts = await get_user_active_matches_info(match["opponent_id"])
        if c_drafts or o_drafts:
            await query.edit_message_text("❌ One of the players entered a draft match. Challenge cancelled.")
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
        # Edit group message to announce DM start
        await query.edit_message_text(
            f"🏏 *Challenge Accepted!*\n\n"
            f"*{esc(match['challenger_name'])}* vs *{esc(match['opponent_name'])}*\n"
            f"• Match is taking place in Bot DMs!\n"
            f"• [Click here to open DM](https://t.me/{bot_username})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

        # Send Toss UI in DMs
        await _send_toss_ui(context, match)


async def cb_mc_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, target_str = parts

    if str(query.from_user.id) != target_str:
        await query.answer("⛔ Not your challenge!", show_alert=True)
        return

    await query.answer()
    match = await _get_match(match_id)
    if match and match["status"] == "pending":
        match["status"] = "declined"
        await _save_match(match)
        await query.edit_message_text(f"❌ *{esc(query.from_user.first_name)} declined the challenge.*", parse_mode="Markdown")


# ── Toss Flow in DMs ──────────────────────────────────────────────────────────

async def _send_toss_ui(context: ContextTypes.DEFAULT_TYPE, match: dict):
    caller_id = match["toss_caller_id"]
    waiting_id = match["toss_waiting_id"]
    match_id = match["match_id"]

    toss_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🪙 Heads", callback_data=f"mc_toss|{match_id}|{caller_id}|heads"),
        InlineKeyboardButton("🪙 Tails", callback_data=f"mc_toss|{match_id}|{caller_id}|tails"),
    ]])

    # Caller gets toss buttons
    try:
        c_msg = await context.bot.send_message(
            chat_id=caller_id,
            text=f"🪙 *Toss Time!* (Mini-Cricket {match['overs']} Over)\n\nCall the coin toss:",
            reply_markup=toss_kb,
            parse_mode="Markdown"
        )
        match["caller_dm_msg_id"] = c_msg.message_id
    except Exception as e:
        logger.warning(f"Could not send toss DM to caller {caller_id}: {e}")

    # Waiting player sees status
    caller_name = match["challenger_name"] if caller_id == match["challenger_id"] else match["opponent_name"]
    try:
        w_msg = await context.bot.send_message(
            chat_id=waiting_id,
            text=f"🪙 *Toss Time!*\nWaiting for *{esc(caller_name)}* to call the toss...",
            parse_mode="Markdown"
        )
        match["waiting_dm_msg_id"] = w_msg.message_id
    except Exception as e:
        logger.warning(f"Could not send toss DM to waiter {waiting_id}: {e}")

    # Start 5-min AFK timer
    _reset_turn_timer(context, match_id, caller_id)
    await _save_match(match)


async def cb_mc_toss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, caller_str, call = parts

    if str(query.from_user.id) != caller_str:
        await query.answer("⛔ Not your toss call!", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await query.answer("⏳ Processing...", show_alert=False)
        return

    async with lock:
        await query.answer()
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
        await _save_match(match)

        decision_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First", callback_data=f"mc_decide|{match_id}|{toss_winner_id}|bat"),
            InlineKeyboardButton("⚡ Bowl First", callback_data=f"mc_decide|{match_id}|{toss_winner_id}|bowl"),
        ]])

        # Winner picks Bat or Bowl
        try:
            await context.bot.send_message(
                chat_id=toss_winner_id,
                text=f"🪙 Coin showed *{coin_flip.title()}*!\n🎉 *You won the toss!*\nChoose to Bat or Bowl:",
                reply_markup=decision_kb,
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # Loser waits
        try:
            await context.bot.send_message(
                chat_id=toss_loser_id,
                text=f"🪙 Coin showed *{coin_flip.title()}*.\n*{esc(winner_name)} won the toss* and is choosing to Bat or Bowl...",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        _reset_turn_timer(context, match_id, toss_winner_id)


async def cb_mc_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, winner_str, choice = parts

    if str(query.from_user.id) != winner_str:
        await query.answer("⛔ Not your decision!", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await query.answer("⏳ Processing...", show_alert=False)
        return

    async with lock:
        await query.answer()
        match = await _get_match(match_id)
        if not match or match["status"] != "decision":
            return

        winner_id = int(winner_str)
        loser_id = match["toss_loser_id"]

        if choice == "bat":
            batting_first_id = winner_id
            bowling_first_id = loser_id
        else:
            batting_first_id = loser_id
            bowling_first_id = winner_id

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

        await _save_match(match)

        # Clear existing messages and launch ball 1
        await _start_new_ball(context, match)


# ── Ball-by-Ball Gameplay Loop ────────────────────────────────────────────────

def _build_over_reel_text(reel: list) -> str:
    if not reel:
        return "[ . . . . . . ]"
    clean = "  ".join(reel)
    return f"[  {clean}  ]"


def _build_bowler_keyboard(match: dict, over_num: int) -> InlineKeyboardMarkup:
    match_id = match["match_id"]
    bowler_id = match["bowler_id"]
    used = match.get("mystery_used_over", {}).get(str(over_num), False)

    row1 = [
        InlineKeyboardButton("⚡ Bouncer", callback_data=f"mc_bowl|{match_id}|{bowler_id}|Bouncer"),
        InlineKeyboardButton("🎯 Yorker",  callback_data=f"mc_bowl|{match_id}|{bowler_id}|Yorker"),
    ]
    row2 = [
        InlineKeyboardButton("🏏 Fuller",  callback_data=f"mc_bowl|{match_id}|{bowler_id}|Fuller"),
        InlineKeyboardButton("🟢 Good",    callback_data=f"mc_bowl|{match_id}|{bowler_id}|Good"),
    ]
    rows = [row1, row2]
    if not used:
        rows.append([InlineKeyboardButton("❓ Mystery Ball (1/over)", callback_data=f"mc_bowl|{match_id}|{bowler_id}|Mystery")])
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
        InlineKeyboardButton("🛡️ Defend", callback_data=f"mc_shot|{match_id}|{batter_id}|Defend"),
        InlineKeyboardButton("🚀 Loft",   callback_data=f"mc_shot|{match_id}|{batter_id}|Loft"),
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
        balls_left = total_balls - balls_bowled
        target_line = f"🎯 Need: *{needed} runs* from *{balls_left} balls*\n"

    over_num = (balls_bowled // 6) + 1
    ball_in_over = (balls_bowled % 6) + 1
    reel_text = _build_over_reel_text(reel)

    # Names
    c_name = match["challenger_name"]
    o_name = match["opponent_name"]
    bat_name = c_name if batter_id == match["challenger_id"] else o_name
    bowl_name = o_name if batter_id == match["challenger_id"] else c_name

    bowler_text = (
        f"🏏 *Mini-Cricket ({overs} Ov) — Innings {innings}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Batting: *{esc(bat_name)}* | Score: *{score}/{wkts}* ({balls_bowled // 6}.{balls_bowled % 6} ov)\n"
        f"{target_line}"
        f"Over {over_num}: `{reel_text}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 {match['last_ball_commentary']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Your Turn to Bowl!* Pick delivery (Ball {ball_in_over}/6):"
    )

    batter_text = (
        f"🏏 *Mini-Cricket ({overs} Ov) — Innings {innings}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Batting: *{esc(bat_name)}* | Score: *{score}/{wkts}* ({balls_bowled // 6}.{balls_bowled % 6} ov)\n"
        f"{target_line}"
        f"Over {over_num}: `{reel_text}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 {match['last_ball_commentary']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏳ Waiting for *{esc(bowl_name)}* to bowl..."
    )

    bowler_kb = _build_bowler_keyboard(match, over_num)

    # Send / Edit Bowler Message
    b_msg_id = match.get("bowler_dm_msg_id")
    if b_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=bowler_id,
                message_id=b_msg_id,
                text=bowler_text,
                reply_markup=bowler_kb,
                parse_mode="Markdown"
            )
        except Exception:
            b_msg = await context.bot.send_message(chat_id=bowler_id, text=bowler_text, reply_markup=bowler_kb, parse_mode="Markdown")
            match["bowler_dm_msg_id"] = b_msg.message_id
    else:
        b_msg = await context.bot.send_message(chat_id=bowler_id, text=bowler_text, reply_markup=bowler_kb, parse_mode="Markdown")
        match["bowler_dm_msg_id"] = b_msg.message_id

    # Send / Edit Batter Message
    bat_msg_id = match.get("batter_dm_msg_id")
    if bat_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=batter_id,
                message_id=bat_msg_id,
                text=batter_text,
                reply_markup=None,
                parse_mode="Markdown"
            )
        except Exception:
            bat_msg = await context.bot.send_message(chat_id=batter_id, text=batter_text, parse_mode="Markdown")
            match["batter_dm_msg_id"] = bat_msg.message_id
    else:
        bat_msg = await context.bot.send_message(chat_id=batter_id, text=batter_text, parse_mode="Markdown")
        match["batter_dm_msg_id"] = bat_msg.message_id

    await _save_match(match)
    _reset_turn_timer(context, match_id, bowler_id)


# ── Delivery Selected Callback ────────────────────────────────────────────────

async def cb_mc_bowl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, bowler_str, delivery = parts

    if str(query.from_user.id) != bowler_str:
        await query.answer("⛔ Not your bowling turn!", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await query.answer("⏳ Processing...", show_alert=False)
        return

    async with lock:
        await query.answer()
        match = await _get_match(match_id)
        if not match or match["status"] != "in_progress":
            return

        bowler_id = int(bowler_str)
        batter_id = match["batter_id"]
        innings = match["innings"]
        balls_bowled = match["innings_1_balls"] if innings == 1 else match["innings_2_balls"]
        over_num = (balls_bowled // 6) + 1

        is_mystery = (delivery == "Mystery")
        actual_del = resolve_mystery_ball() if is_mystery else delivery

        if is_mystery:
            match.setdefault("mystery_used_over", {})[str(over_num)] = True

        match["pending_delivery"] = delivery
        match["pending_delivery_actual"] = actual_del
        await _save_match(match)

        # Update bowler UI (locked in)
        try:
            await query.edit_message_text(
                f"🎯 *Delivery Locked In: {delivery}*\nWaiting for batter to play their shot...",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # Update batter UI with shot choices
        batter_kb = _build_batter_keyboard(match)
        shown_del = "❓ Mystery Ball" if is_mystery else delivery
        try:
            await context.bot.edit_message_text(
                chat_id=batter_id,
                message_id=match["batter_dm_msg_id"],
                text=f"⚡ *Bowler has bowled: {shown_del}!*\nChoose your shot:",
                reply_markup=batter_kb,
                parse_mode="Markdown"
            )
        except Exception:
            bat_msg = await context.bot.send_message(
                chat_id=batter_id,
                text=f"⚡ *Bowler has bowled: {shown_del}!*\nChoose your shot:",
                reply_markup=batter_kb,
                parse_mode="Markdown"
            )
            match["batter_dm_msg_id"] = bat_msg.message_id
            await _save_match(match)

        _reset_turn_timer(context, match_id, batter_id)


# ── Shot Selected Callback & Outcome Resolution ───────────────────────────────

async def cb_mc_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    _, match_id, batter_str, shot = parts

    if str(query.from_user.id) != batter_str:
        await query.answer("⛔ Not your batting turn!", show_alert=True)
        return

    lock = _get_lock(match_id)
    if lock.locked():
        await query.answer("⏳ Processing...", show_alert=False)
        return

    async with lock:
        await query.answer()
        match = await _get_match(match_id)
        if not match or match["status"] != "in_progress":
            return

        delivery = match["pending_delivery"]
        actual_del = match["pending_delivery_actual"]
        if not delivery:
            return

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
                match["innings_1_reel"].append(str(runs) if runs > 0 else ".")
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
                match["innings_2_reel"].append(str(runs) if runs > 0 else ".")
            new_score = match["innings_2_score"]
            reel = match["innings_2_reel"]

        # Check milestone
        milestone = check_milestone(prev_score, new_score, balls)
        if milestone:
            comment = f"{comment}\n{milestone}"

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


# ── Innings Swap (Option A: 3-Second Target Pause) ────────────────────────────

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

    break_text = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"   *INNINGS 1 COMPLETE*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*{esc(b1_name)}*: *{score_1}/{wkts_1}* ({balls_1 // 6}.{balls_1 % 6} ov)\n"
        f"🎯 *Target for {esc(b2_name)}*: *{target} runs* from *{match['total_balls']} balls*\n\n"
        f"💬 {match['last_ball_commentary']}\n\n"
        f"🔄 _Swapping roles... Innings 2 starts now!_"
    )

    p1 = match["challenger_id"]
    p2 = match["opponent_id"]

    try:
        await context.bot.send_message(chat_id=p1, text=break_text, parse_mode="Markdown")
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=p2, text=break_text, parse_mode="Markdown")
    except Exception:
        pass

    await _save_match(match)

    # 3-Second pause (Option A)
    await asyncio.sleep(3)

    # Swap roles
    match["innings"] = 2
    match["batter_id"] = match["bowling_first_id"]
    match["bowler_id"] = match["batting_first_id"]
    match["status"] = "in_progress"
    match["mystery_used_over"] = {}
    match["last_ball_commentary"] = f"Innings 2 begins! Target: {target} runs."
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
        result_str = f"🤝 *Match Tied!* Scores level."
    elif winner_role == "batter":
        winner_id = match["batter_id"]
        loser_id = match["bowler_id"]
        w_name = c_name if winner_id == c_id else o_name
        result_str = f"🏆 *{esc(w_name)} won by 1 wicket!*"
    else:  # bowler won
        winner_id = match["bowler_id"]
        loser_id = match["batter_id"]
        w_name = c_name if winner_id == c_id else o_name
        margin = match["target"] - match["innings_2_score"] - 1
        result_str = f"🏆 *{esc(w_name)} won by {margin} runs!*"

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

    final_card = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"    *MATCH SUMMARY*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*{esc(b1_name)}*: {s1}\n"
        f"*{esc(b2_name)}*: {s2}\n\n"
        f"{result_str}\n\n"
        f"🪙 Rewards:\n"
        f"• *{esc(c_name)}*: +{rewards.get(c_id, 0)}🪙\n"
        f"• *{esc(o_name)}*: +{rewards.get(o_id, 0)}🪙"
    )

    # Send final scorecard to both players in DMs
    try:
        await context.bot.send_message(chat_id=c_id, text=final_card, parse_mode="Markdown")
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=o_id, text=final_card, parse_mode="Markdown")
    except Exception:
        pass

    # Post 3-line result announcement in group chat
    try:
        group_summary = (
            f"🏆 *Mini-Cricket Result ({match['overs']} Over)*\n"
            f"*{esc(b1_name)}*: {s1} · *{esc(b2_name)}*: {s2}\n"
            f"{result_str}\n"
            f"🪙 Rewards: *+{win_coins}🪙* / *+{lose_coins}🪙*"
        )
        await context.bot.send_message(chat_id=match["chat_id"], text=group_summary, parse_mode="Markdown")
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
            msg = f"💤 *Match ended: {esc(afk_name)} went AFK (5 min timeout).* No coins awarded."

            try:
                await context.bot.send_message(chat_id=c_id, text=msg, parse_mode="Markdown")
            except Exception:
                pass
            try:
                await context.bot.send_message(chat_id=o_id, text=msg, parse_mode="Markdown")
            except Exception:
                pass
            await _save_match(match)

    _ACTIVE_TIMERS[match_id] = asyncio.create_task(_timer_coro())


async def handle_mini_cric_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /end
    Mutual termination command in DM. Both players must run /end to abort with 0 coins.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private":
        return

    match_id = _ACTIVE_USER_MATCH.get(user.id)
    if not match_id:
        await update.effective_message.reply_text("❌ You are not in an active Mini Cricket match.")
        return

    lock = _get_lock(match_id)
    async with lock:
        match = await _get_match(match_id)
        if not match or match["status"] not in ("toss", "decision", "in_progress", "innings_break"):
            await update.effective_message.reply_text("❌ No active match to end.")
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
        
            abort_msg = "🛑 *Match mutually ended via /end.* No coins awarded to either player."
            try:
                await context.bot.send_message(chat_id=c_id, text=abort_msg, parse_mode="Markdown")
            except Exception:
                pass
            try:
                await context.bot.send_message(chat_id=o_id, text=abort_msg, parse_mode="Markdown")
            except Exception:
                pass
            await _save_match(match)
        else:
            await update.effective_message.reply_text(
                "🛑 *You voted to end the match.*\nWaiting for your opponent to type /end in their DM to confirm termination.",
                parse_mode="Markdown"
            )
            try:
                await context.bot.send_message(
                    chat_id=other_id,
                    text="🛑 *Your opponent requested to end the match.*\nType `/end` here if you agree to abort (0 coins).",
                    parse_mode="Markdown"
                )
            except Exception:
                pass


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
        resume_notice = "🔄 *Bot restarted — resuming your Mini Cricket match!*"
        try:
            await bot.send_message(chat_id=c_id, text=resume_notice, parse_mode="Markdown")
        except Exception:
            pass
        try:
            await bot.send_message(chat_id=o_id, text=resume_notice, parse_mode="Markdown")
        except Exception:
            pass

        if m["status"] == "in_progress":
            if m.get("pending_delivery"):
                b_kb = _build_batter_keyboard(m)
                shown_del = "🎭 Mystery Ball" if m["pending_delivery"] == "Mystery" else m["pending_delivery"]
                prompt = f"🏏 *Bowler bowled: {shown_del}*\nChoose your shot:"
                try:
                    bat_msg = await bot.send_message(chat_id=m["batter_id"], text=prompt, reply_markup=b_kb, parse_mode="Markdown")
                    m["batter_dm_msg_id"] = bat_msg.message_id
                except Exception:
                    pass
            else:
                balls_bowled = m["innings_1_balls"] if m["innings"] == 1 else m["innings_2_balls"]
                over_num = (balls_bowled // 6) + 1
                bowl_kb = _build_bowler_keyboard(m, over_num)
                ball_in_over = (balls_bowled % 6) + 1
                prompt = f"⚡ *Your Turn to Bowl!* Pick delivery (Ball {ball_in_over}/6):"
                try:
                    bowl_msg = await bot.send_message(chat_id=m["bowler_id"], text=prompt, reply_markup=bowl_kb, parse_mode="Markdown")
                    m["bowler_dm_msg_id"] = bowl_msg.message_id
                except Exception:
                    pass
        await _save_match(m)
