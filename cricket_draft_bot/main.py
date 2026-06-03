# main.py
import logging
import time
import asyncio

# MONKEYPATCH: Fix for Windows Timezone issues with APScheduler
import pytz
import apscheduler.util

# Force UTC for everything
apscheduler.util.get_localzone = lambda: pytz.utc

# Bypass the "Only timezones from the pytz library are supported" check
# by replacing the validator function entirely
def fixed_astimezone(timezone):
    return pytz.utc

apscheduler.util.astimezone = fixed_astimezone

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, AIORateLimiter
from config import BOT_TOKEN
from database import init_db
from handlers.admin import add_player, map_api, remove_player, get_player_stats, reset_matches
from handlers.challenge import challenge_ipl, challenge_intl, challenge_fifa, challenge_wwe, handle_join
from handlers.draft import handle_draft_callback
from handlers.ready import handle_ready

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Suppress noisy httpx request logs (fires on every API call — hundreds/hour)
logging.getLogger("httpx").setLevel(logging.WARNING)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import save_chat
    chat = update.effective_chat

    # Save group chats for broadcast
    if chat.type != "private":
        await save_chat(chat.id)

    # Handle deep-link for Swap (DM only)
    if chat.type == "private" and context.args and context.args[0].startswith("swap_"):
        from handlers.swap import handle_swap_dm_start
        await handle_swap_dm_start(update, context)
        return

    if chat.type == "private":
        # DM — no group-specific features
        await update.effective_message.reply_text(
            "👋 *Hi! I'm Draft Bot.*\n\n"
            "Add me to a group to start playing!\n\n"
            "📖 *Available Modes:*\n"
            "🏏 IPL · International Cricket\n"
            "⚽ FIFA Draft\n"
            "🤼 WWE Draft _(NEW!)_\n\n"
            "Use `/standings` to view the global leaderboard.",
            parse_mode="Markdown"
        )
    else:
        # Group — full command list
        await update.effective_message.reply_text(
            "🏏 *Welcome to Draft Bot!* 🏏\n\n"
            "*Draft Commands:*\n"
            "/challengeipl — IPL Draft\n"
            "/challengeintl — International Draft\n"
            "/challengefifa — FIFA Draft\n"
            "/challengewwe — WWE Draft _(NEW!)_\n\n"
            "/standings — Leaderboard\n"
            "/myprofile — Your stats\n"
            "/help — Show this message",
            parse_mode="Markdown"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Central router for callbacks."""
    data = update.callback_query.data
    if data.startswith("join_"):
        # "join_MODE" or "join_MODE_OWNERID"
        # challenge.py handled this? 
        # In challenge.py we defined `join_challenge`? No, wait.
        # handlers/challenge.py has `challenge_handler` and `handle_join`?
        # Let's check imports.
        # We imported `handle_join`.
        await handle_join(update, context)
        
    elif data.startswith("draw_") or data.startswith("assign_") or data.startswith("redraw_") or data.startswith("replace_"):
        await handle_draft_callback(update, context)
        
    elif data.startswith("ready_"):
        await handle_ready(update, context)
        

    elif data.startswith("map_"):
        from handlers.admin import handle_map_stats_callback
        await handle_map_stats_callback(update, context)

    elif data.startswith("view_ipl_"):
        from handlers.admin import handle_view_ipl_callback
        await handle_view_ipl_callback(update, context)

    elif data.startswith("view_intl_"):
        from handlers.admin import handle_view_intl_callback
        await handle_view_intl_callback(update, context)

    elif data.startswith("gen_intl_"):
        from handlers.admin import handle_gen_intl_callback
        await handle_gen_intl_callback(update, context)

    elif data.startswith("gen_ipl_"):
        from handlers.admin import handle_gen_ipl_callback
        await handle_gen_ipl_callback(update, context)

    elif data.startswith("chk_"):
        from handlers.admin import handle_check_callback
        await handle_check_callback(update, context)

async def post_init(application):
    from database import init_db, get_db
    await init_db()
    # Startup recovery: clean up stuck matches and restart timers
    await _startup_recovery(application.bot)

async def _startup_recovery(bot):
    """On every bot start, scan for stuck matches and:
    - If older than 30min in DRAFTING/READY_CHECK: unpin + delete
    - If in READY_CHECK and draft_completed_at > 5min ago: auto-simulate
    - If < 30min in DRAFTING: restart the 30min abandon timeout
    """
    from database import get_db
    from game.state import load_match_state, save_match_state

    db = get_db()
    now = time.time()
    ABANDON_LIMIT   = 1800  # 30 min
    AUTOREADY_LIMIT = 300   # 5 min

    logger = logging.getLogger(__name__)
    logger.info("Startup recovery: scanning for stuck matches...")

    try:
        stuck = await db.matches.find(
            {"state": {"$in": ["DRAFTING", "READY_CHECK"]}}
        ).to_list(length=200)
    except Exception as e:
        logger.error(f"Startup recovery query failed: {e}")
        return

    cleaned = 0
    restarted = 0

    for doc in stuck:
        match_id  = doc.get("match_id")
        chat_id   = doc.get("chat_id")
        pinned_id = doc.get("pinned_message_id")
        state     = doc.get("state", "DRAFTING")
        # Use match creation time from match_id (format: ownerid_timestamp)
        try:
            created_at = float(match_id.split("_")[1])
        except Exception:
            created_at = now - ABANDON_LIMIT - 1  # Force cleanup if unparseable

        age = now - created_at
        draft_completed_at = doc.get("draft_completed_at", 0.0)

        if age >= ABANDON_LIMIT:
            # Match is stale — clean it up
            try:
                if pinned_id:
                    await bot.unpin_chat_message(chat_id=chat_id, message_id=pinned_id)
            except Exception:
                pass
            try:
                await db.matches.delete_one({"match_id": match_id})
            except Exception:
                pass
            cleaned += 1

        elif state == "READY_CHECK" and draft_completed_at > 0 and (now - draft_completed_at) >= AUTOREADY_LIMIT:
            # Draft complete but players didn't click ready for 5+ min — auto-simulate
            asyncio.create_task(_auto_simulate(bot, match_id))
            restarted += 1

        else:
            # Still young — restart the abandon timeout with remaining time
            remaining = max(30, ABANDON_LIMIT - age)
            async def _delayed_unpin(b, c_id, p_id, m_id, delay):
                await asyncio.sleep(delay)
                from game.state import load_match_state
                m = await load_match_state(m_id)
                if not m or m.state in ["DRAFTING", "READY_CHECK"]:
                    try: await b.unpin_chat_message(chat_id=c_id, message_id=p_id)
                    except: pass
                    try:
                        from database import get_db as _gdb
                        await _gdb().matches.delete_one({"match_id": m_id})
                    except: pass
            asyncio.create_task(_delayed_unpin(bot, chat_id, pinned_id, match_id, remaining))

    logger.info(f"Startup recovery done: {cleaned} cleaned, {restarted} auto-simulating.")

    # ── Expire stale challenges (survived bot restart) ──────────────────
    try:
        from database import get_stale_challenges, delete_pending_challenge
        stale = await get_stale_challenges(expiry_secs=120)
        for ch in stale:
            cid  = ch.get("chat_id")
            mid  = ch.get("message_id")
            oid  = ch.get("owner_id")
            mode = ch.get("mode", "")
            try:
                await bot.edit_message_caption(
                    chat_id=cid, message_id=mid,
                    caption="⏰ <b>Challenge Expired</b>\nNo one joined in time.",
                    parse_mode="HTML"
                )
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=cid, message_id=mid,
                        text="⏰ <b>Challenge Expired</b>\nNo one joined in time.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            try:
                await delete_pending_challenge(oid)
            except Exception:
                pass
        if stale:
            logger.info(f"Startup recovery: expired {len(stale)} stale challenge(s).")
    except Exception as e:
        logger.error(f"Stale challenge cleanup failed: {e}")

async def _auto_simulate(bot, match_id: str):
    """Trigger simulation for a READY_CHECK match that timed out."""
    from game.state import load_match_state, save_match_state
    from game.simulation import run_simulation

    logger = logging.getLogger(__name__)
    try:
        match = await load_match_state(match_id)
        if not match or match.state != "READY_CHECK":
            return

        match.state = "SIMULATING"
        match.team_a.is_ready = True
        match.team_b.is_ready = True
        await save_match_state(match)

        result_text = await run_simulation(match)
        match.state = "FINISHED"
        match.finished_at = time.time()
        await save_match_state(match)

        await bot.send_message(
            chat_id=match.chat_id,
            text=f"⏰ *Auto-Ready triggered (5min timeout)*\n\n{result_text}",
            parse_mode="Markdown"
        )

        # Unpin draft board
        pinned_id = getattr(match, 'pinned_message_id', None)
        if pinned_id:
            try:
                await bot.unpin_chat_message(chat_id=match.chat_id, message_id=pinned_id)
            except Exception:
                pass

        # Update user stats
        from database import update_user_stats
        winner_id = match.team_a.owner_id if match.team_a.score > match.team_b.score else (
            match.team_b.owner_id if match.team_b.score > match.team_a.score else None
        )
        result_a = "W" if match.team_a.score > match.team_b.score else ("D" if match.team_a.score == match.team_b.score else "L")
        result_b = "W" if match.team_b.score > match.team_a.score else ("D" if match.team_a.score == match.team_b.score else "L")
        mode = match.mode
        await update_user_stats(match.team_a.owner_id, result_a, mode=mode, chat_id=match.chat_id)
        await update_user_stats(match.team_b.owner_id, result_b, mode=mode, chat_id=match.chat_id)

    except Exception as e:
        logger.error(f"Auto-simulate failed for {match_id}: {e}")

if __name__ == '__main__':
    # Build application
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .job_queue(None)
        .read_timeout(30)
        .write_timeout(30)
        .connection_pool_size(1024)
        .post_init(post_init)
        .build()
    )

    # Handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))

    # Admin
    application.add_handler(CommandHandler('add_player', add_player))
    application.add_handler(CommandHandler('map_api', map_api))
    application.add_handler(CommandHandler('removeplayer', remove_player))
    application.add_handler(CommandHandler('stats', get_player_stats))
    application.add_handler(CommandHandler('reset_matches', reset_matches))
    from handlers.admin import check_role_stats
    application.add_handler(CommandHandler('check', check_role_stats))

    # Mod management
    from handlers.admin import add_mod_handler, remove_mod_handler
    application.add_handler(CommandHandler('mod', add_mod_handler))
    application.add_handler(CommandHandler('unmod', remove_mod_handler))
    application.add_handler(CommandHandler('modrm', remove_mod_handler))
    from handlers.admin import list_mods_handler
    application.add_handler(CommandHandler('mods', list_mods_handler))

    # Stat modifiers
    from handlers.admin import (
        change_cap, change_wk, change_top, change_middle,
        change_defence, change_pacer, change_spinner,
        change_allrounder, change_finisher, change_fielder,
        set_stats, fix_roles_command, migrate_roles_command,
        add_role_command, rem_role_command, non_role_fix,
        run_fix_now_command, revert_command,
        add_player_ipl, add_role_ipl, rem_role_ipl, update_image_command,
        enable_ipl_command, disable_ipl_command,
        handle_remove_ipl, handle_clearcache, update_image_fifa,
        remove_player_fifa, player_list_ipl,
    )
    application.add_handler(CommandHandler('removeplayerfifa', remove_player_fifa))

    # WWE Admin commands
    from handlers.admin import add_player_wwe, remove_player_wwe, update_image_wwe
    application.add_handler(CommandHandler('add_playerwwe',    add_player_wwe))
    application.add_handler(CommandHandler('addplayerwwe',     add_player_wwe))   # alias
    application.add_handler(CommandHandler('remove_playerwwe', remove_player_wwe))
    application.add_handler(CommandHandler('removeplayerwwe',  remove_player_wwe)) # alias
    application.add_handler(CommandHandler('update_imagewwe',  update_image_wwe))

    application.add_handler(CommandHandler('changecap', change_cap))
    application.add_handler(CommandHandler('changewk', change_wk))
    application.add_handler(CommandHandler('changetop', change_top))
    application.add_handler(CommandHandler('changemiddle', change_middle))
    application.add_handler(CommandHandler('changedefence', change_defence))
    application.add_handler(CommandHandler('changepacer', change_pacer))
    application.add_handler(CommandHandler('changespinner', change_spinner))
    application.add_handler(CommandHandler('changeallrounder', change_allrounder))
    application.add_handler(CommandHandler('changefinisher', change_finisher))
    application.add_handler(CommandHandler('changefielder', change_fielder))
    application.add_handler(CommandHandler('setstats', set_stats))
    application.add_handler(CommandHandler('fix_roles', fix_roles_command))
    application.add_handler(CommandHandler('migrate_roles', migrate_roles_command))
    application.add_handler(CommandHandler('add_role', add_role_command))
    application.add_handler(CommandHandler('rem_role', rem_role_command))
    application.add_handler(CommandHandler('nonrolefix', non_role_fix))
    application.add_handler(CommandHandler('run_fix_now', run_fix_now_command))
    application.add_handler(CommandHandler('revert', revert_command))
    application.add_handler(CommandHandler('add_playeripl', add_player_ipl))
    application.add_handler(CommandHandler('add_roleipl', add_role_ipl))
    application.add_handler(CommandHandler('rem_roleipl', rem_role_ipl))
    application.add_handler(CommandHandler('removeipl', handle_remove_ipl))
    application.add_handler(CommandHandler('update_image', update_image_command))
    application.add_handler(CommandHandler('enable_ipl', enable_ipl_command))
    application.add_handler(CommandHandler('disable_ipl', disable_ipl_command))
    application.add_handler(CommandHandler('clearcache', handle_clearcache))
    application.add_handler(CommandHandler('update_imagefifa', update_image_fifa))
    # FIFA commands
    from handlers.admin import add_player_fifa
    application.add_handler(CommandHandler('add_playerfifa', add_player_fifa))
    application.add_handler(CommandHandler('addplayerfifa',  add_player_fifa))  # alias
    application.add_handler(CommandHandler('addplayer',      add_player))        # alias

    from handlers.admin import handle_broadcast, handle_banner
    application.add_handler(CommandHandler('broadcast', handle_broadcast))
    application.add_handler(CommandHandler('banner', handle_banner))

    # Game
    # Game
    from handlers.challenge import challenge_unified, challenge_ipl, challenge_intl, challenge_fifa
    application.add_handler(CommandHandler('challenge_ipl', challenge_ipl))
    application.add_handler(CommandHandler('challenge_intl', challenge_intl))
    application.add_handler(CommandHandler('challenge_fifa', challenge_fifa))
    
    # Aliases for easier typing
    application.add_handler(CommandHandler('challengeipl', challenge_ipl))
    application.add_handler(CommandHandler('challengeintl', challenge_intl))
    application.add_handler(CommandHandler('challengefifa', challenge_fifa))
    
    application.add_handler(CommandHandler('challenge', challenge_unified))

    # WWE challenge commands
    application.add_handler(CommandHandler('challengewwe',   challenge_wwe))
    application.add_handler(CommandHandler('challenge_wwe',  challenge_wwe))

    # User Profile
    from handlers.profile import handle_profile
    application.add_handler(CommandHandler('myprofile', handle_profile))

    # Standings / Leaderboard
    from handlers.standings import handle_standings, handle_standings_callback
    application.add_handler(CommandHandler('standings', handle_standings))
    application.add_handler(CallbackQueryHandler(handle_standings_callback, pattern=r"^lb_"))

    # Swap System
    from handlers.swap import (
        handle_swap_pick1, handle_swap_pick2, handle_swap_cancel
    )
    application.add_handler(CallbackQueryHandler(handle_swap_pick1, pattern=r"^swap1\|"))
    application.add_handler(CallbackQueryHandler(handle_swap_pick2, pattern=r"^swap2\|"))
    application.add_handler(CallbackQueryHandler(handle_swap_cancel, pattern=r"^swapcancel\|"))

    # Callbacks
    application.add_handler(CallbackQueryHandler(handle_callback))

    # ---- HEALTH SERVER (FREE TIER FIX) ----
    import threading
    import os
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def start_health_server():
        port = int(os.environ.get("PORT", 8000))
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()

    threading.Thread(target=start_health_server, daemon=True).start()


    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        err_str = str(err)
        # Silently ignore known benign post-restart / network noise
        _IGNORE_ERRORS = (
            "Query is too old",
            "Message to be replied not found",
            "Message is not modified",
            "Task was destroyed but it is pending",
            "Connection closed",
        )
        if any(e in err_str for e in _IGNORE_ERRORS):
            return  # Drop silently — not a real bug
        logging.getLogger(__name__).error(msg="Exception while handling an update:", exc_info=err)


    # Trade System
    
    application.add_error_handler(error_handler)

    print("Bot is running (Polling mode, Free tier compatible)...")
    application.run_polling()
