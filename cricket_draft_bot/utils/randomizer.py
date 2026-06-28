# utils/randomizer.py
import random
from typing import List, Any

def get_random_player(player_ids: List[str], exclude_ids: List[str] = None) -> str:
    """Selects a random player ID from the list, excluding specified ones.
    
    Pool is shuffled before picking to prevent systematic correlations
    when multiple matches run concurrently (same mode, similar draw state).
    """
    exclude_set = set(exclude_ids) if exclude_ids else set()
    choices = [pid for pid in player_ids if pid not in exclude_set]
    
    if not choices:
        return None
    
    # Shuffle a copy so each draw is independently randomized,
    # regardless of MongoDB's fixed insertion-order sort.
    random.shuffle(choices)
    return choices[0]

def calculate_variance() -> float:
    """Returns a random variance multiplier (e.g., 0.9 to 1.1)."""
    # Reduces variance to +/- 5% (was +/- 10%)
    return random.uniform(0.95, 1.05)

def simulate_event(probability: float) -> bool:
    """Returns True with the given probability (0.0 to 1.0)."""
    return random.random() < probability
