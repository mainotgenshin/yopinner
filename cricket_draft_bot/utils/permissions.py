# utils/permissions.py
from config import OWNER_IDS
from database import is_mod
from telegram import Update
import logging

logger = logging.getLogger(__name__)

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

def can_manage_bot(user_id: int) -> bool:
    """Returns True if user is Owner OR Mod."""
    if is_owner(user_id):
        return True
    return is_mod(user_id)

async def check_admin(update: Update) -> bool:
    """
    Helper to check permission and reply if denied.
    Returns True if allowed, False if denied.
    """
    user_id = update.effective_user.id
    if not can_manage_bot(user_id):
        await update.message.reply_text("⛔ You do not have permission to use this command.")
        return False
    return True

async def check_owner(update: Update) -> bool:
    """
    Helper to strictly check owner permission.
    """
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ This command is restricted to Bot Owners.")
        return False
    return True
