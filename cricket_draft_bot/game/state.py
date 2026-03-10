# game/state.py
import json
import logging
from typing import Optional, Dict
from game.models import Match, Team, Player
from database import get_match, save_match, get_eligible_players_for_mode, get_player
from utils.randomizer import get_random_player
from config import MAX_REDRAWS

logger = logging.getLogger(__name__)

async def create_match_state(chat_id: int, mode: str, owner_id: int, challenger_id: int, owner_name: str, challenger_name: str) -> Match:
    """Initializes a new match state async."""
    # Use optimized DB projection instead of loading all 20k players
    draft_pool = await get_eligible_players_for_mode(mode)
    
    import random
    first_drafter = random.choice([owner_id, challenger_id])
    
    from config import POSITIONS_T20, POSITIONS_TEST, POSITIONS_FIFA
    
    # Select Slots
    if mode and "Test" in mode:
        slot_keys = POSITIONS_TEST
    elif mode == "FIFA":
        slot_keys = POSITIONS_FIFA
    else:
        slot_keys = POSITIONS_T20
        
    initial_slots = {k: None for k in slot_keys}

    import time
    match_id = f"{owner_id}_{int(time.time())}"
    
    match = Match(
        match_id=match_id,
        chat_id=chat_id,
        mode=mode,
        team_a=Team(owner_id=owner_id, owner_name=owner_name, slots=initial_slots.copy()),
        team_b=Team(owner_id=challenger_id, owner_name=challenger_name, slots=initial_slots.copy()),
        current_turn=first_drafter,
        draft_pool=draft_pool,
        state="DRAFTING"
    )
    
    await save_match_state(match)
    return match

async def save_match_state(match: Match):
    """Serializes and saves the match state."""
    def team_to_dict(team: Team):
        return {
            "owner_id": team.owner_id,
            "owner_name": team.owner_name,
            "slots": {k: (v.player_id if v else None) for k, v in team.slots.items()},
            "redraws_remaining": team.redraws_remaining,
            "replacements_remaining": getattr(team, 'replacements_remaining', 1),
            "is_ready": team.is_ready,
            "score": team.score,
            "trades_used": getattr(team, 'trades_used', 0)
        }

    state_data = {
        "match_id": match.match_id,
        "chat_id": match.chat_id,
        "mode": match.mode,
        "team_a": team_to_dict(match.team_a),
        "team_b": team_to_dict(match.team_b),
        "current_turn": match.current_turn,
        "draft_pool": match.draft_pool,
        "state": match.state,
        "pending_player_id": match.pending_player_id,
        "draft_message_id": match.draft_message_id,
        "card_message_id": match.card_message_id,
        "finished_at": match.finished_at,
        "trade_offer": getattr(match, 'trade_offer', None)
    }
    await save_match(match.match_id, match.chat_id, state_data)

async def load_match_state(match_id: str) -> Optional[Match]:
    data = await get_match(match_id)
    if not data:
        return None
        
    async def dict_to_team(d):
        t = Team(owner_id=d['owner_id'], owner_name=d['owner_name'])
        t.redraws_remaining = d['redraws_remaining']
        t.replacements_remaining = d.get('replacements_remaining', 1)
        t.is_ready = d.get('is_ready', False)
        t.score = d.get('score', 0)
        t.trades_used = d.get('trades_used', 0)
        # Reconstruct slots
        for slot, pid in d['slots'].items():
            if pid:
                p_data = await get_player(pid)
                if p_data:
                    t.slots[slot] = Player(**p_data)
            else:
                 t.slots[slot] = None
        return t

    m = Match(
        match_id=data['match_id'],
        chat_id=data['chat_id'],
        mode=data['mode'],
        team_a=await dict_to_team(data['team_a']),
        team_b=await dict_to_team(data['team_b']),
        current_turn=data['current_turn'],
        draft_pool=data['draft_pool'],
        state=data['state'],
        pending_player_id=data.get('pending_player_id'),
        draft_message_id=data.get('draft_message_id'),
        card_message_id=data.get('card_message_id'),
        finished_at=data.get('finished_at', 0.0)
    )
    m.trade_offer = data.get('trade_offer')
    return m

async def draw_player_for_turn(match: Match) -> Optional[Dict]:
    """Draws a random player for the current turn."""
    taken = []
    for team in [match.team_a, match.team_b]:
        for p in team.slots.values():
            if p:
                taken.append(p.player_id)
    
    pid = get_random_player(match.draft_pool, exclude_ids=taken)
    if not pid:
        return None
        
    return await get_player(pid)

async def switch_turn(match: Match):
    """Switches the turn to the other player."""
    current_team = match.team_a if match.current_turn == match.team_a.owner_id else match.team_b
    next_team = match.team_b if current_team == match.team_a else match.team_a
    
    if next_team.is_complete() and not current_team.is_complete():
        logger.info(f"DEBUG: Keeping turn with {current_team.owner_name} (Opponent done)")
        pass
    else:
        match.current_turn = next_team.owner_id

    await save_match_state(match)
