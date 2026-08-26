# game/simulation.py
import asyncio
from game.models import Match, Team, Player
from config import ROLE_WEIGHTS, WWE_POSITION_STATS
from utils.randomizer import calculate_variance
from telegram.helpers import escape_markdown
import logging

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)

# Helper to safe get stat
def get_stat_value(player: Player, mode: str, stat_key: str) -> int:
    try:
        search_key = mode.lower()
        # Normalize legacy/alias mode keys to DB stat key
        if search_key in ('intl', 'international'):
            search_key = 'odi'
             
        stats = player.stats.get(search_key, {})
        # Handle fallback for old int-style stats
        if isinstance(stats, int):
            return stats
        return int(stats.get(stat_key, 50))
    except:
        return 50

def get_clutch_bonus(player: Player, mode: str) -> float:
    clutch = get_stat_value(player, mode, "clutch")
    return clutch * 0.1

def calculate_slot_score(player: Player, role: str, mode: str) -> float:
    from config import ROLE_STATS_MAP, PENALTY_MULTIPLIERS, ZERO_SKILL_THRESHOLD

    # WWE: pure stat comparison, no role penalties
    if mode in ("WWE", "WWE Women"):
        stat_key = WWE_POSITION_STATS.get(role, "power")
        wwe_stats = player.stats.get("wwe", {})
        val = wwe_stats.get(stat_key, 50)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 50.0
    
    if mode == "FIFA":
        stat_key = role
    else:
        stat_key = ROLE_STATS_MAP.get(role, "all_round")
    
    if mode == "FIFA" and role == "ST/CF":
        val_st = get_stat_value(player, mode, "ST")
        val_cf = get_stat_value(player, mode, "CF")
        stat_val = max(val_st, val_cf)
    else:
        stat_val = get_stat_value(player, mode, stat_key)
    
    if stat_val < ZERO_SKILL_THRESHOLD:
        stat_val *= PENALTY_MULTIPLIERS["ZERO_SKILL"]
        
    multiplier = PENALTY_MULTIPLIERS["MISMATCH"]
    
    if mode == "FIFA":
        effective_roles = player.positions if player.positions else []
        effective_roles_lower = [r.lower() for r in effective_roles]
        role_lower = role.lower()
        
        if role in effective_roles or role_lower in effective_roles_lower:
             multiplier = PENALTY_MULTIPLIERS["NATURAL"]
        elif role_lower == "st/cf":
             if "st" in effective_roles_lower or "cf" in effective_roles_lower:
                 multiplier = PENALTY_MULTIPLIERS["NATURAL"]
        elif role_lower == "cf" and "st" in effective_roles_lower: 
             multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
        elif role_lower == "st" and "cf" in effective_roles_lower:
             multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
        elif role_lower == "cdm" and ("cm" in effective_roles_lower or "cb" in effective_roles_lower):
             multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
             
    else:
        # Cricket Logic
        if "IPL" in mode:
            effective_roles = player.ipl_roles if player.ipl_roles else player.roles
        elif "Test" in mode:
            effective_roles = getattr(player, 'test_roles', None) or player.roles
        else:  # ODI and others
            effective_roles = player.roles

        player_roles_lower = [r.lower() for r in effective_roles]
        role_lower = role.lower()
        
        if role in effective_roles or role_lower in player_roles_lower:
            multiplier = PENALTY_MULTIPLIERS["NATURAL"]
        else:
            if role_lower == "wk" and "wicket keeper" in player_roles_lower:
                multiplier = PENALTY_MULTIPLIERS["NATURAL"]
            elif role_lower in ["hitting", "finisher", "defence"] and "batter" in player_roles_lower:
                 multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
            elif role_lower in ["pace", "spin"] and "bowler" in player_roles_lower:
                 multiplier = PENALTY_MULTIPLIERS["PARTIAL"]
            elif role_lower == "all-rounder" and ("all rounder" in player_roles_lower or "all-rounder" in player_roles_lower):
                 multiplier = PENALTY_MULTIPLIERS["NATURAL"]
            elif role_lower in ["top", "middle", "finisher", "hitting"]:
                if any(r in player_roles_lower for r in ["top", "middle", "finisher", "hitting", "batter"]):
                    multiplier = PENALTY_MULTIPLIERS["PARTIAL"]

    score = stat_val * multiplier
    return score

