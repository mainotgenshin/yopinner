# handlers/bbet.py
"""
/bbet head|tail <amount>  — Coin flip gambling command.
Rules:
  - Min bet: 1 coin | Max bet: 1,000 coins
  - 5 attempts per user per day (resets at midnight UTC)
  - Win: double the bet (net +amount). Lose: lose the bet.
"""
import random
import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes
from database import get_db

logger = logging.getLogger(__name__)

BBET_MAX   = 1000
BBET_MIN   = 1
BBET_DAILY = 5


async def _get_coins(user_id: int) -> int:
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"coins": 1})
    return int(doc.get("coins", 0)) if doc else 0


async def _get_bbet_state(user_id: int):
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"bbet_today_count": 1, "bbet_today_date": 1})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not doc:
        return 0, today
    saved_date = doc.get("bbet_today_date", "")
    if saved_date != today:
        return 0, today
    return int(doc.get("bbet_today_count", 0)), today


async def _record_bbet(user_id: int, delta: int, new_count: int, today: str) -> int:
    db = get_db()
    result = await db.users.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"coins": delta}, "$set": {"bbet_today_count": new_count, "bbet_today_date": today}},
        upsert=True,
        return_document=True
    )
    return int(result.get("coins", 0))


async def handle_bbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id
    args    = context.args

    if not args or len(args) < 2:
        await update.effective_message.reply_text(
            "🎲 *Coin Flip Bet*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Usage: /bbet head 100 or /bbet tail 500\n\n"
            f"• Min bet: {BBET_MIN}🪙  |  Max bet: {BBET_MAX}🪙\n"
            f"• Daily limit: {BBET_DAILY} bets per day\n"
            "• Win: earn 2x your bet\n"
            "• Lose: lose your bet",
            parse_mode="Markdown"
        )
        return

    choice_raw = args[0].lower().strip()
    if choice_raw not in ("head", "tail", "heads", "tails"):
        await update.effective_message.reply_text(
            "❌ Invalid choice. Use head or 	ail.\nExample: /bbet head 100",
            parse_mode="Markdown"
        )
        return

    choice = "head" if choice_raw in ("head", "heads") else "tail"

    try:
        amount = int(args[1])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid amount. Example: /bbet head 100", parse_mode="Markdown")
        return

    if amount < BBET_MIN or amount > BBET_MAX:
        await update.effective_message.reply_text(
            f"❌ Bet must be between {BBET_MIN}🪙 and {BBET_MAX}🪙.", parse_mode="Markdown"
        )
        return

    count_today, today = await _get_bbet_state(user_id)
    if count_today >= BBET_DAILY:
        await update.effective_message.reply_text(
            f"⛔ You have used all *{BBET_DAILY} bets* for today!\nCome back tomorrow 🌙",
            parse_mode="Markdown"
        )
        return

    balance = await _get_coins(user_id)
    if balance < amount:
        await update.effective_message.reply_text(
            f"❌ *Not enough coins!*\nYou need {amount}🪙 but only have {balance}🪙.",
            parse_mode="Markdown"
        )
        return

    result   = random.choice(["head", "tail"])
    won      = (result == choice)
    delta    = +amount if won else -amount
    new_count = count_today + 1
    new_bal  = await _record_bbet(user_id, delta, new_count, today)

    remaining   = BBET_DAILY - new_count
    result_line = "🟢 You won!" if won else "🔴 You lost!"
    coin_line   = f"+{amount}🪙 🎉" if won else f"-{amount}🪙 💸"
    flip_icon   = "⬆️ Heads" if result == "head" else "⬇️ Tails"

    msg = (
        f"🎲 *Coin Flip Result*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"You chose: *{'Heads' if choice == 'head' else 'Tails'}*\n"
        f"Result: *{flip_icon}*\n\n"
        f"{result_line} {coin_line}\n\n"
        f"💰 Balance: {new_bal}🪙\n"
        f"🎯 Remaining bets today: {remaining}/{BBET_DAILY}"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")
