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
from telegram.helpers import escape_markdown

def esc(t): return escape_markdown(str(t), version=1)

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

    if view == "ranked":
        cursor = db.users.find({}, {
            "user_id": 1, "name": 1, "ranked_rp": 1, "peak_rp": 1,
            f"prev_rank_{view}": 1, "_id": 0
        }).sort([("ranked_rp", -1)]).limit(10)
        return [doc async for doc in cursor]

    sort_field = {
        "overall": "wins",
        "daily": "daily_wins",
        "weekly": "weekly_wins",
        "cricket": "cricket_wins",
        "fifa": "fifa_wins",
        "wwe": "wwe_wins",
        "pkl": "pkl_wins",
        "chat": f"chat_wins.{chat_id}",
    }.get(view, "wins")

    projection = {
        "user_id": 1, "name": 1,
        "wins": 1, "daily_wins": 1, "weekly_wins": 1,
        "cricket_wins": 1, "fifa_wins": 1, "wwe_wins": 1, "pkl_wins": 1, "chat_wins": 1,
        "first_win_at": 1,
        f"prev_rank_{view}": 1,
        "_id": 0
    }

    query = {}
    now_ts = time.time()
    if view == "daily":
        # Only users whose daily period is still active (reset_at in the future)
        query = {"daily_reset_at": {"$gt": now_ts}}
    elif view == "weekly":
        query = {"weekly_reset_at": {"$gt": now_ts}}
    elif view == "chat" and chat_id:
        query = {f"chat_wins.{chat_id}": {"$gt": 0}}

    cursor = db.users.find(query, projection).sort(
        [(sort_field, -1), ("first_win_at", 1)]
    ).limit(10)

    return [doc async for doc in cursor]


async def _get_user_rank(user_id: int, view: str, chat_id: int | None = None) -> tuple:
    """Returns (rank, wins_or_rp_for_view) for a specific user. Lightweight count query."""
    from database import get_db
    db = get_db()

    if view == "ranked":
        user_doc = await db.users.find_one({"user_id": user_id}, {"ranked_rp": 1, "_id": 0})
        user_rp = user_doc.get("ranked_rp", 0) if user_doc else 0
        if user_rp <= 0:
            return (None, 0)
        rank = await db.users.count_documents({"ranked_rp": {"$gt": user_rp}}) + 1
        return (rank, user_rp)

    sort_field = {
        "overall": "wins",
        "daily": "daily_wins",
        "weekly": "weekly_wins",
        "cricket": "cricket_wins",
        "fifa": "fifa_wins",
        "wwe": "wwe_wins",
        "pkl": "pkl_wins",
        "chat": f"chat_wins.{chat_id}",
    }.get(view, "wins")

    user_doc = await db.users.find_one(
        {"user_id": user_id},
        {"wins": 1, "daily_wins": 1, "weekly_wins": 1,
         "cricket_wins": 1, "fifa_wins": 1, "wwe_wins": 1, "pkl_wins": 1, "chat_wins": 1, "_id": 0}
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
        "fifa":    "fifa_wins",
        "wwe":     "wwe_wins",
        "pkl":     "pkl_wins",
    }.get(view, "wins"), 0)


