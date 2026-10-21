"""
game/mini_cric_engine.py
Tactical 1v1 Mini-Cricket Simulation Engine.
Implements probability matrices (Tables 1, 2, 3), realistic commentary,
milestone badges, and scaled economy calculation.
"""
import random
from typing import Tuple, Dict, Any, Optional

DELIVERIES = ["Bouncer", "Yorker", "Fuller", "Good", "Mystery"]
SHOTS = ["Drive", "Cut", "Pull", "Defend", "Loft"]

# ── Table 2: Wicket Probabilities (percent out of 100) ────────────────────────
WICKET_PROBS = {
    "Yorker": {
        "Pull": 40,
        "Cut": 35,
        "Loft": 14,
        "Drive": 13,
        "Defend": 8,
    },
    "Bouncer": {
        "Drive": 35,
        "Loft": 15,
        "Pull": 10,
        "Cut": 7,
        "Defend": 5,
    },
    "Fuller": {
        "Cut": 25,
        "Loft": 12,
        "Pull": 10,
        "Drive": 6,
        "Defend": 4,
    },
    "Good": {
        "Pull": 18,
        "Loft": 13,
        "Drive": 8,
        "Cut": 8,
        "Defend": 5,
    },
}

# ── Table 1: Run Probabilities (cumulative brackets out of 100) ───────────────
# Brackets format: list of (runs, threshold)
# None indicates invalid shot (guaranteed 0 runs if surviving wicket check)
RUN_PROBS: Dict[str, Dict[str, Optional[list]]] = {
    "Yorker": {
        "Drive":  [(0, 52), (1, 82), (2, 94), (4, 100)],
        "Cut":    None,  # Invalid
        "Pull":   None,  # Invalid
        "Defend": [(0, 90), (1, 100)],
        "Loft":   [(0, 40), (1, 50), (4, 64), (6, 100)],
    },
    "Bouncer": {
        "Drive":  None,  # Invalid
        "Cut":    [(0, 45), (1, 60), (2, 70), (4, 95), (6, 100)],
        "Pull":   [(0, 32), (1, 44), (2, 52), (4, 80), (6, 100)],
        "Defend": [(0, 92), (1, 100)],
        "Loft":   [(0, 48), (1, 54), (4, 70), (6, 100)],
    },
    "Fuller": {
        "Drive":  [(0, 25), (1, 55), (2, 70), (4, 95), (6, 100)],
        "Cut":    None,  # Invalid
        "Pull":   [(0, 32), (1, 54), (2, 60), (4, 85), (6, 100)],
        "Defend": [(0, 88), (1, 100)],
        "Loft":   [(0, 35), (1, 43), (2, 47), (4, 67), (6, 100)],
    },
    "Good": {
        "Drive":  [(0, 38), (1, 66), (2, 78), (4, 96), (6, 100)],
        "Cut":    [(0, 40), (1, 65), (2, 75), (4, 95), (6, 100)],
        "Pull":   [(0, 36), (1, 56), (2, 64), (4, 88), (6, 100)],
        "Defend": [(0, 85), (1, 100)],
        "Loft":   [(0, 42), (1, 48), (2, 50), (4, 72), (6, 100)],
    },
}

# ── Commentary Matrix ─────────────────────────────────────────────────────────
DISMISSAL_COMMENTARY = {
    ("Yorker", "Loft"): "CLEAN BOWLED! Tried to loft a toe-crusher, beaten for pace as the stumps are rattled!",
    ("Yorker", "Drive"): "BOWLED! Squeezed under the bat before the drive could come down! Timber!",
    ("Yorker", "Cut"): "CHOPPED ON! No room to cut that yorker, inside-edged straight onto off-stump!",
    ("Yorker", "Pull"): "BOWLED! Harrowing swipe across the line against a dead-accurate yorker!",
    ("Yorker", "Defend"): "LBW! Crushed right onto the toe before the bat could descend! Plumb in front!",

    ("Bouncer", "Pull"): "OUT! Top edge on the pull! Swirls high into the deep backward square leg's hands!",
    ("Bouncer", "Loft"): "CAUGHT! Spliced high into the sky, easy catch for mid-on running back!",
    ("Bouncer", "Drive"): "OUT! Reached out for a fiery bouncer, faint tickle through to the wicket-keeper!",
    ("Bouncer", "Cut"): "EDGED & TAKEN! Slash at a steep riser, gloves it straight to the keeper!",
    ("Bouncer", "Defend"): "OUT! Ball spit off the shoulder of the bat, pops up softly to silly point!",

    ("Fuller", "Cut"): "CHOPPED ON! Way too full to cut, dragged back onto the woodwork!",
    ("Fuller", "Drive"): "CAUGHT! Drove it in the air without keeping it down, held brilliantly at cover!",
    ("Fuller", "Loft"): "CAUGHT! Didn't get to the pitch of the ball, sliced high and taken at long-off!",
    ("Fuller", "Pull"): "LBW! Attempted a heave across the line, struck below the knee roll!",
    ("Fuller", "Defend"): "BOWLED! Pitched up and sneaked between bat and pad!",

    ("Good", "Drive"): "EDGED & TAKEN! Feathered outside off-stump, standard catch at first slip!",
    ("Good", "Cut"): "CAUGHT! Slashed hard but straight down backward point's throat!",
    ("Good", "Pull"): "CAUGHT! Misjudged the bounce, scooped in the air and taken at mid-wicket!",
    ("Good", "Loft"): "CAUGHT! Skied straight down the ground, safely pouched at long-on!",
    ("Good", "Defend"): "LBW! Straightens off the seam, hits the back pad right in front of middle!",
}

