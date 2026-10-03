# database.py
import os
import json
import logging
import asyncio
import random
from collections import Counter
from typing import Optional, Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from config import MONGO_URI
from urllib.parse import urlparse
import datetime
import re

logger = logging.getLogger(__name__)

# Global Client
_mongo_client = None
_db = None

# A simple custom cache for get_player
_player_cache: Dict[str, Dict[str, Any]] = {}
CACHE_MAX_SIZE = 3500

# In-memory mode pool cache (TTL 5 min) — avoids re-fetching 500 IDs on every match load
_mode_pool_cache: Dict[str, List] = {}
_mode_pool_cache_time: Dict[str, float] = {}
MODE_POOL_CACHE_TTL = 1800  # 30 minutes (was 5min — player pool rarely changes)

def get_db():
    global _mongo_client, _db
    
    if _db is not None:
        return _db
        
    if MONGO_URI:
        try:
            import certifi
            _mongo_client = AsyncIOMotorClient(
                MONGO_URI,
                tlsCAFile=certifi.where(),
                minPoolSize=5,
                maxPoolSize=50,
                maxIdleTimeMS=30000
            )
            
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

        # ── Performance indexes added for stability ──────────────────────────
        # Speeds up count_user_active_matches, get_user_active_matches_info,
        # and _startup_recovery which all filter by state_data.state
        await db.matches.create_index([("state_data.state", ASCENDING)])
        # Speeds up per-user match lookups (join/challenge limit checks)
        await db.matches.create_index([("state_data.team_a.owner_id", ASCENDING)])
        await db.matches.create_index([("state_data.team_b.owner_id", ASCENDING)])
        # Speeds up find_and_delete_pending_challenge, get_stale_challenges
        await db.pending_challenges.create_index(
            [("owner_id", ASCENDING), ("mode", ASCENDING)], unique=True
        )
        await db.pending_challenges.create_index([("created_at", ASCENDING)])
        # Speeds up broadcast get_all_chats
        await db.chats.create_index([("chat_id", ASCENDING)], unique=True)
        
        # ── Card System Indexes ───────────────────────────────────────────────
        await db.user_cards.create_index(
            [("user_id", ASCENDING), ("player_id", ASCENDING), ("format", ASCENDING)],
            unique=True
        )
        await db.user_cards.create_index([("user_id", ASCENDING)])
        await db.active_trades.create_index([("initiator_id", ASCENDING), ("status", ASCENDING)])
        await db.active_trades.create_index([("target_id", ASCENDING), ("status", ASCENDING)])
        await db.active_trades.create_index([("expires_at", ASCENDING)])
        await db.players.create_index([("cards.ipl.rarity", ASCENDING)])
        await db.players.create_index([("cards.odi.rarity", ASCENDING)])
        await db.players.create_index([("cards.test.rarity", ASCENDING)])
        await db.players.create_index([("cards.wwe.rarity", ASCENDING)])
        await db.players.create_index([("cards.fifa.rarity", ASCENDING)])
        await db.players.create_index([("cards.pkl.rarity", ASCENDING)])
        # Ranked system indexes
        await db.users.create_index([("ranked_rp", DESCENDING)])
        # Active trades auto-cleanup (24h TTL)
        await db.active_trades.create_index([("created_at", ASCENDING)], expireAfterSeconds=86400)

        logger.info("Async MongoDB Indexes Verified.")
    except Exception as e:
        logger.error(f"DB Init Failed: {e}")

def evict_player_cache(player_id: Optional[str] = None):
    """Evict a single player from cache if player_id is provided, otherwise full clear."""
    global _player_cache
    if player_id:
        _player_cache.pop(player_id, None)
    else:
        _player_cache.clear()
        logger.info("Player cache cleared manually.")

def clear_player_cache():
    """Manually clear the player cache."""
    evict_player_cache(None)

async def save_player(player_data: Dict[str, Any]):
    db = get_db()
    await db.players.update_one(
        {"player_id": player_data['player_id']},
        {"$set": player_data},
        upsert=True
    )
    evict_player_cache(player_data.get('player_id'))

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

async def get_player_by_name_and_sport(name_query: str, sport: str) -> Optional[Dict[str, Any]]:
    """
    Sport-aware name lookup — prevents name conflicts across modes.
    sport: 'wwe', 'football', or 'cricket' (backward-compat: also matches players without sport field)
    """
    db = get_db()
    regex = re.compile(re.escape(name_query), re.IGNORECASE)
    name_filter = {"$or": [{"name": regex}, {"full_name": regex}, {"aliases": regex}]}

    if sport == "cricket":
        # Old cricket players may not have a sport field — include both
        query = {"$and": [name_filter, {"$or": [{"sport": "cricket"}, {"sport": {"$exists": False}}]}]}
    elif sport in ("kabaddi", "pkl"):
        query = {"$and": [name_filter, {"$or": [{"sport": "kabaddi"}, {"sport": "pkl"}]}]}
    elif sport in ("football", "fifa"):
        query = {"$and": [name_filter, {"$or": [{"sport": "football"}, {"sport": "fifa"}]}]}
    else:
        query = {"$and": [name_filter, {"sport": sport}]}

    data = await db.players.find_one(query)
    if data:
        data.pop('_id', None)
        return data
    return None

