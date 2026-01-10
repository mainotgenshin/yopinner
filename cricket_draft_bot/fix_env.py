# fix_env.py
content = """TELEGRAM_BOT_TOKEN=7684760520:AAGe9F2y3oUmW8XJeyB2I6FPt7xWKvWCoZY
OWNER_IDS=7343718856
API_ID=26918101
API_HASH=57d6680f6549e21aca4e93c7a4221d29
GEMINI_API_KEY=AIzaSyDHABG_wW_XImSYcoTNQ_6S--ZWIgdDWyI
WEBHOOK_URL=
PORT=8000
MONGO_URI=mongodb+srv://cricketbotuser:NEW_PASSWORD@cluster0.j9nhzpb.mongodb.net/cricket_bot?retryWrites=true&w=majority&tls=true&tlsAllowInvalidCertificates=true

"""

with open('.env', 'w', encoding='utf-8') as f:
    f.write(content)
print("Fixed .env encoding")


