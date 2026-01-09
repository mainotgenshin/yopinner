# handlers/ready.py
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import BadRequest
import logging
from game.state import load_match_state, save_match_state
from game.simulation import run_simulation

logger = logging.getLogger(__name__)

async def handle_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    match_id = "_".join(data.split('_')[1:])
    
    match = load_match_state(match_id)
    if not match:
        await query.answer("Match expired.")
        return
        
    user_id = query.from_user.id
    
    # Mark user as ready
    if user_id == match.team_a.owner_id:
        match.team_a.is_ready = True
        await query.answer("You are ready!")
    elif user_id == match.team_b.owner_id:
        match.team_b.is_ready = True
        await query.answer("You are ready!")
    else:
        await query.answer("You are not part of this match.")
        return
        
    save_match_state(match)
    
    # Check if both ready
    if match.team_a.is_ready and match.team_b.is_ready:
        # Update text to "Simulating..."
        # Update text to "Simulating..."
        # It's a photo message OR text message now
        try:
            if query.message.photo:
                await query.message.edit_caption("⏳ **All Ready! Running Simulation...**", parse_mode="Markdown")
            else:
                await query.message.edit_text("⏳ **All Ready! Running Simulation...**", parse_mode="Markdown")
        except BadRequest as e:
            if "not modified" not in str(e):
                logger.error(f"Ready Handler Error: {e}")
            pass # Ignore if already modified
        
        # Run Simulation
        result_text = run_simulation(match)
        
        # Send Result
        await context.bot.send_message(
            chat_id=match.chat_id,
            text=result_text,
            parse_mode="Markdown"
        )
        
        # Cleanup if needed (remove active match state from memory/db? kept for history usually)
    else:
        # Update message to show who is ready
        a_status = "✅" if match.team_a.is_ready else "⏳"
        b_status = "✅" if match.team_b.is_ready else "⏳"
        
        text = f"✅ **Draft Complete!**\n\n{match.team_a.owner_name}: {a_status}\n{match.team_b.owner_name}: {b_status}\n\nWaiting for both..."
        keyboard = [[InlineKeyboardButton("🚀 READY", callback_data=f"ready_{match.match_id}")]]
        
        # Avoid editing if same content
        if query.message.caption != text.replace('*', '') and query.message.text != text.replace('*', ''): 
             try:
                 if query.message.photo:
                    await query.message.edit_caption(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                 else:
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
             except:
                 pass
