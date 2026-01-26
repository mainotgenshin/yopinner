# config.py
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot Token - User must set this env var or replace string
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Owner IDs (Integer IDs)
# Load from env (comma separated) or default list
_owner_env = os.getenv("OWNER_IDS")
OWNER_IDS = [int(x) for x in _owner_env.split(',')] if _owner_env else []

# API Credentials (Optional for standard bots, required for some clients)
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Webhook / Hosting Config
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))
MONGO_URI = os.getenv("MONGO_URI")

# Role Weights
ROLE_WEIGHTS = {
    "Captain": 1.5,
    "All Rounder": 1.3,
    "Finisher": 1.2,
    "Defence": 1.2, 
    "Top": 1.2,
    "Middle": 1.2,
    "Pacer": 1.0,
    "Spinner": 1.0,
    "WK": 1.0,
    "Fielder": 0.8
}

# Fixed Positions by Mode
POSITIONS_T20 = [
    "Captain",
    "WK",
    "Top",
    "Middle",
    "All Rounder",
    "Finisher",
    "Pacer",
    "Spinner",
    "Fielder"
]

POSITIONS_TEST = [
    "Captain",
    "WK",
    "Top",
    "Middle",
    "All Rounder",
    "Defence",
    "Pacer",
    "Spinner",
    "Fielder"
]

# Legacy/Default for import safety (aliased to T20 for now)
POSITIONS = POSITIONS_T20

# Draft Settings
MAX_REDRAWS = 2
DRAFT_BANNER_INTL = "https://files.catbox.moe/8l3ktm.jpg"
DRAFT_BANNER_IPL = "https://files.catbox.moe/qyrq53.jpg"
DRAFT_BANNER_URL = DRAFT_BANNER_INTL # Fallback alias

# Simulation Constants
ZERO_SKILL_THRESHOLD = 30

PENALTY_MULTIPLIERS = {
    "NATURAL": 1.0,
    "PARTIAL": 0.7,
    "MISMATCH": 0.4,
    "ZERO_SKILL": 0.1
}

ROLE_STATS_MAP = {
    "Captain": "leadership",
    "WK": "wicket_keeping",
    "All Rounder": "all_round",
    "Defence": "batting_defence", 
    "Top": "batting_power",
    "Middle": "batting_control",
    "Finisher": "finishing", 
    "Pacer": "bowling_pace",
    "Spinner": "bowling_spin",
    "Fielder": "fielding"
}

# Players excluded from IPL pool
EXCLUDED_IPL_PLAYERS = [
    "Brian Lara",
    "Joe Root",
    "Jonty Rhodes",
    "Tom Latham",
    "Nathan Lyon",
    "Keshav Maharaj",
    "Ish Sodhi",
    "Mark Chapman",
    "Temba Bavuma"
]