async def run_simulation(match: Match) -> str:
    """
    Runs the simulation with enhanced stats and output format.
    """
    score_a = 0
    score_b = 0
    details = []

    from config import POSITIONS_T20, POSITIONS_TEST, POSITIONS_FIFA, POSITIONS_WWE
    
    if match.mode and "FIFA" in match.mode:
        active_positions = POSITIONS_FIFA
    elif match.mode and "WWE" in match.mode:
        active_positions = POSITIONS_WWE
    elif match.mode and "Test" in match.mode:
        active_positions = POSITIONS_TEST
    else:
        active_positions = POSITIONS_T20
    
    details.append("🏟 *MATCH SIMULATION – POSITION COMPARISON*\n")
    
    ICONS = {
        "Captain": "⚔️",
        "WK": "🧤",
        "Top": "🔸",
        "Middle": "🔸",
        "All Rounder": "🧠",
        "All-Rounder": "🧠",
        "Defence": "🛡",
        "Finisher": "💥",
        "Hitting": "🔥",
        "Pacer": "⚡",
        "Pace": "⚡",
        "Spinner": "🌀",
        "Spin": "🌀",
        "Fielder": "🤾",
        "Fielding": "🤾"
    }

    # Head-to-Head Slot Battles
    for i, pos in enumerate(active_positions, 1):
        p_a = match.team_a.slots.get(pos)
        p_b = match.team_b.slots.get(pos)
        
        if not p_a or not p_b:
            continue
        
        s_a = calculate_slot_score(p_a, pos, match.mode)
        s_b = calculate_slot_score(p_b, pos, match.mode)
        
        icon = ICONS.get(pos, "🔸")
        details.append(f"{icon} *{i}. {pos} vs {pos}*")
        
        if s_a > s_b:
            score_a += 1
            details.append(f"🔵 {esc(p_a.name)} > {esc(p_b.name)}")
        elif s_b > s_a:
            score_b += 1
            details.append(f"🔴 {esc(p_b.name)} > {esc(p_a.name)}")
        else:
            details.append(f"⚖️ Draw: {esc(p_a.name)} vs {esc(p_b.name)}")
            
        details.append("")  # spacing between slots

    # Persist Scores
    match.team_a.score = score_a
    match.team_b.score = score_b

    # Determine outcome & rewards BEFORE building the score line
    res_a = "D"
    res_b = "D"
    if score_a > score_b:
        res_a = "W"
        res_b = "L"
    elif score_b > score_a:
        res_a = "L"
        res_b = "W"

    _CARD_COIN_REWARDS = {"W": 75, "D": 25, "L": 10}
    reward_a = _CARD_COIN_REWARDS.get(res_a, 10)
    reward_b = _CARD_COIN_REWARDS.get(res_b, 10)

    # Final Result — coins shown inline with score
    details.append("➖➖➖➖➖➖➖➖➖➖")
    details.append(f"🔵 {esc(match.team_a.owner_name)} — {score_a} (+{reward_a}🪙)")
    details.append(f"🔴 {esc(match.team_b.owner_name)} — {score_b} (+{reward_b}🪙)")
    details.append("")

    if score_a > score_b:
        details.append(f"🏆 *WINNER*\n🔵 {esc(match.team_a.owner_name)}")
    elif score_b > score_a:
        details.append(f"🏆 *WINNER*\n🔴 {esc(match.team_b.owner_name)}")
    else:
        details.append("🤝 *MATCH DRAWN!*")

    match.state = "FINISHED"

    # PERSIST RESULTS (Background Task - Instant Result Delivery)
    async def _persist_results_bg():
        try:
            from database import update_user_stats, record_match_result, add_card_coins

            async def _award_card_coins(user_id: int, result: str) -> None:
                """Silently award card coins after a match. Never raises."""
                try:
                    coins = _CARD_COIN_REWARDS.get(result, 10)
                    await add_card_coins(user_id, coins)
                except Exception as _ce:
                    logger.warning(f"Card coin award failed for {user_id}: {_ce}")

            is_draw = (res_a == "D")
            winner_id   = match.team_a.owner_id   if res_a == "W" else match.team_b.owner_id
            winner_name = match.team_a.owner_name if res_a == "W" else match.team_b.owner_name
            loser_id    = match.team_b.owner_id   if res_a == "W" else match.team_a.owner_id
            loser_name  = match.team_b.owner_name if res_a == "W" else match.team_a.owner_name

            # Write stats + award card coins + record H2H result concurrently in background
            await asyncio.gather(
                update_user_stats(match.team_a.owner_id, match.team_a.owner_name, res_a,
                                  mode=match.mode, chat_id=match.chat_id),
                update_user_stats(match.team_b.owner_id, match.team_b.owner_name, res_b,
                                  mode=match.mode, chat_id=match.chat_id),
                _award_card_coins(match.team_a.owner_id, res_a),
                _award_card_coins(match.team_b.owner_id, res_b),
                record_match_result(winner_id, winner_name, loser_id, loser_name,
                                    is_draw, match.mode, match.chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to persist user stats in background: {e}")

    asyncio.create_task(_persist_results_bg())

    return "\n".join(details)