async def get_player_by_name(name_query: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    regex = re.compile(re.escape(name_query), re.IGNORECASE)
    
    data = await db.players.find_one({
        "$or": [
            {"name": regex},
            {"full_name": regex},
            {"aliases": regex}
        ]
    })
    if data:
        data.pop('_id', None)
        return data
    return None

async def search_players_by_name(name_query: str, sport: Optional[str] = None) -> List[Dict[str, Any]]:
    db = get_db()
    regex = re.compile(re.escape(name_query), re.IGNORECASE)
    
    query = {
        "$or": [
            {"name": regex},
            {"full_name": regex},
            {"aliases": regex}
        ]
    }
    if sport:
        sport_lower = sport.lower()
        if sport_lower in ("kabaddi", "pkl"):
            query["sport"] = {"$in": ["kabaddi", "pkl"]}
        elif sport_lower in ("football", "fifa"):
            query["sport"] = {"$in": ["football", "fifa"]}
        elif sport_lower == "cricket":
            query["$and"] = [
                {"$or": [{"name": regex}, {"full_name": regex}, {"aliases": regex}]},
                {"$or": [{"sport": "cricket"}, {"sport": {"$exists": False}}]}
            ]
            del query["$or"]
        else:
            query["sport"] = sport
        
    cursor = db.players.find(query).limit(10)
    results = []
    async for doc in cursor:
        doc.pop('_id', None)
        results.append(doc)
    return results

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
    elif mode == "WWE":
        # WWE: Men superstars (sport="wwe" and gender not female)
        query = {"sport": "wwe", "gender": {"$ne": "female"}}
    elif mode == "WWE Women":
        # WWE Women: Women superstars only
        query = {"sport": "wwe", "gender": "female"}
    elif mode in ("PKL", "Kabaddi"):
        query = {"sport": "kabaddi", "pkl_active": {"$ne": False}}
    else:
        # Cricket — map mode string to DB stats key
        _m = mode.lower()
        if _m in ('odi', 'intl', 'international'):
            search_key = 'odi'
        elif _m == 'test':
            search_key = 'test'
        else:
            search_key = _m
        query = {f"stats.{search_key}": {"$ne": None}}

    # Projection to return ONLY the player_id string
    cursor = db.players.find(query, {"player_id": 1, "_id": 0})
    async for doc in cursor:
        if "player_id" in doc:
            draft_pool_ids.append(doc["player_id"])
            
    return draft_pool_ids

async def get_cached_pool_for_mode(mode: str) -> List[str]:
    """
    Returns eligible player IDs for the given mode, using a 5-minute in-memory cache.
    Avoids re-querying MongoDB on every match load — critical for pool delta optimization.
    """
    import time
    now = time.time()
    if mode in _mode_pool_cache and (now - _mode_pool_cache_time.get(mode, 0)) < MODE_POOL_CACHE_TTL:
        return list(_mode_pool_cache[mode])  # Return a copy
    pool = await get_eligible_players_for_mode(mode)
    _mode_pool_cache[mode] = pool
    _mode_pool_cache_time[mode] = now
    logger.debug(f"Mode pool cache refreshed for {mode}: {len(pool)} players")
    return list(pool)

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
    logger.debug(f"Saved match {match_id} to Mongo")

async def get_match(match_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    doc = await db.matches.find_one({"match_id": match_id})
    if doc:
        return doc.get('state_data')
    logger.debug(f"Match not found: {match_id}")
    return None
    
async def clear_all_matches():
    db = get_db()
    await db.matches.delete_many({})

async def count_user_active_matches(user_id: int) -> int:
    """Return how many DRAFTING/READY_CHECK matches this user is currently in."""
    db = get_db()
    return await db.matches.count_documents({
        "state_data.state": {"$in": ["DRAFTING", "READY_CHECK"]},
        "$or": [
            {"state_data.team_a.owner_id": user_id},
            {"state_data.team_b.owner_id": user_id}
        ]
    })

async def get_user_active_matches_info(user_id: int) -> list:
    """Return lightweight info about a user's active matches for the block message."""
    db = get_db()
    cursor = db.matches.find(
        {
            "state_data.state": {"$in": ["DRAFTING", "READY_CHECK"]},
            "$or": [
                {"state_data.team_a.owner_id": user_id},
                {"state_data.team_b.owner_id": user_id}
            ]
        },
        {
            "state_data.mode": 1,
            "state_data.state": 1,
            "state_data.team_a.owner_id": 1,
            "state_data.team_a.owner_name": 1,
            "state_data.team_a.slots": 1,
            "state_data.team_b.owner_id": 1,
            "state_data.team_b.owner_name": 1,
            "state_data.team_b.slots": 1,
            "_id": 0
        }
    )
    return await cursor.to_list(length=10)

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

async def is_admin(user_id: int) -> bool:
    from config import OWNER_IDS
    if user_id in OWNER_IDS:
        return True
    return await is_mod(user_id)

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

async def update_user_stats(user_id: int, name: str, result: str,
                             mode: str = "", chat_id=None):
    """
    Updates user stats after a match.
    mode: 'FIFA', 'IPL', 'International', etc.
    chat_id: the group where the match was played.
    """
    import time as _t
    db = get_db()

    now = _t.time()
    is_win = result == "W"

    # --- Fetch current doc to check reset timestamps ---
    doc = await db.users.find_one({"user_id": user_id}, {
        "daily_wins": 1, "weekly_wins": 1,
        "daily_reset_at": 1, "weekly_reset_at": 1,
        "first_win_at": 1, "joined_at": 1,
        "current_streak": 1, "best_streak": 1,
        "_id": 0
    })


    # Next UTC midnight anchor
    dt = _t.gmtime(now)
    midnight = _t.mktime(_t.strptime(
        f"{dt.tm_year}-{dt.tm_mon:02d}-{dt.tm_mday:02d} 00:00:00", "%Y-%m-%d %H:%M:%S"
    ))
    if midnight <= now:
        midnight += 86400

    # Next Monday UTC anchor
    days_until_monday = (7 - dt.tm_wday) % 7 or 7
    monday = midnight + (days_until_monday - 1) * 86400

    # Determine which period counters need resetting
    daily_reset_at  = (doc or {}).get("daily_reset_at",  0)
    weekly_reset_at = (doc or {}).get("weekly_reset_at", 0)
    reset_daily  = now >= daily_reset_at
    reset_weekly = now >= weekly_reset_at

    # Determine sport
    mode_upper = mode.upper() if mode else ""
    is_fifa    = "FIFA" in mode_upper
    is_wwe     = "WWE"  in mode_upper
    is_pkl     = "PKL"  in mode_upper or "KABADDI" in mode_upper
    if is_wwe:
        sport_win_field = "wwe_wins"
    elif is_fifa:
        sport_win_field = "fifa_wins"
    elif is_pkl:
        sport_win_field = "pkl_wins"
    else:
        sport_win_field = "cricket_wins"

    # Build $set — never overlap with $inc fields
    set_updates: dict = {"name": name, "user_id": user_id}

    if reset_daily:
        # Write the final value directly into $set (avoids $set/$inc conflict)
        set_updates["daily_wins"]     = 1 if is_win else 0
        set_updates["daily_reset_at"] = midnight
    if reset_weekly:
        set_updates["weekly_wins"]     = 1 if is_win else 0
        set_updates["weekly_reset_at"] = monday

    if not doc:
        # New user — set anchor timestamps only.
        # DO NOT initialise cricket_wins/fifa_wins in $set — $inc handles them
        # (MongoDB auto-creates missing fields starting from 0).
        if not reset_daily:
            set_updates["daily_reset_at"]  = midnight
        if not reset_weekly:
            set_updates["weekly_reset_at"] = monday

    if is_win:
        set_updates["last_win_at"] = now
        if not (doc and doc.get("first_win_at")):
            set_updates["first_win_at"] = now

    # Build $inc — only fields NOT already handled by $set above
    inc_updates: dict = {
        "total_matches": 1,
        "wins":    1 if is_win else 0,
        "losses":  1 if result == "L" else 0,
        "draws":   1 if result == "D" else 0,
    }
    # sport_win_field: only add to $inc if NOT already in $set
    if sport_win_field not in set_updates:
        inc_updates[sport_win_field] = 1 if is_win else 0
    # daily_wins / weekly_wins: only $inc if not reset (already written via $set)
    if not reset_daily:
        inc_updates["daily_wins"]  = 1 if is_win else 0
    if not reset_weekly:
        inc_updates["weekly_wins"] = 1 if is_win else 0

    ops: dict = {
        "$set": set_updates,
        "$inc": inc_updates,
        "$push": {
            "recent_results": {
                "$each": [result],
                "$slice": -5
            }
        }
    }

    # Per-chat wins (only for wins)
    if is_win and chat_id:
        ops["$inc"][f"chat_wins.{chat_id}"] = 1

    # Streak tracking
    current_streak = (doc.get("current_streak", 0) if doc else 0)
    if is_win:
        current_streak += 1
    else:
        current_streak = 0
    set_updates["current_streak"] = current_streak
    best_streak = doc.get("best_streak", 0) if doc else 0
    if current_streak > best_streak:
        set_updates["best_streak"] = current_streak

    # Join date — set only once on first match
    if not doc or not doc.get("joined_at"):
        set_updates["joined_at"] = now

    await db.users.update_one({"user_id": user_id}, ops, upsert=True)

    # Invalidate leaderboard cache
    try:
        from handlers.standings import invalidate_lb_cache
        invalidate_lb_cache()
    except Exception:
        pass

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

# ── Pending Challenge persistence (survives restarts) ───────────────────────
import time as _time_mod

async def save_pending_challenge(owner_id: int, chat_id: int, message_id: int, mode: str) -> None:
    """Upsert a pending challenge so startup_recovery can expire it on restart."""
    db = get_db()
    await db.pending_challenges.update_one(
        {"owner_id": owner_id, "mode": mode},
        {"$set": {
            "owner_id": owner_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "mode": mode,
            "created_at": _time_mod.time()
        }},
        upsert=True
    )

async def find_and_delete_pending_challenge(owner_id: int, mode: str) -> Optional[dict]:
    """Atomically find and delete a pending challenge to prevent double-joins."""
    db = get_db()
    return await db.pending_challenges.find_one_and_delete({"owner_id": owner_id, "mode": mode})

async def delete_pending_challenge(owner_id: int, mode: str = None) -> None:
    """Remove a pending challenge (joined or naturally expired)."""
    db = get_db()
    query = {"owner_id": owner_id}
    if mode:
        query["mode"] = mode
    await db.pending_challenges.delete_one(query)

async def get_stale_challenges(expiry_secs: int = 120) -> list:
    """Return all challenges older than expiry_secs seconds."""
    db = get_db()
    cutoff = _time_mod.time() - expiry_secs
    cursor = db.pending_challenges.find({"created_at": {"$lt": cutoff}})
    return await cursor.to_list(length=200)

# ═══════════════════════════════════════════════════════════════════════════
# CARD SYSTEM — Database Functions
# ═══════════════════════════════════════════════════════════════════════════

import time as _time
import random

# ── Card Coins ──────────────────────────────────────────────────────────────

async def get_card_coins(user_id: int) -> int:
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"card_coins": 1})
    return int(doc.get("card_coins", 0)) if doc else 0

