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
    
    db = get_db()
    
    # Query all FINISHED matches where user is either Owner A or Owner B
    query = {
        "state_data.state": "FINISHED",
        "$or": [
            {"state_data.team_a.owner_id": user_id},
            {"state_data.team_b.owner_id": user_id}
        ]
    }
    
    # Sort by match_id (proxy for time if not using timestamps, assuming somewhat chronological)
    # Actually, Mongo _id is chronological, match_id contains chat_id so not strictly.
    # Ideally we'd have a timestamp. But for now let's just grab them.
    # We can try to sort by natural order (insertion).
    # Sort by _id (ascending time) to ensure chronological order
    matches = list(db.matches.find(query).sort("_id", 1))
    
    total_matches = len(matches)
    wins = 0
    losses = 0
    draws = 0
    
    recent_results = [] # List of "W", "L", "D"
    
    # Iterate and calculate
    for m in matches:
        data = m.get('state_data', {})
        team_a = data.get('team_a', {})
        team_b = data.get('team_b', {})
        
        # Identify user's team
        if team_a.get('owner_id') == user_id:
            my_score = team_a.get('score', 0)
            opp_score = team_b.get('score', 0)
        else:
            my_score = team_b.get('score', 0)
            opp_score = team_a.get('score', 0)
            
        # Determine Result
        res = "D"
        if my_score > opp_score:
            wins += 1
            res = "W"
        elif my_score < opp_score:
            losses += 1
            res = "L"
        else:
            draws += 1
            res = "D"
            
        recent_results.append(res)
        
    # Get last 5 (assuming list is chronological or reverse? simple append means chronological)
    # We want most recent. Let's take last 5 of list.
    last_5 = recent_results[-5:]
    # Ideally reverse for display? User asked: "Recent Matches: G | W | L..."
    # Usually strictly left-to-right means "Oldest -> Newest" or "Newest -> Oldest"?
    # The example design showed: 🟢 | ⚪ | 🔴 | 🟢 | 🟢
    # Let's assume Left = Recent? No, usually Right = Most Recent in charts.
    # But often Left = Recent in lists.
    # I'll output Newest -> Oldest (Left to Right)
    last_5.reverse()
    
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
    
    await update.message.reply_text(text, parse_mode="Markdown")
