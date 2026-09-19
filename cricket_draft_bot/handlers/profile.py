import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from database import get_db
from telegram.helpers import escape_markdown
import html

_PROFILE_CLICK_TIMES: dict = {}

def _check_profile_click(user_id: int, debounce_secs: float = 1.0) -> bool:
    now = time.time()
    if now - _PROFILE_CLICK_TIMES.get(user_id, 0) < debounce_secs:
        return False
    _PROFILE_CLICK_TIMES[user_id] = now
    if len(_PROFILE_CLICK_TIMES) > 200:
        cutoff = now - 60
        for k in list(_PROFILE_CLICK_TIMES.keys()):
            if _PROFILE_CLICK_TIMES[k] < cutoff:
                _PROFILE_CLICK_TIMES.pop(k, None)
    return True

def esc(t):
    return escape_markdown(str(t), version=1)

def _profile_kb(user_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard for profile — Achievements button."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏅 Achievements", callback_data=f"profile_ach|{user_id}")
    ]])

async def _build_profile_data(user_id: int, name: str) -> dict:
    from database import get_user_stats
    stats = await get_user_stats(user_id)

    if not stats:
        stats = {
            "total_matches": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "recent_results": []
        }

    wins = stats.get('wins', 0)
    losses = stats.get('losses', 0)
    draws = stats.get('draws', 0)
    total_matches = stats.get('total_matches', 0)
    current_streak = stats.get('current_streak', 0)
    best_streak = stats.get('best_streak', 0)
    joined_at = stats.get('joined_at')

    recent = list(stats.get('recent_results', []))
    recent.reverse()
    last_5 = recent[:5]

    score_icons = {"W": "🟢", "L": "🔴", "D": "⚪"}
    recent_str = " | ".join([score_icons[r] for r in last_5]) if last_5 else "No matches yet"

    win_rate = 0.0
    if total_matches > 0:
        win_rate = (wins / total_matches) * 100

    # Format join date
    if joined_at:
        try:
            from datetime import datetime, timezone
            if isinstance(joined_at, (int, float)):
                dt = datetime.fromtimestamp(joined_at, tz=timezone.utc)
            else:
                dt = joined_at
            joined_str = dt.strftime("%d %b %Y")
        except Exception:
            joined_str = "—"
    else:
        joined_str = "—"

    # Streak emoji
    streak_emoji = "🔥" if current_streak >= 3 else "⚡" if current_streak >= 1 else "💤"

    # Get global rank
    rank = None
    try:
        from handlers.standings import _get_user_rank
        rank, _ = await _get_user_rank(user_id, "overall")
    except Exception:
        pass

    name_html = html.escape(name or "Player")
    rank_line_html = f"🏆 Global Rank: <b>#{rank}</b>\n" if rank else ""

    # Clean HTML version
    body_html = (
        "━━━━━━━━━━━━━━━━━━\n"
        f"    👤 <b>{name_html}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{rank_line_html}"
        f"📅 Joined: <code>{joined_str}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔘 matches : <code>{total_matches}</code>\n"
        f"🟢 wins    : <code>{wins}</code>\n"
        f"🔴 losses  : <code>{losses}</code>\n"
        f"⚪ draws   : <code>{draws}</code>\n"
        f"📊 win %   : <code>{win_rate:.1f}%</code>\n\n"
        f"{streak_emoji} <b>Win Streak</b>\n"
        f"Current: <code>{current_streak}</code> | Best: <code>{best_streak}</code>\n\n"
        "📈 <b>Recent Matches</b>\n"
        f"{recent_str}\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    image_url = None
    image_fid = None
    href_text = None
    full_caption = None

    # Show favorite card image if available
    try:
        from database import get_fav_card, get_player, _get_card_image, _get_card_image_url
        fav = await get_fav_card(user_id)
        if fav:
            player_doc = await get_player(fav["player_id"])
            if player_doc:
                fmt = fav["format"]
                image_url = _get_card_image_url(player_doc, fmt)
                image_fid = _get_card_image(player_doc, fmt)

                card_data = player_doc.get("cards", {}).get(fmt, {})
                fmt_labels = {"ipl": "IPL", "odi": "ODI", "test": "Test", "wwe": "WWE", "fifa": "FIFA", "pkl": "PKL"}
                RARITY_EMOJI_MAP = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legend": "🟡"}
                rarity = card_data.get("rarity", "")
                ovr    = card_data.get("ovr", "")
                p_name_safe = html.escape(player_doc.get("name", "Unknown"))
                fav_line_html = f"\n\n⭐ <b>Fav Card:</b> {p_name_safe} ({fmt_labels.get(fmt, fmt.upper())}) {RARITY_EMOJI_MAP.get(rarity, '')} OVR {ovr}"

                if image_url:
                    href_text = f'<a href="{image_url}">&#8205;</a>' + body_html + fav_line_html
                full_caption = body_html + fav_line_html
    except Exception:
        pass

    return {
        "body_html": body_html,
        "href_text": href_text,
        "full_caption": full_caption or body_html,
        "image_url": image_url,
        "image_fid": image_fid,
    }


async def handle_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    user_id = user.id
    if not _check_profile_click(user_id, 2.0):
        return
    name = user.first_name

    data = await _build_profile_data(user_id, name)
    kb = _profile_kb(user_id)

    if data["image_url"]:
        try:
            await update.effective_message.reply_text(
                data["href_text"],
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=kb
            )
            return
        except Exception:
            pass

    if data["image_fid"]:
        try:
            await update.effective_message.reply_photo(
                photo=data["image_fid"],
                caption=data["full_caption"],
                parse_mode="HTML",
                reply_markup=kb
            )
            return
        except Exception:
            pass

    try:
        await update.effective_message.reply_text(data["body_html"], parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=data["body_html"],
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception:
            pass


async def cb_profile_achievements(update, context):
    """Callback: show achievements for the user who owns the profile."""
    query = update.callback_query
    _, owner_id_str = query.data.split("|")
    viewer_id = query.from_user.id
    owner_id  = int(owner_id_str)

    if viewer_id != owner_id:
        await query.answer("⛔ You can only view your own achievements.", show_alert=True)
        return

    if not _check_profile_click(viewer_id, 1.0):
        await query.answer()
        return

    await query.answer()
    from database import get_achievements
    achievements = await get_achievements(owner_id)

    if not achievements:
        text = "🏅 <b>Your Achievements</b>\n━━━━━━━━━━━━━━━━━━\n<i>No achievements yet. Keep playing!</i>\n━━━━━━━━━━━━━━━━━━"
    else:
        lines = ["🏅 <b>Your Achievements</b>", "━━━━━━━━━━━━━━━━━━"]
        for i, ach in enumerate(achievements, 1):
            lines.append(f"<code>{i}.</code> {html.escape(str(ach))}")
        lines.append("━━━━━━━━━━━━━━━━━━")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Back", callback_data=f"profile_back|{owner_id}")
    ]])

    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


async def cb_profile_back(update, context):
    """Callback: go back to main profile view."""
    query = update.callback_query
    _, owner_id_str = query.data.split("|")
    viewer_id = query.from_user.id
    owner_id  = int(owner_id_str)

    if viewer_id != owner_id:
        await query.answer("⛔ Not your profile.", show_alert=True)
        return

    if not _check_profile_click(viewer_id, 1.0):
        await query.answer()
        return

    await query.answer()
    name = query.from_user.first_name
    data = await _build_profile_data(owner_id, name)
    kb = _profile_kb(owner_id)

    try:
        if query.message.photo:
            caption = data["full_caption"] if data["full_caption"] else data["body_html"]
            await query.edit_message_caption(caption=caption, parse_mode="HTML", reply_markup=kb)
        else:
            text_to_show = data["href_text"] if data["href_text"] else data["body_html"]
            await query.edit_message_text(
                text=text_to_show,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=kb
            )
    except Exception:
        pass
