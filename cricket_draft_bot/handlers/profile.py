from telegram import Update
from telegram.ext import ContextTypes
from database import get_db
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)

async def handle_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    name = user.first_name

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
            import datetime
            if isinstance(joined_at, (int, float)):
                dt = datetime.datetime.utcfromtimestamp(joined_at)
            else:
                dt = joined_at
            joined_str = dt.strftime("%d %b %Y")
        except Exception:
            joined_str = "—"
    else:
        joined_str = "—"

    # Streak emoji
    streak_emoji = "🔥" if current_streak >= 3 else "⚡" if current_streak >= 1 else "💤"

    # Get global rank (lightweight count query, reuses standings cache)
    try:
        from handlers.standings import _get_user_rank
        rank, _ = await _get_user_rank(user_id, "overall")
        rank_line = f"🏆 Global Rank: *#{rank}*\n"
    except Exception:
        rank_line = ""

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        f"    👤 *{esc(name)}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{rank_line}"
        f"📅 Joined: `{joined_str}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔘 matches : `{total_matches}`\n"
        f"🟢 wins    : `{wins}`\n"
        f"🔴 losses : `{losses}`\n"
        f"⚪ draws  : `{draws}`\n"
        f"📊 win %  : `{win_rate:.1f}%`\n\n"
        f"{streak_emoji} *Win Streak*\n"
        f"Current: `{current_streak}` | Best: `{best_streak}`\n\n"
        "📈 *Recent Matches*\n"
        f"{recent_str}\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    # Show favorite card image if available
    try:
        from database import get_fav_card, get_user_cards, get_player, _get_card_image
        fav = await get_fav_card(user_id)
        if fav:
            # Get the card image (uses centralized priority: real photo first, card url fallback)
            player_doc = await get_player(fav["player_id"])
            if player_doc:
                fmt = fav["format"]
                image = _get_card_image(player_doc, fmt)

                
                if image:
                    card_data = player_doc.get("cards", {}).get(fmt, {})
                    fmt_labels = {"ipl": "IPL", "odi": "ODI", "test": "Test", "wwe": "WWE", "fifa": "FIFA"}
                    RARITY_EMOJI_MAP = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legend": "🟡"}
                    rarity = card_data.get("rarity", "")
                    ovr    = card_data.get("ovr", "")
                    fav_line = f"\n\n⭐ *Fav Card:* {player_doc['name']} ({fmt_labels.get(fmt, fmt)}) {RARITY_EMOJI_MAP.get(rarity, '')} OVR {ovr}"
                    full_caption = text + fav_line
                    try:
                        await update.effective_message.reply_photo(
                            photo=image,
                            caption=full_caption,
                            parse_mode="Markdown"
                        )
                        return
                    except Exception:
                        pass  # Fall through to text-only
    except Exception:
        pass  # Never break profile due to card system errors
    
    try:
        await update.effective_message.reply_text(text, parse_mode="Markdown")
    except Exception:
        # Original message deleted (e.g. bot restarted) — send without reply
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception:
            pass


