import asyncio
from telethon import TelegramClient, events
from telethon.errors import ChatAdminRequiredError
from flask import Flask
import threading

# Replace with your credentials
api_id = '26918101'
api_hash = '57d6680f6549e21aca4e93c7a4221d29'
bot_token = '7541906904:AAEpxYEMMj7y2VCPqeOGfEmD09iH4XO1P2M'

# Initialize Telegram client
client = TelegramClient('bot_session', api_id, api_hash).start(bot_token=bot_token)

# Allowed bot IDs
TARGET_BOT_IDS = {
    6967358342,  # @MultiMiniGameBot
    7906407273,  # @CHAT_CRICKET_ROBOT
    7018891546   # @PaladinsRealmBot
}

# Unpin after 1 hour (3600 seconds)
UNPIN_DELAY = 3600

# Match /pin, pin, PIN, etc. case-insensitively
@client.on(events.NewMessage(pattern=r'(?i)^/?pin$', incoming=True))
async def pin_handler(event):
    if not event.is_reply:
        await event.reply("ℹ️ Please reply to the bot message you want to pin.")
        return

    reply_msg = await event.get_reply_message()

    if reply_msg.sender_id not in TARGET_BOT_IDS:
        await event.reply("❌ You can only pin messages from @MultiMiniGameBot, @CHAT_CRICKET_ROBOT, or @PaladinsRealmBot.")
        return

    try:
        # Pin the message
        await client.pin_message(event.chat_id, reply_msg.id, notify=False)
        await event.reply("📌 Message pinned. It will be unpinned in 1 hour.")

        # Delete the triggering /pin message for cleanliness
        await event.delete()

        # Wait 1 hour then unpin
        await asyncio.sleep(UNPIN_DELAY)
        await client.unpin_message(event.chat_id, reply_msg.id)

    except ChatAdminRequiredError:
        await event.reply(
            "⚠️ I can't pin messages because I don't have the proper admin rights.\n"
            "Please make sure I have **'Pin Messages'** permission in this group."
        )
    except Exception as e:
        await event.reply(f"❌ Failed: {e}")

# Flask server to keep bot alive (for hosting)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def run_server():
    app.run(host="0.0.0.0", port=8000)

# Run Flask app in background
threading.Thread(target=run_server, daemon=True).start()

# Start the Telegram client
client.run_until_disconnected()
