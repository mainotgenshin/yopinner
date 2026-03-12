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
        # Fallback or empty
        stats = {
            "total_matches": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "recent_results": []
        }
        
    wins = stats.get('wins', 0)
    losses = stats.get('losses', 0)
    total_matches = stats.get('total_matches', 0)
    
    # recent_results are stored in chronological order (push),
    # so slice -5 gave us last 5.
    # We want to display them Newest -> Oldest (Left -> Right)?
    # Or Oldest -> Newest?
    # User's example: 🟢 | ⚪ | 🔴 | 🟢 | 🟢
    # Assuming Right = Most Recent based on list append.
    # But usually UI shows Recents: [Latest] [Prev]...
    # Let's Reverse for display: [Recent] ... [Old]
    
    recent = list(stats.get('recent_results', []))
    recent.reverse()
    last_5 = recent[:5]
    
    score_icons = {
        "W": "🟢", # Green Circle
        "L": "🔴", # Red Circle
        "D": "⚪"  # White Circle
    }
    
    recent_str = " | ".join([score_icons[r] for r in last_5]) if last_5 else "No matches yet"
    
    win_rate = 0.0
    if total_matches > 0:
        win_rate = (wins / total_matches) * 100
        
    # Format Response
    # ━━━━━━━━━━━━━━━━━━
    #     👤 Shyam
    # ━━━━━━━━━━━━━━━━━━
    # 🏏 Matches : 48
    # ✅ Wins    : 31
    # ❌ Losses : 15
    # 📊 Win %  : 64.5%
    #
    # 📈 Recent Matches
    # 🟢 | ⚪ | 🔴 | 🟢 | 🟢
    # ━━━━━━━━━━━━━━━━━━
    
    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        f"    👤 *{esc(name)}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏏 matches : `{total_matches}`\n"
        f"✅ wins    : `{wins}`\n"
        f"❌ losses : `{losses}`\n" # Draws ignored in visual summary but part of total
        f"📊 win %  : `{win_rate:.1f}%`\n\n"
        "📈 *Recent Matches*\n"
        f"{recent_str}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    
    await update.effective_message.reply_text(text, parse_mode="Markdown")
