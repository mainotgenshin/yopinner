# utils/permissions.py
from config import OWNER_IDS
from database import is_mod
from telegram import Update
import logging

logger = logging.getLogger(__name__)

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

async def can_manage_bot(user_id: int) -> bool:
    """Returns True if user is Owner OR Mod."""
    if is_owner(user_id):
        return True
    return await is_mod(user_id)

async def check_admin(update: Update) -> bool:
    """
    Helper to check permission.
    Returns True if allowed, False if denied.
    Silent ignore: does not send any message if denied, saving API quota and preventing lag.
    """
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    if not await can_manage_bot(user_id):
        return False
    return True

async def check_owner(update: Update) -> bool:
    """
    Helper to strictly check owner permission.
    Silent ignore: does not send any message if denied, saving API quota and preventing lag.
    """
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return False
    return True
