# handlers/checkin.py
"""
Daily check-in system: /checkin
- 50 coins every day
- Streak tracked day-by-day (UTC)
- Every 7th consecutive day: 50 coins + random card (60% common, 30% rare, 10% epic)
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

RARITY_EMOJI = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legend": "🟡"}
FORMAT_LABEL  = {"ipl": "IPL", "odi": "ODI", "test": "Test",
                 "pkl": "PKL", "wwe": "WWE", "fifa": "FIFA"}

def esc(t):
    return escape_markdown(str(t), version=1)


async def handle_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id
    msg     = update.effective_message

    from database import do_checkin, get_checkin_status

    result = await do_checkin(user_id)

    # Already checked in today
    if not result["success"]:
        status = await get_checkin_status(user_id)
        streak = status["checkin_streak"]
        days_to_milestone = 7 - (streak % 7)
        await msg.reply_text(
            f"✅ *Already checked in today!*\n\n"
            f"🔥 Current Streak: *{streak} day{'s' if streak != 1 else ''}*\n"
            f"🎁 Next reward in: *{days_to_milestone} day{'s' if days_to_milestone != 1 else ''}*\n\n"
            f"_Come back tomorrow to keep your streak going!_",
            parse_mode="Markdown"
        )
        return

    streak     = result["new_streak"]
    coins      = result["coins_awarded"]
    is_milestone = result["milestone"]
    card       = result["card_awarded"]

    # Build streak progress bar (7 dots: filled = checked, empty = remaining)
    pos_in_week = streak % 7 or 7
    bar = "🟩" * pos_in_week + "⬜" * (7 - pos_in_week)
    days_to_next = 7 - pos_in_week if pos_in_week < 7 else 0

    lines = [
        f"☀️ *Daily Check-In!*",
        f"",
        f"👤 {esc(user.first_name)}",
        f"🔥 Streak: *{streak} day{'s' if streak != 1 else ''}*",
        f"",
        f"📅 Weekly Progress:",
        f"{bar}",
        f"",
        f"💰 Coins earned: *+{coins}🪙*",
    ]

    if is_milestone:
        # 7-day milestone!
        if card:
            r_emoji = RARITY_EMOJI.get(card["rarity"], "⚪")
            fmt_label = FORMAT_LABEL.get(card["format"], card["format"].upper())
            lines += [
                f"",
                f"🎉 *7-Day Milestone Reward!*",
                f"{r_emoji} *{esc(card['name'])}* ({fmt_label})",
                f"   Rarity: {card['rarity'].title()} | OVR: {card['ovr']}",
                f"   _Added to your /mycards_",
            ]
        else:
            lines += [
                f"",
                f"🎉 *7-Day Milestone!*",
                f"_Card reward will appear in /mycards shortly._",
            ]
        lines += [f"", f"🔄 Streak continues — keep it up!"]
    else:
        if days_to_next > 0:
            lines += [f"", f"🎁 Next card reward in *{days_to_next} day{'s' if days_to_next != 1 else ''}*"]

    await msg.reply_text("\n".join(lines), parse_mode="Markdown")