def _build_text(
    view: str, rows: list, user_id: int,
    user_rank: int | None, user_metric: int,
    chat_id: int | None, last_updated_ts: float | None,
    season_info: dict | None = None,
    gap_info: int | None = None
) -> str:
    if view == "ranked":
        from database import get_rank_tier
        s_num = season_info.get("season_number", 1) if season_info else 1
        is_off = season_info.get("is_off_season", False) if season_info else False
        secs_left = season_info.get("time_remaining", 0) if season_info else 0
        days = int(secs_left // 86400)
        hours = int((secs_left % 86400) // 3600)
        mins = int((secs_left % 3600) // 60)

        title = f"⏸️ *RANKED OFF-SEASON STANDINGS*" if is_off else f"🎖️ *RANKED SEASON {s_num} STANDINGS*"
        lines = [f"{title}\n"]

        if not rows:
            lines.append("No ranked players yet 👀\nPlay matches to earn RP and climb tiers!")
        else:
            for i, doc in enumerate(rows, 1):
                rp = doc.get("ranked_rp", 0)
                tier_name, tier_emoji, _, _, _ = get_rank_tier(rp)
                raw_name = str(doc.get("name", "Player")).replace("[", "(").replace("]", ")")
                name = esc(raw_name)
                uid = doc.get("user_id")
                is_you = uid == user_id
                name_link = f"[{name}](tg://user?id={uid})" if uid else name
                rank_sym = _rank_emoji(i)
                crown = " 👑" if i == 1 else ""
                you = " 👈 *YOU*" if is_you else ""

                change = doc.get("_rank_change", 0)
                if change > 0:
                    change_str = f" ⬆️ +{change}"
                elif change < 0:
                    change_str = f" ⬇️ {change}"
                else:
                    change_str = ""

                lines.append(f"{rank_sym} {name_link} — *{rp:,} RP* ({tier_emoji} {tier_name}){crown}{change_str}{you}")

        lines.append(f"\n━━━━━━━━━━━━━━━")
        u_tier_name, u_tier_emoji, _, _, _ = get_rank_tier(user_metric)
        if user_rank:
            lines.append(f"📍 Your Rank: *#{user_rank}* ({user_metric:,} RP — {u_tier_emoji} {u_tier_name})")
            if user_rank == 1:
                lines.append("👑 You are #1!")
            elif gap_info is not None:
                lines.append(f"⬆️ *{gap_info:,} RP* to reach *#{user_rank - 1}*")
        else:
            lines.append(f"📍 Your Rank: *Unranked* ({user_metric:,} RP — {u_tier_emoji} {u_tier_name})")

        if is_off:
            lines.append(f"\n⏸️ *Off-Season*: Season {s_num + 1} Starts In: *{days}d {hours}h {mins}m*")
        else:
            lines.append(f"\n⏳ Season {s_num} Ends In: *{days}d {hours}h {mins}m*")

        lines.append(f"🕒 Updated: {_time_ago(last_updated_ts)}")
        return "\n".join(lines)

    labels = {
        "overall": "🏆 GLOBAL STANDINGS",
        "daily":   "📅 DAILY STANDINGS",
        "weekly":  "📆 WEEKLY STANDINGS",
        "cricket": "🏏 CRICKET STANDINGS",
        "fifa":    "⚽ FIFA STANDINGS",
        "wwe":     "🤼 WWE STANDINGS",
        "pkl":     "🤸 PKL STANDINGS",
        "chat":    "🏠 THIS CHAT STANDINGS",
    }
    separator = "━━━━━━━━━━━━━━━"

    lines = [f"*{labels.get(view, 'STANDINGS')}*\n"]

    if not rows:
        lines.append("No standings yet 👀\nStart playing to claim the top spot!")
    else:
        for i, doc in enumerate(rows, 1):
            wins = _wins_for_view(doc, view, chat_id)
            raw_name = str(doc.get("name", "Player")).replace("[", "(").replace("]", ")")
            name = esc(raw_name)
            uid  = doc.get("user_id")
            is_you = uid == user_id

            # Clickable profile link — works even without @username
            if uid:
                name_link = f"[{name}](tg://user?id={uid})"
            else:
                name_link = name

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

            lines.append(f"{rank_sym} {name_link} — *{wins}* Wins{crown}{change_str}{you}")

    lines.append(f"\n{separator}")

    # User's own stats
    if user_rank:
        lines.append(f"📍 Your Rank: *#{user_rank}* — {user_metric} Wins")
        if user_rank == 1:
            lines.append("👑 You are #1!")
        else:
            # Gap to next rank: find wins of rank above
            above_wins = None
            for doc in rows:
                dw = _wins_for_view(doc, view, chat_id)
                if dw > user_metric:
                    above_wins = dw
            if above_wins is not None:
                gap = above_wins - user_metric
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


def _build_keyboard(active: str, owner_id: int, is_group: bool = True) -> InlineKeyboardMarkup:
    def btn(label, cb, is_active):
        return InlineKeyboardButton(f"{label} ✅" if is_active else label, callback_data=cb)

    row1 = [
        btn("🏆 Overall", f"lb_overall|{owner_id}", active == "overall"),
        btn("🎖️ Ranked",  f"lb_ranked|{owner_id}",  active == "ranked"),
        btn("📅 Daily",   f"lb_daily|{owner_id}",   active == "daily"),
        btn("📆 Weekly",  f"lb_weekly|{owner_id}",  active == "weekly"),
    ]
    row2 = [
        btn("🏏 Cricket", f"lb_cricket|{owner_id}", active == "cricket"),
        btn("⚽ FIFA",    f"lb_fifa|{owner_id}",    active == "fifa"),
        btn("🤼 WWE",     f"lb_wwe|{owner_id}",     active == "wwe"),
        btn("🤸 PKL",     f"lb_pkl|{owner_id}",     active == "pkl"),
    ]
    rows = [row1, row2]
    # Only show "This Chat" button in groups (not DMs)
    if is_group:
        rows.append([btn("🏠 This Chat", f"lb_chat|{owner_id}", active == "chat")])
    return InlineKeyboardMarkup(rows)


# ── Main handler ───────────────────────────────────────────────────────────

async def _render_standings(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    view: str, owner_id: int | None = None, edit: bool = False
):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if owner_id is None:
        owner_id = user_id

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

    rank_data = await _get_user_rank(user_id, view, chat_id if view == "chat" else None)
    user_rank, user_metric = rank_data

    # Track rank change
    if user_rank:
        delta = await _get_and_update_rank_change(user_id, view, user_rank)
        # Inject into user's row if present in top 10
        for r in rows:
            if r.get("user_id") == user_id:
                r["_rank_change"] = delta

    season_info = None
    gap_info = None
    if view == "ranked":
        from database import get_season_info, get_db
        season_info = await get_season_info()
        if user_rank and user_rank > 1:
            if user_rank <= len(rows) + 1 and user_rank - 2 < len(rows):
                above_rp = rows[user_rank - 2].get("ranked_rp", 0)
                gap_info = max(0, above_rp - user_metric)
            else:
                db = get_db()
                above_doc = await db.users.find({"ranked_rp": {"$gt": user_metric}}).sort([("ranked_rp", 1)]).limit(1).to_list(length=1)
                if above_doc:
                    gap_info = max(0, above_doc[0].get("ranked_rp", 0) - user_metric)

    is_group = update.effective_chat.type != "private"
    text = _build_text(
        view, rows, user_id, user_rank, user_metric, chat_id, last_ts,
        season_info=season_info, gap_info=gap_info
    )
    kb = _build_keyboard(active=view, owner_id=owner_id, is_group=is_group)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=kb, parse_mode="Markdown"
            )
        except Exception:
            pass  # Message unchanged or expired
    else:
        await update.effective_message.reply_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )


async def handle_standings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /standings command."""
    user_id = update.effective_user.id
    await _render_standings(update, context, view="overall", owner_id=user_id, edit=False)


async def handle_standings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles [Overall] [Ranked] [Daily] [Weekly] [This Chat] [Cricket] [FIFA] [WWE] [PKL] button clicks."""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data or ""
    parts = data.split("|")
    action = parts[0]
    owner_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    # Check button ownership: only the user who typed /standings can interact
    if owner_id and user_id != owner_id:
        try:
            await query.answer("⛔ Only the player who opened /standings can use these buttons.", show_alert=True)
        except Exception:
            pass
        return

    now = time.time()
    # Anti-spam cooldown
    if now - _user_cooldown.get(user_id, 0) < COOLDOWN_SECS:
        try:
            await query.answer("⏳ Please wait a moment.", show_alert=False)
        except Exception:
            pass
        return
    _user_cooldown[user_id] = now

    try:
        await query.answer()
    except Exception:
        pass  # Stale query after restart — ignore silently

    view_map = {
        "lb_overall": "overall",
        "lb_ranked":  "ranked",
        "lb_daily":   "daily",
        "lb_weekly":  "weekly",
        "lb_chat":    "chat",
        "lb_cricket": "cricket",
        "lb_fifa":    "fifa",
        "lb_wwe":     "wwe",
        "lb_pkl":     "pkl",
    }
    view = view_map.get(action)
    if not view:
        return

    # Same-tab guard
    current_text = query.message.text or ""
    tab_headers = {
        "overall": "GLOBAL STANDINGS",
        "ranked":  "RANKED",
        "daily":   "DAILY STANDINGS",
        "weekly":  "WEEKLY STANDINGS",
        "cricket": "CRICKET STANDINGS",
        "fifa":    "FIFA STANDINGS",
        "wwe":     "WWE STANDINGS",
        "pkl":     "PKL STANDINGS",
        "chat":    "THIS CHAT STANDINGS",
    }
    if tab_headers.get(view, "") in current_text:
        await query.answer("Already viewing this tab.", show_alert=False)
        return

    await _render_standings(update, context, view=view, owner_id=owner_id, edit=True)