async def add_card_coins(user_id: int, amount: int) -> int:
    """Add card coins to user (or deduct if amount < 0, clamped at 0 minimum). Returns new balance."""
    db = get_db()
    if amount < 0:
        # Atomic deduction clamped to min 0
        result = await db.users.find_one_and_update(
            {"user_id": user_id, "card_coins": {"$gte": abs(amount)}},
            {"$inc": {"card_coins": amount}},
            return_document=True
        )
        if result is None:
            # If user has fewer coins than the deduction, floor at 0
            result = await db.users.find_one_and_update(
                {"user_id": user_id},
                {"$set": {"card_coins": 0}},
                upsert=True,
                return_document=True
            )
        return int(result.get("card_coins", 0)) if result else 0

    result = await db.users.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"card_coins": amount}},
        upsert=True,
        return_document=True
    )
    return int(result.get("card_coins", 0))

async def deduct_card_coins(user_id: int, amount: int) -> tuple[bool, int]:
    """Deduct card coins. Returns (success, new_balance). Fails if insufficient."""
    db = get_db()
    # Atomic check-and-deduct
    result = await db.users.find_one_and_update(
        {"user_id": user_id, "card_coins": {"$gte": amount}},
        {"$inc": {"card_coins": -amount}},
        return_document=True
    )
    if result is None:
        balance = await get_card_coins(user_id)
        return False, balance
    return True, int(result.get("card_coins", 0))

# ── Pack Inventory ───────────────────────────────────────────────────────────

async def get_pack_inventory(user_id: int) -> dict:
    """Returns {basic: int, premium: int, elite: int, sport selections preserved}."""
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"pack_inventory": 1})
    default = {"basic": 0, "premium": 0, "elite": 0}
    if not doc or "pack_inventory" not in doc:
        return default
    inv = doc["pack_inventory"]
    return {
        "basic":   int(inv.get("basic", 0)),
        "premium": int(inv.get("premium", 0)),
        "elite":   int(inv.get("elite", 0)),
    }

async def add_pack_to_user(user_id: int, pack_type: str) -> int:
    """Add one pack of given type to user inventory and return new count."""
    db = get_db()
    result = await db.users.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {f"pack_inventory.{pack_type}": 1}},
        upsert=True,
        return_document=True
    )
    inv = result.get("pack_inventory", {}) if result else {}
    return int(inv.get(pack_type, 1))

add_pack = add_pack_to_user

async def use_pack(user_id: int, pack_type: str) -> bool:
    """Atomically consume one pack. Returns True if successful."""
    db = get_db()
    result = await db.users.find_one_and_update(
        {"user_id": user_id, f"pack_inventory.{pack_type}": {"$gt": 0}},
        {"$inc": {f"pack_inventory.{pack_type}": -1}},
        return_document=True
    )
    return result is not None

async def add_pack_to_all_users(pack_type: str) -> int:
    """Give one pack of type to every user. Returns count of users updated."""
    db = get_db()
    result = await db.users.update_many(
        {},
        {"$inc": {f"pack_inventory.{pack_type}": 1}}
    )
    return result.modified_count

# ── User Cards Collection ────────────────────────────────────────────────────

async def get_user_cards(user_id: int, sport_filter: str = None) -> list:
    """
    Returns list of card dicts with player info attached.
    Each entry: {user_id, player_id, format, quantity, name, rarity, ovr, image}
    sport_filter: 'cricket' | 'football' | 'wwe' | None (all)

    Uses a single batch $in query for all players instead of N individual
    get_player() calls — dramatically reduces DB round-trips for large collections.
    """
    db = get_db()
    cards = await db.user_cards.find({"user_id": user_id}).to_list(None)
    if not cards:
        return []

    # Batch-fetch all referenced players in ONE query
    player_ids = list({c["player_id"] for c in cards})
    player_docs = await db.players.find({"player_id": {"$in": player_ids}}).to_list(None)
    players_by_id = {p["player_id"]: p for p in player_docs}

    result = []
    for card in cards:
        pid = card["player_id"]
        fmt = card.get("format", "")
        p = players_by_id.get(pid)
        if not p:
            continue

        # Determine canonical sport for this card from format first, then player doc
        if fmt in ("ipl", "odi", "test"):
            card_sport = "cricket"
        elif fmt == "fifa":
            card_sport = "football"
        elif fmt == "pkl":
            card_sport = "kabaddi"
        elif fmt == "wwe":
            card_sport = "wwe"
        else:
            ps = str(p.get("sport") or "").lower()
            if ps in ("fifa", "football"):
                card_sport = "football"
            elif ps in ("pkl", "kabaddi"):
                card_sport = "kabaddi"
            elif ps == "wwe":
                card_sport = "wwe"
            else:
                card_sport = "cricket"

        if sport_filter and sport_filter != "all":
            sf = str(sport_filter).lower()
            if sf in ("cricket", "ipl", "odi", "test"):
                target_sport = "cricket"
            elif sf in ("football", "fifa"):
                target_sport = "football"
            elif sf in ("kabaddi", "pkl"):
                target_sport = "kabaddi"
            elif sf == "wwe":
                target_sport = "wwe"
            else:
                target_sport = sf
            if card_sport != target_sport:
                continue

        cards_dict = p.get("cards") if isinstance(p.get("cards"), dict) else {}
        card_data = cards_dict.get(fmt, {}) if isinstance(cards_dict.get(fmt), dict) else {}
        rarity = card_data.get("rarity") or p.get("rarity") or "common"
        ovr = card_data.get("ovr") or p.get("ovr") or 70
        image = _get_card_image(p, fmt)
        result.append({
            "user_id":   user_id,
            "player_id": pid,
            "format":    fmt,
            "quantity":  card.get("quantity", 1),
            "name":      p.get("name", "Unknown"),
            "rarity":    rarity,
            "ovr":       ovr,
            "image":     image,
        })
    return result

def _get_card_image(player_doc: dict, fmt: str) -> Optional[str]:
    """Get best available image for a player-format card.
    Priority: format-specific URL -> format-specific file_id -> generic file_id.
    Returns a URL string (http) or a Telegram file_id string.
    """
    if fmt == "ipl":
        return (player_doc.get("ipl_image_url") or
                player_doc.get("image_url") or
                player_doc.get("ipl_image_file_id") or
                player_doc.get("image_file_id"))
    elif fmt == "odi":
        return (player_doc.get("odi_image_url") or
                player_doc.get("image_url") or
                player_doc.get("odi_image_file_id") or
                player_doc.get("image_file_id"))
    elif fmt == "test":
        return (player_doc.get("test_image_url") or
                player_doc.get("image_url") or
                player_doc.get("image_file_id"))
    elif fmt == "wwe":
        return (player_doc.get("wwe_image_url") or
                player_doc.get("image_url") or
                player_doc.get("image_file_id"))
    elif fmt == "fifa":
        url = player_doc.get("fifa_image_url") or player_doc.get("image_url")
        if url and str(url).startswith("http") and "ratings-images-prod.pulse.ea.com" not in str(url):
            return url
        return (player_doc.get("image_file_id") or
                player_doc.get("fifa_image_url") or
                player_doc.get("image_url"))
    elif fmt == "pkl":
        cards_dict = player_doc.get("cards") if isinstance(player_doc.get("cards"), dict) else {}
        pkl_card = cards_dict.get("pkl") if isinstance(cards_dict.get("pkl"), dict) else {}
        return (pkl_card.get("image") or
                player_doc.get("pkl_image_url") or
                player_doc.get("image_url") or
                player_doc.get("image_file_id"))
    return player_doc.get("image_url") or player_doc.get("image_file_id")

