
import random
import logging

logger = logging.getLogger(__name__)

def apply_stat_rules(stats: dict, roles: list, ipl_roles: list = None) -> dict:
    """
    Applies strict rules to player stats based on their roles.
    Now supports separate roles for IPL vs International.
    """
    
    # 1. International Corrections
    if 'international' in stats:
        _apply_mode_rules(stats['international'], roles)
        
    # 2. IPL Corrections
    if 'ipl' in stats:
        # Use IPL roles if available, else fallback to global roles
        effective_roles = ipl_roles if ipl_roles else roles
        _apply_mode_rules(stats['ipl'], effective_roles)
        
    return stats

def _apply_mode_rules(s: dict, roles: list):
    """
    Helper to apply rules to a single stats dictionary (in-place) based on roles.
    """
    roles_upper = set(r.strip().upper() for r in roles)
    
    def set_val(k, v):
        s[k] = v

    # 0. Ensure all keys exist (fix for "None" in viewer)
    STANDARD_KEYS = [
        "batting_power", "batting_control", "batting_defence",
        "wicket_keeping", "finishing", "bowling_pace",
        "bowling_spin", "all_round", "fielding",
        "leadership", "clutch"
    ]
    for k in STANDARD_KEYS:
        if k not in s or s[k] is None:
            s[k] = 50

    # Rule 1: WK -> pacer = 20, spin = 20
    if "WK" in roles_upper or "WICKET KEEPER" in roles_upper:
        set_val("bowling_pace", 20)
        set_val("bowling_spin", 20)
        
    # Rule 2: Only PACER -> spin=20, wk=20, all=20
    if len(roles_upper) == 1 and "PACER" in roles_upper:
        set_val("bowling_spin", 20)
        set_val("wicket_keeping", 20)
        set_val("all_round", 20)

    # Rule 3: NON-CAPTAIN -> cap = 20
    if "CAPTAIN" not in roles_upper:
        set_val("leadership", 20)

    # Rule 4: ALL-ROUND + SPINNER -> pacer = 20, wk = 20
    has_all_rounder = any(r in roles_upper for r in ["ALL ROUNDER", "ALL-ROUNDER", "ALL"])
    
    if has_all_rounder and "SPINNER" in roles_upper:
         set_val("bowling_pace", 20)
         set_val("wicket_keeping", 20)
         
    # Rule 5: ALL-ROUND + PACER -> spin = 20, wk = 20
    if has_all_rounder and "PACER" in roles_upper:
         set_val("bowling_spin", 20)
         set_val("wicket_keeping", 20)

    # Rule 6: No Fielder Role -> Fielding 65-70
    if "FIELDER" not in roles_upper:
         # To preserve existing randomness, we usually skip unless necessary.
         # But for consistency, let's leave it as is if it exists, roughly.
         pass

    # Rule 7: Pure Batter (No Bowling, No WK) -> Pace=20, Spin=20, WK=20, All=20
    bowling_roles = {"PACER", "SPINNER", "BOWLER", "ALL ROUNDER", "ALL-ROUNDER", "ALL"}
    wk_roles = {"WK", "WICKET KEEPER", "KEEPER", "WICKETKEEPER"}
    
    has_bowling = any(r in bowling_roles for r in roles_upper)
    has_wk = any(r in wk_roles for r in roles_upper)
    
    if not has_bowling and not has_wk:
         set_val("bowling_pace", 20)
         set_val("bowling_spin", 20)
         set_val("wicket_keeping", 20)
         s['all_round'] = 20 

    # Rule 8 & 9: Pure Bowlers
    batting_roles = {"TOP", "MIDDLE", "FINISHER", "BATTER", "OPENER", "DEFENCE"} 
    all_round_roles = {"ALL ROUNDER", "ALL-ROUNDER", "ALL"}
    
    has_batting = any(r in batting_roles for r in roles_upper)
    has_all_round = any(r in all_round_roles for r in roles_upper)
    
    # Pure Pacer
    if "PACER" in roles_upper and not has_batting and not has_all_round and not has_wk:
        set_val("batting_power", 20)   
        set_val("batting_control", 20) 
        set_val("all_round", 20)       
        set_val("bowling_spin", 20)    
        set_val("wicket_keeping", 20)  
        set_val("finishing", 20)       

    # Pure Spinner
    if "SPINNER" in roles_upper and not has_batting and not has_all_round and not has_wk:
        set_val("batting_power", 20)   
        set_val("batting_control", 20) 
        set_val("all_round", 20)       
        set_val("bowling_pace", 20)    
        set_val("wicket_keeping", 20)  
        set_val("finishing", 20)       
        
    # Rule 10: Adjacency (Bleed)
    
    top = s.get('batting_power', 50)
    mid = s.get('batting_control', 50)
    fin = s.get('finishing', 50)
    
    # 1. Top -> Middle Support 
    if top > 75:
        min_mid = top - 15
        if mid < min_mid: s['batting_control'] = min_mid

    # 2. Middle -> Top Support
    if mid > 75:
        min_top = mid - 15
        if top < min_top: s['batting_power'] = min_top
            
    # 3. Middle -> Finisher Support
    if mid > 75:
        min_fin = mid - 15
        if fin < min_fin: s['finishing'] = min_fin
            
    # Rule 11: Primary Role Dominance (Strict Cap)
    has_top = "TOP" in roles_upper or "OPENER" in roles_upper
    has_mid = "MIDDLE" in roles_upper
    
    if has_top and not has_mid:
        val_top = s.get('batting_power', 50)
        target_mid = val_top - 15
        if target_mid < 20: target_mid = 20
        s['batting_control'] = target_mid
            
    if has_mid and not has_top:
        val_mid = s.get('batting_control', 50)
        target_top = val_mid - 15
        if target_top < 20: target_top = 20
        s['batting_power'] = target_top
