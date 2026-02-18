# game/models.py
from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class Player:
    player_id: str
    name: str
    full_name: Optional[str] = None
    role: Optional[str] = None # Legacy singular role
    roles: List[str] = field(default_factory=list) # Cricket Roles
    image_file_id: Optional[str] = None
    ipl_image_file_id: Optional[str] = None
    api_reference: Dict = field(default_factory=dict)
    stats: Dict = field(default_factory=dict) # {"ipl": 45, "international": 50}
    ipl_roles: List[str] = field(default_factory=list)
    
    # FIFA / Generic Fields
    sport: str = "cricket"
    mode: str = "default"
    position: Optional[str] = None # Primary Position (e.g. ST)
    positions: List[str] = field(default_factory=list) # Football Positions
    fifa_image_url: Optional[str] = None
    overall: int = 0 # FIFA Overall Rating
    broken_image: bool = False # If validated and broken
    source_db: Optional[str] = None # "eafc_26" or None
    league: Optional[str] = None
    team: Optional[str] = None

    def get_stat(self, mode: str) -> int:
        # Default to 0, or some base value if stats missing
        return self.stats.get(mode.lower(), 50) 

@dataclass
class Team:
    owner_id: int
    owner_name: str
    slots: Dict[str, Optional[Player]] = field(default_factory=dict) # "Captain": PlayerObject
    redraws_remaining: int = 2
    replacements_remaining: int = 1
    is_ready: bool = False
    score: int = 0
    trades_used: int = 0 # Track trades used (Limit 1)

    # __post_init__ removed to allow dynamic slots via constructor
    
    def is_complete(self) -> bool:
        # Check if we have intended slots and all are filled
        # If slots is empty (not initialized), it's not complete unless that's valid?
        # Assuming slots initialized by factory/match creation
        if not self.slots: return False
        return all(p is not None for p in self.slots.values())

@dataclass
class Match:
    match_id: str
    chat_id: int
    mode: str  # "IPL" or "International"
    team_a: Team
    team_b: Team
    current_turn: int # owner_id of current drafter
    draft_pool: List[str] # List of available player_ids
    state: str = "DRAFTING"
    pending_player_id: Optional[str] = None
    draft_message_id: Optional[int] = None
    card_message_id: Optional[int] = None
    finished_at: float = 0.0 # Timestamp
    trade_offer: Optional[Dict] = None # {initiator: int, target_msg: int, picks: {}}