def _get_card_image_url(player_doc: dict, fmt: str) -> Optional[str]:
    """Get direct web URL for a player-format card (for href preview).
    Returns an http URL if available, otherwise None (caller should fall back to file_id).
    """
    if fmt == "ipl":
        url = player_doc.get("ipl_image_url") or player_doc.get("image_url")
    elif fmt == "odi":
        url = player_doc.get("odi_image_url") or player_doc.get("image_url")
    elif fmt == "test":
        url = player_doc.get("test_image_url") or player_doc.get("image_url")
    elif fmt == "wwe":
        url = player_doc.get("wwe_image_url") or player_doc.get("image_url")
    elif fmt == "fifa":
        url = player_doc.get("fifa_image_url") or player_doc.get("image_url")
        # If the URL is the default EA FC gold base card and user has an image_file_id, fall back to file_id
        if url and "ratings-images-prod.pulse.ea.com" in url and player_doc.get("image_file_id"):
            return None
        return url if url and str(url).startswith("http") else None
    elif fmt == "pkl":
        cards_dict = player_doc.get("cards") if isinstance(player_doc.get("cards"), dict) else {}
        pkl_card = cards_dict.get("pkl") if isinstance(cards_dict.get("pkl"), dict) else {}
        url = (pkl_card.get("image") or
               player_doc.get("pkl_image_url") or
               player_doc.get("image_url"))
    else:
        url = player_doc.get("image_url")
    return url if url and url.startswith("http") else None




async def get_user_card(user_id: int, player_id: str, fmt: str) -> Optional[dict]:
    """Get a single user card entry or None."""
    db = get_db()
    return await db.user_cards.find_one({"user_id": user_id, "player_id": player_id, "format": fmt})

async def add_card_to_user(user_id: int, player_id: str, fmt: str, quantity: int = 1) -> int:
    """Add one or more copies of a card. Returns new quantity."""
    db = get_db()
    result = await db.user_cards.find_one_and_update(
        {"user_id": user_id, "player_id": player_id, "format": fmt},
        {"$inc": {"quantity": quantity}},
        upsert=True,
        return_document=True
    )
    return result["quantity"]

async def remove_card_from_user(user_id: int, player_id: str, fmt: str) -> int:
    """Remove one copy. Deletes doc if quantity reaches 0. Returns remaining quantity."""
    db = get_db()
    # Decrement
    result = await db.user_cards.find_one_and_update(
        {"user_id": user_id, "player_id": player_id, "format": fmt, "quantity": {"$gt": 0}},
        {"$inc": {"quantity": -1}},
        return_document=True
    )
    if result is None:
        return -1
    new_qty = result["quantity"]

    if new_qty <= 0:
        await db.user_cards.delete_one({"user_id": user_id, "player_id": player_id, "format": fmt})
        return 0
    return new_qty

async def count_user_cards(user_id: int) -> int:
    """Total number of card copies owned."""
    db = get_db()
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}}
    ]
    result = await db.user_cards.aggregate(pipeline).to_list(1)
    return result[0]["total"] if result else 0

# ── Favourite Card ────────────────────────────────────────────────────────────

async def get_fav_card(user_id: int) -> Optional[dict]:
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"fav_card": 1})
    return doc.get("fav_card") if doc else None

async def set_fav_card(user_id: int, player_id: str, fmt: str) -> None:
    db = get_db()
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"fav_card": {"player_id": player_id, "format": fmt}}},
        upsert=True
    )

async def clear_fav_card(user_id: int) -> None:
    db = get_db()
    await db.users.update_one(
        {"user_id": user_id},
        {"$unset": {"fav_card": ""}}
    )

# ── Daily Quests ──────────────────────────────────────────────────────────────

def _next_midnight_utc() -> float:
    """Returns the Unix timestamp of the next midnight UTC.
    Everyone resets at the same wall-clock time — no more per-user rolling windows.
    """
    import datetime as _dt
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    tomorrow = (now_utc + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.timestamp()

QUEST_DEFINITIONS = {
    "obtain_2": {"label": "Obtain 2 cards via pack",  "field": "cards_obtained",  "target": 2,   "reward": 10},
    "obtain_5": {"label": "Obtain 5 cards via pack",  "field": "cards_obtained",  "target": 5,   "reward": 20},
    "trade_1":  {"label": "Trade 1 card",             "field": "cards_traded",    "target": 1,   "reward": 20},
    "sell_3":   {"label": "Sell 3 cards",             "field": "cards_sold",      "target": 3,   "reward": 20},
    "bbet_500": {"label": "Bet 500🪙 in /bbet",       "field": "bbet_coins_spent","target": 500, "reward": 50},
    "streak_3": {"label": "Win 3 matches in a row today", "field": "win_streak",  "target": 3,   "reward": 50},
}

async def get_daily_quests(user_id: int) -> dict:
    """
    Returns quest state. Auto-resets at midnight UTC (same time for all users).
    Structure: {reset_at, cards_obtained, cards_traded, cards_sold, claimed: []}
    """
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"daily_quests": 1})
    now = _time.time()

    default = {
        "reset_at":         _next_midnight_utc(),
        "cards_obtained":   0,
        "cards_traded":     0,
        "cards_sold":       0,
        "bbet_coins_spent": 0,
        "win_streak":       0,
        "claimed":          [],
    }

    if not doc or "daily_quests" not in doc:
        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {"daily_quests": default}},
            upsert=True
        )
        return default

    quests = doc["daily_quests"]
    # Check if reset needed
    if now >= quests.get("reset_at", 0):
        new_quests = {
            "reset_at":         _next_midnight_utc(),
            "cards_obtained":   0,
            "cards_traded":     0,
            "cards_sold":       0,
            "bbet_coins_spent": 0,
            "win_streak":       0,
            "claimed":          [],
        }

        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {"daily_quests": new_quests}}
        )
        return new_quests
    return quests

async def increment_quest_progress(user_id: int, field: str, amount: int = 1) -> None:
    """Increment a quest progress counter (cards_obtained / cards_traded / cards_sold)."""
    db = get_db()
    # Only increment if quests are not reset (ensure doc exists)
    await get_daily_quests(user_id)  # ensures doc + handles reset
    await db.users.update_one(
        {"user_id": user_id},
        {"$inc": {f"daily_quests.{field}": amount}}
    )

async def claim_quest_rewards(user_id: int) -> tuple[int, list]:
    """
    Claims all completed, unclaimed quest rewards.
    Returns (total_coins_awarded, list_of_claimed_quest_keys).
    """
    quests = await get_daily_quests(user_id)
    claimed = quests.get("claimed", [])
    total_coins = 0
    newly_claimed = []

    for key, defn in QUEST_DEFINITIONS.items():
        if key in claimed:
            continue  # already claimed
        progress = quests.get(defn["field"], 0)
        if progress >= defn["target"]:
            total_coins += defn["reward"]
            newly_claimed.append(key)

    if not newly_claimed:
        return 0, []

    # Mark as claimed + award coins atomically
    db = get_db()
    await db.users.update_one(
        {"user_id": user_id},
        {
            "$push": {"daily_quests.claimed": {"$each": newly_claimed}},
            "$inc":  {"card_coins": total_coins}
        }
    )
    return total_coins, newly_claimed

# ── Card Catalog ──────────────────────────────────────────────────────────────

async def get_card_catalog_entry(player_id: str, fmt: str) -> Optional[dict]:
    """Returns {ovr, rarity} or None if not in catalog."""
    db = get_db()
    doc = await db.players.find_one(
        {"player_id": player_id},
        {f"cards.{fmt}": 1}
    )
    if not doc:
        return None
    return doc.get("cards", {}).get(fmt)

async def add_to_card_catalog(player_id: str, fmt: str, ovr: int, rarity: str) -> bool:
    """Add a player-format to card catalog. Returns False if already exists."""
    # Check existing
    existing = await get_card_catalog_entry(player_id, fmt)
    if existing:
        return False
    db = get_db()
    await db.players.update_one(
        {"player_id": player_id},
        {"$set": {f"cards.{fmt}": {"ovr": ovr, "rarity": rarity.lower()}}}
    )
    evict_player_cache(player_id)
    return True

