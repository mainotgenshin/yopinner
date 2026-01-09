# database.py
import sqlite3
import json
import logging
from typing import Optional, Dict, Any

import os

# Use absolute path to ensure DB is found regardless of CWD
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "cricket_bot.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_connection():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    # Players Table
    c.execute('''CREATE TABLE IF NOT EXISTS players (
        player_id TEXT PRIMARY KEY,
        name TEXT,
        roles TEXT,
        image_file_id TEXT,
        api_reference TEXT,
        stats TEXT
    )''')
    
    # Active Matches Table - Stores serialized match state
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
        match_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        state_data TEXT
    )''')
    
    # Mods Table
    c.execute('''CREATE TABLE IF NOT EXISTS mods (
        user_id INTEGER PRIMARY KEY
    )''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def save_player(player_data: Dict[str, Any]):
    conn = get_connection()
    c = conn.cursor()
    # Serialize complex fields
    roles_str = ",".join(player_data.get('roles', []))
    api_ref_str = json.dumps(player_data.get('api_reference', {}))
    stats_str = json.dumps(player_data.get('stats', {}))
    
    c.execute('''INSERT OR REPLACE INTO players 
                 (player_id, name, roles, image_file_id, api_reference, stats) 
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (player_data['player_id'], player_data['name'], roles_str, 
               player_data['image_file_id'], api_ref_str, stats_str))
    conn.commit()
    conn.close()

def get_player(player_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE player_id = ?", (player_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {
            "player_id": row[0],
            "name": row[1],
            "roles": row[2].split(","),
            "image_file_id": row[3],
            "api_reference": json.loads(row[4]) if row[4] else {},
            "stats": json.loads(row[5]) if row[5] else {}
        }
    return None

def get_player_by_name(name_query: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    # Case-insensitive partial match
    c.execute("SELECT * FROM players WHERE name LIKE ?", (f"%{name_query}%",))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {
            "player_id": row[0],
            "name": row[1],
            "roles": row[2].split(","),
            "image_file_id": row[3],
            "api_reference": json.loads(row[4]) if row[4] else {},
            "stats": json.loads(row[5]) if row[5] else {}
        }
    return None

def delete_player(player_id: str) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM players WHERE player_id = ?", (player_id,))
    rows = c.rowcount
    conn.commit()
    conn.close()
    return rows > 0

def get_all_players() -> list:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM players")
    rows = c.fetchall()
    conn.close()
    
    players = []
    for row in rows:
        players.append({
            "player_id": row[0],
            "name": row[1],
            "roles": row[2].split(","),
            "image_file_id": row[3],
            "api_reference": json.loads(row[4]) if row[4] else {},
            "stats": json.loads(row[5]) if row[5] else {}
        })
    return players

def save_match(match_id: str, chat_id: int, state_data: Dict[str, Any]):
    conn = get_connection()
    c = conn.cursor()
    state_json = json.dumps(state_data)
    c.execute('''INSERT OR REPLACE INTO matches (match_id, chat_id, state_data) 
                 VALUES (?, ?, ?)''', (match_id, chat_id, state_json))
    conn.commit()
    conn.close()
    logger.info(f"DEBUG: Saved Match {match_id}")

def get_match(match_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT state_data FROM matches WHERE match_id = ?", (match_id,))
    row = c.fetchone()
    conn.close()
    if row:
        logger.info(f"DEBUG: Found Match {match_id}")
        return json.loads(row[0])
    logger.warning(f"DEBUG: NOT Found Match {match_id}")
    return None

    conn.commit()
    conn.close()

def add_mod(user_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO mods (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_mod(user_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM mods WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_mod(user_id: int) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM mods WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

