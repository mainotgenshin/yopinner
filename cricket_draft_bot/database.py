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
        # H2H result lookups by player pair
        await db.match_results.create_index([("player_a_id", ASCENDING), ("player_b_id", ASCENDING)])
        await db.match_results.create_index([("played_at", ASCENDING)])

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
    if is_wwe:
        sport_win_field = "wwe_wins"
    elif is_fifa:
        sport_win_field = "fifa_wins"
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
    """Add card coins to user. Returns new balance."""
    db = get_db()
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

async def add_pack(user_id: int, pack_type: str) -> None:
    """Add one pack of given type to user inventory."""
    db = get_db()
    await db.users.update_one(
        {"user_id": user_id},
        {"$inc": {f"pack_inventory.{pack_type}": 1}},
        upsert=True
    )

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
        fmt = card["format"]
        p = players_by_id.get(pid)
        if not p:
            continue
        card_data = p.get("cards", {}).get(fmt, {})
        if not card_data:
            continue
        p_sport = p.get("sport", "cricket")
        if sport_filter == "cricket" and p_sport in ("wwe", "football"):
            continue
        if sport_filter == "football" and p_sport != "football":
            continue
        if sport_filter == "wwe" and p_sport != "wwe":
            continue
        image = _get_card_image(p, fmt)
        result.append({
            "user_id":   user_id,
            "player_id": pid,
            "format":    fmt,
            "quantity":  card["quantity"],
            "name":      p["name"],
            "rarity":    card_data.get("rarity", "common"),
            "ovr":       card_data.get("ovr", 0),
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
        return (player_doc.get("image_file_id") or
                player_doc.get("fifa_image_url") or
                player_doc.get("image_url"))
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
    else:
        url = player_doc.get("image_url")
    return url if url and url.startswith("http") else None


async def get_user_card(user_id: int, player_id: str, fmt: str) -> Optional[dict]:
    """Get a single user card entry or None."""
    db = get_db()
    return await db.user_cards.find_one({"user_id": user_id, "player_id": player_id, "format": fmt})

async def add_card_to_user(user_id: int, player_id: str, fmt: str) -> int:
    """Add one copy of a card. Returns new quantity."""
    db = get_db()
    result = await db.user_cards.find_one_and_update(
        {"user_id": user_id, "player_id": player_id, "format": fmt},
        {"$inc": {"quantity": 1}},
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
        return 0
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
    "obtain_2": {"label": "Obtain 2 cards via pack",  "field": "cards_obtained", "target": 2,  "reward": 10},
    "obtain_5": {"label": "Obtain 5 cards via pack",  "field": "cards_obtained", "target": 5,  "reward": 20},
    "trade_1":  {"label": "Trade 1 card",             "field": "cards_traded",   "target": 1,  "reward": 10},
    "sell_2":   {"label": "Sell 2 cards",             "field": "cards_sold",     "target": 2,  "reward": 10},
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
        "reset_at":       _next_midnight_utc(),
        "cards_obtained": 0,
        "cards_traded":   0,
        "cards_sold":     0,
        "claimed":        [],
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
            "reset_at":       _next_midnight_utc(),
            "cards_obtained": 0,
            "cards_traded":   0,
            "cards_sold":     0,
            "claimed":        [],
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
    clear_player_cache()
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
    clear_player_cache()
    return True

# ── Card Pack Drawing ─────────────────────────────────────────────────────────

PACK_ODDS = {
    "basic":   {"legend": 0,  "epic": 5,  "rare": 35, "common": 60},
    "premium": {"legend": 5,  "epic": 35, "rare": 45, "common": 15},
    "elite":   {"legend": 30, "epic": 55, "rare": 15, "common": 0},
}

_card_pool_cache: dict = {}  # sport -> list of {player_id, name, format, rarity, ovr, image}
_card_pool_cache_time: dict = {}
CARD_POOL_CACHE_TTL = 300  # 5 minutes

async def _build_card_pool(sport: str) -> list:
    """Build and cache the drawable card pool for a sport."""
    now = _time.time()
    if sport in _card_pool_cache and (now - _card_pool_cache_time.get(sport, 0)) < CARD_POOL_CACHE_TTL:
        return _card_pool_cache[sport]

    db = get_db()
    pool = []

    if sport == "cricket":
        query = {"sport": {"$nin": ["wwe", "football"]}, "cards": {"$exists": True}}
        formats = ["ipl", "odi", "test"]
    elif sport == "wwe":
        query = {"sport": "wwe", "gender": {"$ne": "female"}, "cards": {"$exists": True}}
        formats = ["wwe"]
    elif sport == "football":
        query = {"sport": "football", "cards": {"$exists": True}}
        formats = ["fifa"]
    else:
        return []

    async for p in db.players.find(query, {"player_id": 1, "name": 1, "cards": 1,
                                            "ipl_image_file_id": 1, "image_file_id": 1,
                                            "wwe_image_url": 1, "fifa_image_url": 1,
                                            "test_image_url": 1}):
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
    _card_pool_cache.clear()
    _card_pool_cache_time.clear()

async def warmup_card_pools() -> None:
    """
    Pre-fetch all card pools into RAM cache.
    Call on startup and periodically (every ~4 min) so players never
    experience a cold-cache DB scan when drawing from a pack or starting a draft.
    Silently swallows errors — this is a background optimisation, not critical path.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)
    for sport in ("cricket", "football", "wwe"):
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
# HEAD-TO-HEAD (H2H) RECORDS
# ═══════════════════════════════════════════════════════════════════════════

async def record_match_result(
    winner_id: int, winner_name: str,
    loser_id: int, loser_name: str,
    is_draw: bool, mode: str, chat_id: int
) -> None:
    """Store a completed match result for H2H lookups. Called from simulation.py."""
    db = get_db()
    import time as _t
    await db.match_results.insert_one({
        "winner_id":   winner_id if not is_draw else None,
        "loser_id":    loser_id  if not is_draw else None,
        "player_a_id": winner_id,
        "player_a_name": winner_name,
        "player_b_id": loser_id,
        "player_b_name": loser_name,
        "is_draw":     is_draw,
        "mode":        mode,
        "chat_id":     chat_id,
        "played_at":   _t.time(),
    })

async def get_h2h_stats(user_a_id: int, user_b_id: int) -> dict:
    """
    Returns combined H2H stats between two users across all modes.
    {total, a_wins, b_wins, draws}
    """
    db = get_db()
    docs = await db.match_results.find({
        "$or": [
            {"player_a_id": user_a_id, "player_b_id": user_b_id},
            {"player_a_id": user_b_id, "player_b_id": user_a_id},
        ]
    }).to_list(None)

    a_wins = sum(1 for d in docs if d.get("winner_id") == user_a_id)
    b_wins = sum(1 for d in docs if d.get("winner_id") == user_b_id)
    draws  = sum(1 for d in docs if d.get("is_draw"))
    return {"total": len(docs), "a_wins": a_wins, "b_wins": b_wins, "draws": draws}