async def update_card_catalog(player_id: str, fmt: str, ovr: int, rarity: str) -> bool:
    """Update a player-format in catalog. Returns False if not found."""
    existing = await get_card_catalog_entry(player_id, fmt)
    if not existing:
        return False
    db = get_db()
    await db.players.update_one(
        {"player_id": player_id},
        {"$set": {f"cards.{fmt}": {"ovr": ovr, "rarity": rarity.lower()}}}
    )
    evict_player_cache(player_id)
    return True

# ── Card Pack Drawing ─────────────────────────────────────────────────────────

PACK_ODDS = {
    "basic":   {"legend": 0,  "epic": 5,  "rare": 35, "common": 60},
    "premium": {"legend": 5,  "epic": 35, "rare": 45, "common": 15},
    "elite":   {"legend": 30, "epic": 55, "rare": 15, "common": 0},
}

_card_pool_cache: dict = {}  # sport -> list of {player_id, name, format, rarity, ovr, image}
_card_pool_cache_time: dict = {}
CARD_POOL_CACHE_TTL = 3600  # 1 hour (invalidated explicitly when cards are added/updated)

async def _build_card_pool(sport: str) -> list:
    """Build and cache the drawable card pool for a sport."""
    now = _time.time()
    if sport in _card_pool_cache and (now - _card_pool_cache_time.get(sport, 0)) < CARD_POOL_CACHE_TTL:
        return _card_pool_cache[sport]

    db = get_db()
    pool = []

    if sport == "cricket":
        query = {"sport": {"$nin": ["wwe", "football", "kabaddi"]}, "cards": {"$exists": True}}
        formats = ["ipl", "odi", "test"]
    elif sport == "wwe":
        query = {"sport": "wwe", "gender": {"$ne": "female"}, "cards": {"$exists": True}}
        formats = ["wwe"]
    elif sport == "football":
        query = {"sport": "football", "cards": {"$exists": True}}
        formats = ["fifa"]
    elif sport in ("kabaddi", "pkl"):
        query = {"sport": "kabaddi", "cards": {"$exists": True}}
        formats = ["pkl"]
    else:
        return []

    async for p in db.players.find(query, {"player_id": 1, "name": 1, "cards": 1,
                                            "ipl_image_url": 1, "odi_image_url": 1, "image_url": 1,
                                            "ipl_image_file_id": 1, "image_file_id": 1,
                                            "wwe_image_url": 1, "fifa_image_url": 1,
                                            "test_image_url": 1, "pkl_image_url": 1}):

        for fmt in formats:
            card_data = p.get("cards", {}).get(fmt)
            if not card_data:
                continue
            pool.append({
                "player_id": p["player_id"],
                "name":      p["name"],
                "format":    fmt,
                "rarity":    card_data.get("rarity", "common"),
                "ovr":       card_data.get("ovr", 0),
                "image":     _get_card_image(p, fmt),
            })

    _card_pool_cache[sport] = pool
    _card_pool_cache_time[sport] = now
    return pool

def _invalidate_card_pool_cache():
    """Call after /add_card or /update_card to refresh pool."""
    global _catalog_totals_cache, _catalog_totals_time
    _card_pool_cache.clear()
    _card_pool_cache_time.clear()
    _catalog_totals_cache = {}
    _catalog_totals_time = 0.0

_catalog_totals_cache: dict = {}
_catalog_totals_time: float = 0.0

async def get_catalog_totals() -> dict:
    """
    Returns total cards available in catalog:
    {
        'by_format_rarity': {(fmt, rarity): int},
        'by_format': {fmt: int},
        'grand_total': int
    }
    Cached in memory for 10 minutes.
    """
    global _catalog_totals_cache, _catalog_totals_time
    now = _time.time()
    if _catalog_totals_cache and (now - _catalog_totals_time) < 600:
        return _catalog_totals_cache

    pools = [
        await _build_card_pool("cricket"),
        await _build_card_pool("football"),
        await _build_card_pool("wwe"),
        await _build_card_pool("kabaddi")
    ]
    by_fmt_rarity = {}
    by_fmt = {}
    grand = 0
    for pool in pools:
        for card in pool:
            fmt = card["format"]
            rarity = card["rarity"]
            by_fmt_rarity[(fmt, rarity)] = by_fmt_rarity.get((fmt, rarity), 0) + 1
            by_fmt[fmt] = by_fmt.get(fmt, 0) + 1
            grand += 1

    _catalog_totals_cache = {
        "by_format_rarity": by_fmt_rarity,
        "by_format": by_fmt,
        "grand_total": grand
    }
    _catalog_totals_time = now
    return _catalog_totals_cache

