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
    
    if not has_bowling and not has_wk:
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
        
    # Rule 10: Stat Adjacency (Bleed) - Ensure specialists have floors in adjacent roles
    # Only applies if NOT a Pure Bowler (checked implicitly by clamps logic, but we run this after clamps)
    
    for mode in modes:
        if mode not in stats: continue
        
        s = stats[mode]
        top = s.get('batting_power', 50)
        mid = s.get('batting_control', 50)
        fin = s.get('finishing', 50)
        
        changed = False
        
        # 1. Top -> Middle Support (Power Hitter can usually play decent middle)
        if top > 75:
            min_mid = top - 15
            if mid < min_mid:
                s['batting_control'] = min_mid
                changed = True

        # 2. Middle -> Top Support (Control player can usually play decent top)
        if mid > 75:
            min_top = mid - 15
            if top < min_top:
                s['batting_power'] = min_top
                changed = True
                
        # 3. Middle -> Finisher Support (Control player can usually finish)
        if mid > 75:
            min_fin = mid - 15
            if fin < min_fin:
                s['finishing'] = min_fin
                s['finishing'] = min_fin
                changed = True
                
        # Rule 11: Primary Role Dominance (Specialist Bias)
        # Enforce that specialists are strictly better at their primary role.
        # Logic: Secondary = Primary - 15 (Cap)
        
        has_top = "TOP" in roles_upper or "OPENER" in roles_upper
        has_mid = "MIDDLE" in roles_upper
        
        # Pure Top: Middle should not exceed Top - 15
        if has_top and not has_mid:
            # We already have a floor (Middle >= Top - 15) from Rule 10 (Adjacency)
            # If we enforce Middle <= Top - 15, then Middle == Top - 15.
            # This creates a deterministic spread which is what the user asked for.
            
            target_mid = s['batting_power'] - 15
            # Ensure strictly capped, but don't go below 20 unless purely inept (handled earlier)
            if target_mid < 20: target_mid = 20
            
            # Apply Cap
            s['batting_control'] = target_mid
            changed = True
                
        # Pure Middle: Top should not exceed Middle - 15
        if has_mid and not has_top:
            target_top = s['batting_control'] - 15
            if target_top < 20: target_top = 20
            
            s['batting_power'] = target_top
            changed = True

        if changed:
            stats[mode] = s

    return stats
