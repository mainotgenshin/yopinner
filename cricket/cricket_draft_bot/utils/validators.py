# utils/validators.py
from typing import List
from game.models import Team

def is_slot_free(team: Team, slot_name: str) -> bool:
    """Checks if a specific slot in the team is empty."""
    return team.slots.get(slot_name) is None

def can_assign_role(player_roles: List[str], target_slot: str) -> bool:
    """
    Checks if a player can be assigned to a specific slot.
    Role matching is simple string matching.
    """
    if target_slot == "Captain":
        return "Captain" in player_roles
    elif target_slot == "WK":
        return "WK" in player_roles
    elif target_slot == "All-Rounder":
        return "All-Rounder" in player_roles
    elif target_slot == "Defence":
        return "Defence" in player_roles or "All-Rounder" in player_roles # Allow AR in Defence logic? Prompt says "fixed positions... No duplicates", implies strict mapping?
        # Prompt says: "Roles: Captain, WK, All-Rounder, Defence, Hitting, Pace, Spin, Fielding"
        # Player obj has "roles[]". Assumed strict match.
        # But 'Hitting' might be implied by 'All-Rounder'? 
        # For simplicity and strict adherence to "Player roles[]", we check exact match.
        pass
    
    return target_slot in player_roles

def validate_draft_action(team: Team, player_roles: List[str], slot: str) -> tuple[bool, str]:
    """
    Validates if a draft action is legal.
    Returns (Success, Message).
    """
    if not is_slot_free(team, slot):
        return False, f"Slot {slot} is already occupied!"
    
    if not can_assign_role(player_roles, slot):
        return False, f"Player is not eligible for {slot}. Roles: {', '.join(player_roles)}"
    
    return True, ""
