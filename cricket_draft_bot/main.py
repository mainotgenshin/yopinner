# main.py
import logging
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
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler
from config import BOT_TOKEN
from database import init_db
from handlers.admin import add_player, map_api, remove_player, player_list, get_player_stats, reset_matches
from handlers.challenge import challenge_ipl, challenge_intl, join_challenge, handle_join
from handlers.draft import handle_draft_callback
from handlers.ready import handle_ready
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏏 **Welcome to Cricket Draft Bot!** 🏏\n\n"
        "Commands:\n"
        "/challenge_ipl - Start IPL Draft\n"
        "/challenge_intl - Start Intl Draft\n"
        "/add_player - (Admin) Add Player\n"
        "/map_api - (Admin) Map Stats\n"
        "/help - Show this message",
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
        
    elif data.startswith("plist_"):
        # Avoid circular import issues if possible, or import inside
        from handlers.admin import handle_playerlist_callback
        await handle_playerlist_callback(update, context)
        
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
if __name__ == '__main__':
    # Initialize DB
    init_db()
    # Build application
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(None)
        .read_timeout(30)
        .write_timeout(30)
        .connection_pool_size(1024)
        .build()
    )
    # Handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    # Admin
    application.add_handler(CommandHandler('add_player', add_player))
    application.add_handler(CommandHandler('map_api', map_api))
    application.add_handler(CommandHandler('removeplayer', remove_player))
    application.add_handler(CommandHandler('playerlist', player_list))
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
    # Stat modifiers
    from handlers.admin import (
        change_cap, change_wk, change_top, change_middle,
        change_defence, change_pacer, change_spinner, 
        change_allrounder, change_finisher, change_fielder,
        set_stats, fix_roles_command, migrate_roles_command,
        add_role_command, rem_role_command, non_role_fix,
        run_fix_now_command, revert_command,
        add_player_ipl, add_role_ipl, rem_role_ipl, update_image_command,
        enable_ipl_command, disable_ipl_command
    )
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
    application.add_handler(CommandHandler('update_image', update_image_command))
    application.add_handler(CommandHandler('enable_ipl', enable_ipl_command))
    application.add_handler(CommandHandler('disable_ipl', disable_ipl_command))
    # Game
    application.add_handler(CommandHandler('challenge_ipl', challenge_ipl))
    application.add_handler(CommandHandler('challenge_intl', challenge_intl))
    from handlers.challenge import challenge_unified
    application.add_handler(CommandHandler('challenge', challenge_unified))
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
    print("Bot is running (Polling mode, Free tier compatible)...")
    application.run_polling()
