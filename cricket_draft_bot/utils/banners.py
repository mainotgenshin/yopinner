# utils/banners.py
"""
Central banner lookup.
Always call get_banner_for_match(match) or get_banner_for_mode(mode)
instead of importing DRAFT_BANNER_* from config directly.
This ensures /banner overrides stored in MongoDB are always respected.
"""

from config import DRAFT_BANNER_IPL, DRAFT_BANNER_ODI, DRAFT_BANNER_TEST, DRAFT_BANNER_FIFA, DRAFT_BANNER_WWE

_DEFAULTS = {
    "ipl":  DRAFT_BANNER_IPL,
    "odi":  DRAFT_BANNER_ODI,
    "intl": DRAFT_BANNER_ODI,   # backward-compat alias
    "test": DRAFT_BANNER_TEST,
    "fifa": DRAFT_BANNER_FIFA,
    "wwe":  DRAFT_BANNER_WWE,
}


async def get_banner_for_mode(mode: str) -> str:
    """Return the active banner URL for mode = 'ipl' | 'odi' | 'test' | 'fifa' | 'wwe'."""
    from database import get_banner
    # Normalise legacy key
    _mode = "odi" if mode == "intl" else mode
    override = await get_banner(_mode)
    return override if override else _DEFAULTS.get(mode, DRAFT_BANNER_ODI)


async def get_banner_for_match(match) -> str:
    """Return the correct banner for a match object."""
    if "IPL" in match.mode:
        return await get_banner_for_mode("ipl")
    elif match.mode == "FIFA":
        return await get_banner_for_mode("fifa")
    elif match.mode == "WWE":
        return await get_banner_for_mode("wwe")
    elif match.mode == "Test":
        return await get_banner_for_mode("test")
    else:  # ODI (and legacy International)
        return await get_banner_for_mode("odi")
