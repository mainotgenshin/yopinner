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
from handlers.challenge import challenge_ipl, challenge_odi, challenge_test, challenge_fifa, challenge_wwe, handle_join, handle_mode_pick_callback
from handlers.draft import handle_draft_callback
from handlers.ready import handle_ready

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Suppress noisy httpx request logs (fires on every API call — hundreds/hour)
logging.getLogger("httpx").setLevel(logging.WARNING)

def wrap_admin_logging(handler_func, action_name):
    """Wraps admin handlers to log their actions to the admin log channel/group."""
    from config import ADMIN_LOG_GROUP_ID
    import asyncio
    import logging
    
    logger = logging.getLogger(__name__)

    async def logged_handler(update, context):
        res = await handler_func(update, context)
        
        # Only log if log group is configured and sender is authenticated admin
        if ADMIN_LOG_GROUP_ID and update.effective_user and update.message and update.message.text:
            async def _send_log():
                try:
                    from utils.permissions import check_admin
                    if not await check_admin(update):
                        return
                    
                    user = update.effective_user
                    chat = update.effective_chat
                    user_info = f"{user.first_name} (ID: <code>{user.id}</code>)"
                    if user.username:
                        user_info += f" @{user.username}"
                    
                    chat_info = "Private DM"
                    if chat and chat.type != "private":
                        chat_info = f"Group: <b>{chat.title}</b> (ID: <code>{chat.id}</code>)"
                        
                    msg = (
                        f"🛠️ <b>Admin Action Log</b>\n\n"
                        f"👤 <b>Admin</b>: {user_info}\n"
                        f"📍 <b>Location</b>: {chat_info}\n"
                        f"📝 <b>Action</b>: {action_name}\n"
                        f"💬 <b>Command</b>: <code>{update.message.text}</code>"
                    )
                    await context.bot.send_message(
                        chat_id=ADMIN_LOG_GROUP_ID,
                        text=msg,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to send admin log: {e}")

            asyncio.create_task(_send_log())
        return res
        
    return logged_handler

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
            "/challenge — Select and start any challenge\n"
            "/challengeipl — IPL Draft\n"
            "/challengeodi — ODI Draft\n"
            "/challengetest — Test Draft\n"
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

    elif data.startswith("view_odi_"):
        from handlers.admin import handle_view_odi_callback
        await handle_view_odi_callback(update, context)

    elif data.startswith("view_test_"):
        from handlers.admin import handle_view_test_callback
        await handle_view_test_callback(update, context)

    elif data.startswith("gen_odi_"):
        from handlers.admin import handle_gen_odi_callback
        await handle_gen_odi_callback(update, context)

    elif data.startswith("challenge_pick_"):
        await handle_mode_pick_callback(update, context)

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
            
            # Restart AFK forfeit timer if in drafting phase
            if state == "DRAFTING":
                async def _startup_afk_recovery(m_id, b):
                    try:
                        from game.state import load_match_state
                        from handlers.draft import start_forfeit_timer_on_startup
                        m = await load_match_state(m_id)
                        if m:
                            start_forfeit_timer_on_startup(m, b)
                    except Exception:
                        pass
                asyncio.create_task(_startup_afk_recovery(match_id, bot))

    logger.info(f"Startup recovery done: {cleaned} cleaned, {restarted} auto-simulating.")

    # ── Expire stale challenges (survived bot restart) ──────────────────
    try:
        from database import get_stale_challenges, delete_pending_challenge, get_db as _gdb
        EXPIRED_TEXT = "⏰ <b>Challenge Expired</b>\nNo one joined in time. Start a new one with /challenge odi or /challengeipl."

        # 1. Immediately expire challenges already older than 2 min
        stale = await get_stale_challenges(expiry_secs=120)
        for ch in stale:
            cid  = ch.get("chat_id")
            mid  = ch.get("message_id")
            oid  = ch.get("owner_id")
            mode = ch.get("mode", "")
            try:
                await bot.edit_message_caption(chat_id=cid, message_id=mid, caption=EXPIRED_TEXT, parse_mode="HTML")
            except Exception:
                try:
                    await bot.edit_message_text(chat_id=cid, message_id=mid, text=EXPIRED_TEXT, parse_mode="HTML")
                except Exception:
                    pass
            try:
                await delete_pending_challenge(oid, mode)
            except Exception:
                pass
            # Throttle: 50ms gap between edits to stay under Telegram's 30 msg/sec limit
            await asyncio.sleep(0.05)
        if stale:
            logger.info(f"Startup recovery: expired {len(stale)} stale challenge(s).")

        # 2. Reschedule expiry for young challenges (<120s) — bot restarted mid-timer
        db2 = _gdb()
        young_cursor = db2.pending_challenges.find({"created_at": {"$gte": now - 120}})
        young = await young_cursor.to_list(length=200)
        for ch in young:
            cid      = ch.get("chat_id")
            mid      = ch.get("message_id")
            oid      = ch.get("owner_id")
            mode     = ch.get("mode", "")
            created  = ch.get("created_at", now)
            remaining = max(5, 120 - (now - created))  # seconds left until expiry

            async def _recover_expire(bot, cid, mid, oid, mode, delay, text=EXPIRED_TEXT):
                await asyncio.sleep(delay)
                try:
                    from database import delete_pending_challenge as _del
                    await _del(oid, mode)
                except Exception:
                    pass
                try:
                    await bot.edit_message_caption(chat_id=cid, message_id=mid, caption=text, parse_mode="HTML")
                except Exception:
                    try:
                        await bot.edit_message_text(chat_id=cid, message_id=mid, text=text, parse_mode="HTML")
                    except Exception:
                        pass

            asyncio.create_task(_recover_expire(bot, cid, mid, oid, mode, remaining))

        if young:
            logger.info(f"Startup recovery: rescheduled expiry for {len(young)} young challenge(s).")

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
        .rate_limiter(AIORateLimiter(max_retries=5))
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
    application.add_handler(CommandHandler('add_player', wrap_admin_logging(add_player, "Add/Update Player (Cricket)")))
    application.add_handler(CommandHandler('map_api', map_api))
    application.add_handler(CommandHandler('removeplayer', wrap_admin_logging(remove_player, "Remove Player")))
    application.add_handler(CommandHandler('stats', get_player_stats))
    application.add_handler(CommandHandler('reset_matches', wrap_admin_logging(reset_matches, "Force Reset Match State")))
    from handlers.admin import check_role_stats
    application.add_handler(CommandHandler('check', check_role_stats))

    # Mod management
    from handlers.admin import add_mod_handler, remove_mod_handler
    application.add_handler(CommandHandler('mod', wrap_admin_logging(add_mod_handler, "Add Moderator")))
    application.add_handler(CommandHandler('unmod', wrap_admin_logging(remove_mod_handler, "Remove Moderator")))
    application.add_handler(CommandHandler('modrm', wrap_admin_logging(remove_mod_handler, "Remove Moderator")))
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
        add_role_test, rem_role_test, rem_player_odi, rem_player_test, add_player_test,
    )
    application.add_handler(CommandHandler('removeplayerfifa', wrap_admin_logging(remove_player_fifa, "Remove Player (FIFA)")))

    # WWE Admin commands
    from handlers.admin import add_player_wwe, remove_player_wwe, update_image_wwe
    application.add_handler(CommandHandler('add_playerwwe',    wrap_admin_logging(add_player_wwe, "Add Superstar (WWE)")))
    application.add_handler(CommandHandler('addplayerwwe',     wrap_admin_logging(add_player_wwe, "Add Superstar (WWE)")))
    application.add_handler(CommandHandler('remove_playerwwe', wrap_admin_logging(remove_player_wwe, "Remove Superstar (WWE)")))
    application.add_handler(CommandHandler('removeplayerwwe',  wrap_admin_logging(remove_player_wwe, "Remove Superstar (WWE)")))
    application.add_handler(CommandHandler('update_imagewwe',  wrap_admin_logging(update_image_wwe, "Update Superstar Image (WWE)")))

    application.add_handler(CommandHandler('changecap', wrap_admin_logging(change_cap, "Modify Captain Stat")))
    application.add_handler(CommandHandler('changewk', wrap_admin_logging(change_wk, "Modify WK Stat")))
    application.add_handler(CommandHandler('changetop', wrap_admin_logging(change_top, "Modify Top Order Stat")))
    application.add_handler(CommandHandler('changemiddle', wrap_admin_logging(change_middle, "Modify Middle Order Stat")))
    application.add_handler(CommandHandler('changedefence', wrap_admin_logging(change_defence, "Modify Defence Stat")))
    application.add_handler(CommandHandler('changepacer', wrap_admin_logging(change_pacer, "Modify Pacer Stat")))
    application.add_handler(CommandHandler('changespinner', wrap_admin_logging(change_spinner, "Modify Spinner Stat")))
    application.add_handler(CommandHandler('changeallrounder', wrap_admin_logging(change_allrounder, "Modify All Rounder Stat")))
    application.add_handler(CommandHandler('changefinisher', wrap_admin_logging(change_finisher, "Modify Finisher Stat")))
    application.add_handler(CommandHandler('changefielder', wrap_admin_logging(change_fielder, "Modify Fielder Stat")))
    application.add_handler(CommandHandler('setstats', wrap_admin_logging(set_stats, "Set Player Stats")))
    application.add_handler(CommandHandler('fix_roles', wrap_admin_logging(fix_roles_command, "Run Fix Roles")))
    application.add_handler(CommandHandler('migrate_roles', wrap_admin_logging(migrate_roles_command, "Run Migrate Roles")))
    application.add_handler(CommandHandler('add_role', wrap_admin_logging(add_role_command, "Add Player Role (Global)")))
    application.add_handler(CommandHandler('rem_role', wrap_admin_logging(rem_role_command, "Remove Player Role (Global)")))
    application.add_handler(CommandHandler('nonrolefix', wrap_admin_logging(non_role_fix, "Run Non-Role Fix")))
    application.add_handler(CommandHandler('run_fix_now', wrap_admin_logging(run_fix_now_command, "Force Database Alignment Fixes")))
    application.add_handler(CommandHandler('revert', wrap_admin_logging(revert_command, "Revert Database Operations")))
    application.add_handler(CommandHandler('add_playeripl', wrap_admin_logging(add_player_ipl, "Add Player (IPL)")))
    application.add_handler(CommandHandler('add_roleipl', wrap_admin_logging(add_role_ipl, "Add Player Role (IPL)")))
    application.add_handler(CommandHandler('rem_roleipl', wrap_admin_logging(rem_role_ipl, "Remove Player Role (IPL)")))
    application.add_handler(CommandHandler('removeipl', wrap_admin_logging(handle_remove_ipl, "Remove Player from IPL")))
    application.add_handler(CommandHandler('add_playertest', wrap_admin_logging(add_player_test, "Add Player (Test)")))
    application.add_handler(CommandHandler('add_roletest', wrap_admin_logging(add_role_test, "Add Player Role (Test)")))
    application.add_handler(CommandHandler('rem_roletest', wrap_admin_logging(rem_role_test, "Remove Player Role (Test)")))
    application.add_handler(CommandHandler('rem_playerodi', wrap_admin_logging(rem_player_odi, "Remove Player from ODI")))
    application.add_handler(CommandHandler('rem_playertest', wrap_admin_logging(rem_player_test, "Remove Player from Test")))
    application.add_handler(CommandHandler('update_image', wrap_admin_logging(update_image_command, "Update Player Image (Cricket)")))
    application.add_handler(CommandHandler('enable_ipl', wrap_admin_logging(enable_ipl_command, "Enable IPL Mode")))
    application.add_handler(CommandHandler('disable_ipl', wrap_admin_logging(disable_ipl_command, "Disable IPL Mode")))
    application.add_handler(CommandHandler('clearcache', wrap_admin_logging(handle_clearcache, "Flush Cache Databases")))
    application.add_handler(CommandHandler('update_imagefifa', wrap_admin_logging(update_image_fifa, "Update Player Image (FIFA)")))
    # FIFA commands
    from handlers.admin import add_player_fifa
    application.add_handler(CommandHandler('add_playerfifa', wrap_admin_logging(add_player_fifa, "Add Player (FIFA)")))
    application.add_handler(CommandHandler('addplayerfifa',  wrap_admin_logging(add_player_fifa, "Add Player (FIFA)")))
    application.add_handler(CommandHandler('addplayer',      wrap_admin_logging(add_player, "Add/Update Player (Cricket)")))

    from handlers.admin import handle_broadcast, handle_banner
    application.add_handler(CommandHandler('broadcast', wrap_admin_logging(handle_broadcast, "Send Broadcast Message")))
    application.add_handler(CommandHandler('banner', wrap_admin_logging(handle_banner, "Modify Banner overrides")))

    # Game
    from handlers.challenge import challenge_unified, challenge_ipl, challenge_odi, challenge_test, challenge_fifa
    application.add_handler(CommandHandler('challenge_ipl', challenge_ipl))
    application.add_handler(CommandHandler('challenge_odi', challenge_odi))
    application.add_handler(CommandHandler('challenge_intl', challenge_odi)) # alias
    application.add_handler(CommandHandler('challenge_test', challenge_test))
    application.add_handler(CommandHandler('challenge_fifa', challenge_fifa))
    
    # Aliases for easier typing
    application.add_handler(CommandHandler('challengeipl', challenge_ipl))
    application.add_handler(CommandHandler('challengeodi', challenge_odi))
    application.add_handler(CommandHandler('challengeintl', challenge_odi)) # alias
    application.add_handler(CommandHandler('challengetest', challenge_test))
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
