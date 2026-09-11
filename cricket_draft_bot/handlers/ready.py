# handlers/ready.py
import asyncio
import logging
import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import BadRequest, RetryAfter
from telegram.helpers import escape_markdown
from game.state import load_match_state, save_match_state
from game.simulation import run_simulation

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)

# Per-match lock prevents race condition when both players click READY concurrently
_READY_LOCKS: dict[str, asyncio.Lock] = {}

def _get_ready_lock(match_id: str) -> asyncio.Lock:
    if match_id not in _READY_LOCKS:
        _READY_LOCKS[match_id] = asyncio.Lock()
    return _READY_LOCKS[match_id]


async def handle_ready(update: Update, context: ContextTypes.DEFAULT_TYPE, match_id_override=None):
    query = update.callback_query

    if match_id_override:
        match_id = match_id_override
    else:
        data = query.data
        match_id = "_".join(data.split('_')[1:])

    async def safe_answer(text, alert=True):
        if query:
            try:
                await query.answer(text, show_alert=alert)
            except Exception:
                pass

    lock = _get_ready_lock(match_id)
    async with lock:
        match = await load_match_state(match_id)
        if not match:
            await safe_answer("Match expired.", alert=True)
            return

        user_id = query.from_user.id if query else None

        # Check concurrency state — includes FINISHED
        if match.state in ["SIMULATING", "COMPLETED", "FINISHED"]:
            await safe_answer("Simulation is already running or complete!", alert=True)
            return

        # Mark user as ready
        if user_id:
            if user_id == match.team_a.owner_id:
                match.team_a.is_ready = True
                await safe_answer("You are ready!", alert=False)
            elif user_id == match.team_b.owner_id:
                match.team_b.is_ready = True
                await safe_answer("You are ready!", alert=False)
            else:
                await safe_answer("You are not part of this match.", alert=True)
                return

        await save_match_state(match)

        # Check if both ready
        if match.team_a.is_ready and match.team_b.is_ready:
            # Prevent double-entry here
            match.state = "SIMULATING"
            await save_match_state(match)

            # Update text to "Simulating..."
            try:
                if query and query.message:
                    if query.message.photo:
                        await query.message.edit_caption("⏳ *All Ready! Running Simulation...*", parse_mode="Markdown")
                    else:
                        await query.message.edit_text("⏳ *All Ready! Running Simulation...*", parse_mode="Markdown")
            except BadRequest as e:
                if "not modified" not in str(e):
                    logger.warning(f"Ready handler status edit note: {e}")
            except Exception:
                pass  # Ignore flood/other errors during cosmetic update

            # Run Simulation with complete exception safety
            try:
                result_text = await run_simulation(match)
            except Exception as sim_err:
                logger.error(f"Simulation failed for match {match_id}: {sim_err}", exc_info=True)
                match.state = "READY_CHECK"
                match.team_a.is_ready = False
                match.team_b.is_ready = False
                await save_match_state(match)
                try:
                    await context.bot.send_message(
                        chat_id=match.chat_id,
                        text="⚠️ *An error occurred while simulating the match.* Both players please click 🚀 READY again.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                return

            match.state = "FINISHED"
            match.finished_at = time.time()
            await save_match_state(match)

            # Clean up lock for finished match
            _READY_LOCKS.pop(match_id, None)

            # Merge Result into Banner (Edit Caption or Text)
            edited = False
            try:
                if query and query.message:
                    if query.message.photo:
                        await query.message.edit_caption(
                            caption=result_text,
                            parse_mode="Markdown"
                        )
                    else:
                        await query.message.edit_text(
                            text=result_text,
                            parse_mode="Markdown"
                        )
                    edited = True
            except Exception as edit_err:
                logger.warning(f"Failed to edit simulation result with Markdown: {edit_err}")
                # Fallback: attempt edit without Markdown formatting to avoid parsing failures
                try:
                    if query and query.message:
                        if query.message.photo:
                            await query.message.edit_caption(caption=result_text, parse_mode=None)
                        else:
                            await query.message.edit_text(text=result_text, parse_mode=None)
                        edited = True
                except Exception as plain_err:
                    logger.error(f"Failed plain-text edit of simulation result: {plain_err}")

            if not edited:
                # Fallback to sending new message if edit fails
                try:
                    await context.bot.send_message(chat_id=match.chat_id, text=result_text, parse_mode="Markdown")
                except RetryAfter as retry_err:
                    wait = retry_err.retry_after + 1
                    logger.warning(f"Flood control on result send, retrying in {wait}s")
                    await asyncio.sleep(wait)
                    try:
                        await context.bot.send_message(chat_id=match.chat_id, text=result_text, parse_mode="Markdown")
                    except Exception:
                        try:
                            await context.bot.send_message(chat_id=match.chat_id, text=result_text, parse_mode=None)
                        except Exception as final_e:
                            logger.error(f"Final retry also failed sending result: {final_e}")
                except Exception as send_e:
                    logger.warning(f"Markdown send failed: {send_e}. Retrying as plain text...")
                    try:
                        await context.bot.send_message(chat_id=match.chat_id, text=result_text, parse_mode=None)
                    except Exception as fallback_e:
                        logger.error(f"Fallback send_message also failed: {fallback_e}")

            # Auto-unpin the draft board immediately after result (non-blocking)
            pinned_id = getattr(match, 'pinned_message_id', None)
            if pinned_id:
                async def _bg_unpin(bot, chat_id, msg_id):
                    try:
                        await bot.unpin_chat_message(chat_id=chat_id, message_id=msg_id)
                    except Exception:
                        pass
                asyncio.create_task(_bg_unpin(context.bot, match.chat_id, pinned_id))

        else:
            # Update message to show who is ready, keeping the full board visible
            import html as _html
            from handlers.draft import format_draft_board
            from utils.banners import get_banner_for_match

            a_status = "✅" if match.team_a.is_ready else "⏳"
            b_status = "✅" if match.team_b.is_ready else "⏳"
            board = format_draft_board(match, include_turn=False)
            name_a_safe = _html.escape(match.team_a.owner_name or "Player 1")
            name_b_safe = _html.escape(match.team_b.owner_name or "Player 2")

            status_line = "Waiting for both..." if not (match.team_a.is_ready or match.team_b.is_ready) else "Waiting for ready..."

            ready_text = (
                f"{board}\n\n"
                f"✅ <b>Draft Complete!</b>\n\n"
                f"{name_a_safe}: {a_status}\n"
                f"{name_b_safe}: {b_status}\n\n"
                f"{status_line}"
            )

            # Build Keyboard
            row1 = [InlineKeyboardButton("🚀 READY", callback_data=f"ready_{match.match_id}")]
            keyboard = [row1]

            # Add Swap button as a direct DM deep-link (each team gets 1 swap)
            a_swaps = getattr(match.team_a, 'swaps_used', 0)
            b_swaps = getattr(match.team_b, 'swaps_used', 0)
            if a_swaps < 1 or b_swaps < 1:
                bot_uname = context.bot.username
                swap_url = f"https://t.me/{bot_uname}?start=swap_{match.match_id}"
                keyboard.append([InlineKeyboardButton("🔀 Swap Positions (1 Left)", url=swap_url)])

            banner = await get_banner_for_match(match)
            media_is_url = bool(banner and str(banner).startswith("http"))
            href_text = f'<a href="{banner}">&#8205;</a>' + ready_text if media_is_url else ready_text

            try:
                if query and query.message:
                    if query.message.photo:
                        await query.message.edit_caption(ready_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
                    else:
                        await query.message.edit_text(href_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML", disable_web_page_preview=False)
            except Exception:
                pass

