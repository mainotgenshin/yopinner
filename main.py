import asyncio
from telethon import TelegramClient, events
from telethon.errors import ChatAdminRequiredError
from flask import Flask
import threading

# Replace with your credentials
api_id = '26918101'
api_hash = '57d6680f6549e21aca4e93c7a4221d29'
bot_token = '7541906904:AAEpxYEMMj7y2VCPqeOGfEmD09iH4XO1P2M'
client = TelegramClient('bot_session', api_id, api_hash).start(bot_token=bot_token)

# Constants
TARGET_BOT_IDS = {6967358342, 7906407273}  # @MultiMiniGameBot and @CHAT_CRICKET_ROBOT
UNPIN_DELAY = 86400  # 24 hours in seconds

@client.on(events.NewMessage(pattern=r'^/?pin$', incoming=True))
async def pin_handler(event):
    if not event.is_reply:
        await event.reply("ℹ️ Please reply to the bot message you want to pin.")
        return

    reply_msg = await event.get_reply_message()

    if reply_msg.sender_id not in TARGET_BOT_IDS:
        await event.reply("❌ You can only pin messages from @MultiMiniGameBot or @CHAT_CRICKET_ROBOT.")
        return

    try:
        await client.pin_message(event.chat_id, reply_msg.id, notify=False)
        await event.reply("📌 Message pinned. It will be unpinned in 24 hours.")

        # Wait 24 hours then unpin
        await asyncio.sleep(UNPIN_DELAY)
        await client.unpin_message(event.chat_id, reply_msg.id)

    except ChatAdminRequiredError:
        await event.reply(
            "⚠️ I can't pin messages because I don't have the proper admin rights.\n"
            "Please make sure I have **'Pin Messages'** permission in this group."
        )

    except Exception as e:
        await event.reply(f"❌ Failed: {e}")

# Flask app to keep bot alive (for platforms like Koyeb, Replit, etc.)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def run_server():
    app.run(host="0.0.0.0", port=8000)

threading.Thread(target=run_server, daemon=True).start()

client.run_until_disconnected()
