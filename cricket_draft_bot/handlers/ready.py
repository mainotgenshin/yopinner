# handlers/ready.py
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import BadRequest
import logging
from game.state import load_match_state, save_match_state
from game.simulation import run_simulation
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)

async def handle_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    match_id = "_".join(data.split('_')[1:])
    
    async def safe_answer(text, alert=True):
        try:
             await query.answer(text, show_alert=alert)
        except:
             pass

    match = load_match_state(match_id)
    if not match:
        await safe_answer("Match expired.", alert=True)
        return
        
    user_id = query.from_user.id
    
    # Check concurrency state
    if match.state in ["SIMULATING", "COMPLETED"]:
        await safe_answer("Simulation is already running or complete!", alert=True)
        return
        
    # Mark user as ready
    if user_id == match.team_a.owner_id:
        match.team_a.is_ready = True
        await safe_answer("You are ready!", alert=False)
    elif user_id == match.team_b.owner_id:
        match.team_b.is_ready = True
        await safe_answer("You are ready!", alert=False)
    else:
        await safe_answer("You are not part of this match.", alert=True)
        return
        
    save_match_state(match)
    
    # Check if both ready
    if match.team_a.is_ready and match.team_b.is_ready:
        # Prevent double-entry here
        match.state = "SIMULATING"
        save_match_state(match)
        
        # Update text to "Simulating..."
        try:
            if query.message.photo:
                await query.message.edit_caption("⏳ **All Ready! Running Simulation...**", parse_mode="Markdown")
            else:
                await query.message.edit_text("⏳ **All Ready! Running Simulation...**", parse_mode="Markdown")
        except BadRequest as e:
            if "not modified" not in str(e):
                logger.error(f"Ready Handler Error: {e}")
            pass 
        
        # Run Simulation
        result_text = run_simulation(match)
        
        match.state = "FINISHED"
        save_match_state(match)
        
        # Send Result
        # Send Result with Retry
        from telegram.error import RetryAfter
        import asyncio
        
        for attempt in range(3):
            try:
                await context.bot.send_message(
                    chat_id=match.chat_id,
                    text=result_text,
                    parse_mode="Markdown"
                )
                break
            except RetryAfter as e:
                wait_time = e.retry_after + 1
                logger.warning(f"Flood limit exceeded in Ready Handler. Sleeping {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            except Exception as e:
                 logger.error(f"Failed to send simulation result: {e}")
                 # Try without markdown
                 try:
                     await context.bot.send_message(chat_id=match.chat_id, text=result_text)
                 except: pass # Give up
                 break
    else:
        # Update message to show who is ready
        a_status = "✅" if match.team_a.is_ready else "⏳"
        b_status = "✅" if match.team_b.is_ready else "⏳"
        
        text = f"✅ *Draft Complete!*\n\n{esc(match.team_a.owner_name)}: {a_status}\n{esc(match.team_b.owner_name)}: {b_status}\n\nWaiting for both..."
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