async def warmup_card_pools() -> None:
    """
    Pre-fetch all card pools into RAM cache.
    Call on startup and periodically (every ~4 min) so players never
    experience a cold-cache DB scan when drawing from a pack or starting a draft.
    Silently swallows errors — this is a background optimisation, not critical path.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)
    for sport in ("cricket", "football", "wwe", "kabaddi"):
        try:
            pool = await _build_card_pool(sport)
            _logger.debug(f"Card pool warmed: {sport} ({len(pool)} cards)")
        except Exception as e:
            _logger.warning(f"Card pool warmup failed for {sport}: {e}")


async def draw_pack_cards(pack_type: str, sport: str, count: int = 3) -> list:
    """
    Draw `count` cards from the pool for given pack_type and sport.
    Returns list of card dicts. Empty list if pool is empty.
    """
    pool = await _build_card_pool(sport)
    if not pool:
        return []

    drawn = []
    odds = PACK_ODDS[pack_type]

    for _ in range(count):
        # Roll rarity
        roll = random.randint(1, 100)
        cumulative = 0
        rarity = "common"
        for r in ["legend", "epic", "rare", "common"]:
            cumulative += odds[r]
            if roll <= cumulative:
                rarity = r
                break

        # Pick random card of that rarity
        rarity_pool = [c for c in pool if c["rarity"] == rarity]
        # Fallback to next rarity up if rarity pool empty
        if not rarity_pool:
            for fallback in ["rare", "epic", "legend", "common"]:
                rarity_pool = [c for c in pool if c["rarity"] == fallback]
                if rarity_pool:
                    break
        if not rarity_pool:
            continue
        drawn.append(random.choice(rarity_pool))

    return drawn

# ── Active Trades ─────────────────────────────────────────────────────────────

async def create_trade(data: dict) -> str:
    """Insert a new trade. Returns trade_id."""
    db = get_db()
    await db.active_trades.insert_one(data)
    return data["trade_id"]

async def get_trade(trade_id: str) -> Optional[dict]:
    db = get_db()
    doc = await db.active_trades.find_one({"trade_id": trade_id})
    if doc:
        doc.pop("_id", None)
    return doc

async def update_trade(trade_id: str, update_data: dict) -> None:
    db = get_db()
    await db.active_trades.update_one(
        {"trade_id": trade_id},
        {"$set": update_data}
    )

async def get_user_active_trade(user_id: int) -> Optional[dict]:
    """Get the active trade for a user (initiator or target), if any.
    Automatically excludes trades older than 5 minutes so users are never
    permanently blocked by a forgotten/abandoned trade.
    """
    import time as _t
    db = get_db()
    cutoff = _t.time() - 300  # 5 minutes
    doc = await db.active_trades.find_one({
        "$or": [{"initiator_id": user_id}, {"target_id": user_id}],
        "status": {"$in": ["awaiting_target_pick", "awaiting_confirmation", "completing"]},
        "created_at": {"$gte": cutoff}   # ← exclude trades older than 5 min
    })
    if doc:
        doc.pop("_id", None)
    return doc

async def cancel_trade(trade_id: str) -> None:
    db = get_db()
    await db.active_trades.update_one(
        {"trade_id": trade_id},
        {"$set": {"status": "cancelled"}}
    )

async def expire_old_trades() -> int:
    """Cancel all trades older than 5 minutes. Returns count cancelled."""
    db = get_db()
    cutoff = _time.time() - 300  # 5 minutes
    result = await db.active_trades.update_many(
        {
            "created_at": {"$lt": cutoff},
            "status": {"$in": ["awaiting_target_pick", "awaiting_confirmation"]}
        },
        {"$set": {"status": "expired"}}
    )
    return result.modified_count

# ── Admin: Gift Coins ─────────────────────────────────────────────────────────

async def gift_card_coins(target_user_id: int, amount: int) -> int:
    """Admin gift: add coins to any user by Telegram ID. Returns new balance."""
    return await add_card_coins(target_user_id, amount)

# ── Atomic Action Cooldown (spam-click protection) ────────────────────────────

async def try_acquire_action_cooldown(user_id: int, action: str, cooldown_seconds: int = 5) -> bool:
    """
    Atomically acquire a per-user per-action cooldown stored in MongoDB.
    Returns True if the action is allowed to proceed (cooldown not active).
    Returns False if the user is still within the cooldown window.

    This is the primary defense against spam-clicks that arrive as sequential
    Telegram updates (which bypass in-memory asyncio.Lock checks).
    """
    import time as _time
    db = get_db()
    now = _time.time()
    cutoff = now - cooldown_seconds
    field = f"cooldowns.{action}"

    result = await db.users.find_one_and_update(
        {
            "user_id": user_id,
            "$or": [
                {field: {"$exists": False}},
                {field: {"$lt": cutoff}},
            ]
        },
        {"$set": {field: now}},
        upsert=False,           # User must already exist
        return_document=False,  # We only need modified_count logic
    )
    # find_one_and_update returns the document if matched, None if no match
    return result is not None

# ═══════════════════════════════════════════════════════════════════════════
# FAV CARD VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

async def validate_fav_card(user_id: int) -> Optional[dict]:
    """
    Returns the fav card dict only if the user still owns at least 1 copy.
    If the card was traded/sold, auto-clears the stale fav and returns None.
    """
    fav = await get_fav_card(user_id)
    if not fav:
        return None
    db = get_db()
    owned = await db.user_cards.find_one(
        {"user_id": user_id, "player_id": fav["player_id"],
         "format": fav["format"], "quantity": {"$gt": 0}}
    )
    if not owned:
        await clear_fav_card(user_id)
        return None
    return fav

# ═══════════════════════════════════════════════════════════════════════════
# RANKED & EXP PROGRESSION SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

RANK_TIERS = [
    # (name, badge, min_rp, max_rp, order)
    ("Unranked",     "🔘", 0,     0,      0),
    ("Bronze III",   "🥉", 1,     224,    1),
    ("Bronze II",    "🥉", 225,   449,    2),
    ("Bronze I",     "🥉", 450,   674,    3),
    ("Silver III",   "🥈", 675,   899,    4),
    ("Silver II",    "🥈", 900,   1124,   5),
    ("Silver I",     "🥈", 1125,  1349,   6),
    ("Gold III",     "🥇", 1350,  1574,   7),
    ("Gold II",      "🥇", 1575,  1799,   8),
    ("Gold I",       "🥇", 1800,  2024,   9),
    ("Platinum III", "💎", 2025,  2249,   10),
    ("Platinum II",  "💎", 2250,  2474,   11),
    ("Platinum I",   "💎", 2475,  2699,   12),
    ("Diamond III",  "🔥", 2700,  2924,   13),
    ("Diamond II",   "🔥", 2925,  3149,   14),
    ("Diamond I",    "🔥", 3150,  3374,   15),
    ("Master III",   "👑", 3375,  3599,   16),
    ("Master II",    "👑", 3600,  3824,   17),
    ("Master I",     "👑", 3825,  4049,   18),
    ("Champion",     "⚡", 4050,  4499,   19),
    ("Legend",       "🌟", 4500,  999999, 20),
]

def get_rank_tier(rp: int) -> tuple:
    """Returns (name, badge, min_rp, max_rp, order)."""
    if rp <= 0:
        return RANK_TIERS[0]
    for tier in reversed(RANK_TIERS):
        if rp >= tier[2]:
            return tier
    return RANK_TIERS[0]

def get_next_rank_tier(rp: int) -> Optional[tuple]:
    """Returns the next tier above current RP, or None if Legend."""
    if rp <= 0:
        return RANK_TIERS[1]  # Bronze III
    for i, tier in enumerate(RANK_TIERS):
        if tier[2] <= rp <= tier[3]:
            if i + 1 < len(RANK_TIERS):
                return RANK_TIERS[i + 1]
            return None
    return None

def exp_required_for_next_level(level: int) -> int:
    """Formula: 500 + (level - 1) * 200."""
    return 500 + max(0, level - 1) * 200

def calculate_match_exp(is_winner: bool, is_draw: bool, positions_won: int) -> int:
    """
    Winner: 50 base + 5 * positions won
    Loser: 5 * positions won
    Draw: 40
    """
    if is_draw:
        return 40
    if is_winner:
        return 50 + max(0, positions_won) * 5
    return max(0, positions_won) * 5

def process_exp_gain(current_level: int, current_exp: int, exp_gained: int) -> tuple[int, int, bool]:
    """
    Adds EXP and handles level rollover.
    Returns (new_level, new_exp, did_level_up).
    """
    lvl = max(1, current_level)
    xp = max(0, current_exp) + exp_gained
    leveled_up = False
    while True:
        req = exp_required_for_next_level(lvl)
        if xp >= req:
            xp -= req
            lvl += 1
            leveled_up = True
        else:
            break
    return lvl, xp, leveled_up

async def update_ranked_and_exp(
    user_id: int, user_name: str, rp_delta: int, exp_gained: int
) -> dict:
    """
    Atomically updates ranked RP and EXP for a user.
    Handles RP floor (>= 0), peak RP tracking, level rollover, and detects promotions/demotions.
    Returns summary dict for notifications.
    """
    db = get_db()
    user_doc = await db.users.find_one(
        {"user_id": user_id},
        {"ranked_rp": 1, "peak_rp": 1, "level": 1, "current_exp": 1, "_id": 0}
    )
    if not user_doc:
        user_doc = {}

    old_rp = user_doc.get("ranked_rp", 0)
    old_peak_rp = user_doc.get("peak_rp", old_rp)
    old_lvl = user_doc.get("level", 1)
    old_exp = user_doc.get("current_exp", 0)

    # Calculate new RP
    new_rp = max(0, old_rp + rp_delta)
    new_peak_rp = max(old_peak_rp, new_rp)

    # Calculate new Level & EXP
    new_lvl, new_exp, did_level_up = process_exp_gain(old_lvl, old_exp, exp_gained)

    # Detect promotion/demotion
    old_tier = get_rank_tier(old_rp)
    new_tier = get_rank_tier(new_rp)
    promoted = new_tier[4] > old_tier[4]
    demoted  = new_tier[4] < old_tier[4]

    # Atomic DB update
    await db.users.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "name": user_name,
                "ranked_rp": new_rp,
                "peak_rp": new_peak_rp,
                "level": new_lvl,
                "current_exp": new_exp,
            }
        },
        upsert=True
    )

    return {
        "user_id": user_id,
        "old_rp": old_rp,
        "new_rp": new_rp,
        "rp_delta": rp_delta,
        "old_tier": old_tier,
        "new_tier": new_tier,
        "promoted": promoted,
        "demoted": demoted,
        "old_level": old_lvl,
        "new_level": new_lvl,
        "leveled_up": did_level_up,
        "exp_gained": exp_gained,
        "current_exp": new_exp,
        "exp_req": exp_required_for_next_level(new_lvl),
    }

# ── Season Management ─────────────────────────────────────────────────────────

SEASON_DURATION_SECONDS = 60 * 86400  # 60 days (2 months)
OFF_SEASON_DURATION_SECONDS = 86400   # 24 hours

SOFT_RESET_TIER_RP = {
    # Tier Order -> Starting RP in next season
    20: 2700,  # Legend -> Diamond III (2,700)
    19: 2475,  # Champion -> Platinum I (2,475)
    18: 1800,  # Master I -> Gold I (1,800)
    17: 1800,  # Master II -> Gold I
    16: 1800,  # Master III -> Gold I
    15: 1350,  # Diamond I -> Gold III (1,350)
    14: 1350,  # Diamond II -> Gold III
    13: 1350,  # Diamond III -> Gold III
    12: 1125,  # Platinum I -> Silver I (1,125)
    11: 1125,  # Platinum II -> Silver I
    10: 1125,  # Platinum III -> Silver I
    9:  675,   # Gold I -> Silver III (675)
    8:  675,   # Gold II -> Silver III
    7:  675,   # Gold III -> Silver III
    6:  450,   # Silver I -> Bronze I (450)
    5:  450,   # Silver II -> Bronze I
    4:  450,   # Silver III -> Bronze I
    3:  0,     # Bronze I -> 0
    2:  0,     # Bronze II -> 0
    1:  0,     # Bronze III -> 0
    0:  0,     # Unranked -> 0
}

async def get_season_info() -> dict:
    """Returns current season status document from db.config."""
    db = get_db()
    doc = await db.config.find_one({"key": "ranked_season"})
    now = _time.time()
    if not doc:
        # Initialize Season 1 starting now
        doc = {
            "key": "ranked_season",
            "season_number": 1,
            "season_start": now,
            "season_end": now + SEASON_DURATION_SECONDS,
            "is_off_season": False,
            "off_season_end": 0.0,
        }
        await db.config.update_one({"key": "ranked_season"}, {"$set": doc}, upsert=True)

    if doc.get("is_off_season", False):
        doc["time_remaining"] = max(0.0, float(doc.get("off_season_end", 0.0)) - now)
    else:
        doc["time_remaining"] = max(0.0, float(doc.get("season_end", 0.0)) - now)

    return doc

async def check_and_process_season_transition(bot=None) -> None:
    """
    Checks if the season has ended and transitions to off-season, distributes rewards,
    or starts the new season after off-season expires.
    """
    try:
        db = get_db()
        season = await get_season_info()
        now = _time.time()

        # Case 1: Active season has expired -> enter off-season & distribute rewards
        if not season.get("is_off_season", False) and now >= season.get("season_end", 0):
            logger.info(f"Season {season.get('season_number', 1)} concluded! Starting reward distribution & off-season.")
            await _distribute_season_rewards_and_soft_reset(season.get("season_number", 1), bot)
            await db.config.update_one(
                {"key": "ranked_season"},
                {
                    "$set": {
                        "is_off_season": True,
                        "off_season_end": now + OFF_SEASON_DURATION_SECONDS
                    }
                }
            )

        # Case 2: Off-season has expired -> launch next season
        elif season.get("is_off_season", False) and now >= season.get("off_season_end", 0):
            next_season_num = season.get("season_number", 1) + 1
            logger.info(f"Off-season ended. Launching Season {next_season_num}!")
            await db.config.update_one(
                {"key": "ranked_season"},
                {
                    "$set": {
                        "season_number": next_season_num,
                        "season_start": now,
                        "season_end": now + SEASON_DURATION_SECONDS,
                        "is_off_season": False,
                        "off_season_end": 0.0
                    }
                }
            )
    except Exception as e:
        logger.error(f"check_and_process_season_transition error: {e}")

async def _distribute_season_rewards_and_soft_reset(season_number: int, bot=None) -> None:
    """Distributes packs/coins to all ranked participants, applies soft reset, and sends private DMs."""
    db = get_db()
    ranked_users = await db.users.find({"ranked_rp": {"$gt": 0}}).to_list(None)
    logger.info(f"Processing season rewards for {len(ranked_users)} ranked participants...")

    sports = ["cricket", "football", "wwe", "kabaddi"]

    for user in ranked_users:
        uid = user["user_id"]
        rp = user.get("ranked_rp", 0)
        tier = get_rank_tier(rp)
        tier_name = tier[0]
        tier_order = tier[4]

        packs_to_give = []
        bonus_coins = 0

        if "Bronze" in tier_name:
            packs_to_give.append(f"basic_{random.choice(sports)}")
        elif "Silver" in tier_name:
            packs_to_give.extend([f"basic_{random.choice(sports)}" for _ in range(2)])
        elif "Gold" in tier_name:
            packs_to_give.append(f"premium_{random.choice(sports)}")
        elif "Platinum" in tier_name:
            packs_to_give.extend([f"premium_{random.choice(sports)}" for _ in range(2)])
        elif "Diamond" in tier_name:
            packs_to_give.append(f"elite_{random.choice(sports)}")
        elif "Master" in tier_name:
            packs_to_give.extend([f"elite_{random.choice(sports)}" for _ in range(2)])
        elif "Champion" in tier_name:
            packs_to_give.extend([f"elite_{random.choice(sports)}" for _ in range(2)])
            bonus_coins = 2000
        elif "Legend" in tier_name:
            packs_to_give.extend([f"elite_{random.choice(sports)}" for _ in range(3)])
            bonus_coins = 5000

        counts = Counter(packs_to_give)
        pack_inc = {f"pack_inventory.{p}": qty for p, qty in counts.items()}
        update_doc = {}
        if pack_inc:
            update_doc.setdefault("$inc", {}).update(pack_inc)
        if bonus_coins > 0:
            update_doc.setdefault("$inc", {})["card_coins"] = bonus_coins

        new_start_rp = SOFT_RESET_TIER_RP.get(tier_order, 0)
        update_doc.setdefault("$set", {})["ranked_rp"] = new_start_rp

        await db.users.update_one({"user_id": uid}, update_doc)

        if bot:
            try:
                start_tier = get_rank_tier(new_start_rp)
                formatted_packs = [
                    f"{qty}x {p.replace('_', ' ').title()} Pack" if qty > 1 else f"{p.replace('_', ' ').title()} Pack"
                    for p, qty in counts.items()
                ]
                reward_desc = ", ".join(formatted_packs) if formatted_packs else "None"
                if bonus_coins:
                    reward_desc += f" + {bonus_coins}🪙"
                text = (
                    f"🏆 <b>SEASON {season_number} CONCLUDED!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"You finished in: {tier[1]} <b>{tier_name}</b> ({rp} RP)\n\n"
                    f"🎁 <b>Your Season Rewards:</b>\n"
                    f"• {reward_desc} (added to /inventory)\n\n"
                    f"🔄 <b>Soft Reset Applied:</b>\n"
                    f"Starting Rank for Season {season_number + 1}: {start_tier[1]} <b>{start_tier[0]}</b> ({new_start_rp} RP)\n\n"
                    f"⏳ Next season begins in 24 hours. Get ready! 🚀\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                await asyncio.sleep(0.05)
            except Exception:
                pass


# ── Ban / Unban System ───────────────────────────────────────────────────────

_banned_users_cache: set = set()
_banned_cache_loaded: bool = False

async def is_user_banned(user_id: int) -> bool:
    """Checks if a user is banned (cached in-memory for instant 0ms checks). Owners are immune."""
    from config import OWNER_IDS
    if user_id in OWNER_IDS:
        return False
    global _banned_cache_loaded, _banned_users_cache
    if not _banned_cache_loaded:
        try:
            db = get_db()
            cursor = db.banned_users.find({}, {"user_id": 1})
            _banned_users_cache = {doc["user_id"] async for doc in cursor}
            _banned_cache_loaded = True
        except Exception:
            pass
    if user_id in _banned_users_cache:
        if await is_admin(user_id):
            return False
        return True
    return False


async def ban_user(user_id: int, reason: str = "Banned by admin") -> bool:
    """Bans a user by ID. Owners/admins cannot be banned."""
    from config import OWNER_IDS
    if user_id in OWNER_IDS:
        return False
    global _banned_users_cache
    db = get_db()

    import time as _t
    await db.banned_users.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "banned_at": _t.time(), "reason": reason}},
        upsert=True
    )
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"banned": True, "banned_at": _t.time(), "ban_reason": reason}}
    )
    _banned_users_cache.add(user_id)
    return True

async def unban_user(user_id: int) -> bool:
    """Unbans a user by ID."""
    global _banned_users_cache
    db = get_db()
    await db.banned_users.delete_one({"user_id": user_id})
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"banned": False}}
    )
    _banned_users_cache.discard(user_id)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# DAILY CHECK-IN SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

import datetime as _datetime

def _checkin_day_key() -> str:
    """Returns today's date string in UTC as YYYY-MM-DD for dedup."""
    return _datetime.datetime.utcnow().strftime("%Y-%m-%d")

