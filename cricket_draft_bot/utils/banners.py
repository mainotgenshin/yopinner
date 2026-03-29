# utils/banners.py
"""
Central banner lookup.
Always call get_banner_for_match(match) or get_banner_for_mode(mode)
instead of importing DRAFT_BANNER_* from config directly.
This ensures /banner overrides stored in MongoDB are always respected.
"""

from config import DRAFT_BANNER_IPL, DRAFT_BANNER_INTL, DRAFT_BANNER_FIFA

_DEFAULTS = {
    "ipl": DRAFT_BANNER_IPL,
    "intl": DRAFT_BANNER_INTL,
    "fifa": DRAFT_BANNER_FIFA,
}


async def get_banner_for_mode(mode: str) -> str:
    """Return the active banner URL for mode = 'ipl' | 'intl' | 'fifa'."""
    from database import get_banner
    override = await get_banner(mode)
    return override if override else _DEFAULTS.get(mode, DRAFT_BANNER_INTL)


async def get_banner_for_match(match) -> str:
    """Return the correct banner for a match object."""
    if "IPL" in match.mode:
        return await get_banner_for_mode("ipl")
    elif match.mode == "FIFA":
        return await get_banner_for_mode("fifa")
    else:
        return await get_banner_for_mode("intl")
