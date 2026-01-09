# debug_token.py
import os
from dotenv import load_dotenv

# Load env
load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
print(f"Raw Token: '{token}'")
if token:
    print(f"Length: {len(token)}")
    print(f"Repr: {repr(token)}")
else:
    print("Token is None!")
