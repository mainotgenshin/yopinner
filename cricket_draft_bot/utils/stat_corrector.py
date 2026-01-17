import random
import logging

logger = logging.getLogger(__name__)

def apply_stat_rules(stats: dict, roles: list) -> dict:
    """
    Applies strict rules to player stats based on their roles.
    This ensures that 'Pure Pacers' don't have high batting stats, etc.
    Modifies the stats dictionary in-place and returns it.
    """
    
    # helper to set stats for all modes
    modes = ['ipl', 'international']
    
    # Normalize roles
    roles_upper = set(r.strip().upper() for r in roles)
    
    def set_stat(key, value):
        for mode in modes:
            # Fix: Only update if mode exists! 
            # run_fix_now.py previously created keys blindly.
            if mode in stats:
                stats[mode][key] = value

    # Rule 1: WK -> pacer = 20, spin = 20
    if "WK" in roles_upper or "WICKET KEEPER" in roles_upper:
        set_stat("bowling_pace", 20)
        set_stat("bowling_spin", 20)
        
    # Rule 2: Only PACER -> spin=20, wk=20, all=20
    if len(roles_upper) == 1 and "PACER" in roles_upper:
        set_stat("bowling_spin", 20)
        set_stat("wicket_keeping", 20)
        set_stat("all_round", 20)

    # Rule 3: NON-CAPTAIN -> cap = 20
    if "CAPTAIN" not in roles_upper:
        set_stat("leadership", 20)

    # Rule 4: ALL-ROUND + SPINNER -> pacer = 20, wk = 20
    has_all_rounder = any(r in roles_upper for r in ["ALL ROUNDER", "ALL-ROUNDER", "ALL"])
    
    if has_all_rounder and "SPINNER" in roles_upper:
         set_stat("bowling_pace", 20)
         set_stat("wicket_keeping", 20)
         
    # Rule 5: ALL-ROUND + PACER -> spin = 20, wk = 20
    if has_all_rounder and "PACER" in roles_upper:
         set_stat("bowling_spin", 20)
         set_stat("wicket_keeping", 20)

    # Rule 6: No Fielder Role -> Fielding 65-70
    if "FIELDER" not in roles_upper:
        # We want consistency between IPL/Intl for the same generation run
        val = random.randint(65, 70)
        set_stat("fielding", val)

    # Rule 7: Pure Batter (No Bowling, No WK) -> Pace=20, Spin=20, WK=20, All=20
    bowling_roles = {"PACER", "SPINNER", "BOWLER", "ALL ROUNDER", "ALL-ROUNDER", "ALL"}
    wk_roles = {"WK", "WICKET KEEPER", "KEEPER", "WICKETKEEPER"}
    
    has_bowling = any(r in bowling_roles for r in roles_upper)
    has_wk = any(r in wk_roles for r in roles_upper)
    
    print(f"DEBUG: StatCorrect Entry. Roles={roles_upper}")
    
    if not has_bowling and not has_wk:
         print("DEBUG: Pure Batter Detected! Forcing Stats...")
         set_stat("bowling_pace", 20)
         set_stat("bowling_spin", 20)
         set_stat("wicket_keeping", 20)
         set_stat("all_round", 20)

    # Rule 8 & 9: Pure Bowlers (Pacer/Spinner) -> Top=20, Middle=20, All=20, Fin=20
    batting_roles = {"TOP", "MIDDLE", "FINISHER", "BATTER", "OPENER", "DEFENCE"} 
    all_round_roles = {"ALL ROUNDER", "ALL-ROUNDER", "ALL"}
    
    has_batting = any(r in batting_roles for r in roles_upper)
    has_all_round = any(r in all_round_roles for r in roles_upper)
    
    # Pure Pacer
    if "PACER" in roles_upper and not has_batting and not has_all_round and not has_wk:
        set_stat("batting_power", 20)   # Top
        set_stat("batting_control", 20) # Middle
        set_stat("all_round", 20)       # All Round
        set_stat("bowling_spin", 20)    # No Spin
        set_stat("wicket_keeping", 20)  # No WK
        set_stat("finishing", 20)       # No Finisher

    # Pure Spinner
    if "SPINNER" in roles_upper and not has_batting and not has_all_round and not has_wk:
        set_stat("batting_power", 20)   # Top
        set_stat("batting_control", 20) # Middle
        set_stat("all_round", 20)       # All Round
        set_stat("bowling_pace", 20)    # No Pace
        set_stat("wicket_keeping", 20)  # No WK
        set_stat("finishing", 20)       # No Finisher
        
    return stats
