# handlers/standings.py
"""
/standings — Leaderboard system with 6 views:
  Tabs: [Overall] [Daily] [Weekly]
  Filter: [This Chat] [Cricket] [FIFA]

Anti-spam: 3s per-user cooldown, same-tab guard
Performance: 60s in-memory cache, all sorting at MongoDB level
Isolation: zero shared state with game/draft logic
"""

import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# ── In-memory caches ───────────────────────────────────────────────────────
# { cache_key: (data_list, timestamp) }
_lb_cache: dict = {}
CACHE_TTL = 60  # seconds

# Per-user last-click timestamp for anti-spam
_user_cooldown: dict = {}
COOLDOWN_SECS = 3


def _cache_key(view: str, chat_id: int | None = None) -> str:
    return f"{view}_{chat_id}" if chat_id else view


def _get_cached(key: str):
    entry = _lb_cache.get(key)
    if entry and (time.time() - entry[1]) < CACHE_TTL:
        return entry[0]
    return None


def _set_cache(key: str, data):
    _lb_cache[key] = (data, time.time())


def invalidate_lb_cache():
    """Call this from simulation.py after a match finishes."""
    _lb_cache.clear()


# ── Helpers ────────────────────────────────────────────────────────────────

def _rank_emoji(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")


def _time_ago(ts: float | None) -> str:
    if not ts:
        return "just now"
    diff = int(time.time() - ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    return f"{diff // 3600}h ago"


def _reset_timer(anchor_ts: float) -> str:
    """Returns 'Xd Xh' or 'Xh' countdown from now to next anchor reset."""
    now = time.time()
    left = max(0, int(anchor_ts - now))
    days = left // 86400
    hours = (left % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h"


def _next_midnight_utc() -> float:
    import datetime
    now = time.time()
    dt = time.gmtime(now)
    midnight = time.mktime(time.strptime(
        f"{dt.tm_year}-{dt.tm_mon:02d}-{dt.tm_mday:02d} 00:00:00",
        "%Y-%m-%d %H:%M:%S"
    ))
    if midnight <= now:
        midnight += 86400
    return midnight


def _next_monday_utc() -> float:
    import datetime
    now = time.time()
    dt = time.gmtime(now)
    days_until_monday = (7 - dt.tm_wday) % 7 or 7
    midnight_today = time.mktime(time.strptime(
        f"{dt.tm_year}-{dt.tm_mon:02d}-{dt.tm_mday:02d} 00:00:00",
        "%Y-%m-%d %H:%M:%S"
    ))
    return midnight_today + days_until_monday * 86400


# ── Reset check (called on /standings open) ───────────────────────────────

async def _check_and_apply_resets(user_id: int):
    """
    Checks if daily/weekly periods have expired for this user and resets
    their counters in the DB if so. Called when /standings is opened so
    the display is always accurate even without a recent match.
    """
    import time as _t
    from database import get_db
    db = get_db()

    now = _t.time()
    doc = await db.users.find_one(
        {"user_id": user_id},
        {"daily_reset_at": 1, "weekly_reset_at": 1, "_id": 0}
    )
    if not doc:
        return  # no stats yet, nothing to reset

    set_fields = {}

    if now >= doc.get("daily_reset_at", 0):
        # Compute next midnight UTC
        dt = _t.gmtime(now)
        midnight = _t.mktime(_t.strptime(
            f"{dt.tm_year}-{dt.tm_mon:02d}-{dt.tm_mday:02d} 00:00:00",
            "%Y-%m-%d %H:%M:%S"
        ))
        if midnight <= now:
            midnight += 86400
        set_fields["daily_wins"] = 0
        set_fields["daily_reset_at"] = midnight

    if now >= doc.get("weekly_reset_at", 0):
        dt = _t.gmtime(now)
        days_until_monday = (7 - dt.tm_wday) % 7 or 7
        midnight_today = _t.mktime(_t.strptime(
            f"{dt.tm_year}-{dt.tm_mon:02d}-{dt.tm_mday:02d} 00:00:00",
            "%Y-%m-%d %H:%M:%S"
        ))
        if midnight_today <= now:
            midnight_today += 86400
        monday = midnight_today + (days_until_monday - 1) * 86400
        set_fields["weekly_wins"] = 0
        set_fields["weekly_reset_at"] = monday

    if set_fields:
        await db.users.update_one({"user_id": user_id}, {"$set": set_fields})
        invalidate_lb_cache()  # data changed, force fresh fetch


# ── DB Queries ─────────────────────────────────────────────────────────────

async def _fetch_leaderboard(view: str, chat_id: int | None = None) -> list:
    """
    Returns top-10 user dicts for the given view.
    All sorting/limiting done at MongoDB level.
    """
    from database import get_db
    db = get_db()

    sort_field = {
        "overall": "wins",
        "daily": "daily_wins",
        "weekly": "weekly_wins",
        "cricket": "cricket_wins",
        "fifa": "fifa_wins",
        "chat": f"chat_wins.{chat_id}",
    }.get(view, "wins")

    projection = {
        "user_id": 1, "name": 1,
        "wins": 1, "daily_wins": 1, "weekly_wins": 1,
        "cricket_wins": 1, "fifa_wins": 1, "chat_wins": 1,
        "first_win_at": 1,
        f"prev_rank_{view}": 1,
        "_id": 0
    }

    query = {}
    if view == "chat" and chat_id:
        query = {f"chat_wins.{chat_id}": {"$gt": 0}}

    cursor = db.users.find(query, projection).sort(
        [(sort_field, -1), ("first_win_at", 1)]
    ).limit(10)

    return [doc async for doc in cursor]


async def _get_user_rank(user_id: int, view: str, chat_id: int | None = None) -> tuple:
    """Returns (rank, wins_for_view) for a specific user. Lightweight count query."""
    from database import get_db
    db = get_db()

    sort_field = {
        "overall": "wins",
        "daily": "daily_wins",
        "weekly": "weekly_wins",
        "cricket": "cricket_wins",
        "fifa": "fifa_wins",
        "chat": f"chat_wins.{chat_id}",
    }.get(view, "wins")

    user_doc = await db.users.find_one(
        {"user_id": user_id},
        {"wins": 1, "daily_wins": 1, "weekly_wins": 1,
         "cricket_wins": 1, "fifa_wins": 1, "chat_wins": 1, "_id": 0}
    )
    if not user_doc:
        return (None, 0)

    if view == "chat" and chat_id:
        user_wins = user_doc.get("chat_wins", {}).get(str(chat_id), 0)
    else:
        user_wins = user_doc.get(sort_field.split(".")[-1], 0)

    gt_query = {sort_field: {"$gt": user_wins}}
    if view == "chat" and chat_id:
        gt_query[f"chat_wins.{chat_id}"] = {"$gt": user_wins}
    rank = await db.users.count_documents(gt_query) + 1
    return (rank, user_wins)


# ── Rank change tracking ───────────────────────────────────────────────────

async def _get_and_update_rank_change(user_id: int, view: str, current_rank: int) -> int:
    """Returns delta (positive = moved up, negative = moved down). Updates stored rank."""
    from database import get_db
    db = get_db()
    field = f"prev_rank_{view}"
    doc = await db.users.find_one({"user_id": user_id}, {field: 1, "_id": 0})
    prev = doc.get(field) if doc else None
    await db.users.update_one({"user_id": user_id}, {"$set": {field: current_rank}}, upsert=True)
    if prev is None:
        return 0
    return prev - current_rank  # positive = moved up


# ── Text builder ───────────────────────────────────────────────────────────

def _wins_for_view(doc: dict, view: str, chat_id: int | None) -> int:
    if view == "chat" and chat_id:
        return doc.get("chat_wins", {}).get(str(chat_id), 0)
    return doc.get({
        "overall": "wins",
        "daily": "daily_wins",
        "weekly": "weekly_wins",
        "cricket": "cricket_wins",
        "fifa": "fifa_wins",
    }.get(view, "wins"), 0)


def _build_text(
    view: str, rows: list, user_id: int,
    user_rank: int | None, user_wins: int,
    chat_id: int | None, last_updated_ts: float | None
) -> str:
    labels = {
        "overall": "🏆 GLOBAL STANDINGS",
        "daily":   "📅 DAILY STANDINGS",
        "weekly":  "📆 WEEKLY STANDINGS",
        "cricket": "🏏 CRICKET STANDINGS",
        "fifa":    "⚽ FIFA STANDINGS",
        "chat":    "🏠 THIS CHAT STANDINGS",
    }
    separator = "━━━━━━━━━━━━━━━"

    lines = [f"*{labels.get(view, 'STANDINGS')}*\n"]

    if not rows:
        lines.append("No standings yet 👀\nStart playing to claim the top spot!")
    else:
        for i, doc in enumerate(rows, 1):
            wins = _wins_for_view(doc, view, chat_id)
            name = doc.get("name", "Player")
            is_you = doc.get("user_id") == user_id

            rank_sym = _rank_emoji(i)
            crown = " 👑" if i == 1 else ""
            you = " 👈 *YOU*" if is_you else ""

            # Rank change
            change = doc.get(f"_rank_change", 0)
            if change > 0:
                change_str = f" ⬆️ +{change}"
            elif change < 0:
                change_str = f" ⬇️ {change}"
            else:
                change_str = ""

            lines.append(f"{rank_sym} {name} — *{wins}* Wins{crown}{change_str}{you}")

    lines.append(f"\n{separator}")

    # User's own stats
    if user_rank:
        lines.append(f"📍 Your Rank: *#{user_rank}* — {user_wins} Wins")
        if user_rank == 1:
            lines.append("👑 You are #1!")
        else:
            # Gap to next rank: find wins of rank above
            above_wins = None
            for doc in rows:
                dw = _wins_for_view(doc, view, chat_id)
                if dw > user_wins:
                    above_wins = dw
            if above_wins is not None:
                gap = above_wins - user_wins
                lines.append(f"⬆️ *{gap}* wins to reach *#{user_rank - 1}*")
    else:
        lines.append("📍 Your Rank: *Unranked*")

    # Reset timer (daily/weekly only)
    if view == "daily":
        lines.append(f"⏳ Resets In: *{_reset_timer(_next_midnight_utc())}*")
    elif view == "weekly":
        lines.append(f"⏳ Resets In: *{_reset_timer(_next_monday_utc())}*")

    lines.append(f"🕒 Updated: {_time_ago(last_updated_ts)}")

    return "\n".join(lines)


def _build_keyboard(active: str) -> InlineKeyboardMarkup:
    def btn(label, cb, is_active):
        return InlineKeyboardButton(f"{label} ✅" if is_active else label, callback_data=cb)

    row1 = [
        btn("🏆 Overall", "lb_overall", active == "overall"),
        btn("📅 Daily",   "lb_daily",   active == "daily"),
        btn("📆 Weekly",  "lb_weekly",  active == "weekly"),
    ]
    row2 = [
        btn("🏠 This Chat", "lb_chat",    active == "chat"),
        btn("🏏 Cricket",   "lb_cricket", active == "cricket"),
        btn("⚽ FIFA",      "lb_fifa",    active == "fifa"),
    ]
    return InlineKeyboardMarkup([row1, row2])


# ── Main handler ───────────────────────────────────────────────────────────

async def _render_standings(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    view: str, edit: bool = False
):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Run reset check silently in background — ensures daily/weekly
    # counters are correct even if user hasn't played a match today.
    try:
        await _check_and_apply_resets(user_id)
    except Exception as e:
        logger.warning(f"Reset check failed: {e}")

    ck = _cache_key(view, chat_id if view == "chat" else None)
    cached = _get_cached(ck)

    if cached:
        rows, last_ts = cached
    else:
        rows = await _fetch_leaderboard(view, chat_id)
        last_ts = time.time()
        _set_cache(ck, (rows, last_ts))

    user_rank, user_wins = await _get_user_rank(
        user_id, view, chat_id if view == "chat" else None
    )

    text = _build_text(view, rows, user_id, user_rank, user_wins, chat_id, last_ts)
    keyboard = _build_keyboard(view)

    if edit:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"Standings edit error: {e}")
    else:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )


async def handle_standings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/standings command — shows Overall leaderboard."""
    await _render_standings(update, context, view="overall", edit=False)


async def handle_standings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles [Overall] [Daily] [Weekly] [This Chat] [Cricket] [FIFA] button clicks."""
    query = update.callback_query
    user_id = query.from_user.id
    now = time.time()

    # Anti-spam cooldown
    if now - _user_cooldown.get(user_id, 0) < COOLDOWN_SECS:
        await query.answer("⏳ Please wait a moment.", show_alert=False)
        return
    _user_cooldown[user_id] = now

    await query.answer()

    view_map = {
        "lb_overall": "overall",
        "lb_daily":   "daily",
        "lb_weekly":  "weekly",
        "lb_chat":    "chat",
        "lb_cricket": "cricket",
        "lb_fifa":    "fifa",
    }
    view = view_map.get(query.data)
    if not view:
        return

    # Same-tab guard
    current_text = query.message.text or ""
    tab_headers = {
        "overall": "GLOBAL STANDINGS",
        "daily":   "DAILY STANDINGS",
        "weekly":  "WEEKLY STANDINGS",
        "cricket": "CRICKET STANDINGS",
        "fifa":    "FIFA STANDINGS",
        "chat":    "THIS CHAT STANDINGS",
    }
    if tab_headers.get(view, "") in current_text:
        await query.answer("Already viewing this tab.", show_alert=False)
        return

    await _render_standings(update, context, view=view, edit=True)
