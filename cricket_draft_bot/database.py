# database.py
import os
import json
import logging
from typing import Optional, Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from config import MONGO_URI
from urllib.parse import urlparse
import datetime
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global Client
_mongo_client = None
_db = None

# A simple custom cache for get_player
_player_cache: Dict[str, Dict[str, Any]] = {}
CACHE_MAX_SIZE = 2000

def get_db():
    global _mongo_client, _db
    
    if _db is not None:
        return _db
        
    if MONGO_URI:
        try:
            import certifi
            _mongo_client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
            
            parsed = urlparse(MONGO_URI)
            db_name = parsed.path[1:] if parsed.path and len(parsed.path) > 1 else 'cricket_bot'
            
            _db = _mongo_client[db_name]
            logger.info(f"Connected to Async MongoDB: {db_name}")
            return _db
        except Exception as e:
            logger.error(f"Failed to connect to Async MongoDB: {e}")
            raise e
    else:
        logger.error("No MONGO_URI found!")
        raise ValueError("MONGO_URI is not set in environment.")

async def init_db():
    """Initializes collections and indexes."""
    try:
        db = get_db()
        await db.players.create_index([("player_id", ASCENDING)], unique=True)
        await db.players.create_index([("name", ASCENDING)])
        
        await db.matches.create_index([("match_id", ASCENDING)], unique=True)
        await db.mods.create_index([("user_id", ASCENDING)], unique=True)
        
        await db.matches.create_index([("last_updated", ASCENDING)], expireAfterSeconds=86400)
        await db.users.create_index([("user_id", ASCENDING)], unique=True)
        
        logger.info("Async MongoDB Indexes Verified.")
    except Exception as e:
        logger.error(f"DB Init Failed: {e}")

async def save_player(player_data: Dict[str, Any]):
    db = get_db()
    await db.players.update_one(
        {"player_id": player_data['player_id']},
        {"$set": player_data},
        upsert=True
    )
    clear_player_cache()

async def get_player(player_id: str) -> Optional[Dict[str, Any]]:
    # Simple LRU-like cache retrieval
    if player_id in _player_cache:
        # Move to end to mark as recently used
        data = _player_cache.pop(player_id)
        _player_cache[player_id] = data
        return data

    db = get_db()
    data = await db.players.find_one({"player_id": player_id})
    if data:
        data.pop('_id', None)
        # Cache management
        if len(_player_cache) >= CACHE_MAX_SIZE:
            # Pop oldest (first item in dict)
            _player_cache.pop(next(iter(_player_cache)))
        _player_cache[player_id] = data
        return data
    return None

def clear_player_cache():
    """Manually clear the player cache."""
    global _player_cache
    _player_cache.clear()
    logger.info("Player cache cleared manually.")

async def get_player_by_name(name_query: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    regex = re.compile(re.escape(name_query), re.IGNORECASE)
    
    data = await db.players.find_one({
        "$or": [
            {"name": regex},
            {"full_name": regex}
        ]
    })
    if data:
        data.pop('_id', None)
        return data
    return None

async def delete_player(identifier: str) -> bool:
    """Deletes a player by ID or Name (case-insensitive)."""
    db = get_db()
    
    # Try ID First
    res = await db.players.delete_one({"player_id": identifier})
    if res.deleted_count > 0:
        clear_player_cache()
        return True
        
    regex = f"^{identifier}$"
    res = await db.players.delete_one({"name": {"$regex": regex, "$options": "i"}})
    
    clear_player_cache()
    return res.deleted_count > 0

async def get_all_players() -> list:
    db = get_db()
    cursor = db.players.find({})
    players = []
    async for doc in cursor:
        doc.pop('_id', None)
        players.append(doc)
    return players

async def get_eligible_players_for_mode(mode: str) -> List[str]:
    """
    Optimized DB projection to only fetch player IDs needed for a given mode.
    Solves memory bloat by not deserializing entire player objects.
    """
    db = get_db()
    draft_pool_ids = []
    
    if mode == "FIFA":
        # FIFA Memory Optimization: Only pull players meeting criteria
        query = {
            "sport": "football",
            "overall": {"$gt": 80},
            "$or": [
                {"overall": {"$gt": 83}},
                {"league": {"$in": ["Premier League", "LALIGA EA SPORTS", "Bundesliga", "Serie A Enilive", "Ligue 1 McDonald's"]}}
            ]
        }
    else:
        # Cricket Optimization
        search_key = 'international' if mode.lower() == 'intl' else mode.lower()
        query = {
            f"stats.{search_key}": {"$ne": None}
        }

    # Projection to return ONLY the player_id string
    cursor = db.players.find(query, {"player_id": 1, "_id": 0})
    async for doc in cursor:
        if "player_id" in doc:
            draft_pool_ids.append(doc["player_id"])
            
    return draft_pool_ids

async def save_match(match_id: str, chat_id: int, state_data: Dict[str, Any]):
    db = get_db()
    await db.matches.update_one(
        {"match_id": match_id},
        {"$set": {
            "state_data": state_data, 
            "chat_id": chat_id,
            "last_updated": datetime.datetime.utcnow() 
        }},
        upsert=True
    )
    logger.info(f"DEBUG: Saved Match {match_id} to Mongo")

async def get_match(match_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    doc = await db.matches.find_one({"match_id": match_id})
    if doc:
        logger.info(f"DEBUG: Found Match {match_id}")
        return doc.get('state_data')
    logger.warning(f"DEBUG: NOT Found Match {match_id}")
    return None
    
async def clear_all_matches():
    db = get_db()
    await db.matches.delete_many({})

async def add_mod(user_id: int):
    db = get_db()
    await db.mods.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True
    )

async def remove_mod(user_id: int):
    db = get_db()
    await db.mods.delete_one({"user_id": user_id})

async def is_mod(user_id: int) -> bool:
    db = get_db()
    doc = await db.mods.find_one({"user_id": user_id})
    return doc is not None

async def get_all_mods() -> list:
    db = get_db()
    cursor = db.mods.find({})
    return [doc['user_id'] async for doc in cursor]

async def save_chat(chat_id: int):
    db = get_db()
    await db.chats.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id}},
        upsert=True
    )

async def get_all_chats() -> list:
    db = get_db()
    cursor = db.chats.find({})
    return [doc['chat_id'] async for doc in cursor]

async def update_user_stats(user_id: int, name: str, result: str):
    db = get_db()
    inc_updates = {
        "total_matches": 1,
        "wins": 1 if result == "W" else 0,
        "losses": 1 if result == "L" else 0,
        "draws": 1 if result == "D" else 0
    }
    
    await db.users.update_one(
        {"user_id": user_id},
        {
            "$set": {"name": name, "user_id": user_id},
            "$inc": inc_updates,
            "$push": {
                "recent_results": {
                    "$each": [result],
                    "$slice": -5
                }
            }
        },
        upsert=True
    )

async def get_user_stats(user_id: int) -> Optional[Dict[str, Any]]:
    db = get_db()
    return await db.users.find_one({"user_id": user_id})

# ── Banner helpers ──────────────────────────────────────────────────────────
async def get_banner(mode: str) -> Optional[str]:
    """Return the overridden banner URL for 'mode' (ipl/intl/fifa), or None."""
    db = get_db()
    doc = await db.config.find_one({"key": f"banner_{mode}"})
    return doc["value"] if doc else None

async def set_banner(mode: str, url: str) -> None:
    """Persist a banner URL override for the given mode."""
    db = get_db()
    await db.config.update_one(
        {"key": f"banner_{mode}"},
        {"$set": {"key": f"banner_{mode}", "value": url}},
        upsert=True
    )