async def get_checkin_status(user_id: int) -> dict:
    """
    Returns the user's check-in state:
    {checkin_streak, last_checkin_day, checked_in_today}
    """
    db = get_db()
    doc = await db.users.find_one(
        {"user_id": user_id},
        {"checkin_streak": 1, "last_checkin_day": 1, "_id": 0}
    )
    today = _checkin_day_key()
    if not doc:
        return {"checkin_streak": 0, "last_checkin_day": None, "checked_in_today": False}

    last = doc.get("last_checkin_day")
    streak = doc.get("checkin_streak", 0)
    checked_in_today = (last == today)
    return {
        "checkin_streak": streak,
        "last_checkin_day": last,
        "checked_in_today": checked_in_today,
    }


async def do_checkin(user_id: int) -> dict:
    """
    Performs a daily check-in. Returns:
    {
      success: bool,           # False if already checked in today
      coins_awarded: int,
      new_streak: int,
      milestone: bool,         # True if this is a 7-day milestone
      card_awarded: dict|None  # {player_id, name, format, rarity, ovr} or None
    }
    """
    import random as _random

    db = get_db()
    today = _checkin_day_key()

    doc = await db.users.find_one(
        {"user_id": user_id},
        {"checkin_streak": 1, "last_checkin_day": 1, "_id": 0}
    )

    last_day = (doc or {}).get("last_checkin_day")
    streak   = (doc or {}).get("checkin_streak", 0)

    # Already checked in today?
    if last_day == today:
        return {"success": False, "coins_awarded": 0, "new_streak": streak,
                "milestone": False, "card_awarded": None}

    # Calculate yesterday
    yesterday = (_datetime.datetime.utcnow() - _datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    if last_day == yesterday:
        new_streak = streak + 1  # consecutive day
    else:
        new_streak = 1           # streak broken or first check-in

    coins = 50
    is_milestone = (new_streak % 7 == 0)
    card_awarded = None

    if is_milestone:
        # Award a random card from the pool (common 60%, rare 30%, epic 10%)
        rarity_roll = _random.random()
        if rarity_roll < 0.60:
            target_rarity = "common"
        elif rarity_roll < 0.90:
            target_rarity = "rare"
        else:
            target_rarity = "epic"

        # Pick a random player with a card of that rarity (any format/sport)
        # Try multiple formats, pick a random matching player
        fmt_fields = [
            ("cards.odi.rarity",  "odi"),
            ("cards.ipl.rarity",  "ipl"),
            ("cards.test.rarity", "test"),
            ("cards.pkl.rarity",  "pkl"),
            ("cards.wwe.rarity",  "wwe"),
            ("cards.fifa.rarity", "fifa"),
        ]
        _random.shuffle(fmt_fields)

        for rarity_field, fmt in fmt_fields:
            candidates = await db.players.find(
                {rarity_field: target_rarity},
                {"player_id": 1, "name": 1, f"cards.{fmt}": 1, "_id": 0}
            ).to_list(200)

            if candidates:
                chosen_player = _random.choice(candidates)
                card_data = chosen_player.get("cards", {}).get(fmt, {})
                card_awarded = {
                    "player_id": chosen_player["player_id"],
                    "name":      chosen_player["name"],
                    "format":    fmt,
                    "rarity":    card_data.get("rarity", target_rarity),
                    "ovr":       card_data.get("ovr", 0),
                }
                # Add card to user inventory
                await add_card_to_user(user_id, chosen_player["player_id"], fmt)
                break

    # Persist check-in
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {
            "checkin_streak":   new_streak,
            "last_checkin_day": today,
        }},
        upsert=True
    )

    # Award coins
    await add_card_coins(user_id, coins)

    return {
        "success":       True,
        "coins_awarded": coins,
        "new_streak":    new_streak,
        "milestone":     is_milestone,
        "card_awarded":  card_awarded,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ACHIEVEMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

async def get_achievements(user_id: int) -> list:
    """Returns list of achievement strings for a user."""
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"achievements": 1, "_id": 0})
    return (doc or {}).get("achievements", [])


