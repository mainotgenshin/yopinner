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
        
    elif data.startswith("draw_") or data.startswith("assign_") or data.startswith("redraw_"):
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
        .connection_pool_size(16)
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

    # Mod management
    from handlers.admin import add_mod_handler, remove_mod_handler
    application.add_handler(CommandHandler('mod', add_mod_handler))
    application.add_handler(CommandHandler('unmod', remove_mod_handler))
    application.add_handler(CommandHandler('modrm', remove_mod_handler))

    # Stat modifiers
    from handlers.admin import (
        change_cap, change_wk, change_hitting, change_pace,
        change_spin, change_allround, change_finisher,
        change_field, set_stats
    )

    application.add_handler(CommandHandler('changecap', change_cap))
    application.add_handler(CommandHandler('changewk', change_wk))
    application.add_handler(CommandHandler('changehitting', change_hitting))
    application.add_handler(CommandHandler('changepace', change_pace))
    application.add_handler(CommandHandler('changespin', change_spin))
    application.add_handler(CommandHandler('changeallround', change_allround))
    application.add_handler(CommandHandler('changefinisher', change_finisher))
    application.add_handler(CommandHandler('changefield', change_field))
    application.add_handler(CommandHandler('setstats', set_stats))

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