BOUNDARY_FOUR_COMMENTARY = {
    "Drive": "Cracking cover drive! Pierces the gap between extra cover and mid-off for FOUR!",
    "Cut": "Cracking shot! Flashed hard through backward point, races away to the boundary!",
    "Pull": "Dispatched! Rolled the wrists on the pull, beats deep square leg to the ropes!",
    "Loft": "One bounce into the fence! Pierces mid-wicket with ferocious power!",
    "Defend": "Pushed softly into the gap for FOUR!",
}

BOUNDARY_SIX_COMMENTARY = {
    "Loft": "BOOM! Towering lofted drive right back over the bowler's head into the stands! MAXIMUM!",
    "Pull": "PULLED AWAY! Struck with venom over deep mid-wicket, into the top tier! SIX!",
    "Drive": "Stand and deliver! High elbow, lofted through long-off for an effortless SIX!",
    "Cut": "Upper-cut sailed all the way over the third-man ropes for SIX!",
    "Defend": "Defended with unexpected power for SIX!",
}

DOT_COMMENTARY = [
    "Beaten outside off! No run.",
    "Solidly defended, right back to the bowler.",
    "Missed the swing, through to the keeper. Dot ball!",
    "Right on the money! Defended cautiously, no run.",
]


def resolve_mystery_ball() -> str:
    """Table 3: Secretly roll Bouncer (25%), Yorker (25%), Fuller (25%), or Good (25%)."""
    return random.choice(["Bouncer", "Yorker", "Fuller", "Good"])


def simulate_ball(
    delivery: str,
    shot: str,
    is_mystery: bool = False,
    actual_delivery: Optional[str] = None
) -> Tuple[bool, int, str]:
    """
    Simulate the outcome of a single ball.
    Returns: (is_wicket, runs, commentary)
    """
    effective_delivery = actual_delivery or delivery
    if effective_delivery not in WICKET_PROBS:
        effective_delivery = "Good"

    # 1. Wicket Check (Table 2)
    wicket_pct = WICKET_PROBS.get(effective_delivery, {}).get(shot, 10)
    roll = random.randint(1, 100)
    if roll <= wicket_pct:
        comment = DISMISSAL_COMMENTARY.get(
            (effective_delivery, shot),
            f"OUT! {shot} fails against {effective_delivery} delivery!"
        )
        if is_mystery:
            comment = f"⚡ *[Mystery Ball revealed: {effective_delivery}]*\n" + comment
        return True, 0, comment

    # 2. Run Resolution (Table 1)
    brackets = RUN_PROBS.get(effective_delivery, {}).get(shot)
    if brackets is None:
        # Invalid shot combination: survivable but guaranteed 0 runs
        comment = f"Awkward stroke! {shot} was not the right shot against that {effective_delivery}. Dot ball!"
        if is_mystery:
            comment = f"⚡ *[Mystery Ball revealed: {effective_delivery}]*\n" + comment
        return False, 0, comment

    run_roll = random.randint(1, 100)
    runs = 0
    for r, threshold in brackets:
        if run_roll <= threshold:
            runs = r
            break

    # 3. Commentary generation
    if runs == 6:
        comment = BOUNDARY_SIX_COMMENTARY.get(shot, "MASSIVE SIX! Clean strike into the stands!")
    elif runs == 4:
        comment = BOUNDARY_FOUR_COMMENTARY.get(shot, "FOUR! Pierces the infield beautifully!")
    elif runs == 2:
        comment = "Worked into the deep gap, quick running turns one into TWO!"
    elif runs == 1:
        comment = "Pushed into the gap for a brisk single."
    else:
        comment = random.choice(DOT_COMMENTARY)

    if is_mystery:
        comment = f"⚡ *[Mystery Ball revealed: {effective_delivery}]*\n" + comment

    return False, runs, comment


def check_milestone(prev_runs: int, new_runs: int, balls_faced: int) -> Optional[str]:
    """Return milestone commentary line if 50 or 100 crossed."""
    if prev_runs < 50 and new_runs >= 50:
        return f"★ *FIFTY!* Sensational half-century in just {balls_faced} balls! Outstanding batting under pressure!"
    if prev_runs < 100 and new_runs >= 100:
        return f"★ *HUNDRED!* Incredible century in {balls_faced} balls! An extraordinary knock for the ages!"
    return None


WINNER_REWARDS = {
    1: 3,
    2: 5,
    3: 7,
    4: 9,
    5: 13,
}

def calculate_rewards(overs: int, winner_id: int, loser_id: int, is_draw: bool, is_forfeit: bool) -> Dict[int, int]:
    """
    Calculate coin rewards based on allocated overs:
    1 Over: Winner +3, Loser 0
    2 Overs: Winner +5, Loser 0
    3 Overs: Winner +7, Loser 0
    4 Overs: Winner +9, Loser 0
    5 Overs: Winner +13, Loser 0
    Tie: +1 each.
    If forfeit / abandon / /end: 0 coins to both.
    """
    if is_forfeit:
        return {winner_id: 0, loser_id: 0}

    if is_draw:
        return {winner_id: 1, loser_id: 1}

    win_coins = WINNER_REWARDS.get(overs, max(3, overs * 2))
    return {
        winner_id: win_coins,
        loser_id: 0
    }
