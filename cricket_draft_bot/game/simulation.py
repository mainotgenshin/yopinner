# game/simulation.py
from game.models import Match, Team, Player
from config import ROLE_WEIGHTS
from utils.randomizer import calculate_variance
from telegram.helpers import escape_markdown
import logging

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)

# Helper to safe get stat
def get_stat_value(player: Player, mode: str, stat_key: str) -> int:
    try:
        stats = player.stats.get(mode.lower(), {})
        # Handle fallback for old int-style stats
        if isinstance(stats, int):
            return stats
        return int(stats.get(stat_key, 50))
    except:
        return 50

def get_clutch_bonus(player: Player, mode: str) -> float:
    # "Clutch works behind the scenes"
    # Returns a small multiplier bonus (e.g. 0.0 to 5.0 added to score)
    clutch = get_stat_value(player, mode, "clutch")
    # Clutch is 0-100. Let's say map to 0-5 pts max impact?
    # User said: final_score += (clutch * 0.1)
    return clutch * 0.1

def calculate_slot_score(player: Player, role: str, mode: str) -> float:
    from config import ROLE_STATS_MAP, PENALTY_MULTIPLIERS, ZERO_SKILL_THRESHOLD
    
    # 1. Stat Dependency Check
    # Get primary stat key for this slot
    stat_key = ROLE_STATS_MAP.get(role, "all_round")
    
    # Get stat value (fallback 50 if missing, but we handle low stats)
    stat_val = get_stat_value(player, mode, stat_key)
    
    # 2. Zero-Skill Penalty
    if stat_val < ZERO_SKILL_THRESHOLD:
        stat_val *= PENALTY_MULTIPLIERS["ZERO_SKILL"] # Severe penalty (e.g. 15 -> 1.5)
        
    # 3. Role Match Multiplier
    multiplier = PENALTY_MULTIPLIERS["MISMATCH"]
    
    # Case-insensitive comparison
    # Case-insensitive comparison
    if mode and "IPL" in mode:
        # Use IPL roles if available, fallback to normal roles
        effective_roles = player.ipl_roles if player.ipl_roles else player.roles
    else:
        effective_roles = player.roles

    player_roles_lower = [r.lower() for r in effective_roles]
    role_lower = role.lower()
    
    if role in effective_roles or role_lower in player_roles_lower:
        multiplier = PENALTY_MULTIPLIERS["NATURAL"]
    else:
        # Partial Match Logic
        # e.g. "Wicket Keeper" in roles matches "WK" slot
        
        if role_lower == "wk" and "wicket keeper" in player_roles_lower:
            multiplier = PENALTY_MULTIPLIERS["NATURAL"]
        elif role_lower in ["hitting", "finisher", "defence"] and "batter" in player_roles_lower:
             multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
        elif role_lower in ["pace", "spin"] and "bowler" in player_roles_lower:
             multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
        elif role_lower == "all-rounder" and ("all rounder" in player_roles_lower or "all-rounder" in player_roles_lower):
             multiplier = PENALTY_MULTIPLIERS["NATURAL"]
        
        # New Rule: Batting Compatibility (Top/Middle/Finisher/Hitting are all partially compatible)
        # If we reached here, it's not a NATURAL match (exact role).
        elif role_lower in ["top", "middle", "finisher", "hitting"]:
            # Check if player has ANY other batting role
            if any(r in player_roles_lower for r in ["top", "middle", "finisher", "hitting", "batter"]):
                multiplier = PENALTY_MULTIPLIERS["PARTIAL"]

    # weight = ROLE_WEIGHTS.get(role, 1.0) # Weight disabled per strict stat comparison request? 
    # Actually user said "overall stat comparison". We should probably keep natural weight of role?
    # But user example shows direct player vs player. 
    # Let's keep weight to differentiate "important" roles if needed, but remove variance.
    # Actually, to make it purely stat based, removing weight makes it cleaner 1:1 stat comparison.
    # "Andre Russell wins against Shami" -> All Rounder vs All Rounder (65 vs 45).
    # Multiplier still applies for mismatches.
    
    score = stat_val * multiplier
    return score

def run_simulation(match: Match) -> str:
    """
    Runs the simulation with enhanced stats and output format.
    """
    score_a = 0
    score_b = 0
    details = []

    from config import POSITIONS_T20, POSITIONS_TEST
    
    active_positions = POSITIONS_TEST if match.mode and "Test" in match.mode else POSITIONS_T20
    
    details.append("🏟 **MATCH SIMULATION – POSITION COMPARISON**\n")
    
    # Icon Map
    ICONS = {
        "Captain": "⚔️",
        "WK": "🧤",
        "All-Rounder": "🧠",
        "Defence": "🛡",
        "Finisher": "💥", # Added Finisher icon
        "Hitting": "🔥",
        "Pace": "⚡",
        "Spin": "🌀",
        "Fielding": "🤾"
    }

    # 1. Head to Head Slot Battles
    for i, pos in enumerate(active_positions, 1):
        p_a = match.team_a.slots.get(pos)
        p_b = match.team_b.slots.get(pos)
        
        # If missing player? Should not happen if draft complete.
        if not p_a or not p_b: continue
        
        s_a = calculate_slot_score(p_a, pos, match.mode)
        s_b = calculate_slot_score(p_b, pos, match.mode)
        
        icon = ICONS.get(pos, "🔸")
        details.append(f"{icon} **{i}. {pos} vs {pos}**")
        
        if s_a > s_b:
            score_a += 1
            details.append(f"🔵 {esc(p_a.name)} wins against {esc(p_b.name)}\n(+1 Point to {esc(match.team_a.owner_name)})\n")
        elif s_b > s_a:
            score_b += 1
            details.append(f"🔴 {esc(p_b.name)} wins against {esc(p_a.name)}\n(+1 Point to {esc(match.team_b.owner_name)})\n")
        else:
            details.append(f"⚖️ Draw: {esc(p_a.name)} vs {esc(p_b.name)}\n(0 Points)\n")
            
    # Final Result
    details.append("➖➖➖➖➖➖➖➖➖➖")
    details.append(f"🔵 {esc(match.team_a.owner_name)} Score: {score_a}")
    details.append(f"🔴 {esc(match.team_b.owner_name)} Score: {score_b}\n")
    
    # Persist Scores
    match.team_a.score = score_a
    match.team_b.score = score_b
    
    winner_text = "🤝 **MATCH DRAWN!**"
    if score_a > score_b:
        winner_text = f"🏆 **WINNER:** 🔵 {esc(match.team_a.owner_name)}"
    elif score_b > score_a:
        winner_text = f"🏆 **WINNER:** 🔴 {esc(match.team_b.owner_name)}"
    else:
        # User requested no Super Over, just a Draw result.
        pass
    
    details.append(winner_text)
    
    match.state = "FINISHED"

    # PERSIST RESULTS
    try:
        from database import update_user_stats
        
        # Determine Results
        res_a = "D"
        res_b = "D"
        
        if score_a > score_b:
            res_a = "W"
            res_b = "L"
        elif score_b > score_a:
            res_a = "L"
            res_b = "W"
            
        update_user_stats(match.team_a.owner_id, match.team_a.owner_name, res_a)
        update_user_stats(match.team_b.owner_id, match.team_b.owner_name, res_b)
        
    except Exception as e:
        logger.error(f"Failed to persist user stats: {e}")

    return "\n".join(details)