async def add_achievement(user_id: int, text: str) -> int:
    """
    Adds an achievement to a user. Returns total achievement count.
    """
    db = get_db()
    result = await db.users.find_one_and_update(
        {"user_id": user_id},
        {"$push": {"achievements": text}},
        upsert=True,
        return_document=True
    )
    return len((result or {}).get("achievements", [text]))


async def remove_achievement(user_id: int, index: int) -> tuple[bool, str]:
    """
    Removes achievement by 1-based index.
    Returns (success, removed_text_or_error).
    """
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"achievements": 1, "_id": 0})
    achievements = (doc or {}).get("achievements", [])

    if not achievements:
        return False, "No achievements found."
    if index < 1 or index > len(achievements):
        return False, f"Invalid number. Must be 1–{len(achievements)}."

    removed = achievements[index - 1]
    achievements.pop(index - 1)
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"achievements": achievements}}
    )
    return True, removed


# ═══════════════════════════════════════════════════════════════════════════
# BOT STATUS HELPERS
# ═══════════════════════════════════════════════════════════════════════════

async def get_botstatus_data() -> dict:
    """
    Returns a dict of bot status metrics for /botstatus command.
    All DB calls run concurrently.
    """
    import asyncio as _asyncio
    db = get_db()

    async def _count_users():
        return await db.users.count_documents({})

    async def _count_cards():
        return await db.user_cards.count_documents({})

    async def _count_active_matches():
        """Returns total active count + breakdown by mode."""
        cursor = db.matches.find(
            {"state_data.state": {"$in": ["DRAFTING", "ACTIVE", "IN_PROGRESS"]}},
            {"state_data.mode": 1, "_id": 0}
        )
        docs = await cursor.to_list(500)
        mode_counts: dict = {}
        for d in docs:
            mode = (d.get("state_data") or {}).get("mode", "Unknown") or "Unknown"
            # Normalise
            if "IPL" in mode:       key = "IPL"
            elif "Test" in mode:    key = "Test"
            elif "FIFA" in mode:    key = "FIFA"
            elif "WWE" in mode:     key = "WWE"
            elif "PKL" in mode or "Kabaddi" in mode: key = "PKL"
            else:                   key = "ODI"
            mode_counts[key] = mode_counts.get(key, 0) + 1
        return len(docs), mode_counts

    users, cards, (total_active, mode_counts) = await _asyncio.gather(
        _count_users(), _count_cards(), _count_active_matches()
    )
    return {
        "total_users":   users,
        "total_cards":   cards,
        "active_total":  total_active,
        "active_modes":  mode_counts,
        "cache_size":    len(_player_cache),
        "cache_max":     CACHE_MAX_SIZE,
    }


# ── User Card Sorting Preference ─────────────────────────────────────────────
_USER_SORT_CACHE: dict[int, tuple[str, str]] = {}

async def get_user_card_sort(user_id: int) -> tuple[str, str]:
    """Returns (criteria, order) for user_id. Defaults to ('rarity', 'desc'). Cached in memory."""
    if user_id in _USER_SORT_CACHE:
        return _USER_SORT_CACHE[user_id]
    db = get_db()
    try:
        doc = await db.users.find_one({"user_id": user_id}, {"card_sort": 1})
        if doc and "card_sort" in doc:
            cs = doc["card_sort"]
            res = (cs.get("crit", "rarity"), cs.get("order", "desc"))
            _USER_SORT_CACHE[user_id] = res
            return res
    except Exception:
        pass
    return ("rarity", "desc")

async def save_user_card_sort(user_id: int, crit: str, order: str) -> None:
    """Persists user's sort preference in MongoDB and updates memory cache."""
    _USER_SORT_CACHE[user_id] = (crit, order)
    db = get_db()
    try:
        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {"card_sort": {"crit": crit, "order": order}}},
            upsert=True
        )
    except Exception:
        pass



