# database.py
import os
import json
import logging
from typing import Optional, Dict, Any
from pymongo import MongoClient, ASCENDING
from config import MONGO_URI
from urllib.parse import urlparse
from functools import lru_cache
# Fallback for local testing (though we want to encourage Mongo now)
DB_FILE = "cricket_bot.db"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Global Client
_mongo_client = None
_db = None
def get_db():
    global _mongo_client, _db
    
    if _db is not None:
        return _db
        
    if MONGO_URI:
        try:
            # Create a connection using MongoClient
            import certifi
            _mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
            
            # Extract DB name from URI or default to 'cricket_bot'
            # Uri format: mongodb+srv://user:pass@host/dbname?params
            parsed = urlparse(MONGO_URI)
            db_name = parsed.path[1:] if parsed.path and len(parsed.path) > 1 else 'cricket_bot'
            
            _db = _mongo_client[db_name]
            logger.info(f"Connected to MongoDB: {db_name}")
            return _db
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise e
    else:
        logger.error("No MONGO_URI found!")
        raise ValueError("MONGO_URI is not set in environment.")
def init_db():
    """Initializes collections and indexes."""
    try:
        db = get_db()
        # Initialize Indexes
        db.players.create_index([("player_id", ASCENDING)], unique=True)
        db.players.create_index([("name", ASCENDING)]) # For partial search
        
        db.matches.create_index([("match_id", ASCENDING)], unique=True)
        
        db.mods.create_index([("user_id", ASCENDING)], unique=True)
        
        logger.info("MongoDB Indexes Verified.")
    except Exception as e:
        logger.error(f"DB Init Failed: {e}")
def save_player(player_data: Dict[str, Any]):
    db = get_db()
    # MongoDB stores dicts directly, no JSON stringification needed
    # But code expects `roles` as list, etc.
    # Player data passed here is already a dict, assuming it matches the model.
    # Ensure player_id is the _id or indexed
    
    # Clean data to ensure it's Mongo compatible (no specialized objects)
    # The current app passes simple dicts, so we are good.
    
    # Upsert
    db.players.update_one(
        {"player_id": player_data['player_id']},
        {"$set": player_data},
        upsert=True
    )
    # Clear cache to reflect updates
    get_player.cache_clear()
@lru_cache(maxsize=2000)
def get_player(player_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    data = db.players.find_one({"player_id": player_id})
    if data:
        # Remove _id (ObjectId)
        data.pop('_id', None)
        return data
    return None
def get_player_by_name(name_query: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    # Case-insensitive partial regex
    # Warning: Regex at start is efficient with index only if anchored, which this isn't.
    # But for 50 players it's fine.
    
    import re
    regex = re.compile(re.escape(name_query), re.IGNORECASE)
    
    data = db.players.find_one({"name": regex})
    if data:
        data.pop('_id', None)
        return data
    return None
def delete_player(player_id: str) -> bool:
    db = get_db()
    result = db.players.delete_one({"player_id": player_id})
    get_player.cache_clear()
    return result.deleted_count > 0
def get_all_players() -> list:
    db = get_db()
    cursor = db.players.find({})
    players = []
    for doc in cursor:
        doc.pop('_id', None)
        players.append(doc)
    return players
def save_match(match_id: str, chat_id: int, state_data: Dict[str, Any]):
    db = get_db()
    # Save the whole state dict
    # Add metadata fields for query convenience
    document = {
        "match_id": match_id,
        "chat_id": chat_id,
        "state_data": state_data # Storing nested or flattened?
        # SQLite used serialized JSON string for state_data.
        # Mongo can store it natively. 
        # But `game/state.py` expects to load it back.
        # If we store natively, load_match_state in state.py needs to NOT json.load it?
        # WAIT. state.py calls `get_match` which returned `json.loads(row[0])`.
        # We need `get_match` to return the dict directly.
    }
    
    db.matches.update_one(
        {"match_id": match_id},
        {"$set": {"state_data": state_data, "chat_id": chat_id}},
        upsert=True
    )
    logger.info(f"DEBUG: Saved Match {match_id} to Mongo")
def get_match(match_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    doc = db.matches.find_one({"match_id": match_id})
    if doc:
        logger.info(f"DEBUG: Found Match {match_id}")
        return doc.get('state_data')
    logger.warning(f"DEBUG: NOT Found Match {match_id}")
    return None
    
def clear_all_matches():
    db = get_db()
    db.matches.delete_many({})
def add_mod(user_id: int):
    db = get_db()
    db.mods.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True
    )
def remove_mod(user_id: int):
    db = get_db()
    db.mods.delete_one({"user_id": user_id})
def is_mod(user_id: int) -> bool:
    db = get_db()
    doc = db.mods.find_one({"user_id": user_id})
    return doc is not None
def get_all_mods() -> list:
    db = get_db()
    cursor = db.mods.find({})
    return [doc['user_id'] for doc in cursor]
