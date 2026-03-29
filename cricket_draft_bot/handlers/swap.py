# handlers/swap.py
"""
Post-Draft Position Swap System.

Flow (Option B: Private DM):
  1. In the group, user clicks "🔀 Swap Positions (1 Left)"
     → Bot replies with a deep-link button to open the DM.
  2. In DM, /start swap_MATCHID is received.
     → Bot shows the user's drafted squad; user picks Player 1.
  3. Bot shows squad again (minus Player 1); user picks Player 2.
  4. Bot swaps their slot positions, saves, edits the group match message.
  5. Swap button disappears for that user.

Callback patterns:
  swapstart_<match_id>       → handle_swap_start  (group: sends deep link)
  swap1_<match_id>_<p_id>    → handle_swap_pick1  (DM: player 1 chosen)
  swap2_<match_id>_<p1id>_<p2id> → handle_swap_pick2 (DM: player 2 chosen → execute)
  swapcancel_<match_id>      → handle_swap_cancel (DM: cancel button)
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown
from game.state import load_match_state, save_match_state

logger = logging.getLogger(__name__)


def esc(t):
    return escape_markdown(str(t), version=1)


def _get_user_team(match, user_id):
    """Return the team object for the given user, or None."""
    if match.team_a.owner_id == user_id:
        return match.team_a
    if match.team_b.owner_id == user_id:
        return match.team_b
    return None


def _build_squad_buttons(team, cb_prefix, exclude_slot=None):
    """Return inline keyboard rows for each filled slot, keyed by SLOT NAME.

    Using slot names (e.g. 'ST/CF', 'Captain') keeps callback_data short and
    avoids the 64-byte Telegram limit that truncated long FIFA player IDs like
    'Ronaldo Luis Nazario de Lima', making player lookup fail silently.
    """
    buttons = []
    for slot_name, player in team.slots.items():
        if not player:
            continue
        if exclude_slot and slot_name == exclude_slot:
            continue
        label = f"{player.name}  ({slot_name})"
        cb = f"{cb_prefix}|{slot_name}"
        # Slot names are always short — no truncation needed
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    return buttons


# ─────────────────────────────────────────────────────────────
# Step 0b: /start swap_MATCHID received in DM
# ─────────────────────────────────────────────────────────────

async def handle_swap_dm_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Called from main.py when /start swap_<match_id> is received in a private chat.
    Builds the squad selection menu for step 1.
    """
    user_id = update.effective_user.id
    args = context.args  # e.g. ["swap_MATCHID"]

    if not args or not args[0].startswith("swap_"):
        return  # Normal /start – handled elsewhere

    match_id = args[0][len("swap_"):]
    match = await load_match_state(match_id)

    if not match:
        await update.message.reply_text("⛔ This swap link is no longer valid (match ended or expired).")
        return

    if match.state not in ("READY_CHECK", "DRAFTING"):
        await update.message.reply_text("⛔ Swap is only available after the draft completes and before both players are ready.")
        return

    team = _get_user_team(match, user_id)
    if not team:
        await update.message.reply_text("⛔ You are not a participant in this match.")
        return

    if getattr(team, "swaps_used", 0) >= 1:
        await update.message.reply_text("⛔ You have already used your swap for this match.")
        return

    # Build squad buttons for step 1 — format: swap1|{match_id}|{player_id}
    buttons = _build_squad_buttons(team, f"swap1|{match_id}")
    if not buttons:
        await update.message.reply_text("⛔ Your squad is empty — nothing to swap.")
        return

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"swapcancel|{match_id}")])
    await update.message.reply_text(
        "🔀 *Swap Positions — Step 1 of 2*\n\nPick the *first* player whose slot you want to reassign:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ─────────────────────────────────────────────────────────────
# Step 1: Player 1 chosen → ask for Player 2
# ─────────────────────────────────────────────────────────────

async def handle_swap_pick1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked the first player. Ask for the second."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # Query may have timed out (e.g. stale callback after bot restart)

    # Format: swap1|<match_id>|<slot_name>
    parts = query.data.split("|")
    if len(parts) < 3:
        await query.edit_message_text("⛔ Invalid selection. Please try again.")
        return

    match_id = parts[1]
    p1_slot = parts[2]  # This is the slot name now, not the player_id

    user_id = query.from_user.id
    match = await load_match_state(match_id)
    if not match:
        await query.edit_message_text("⛔ Match no longer exists.")
        return

    team = _get_user_team(match, user_id)
    if not team:
        await query.edit_message_text("⛔ You are not part of this match.")
        return

    if getattr(team, "swaps_used", 0) >= 1:
        await query.edit_message_text("⛔ You already used your swap.")
        return

    # Look up the player by slot name
    p1_obj = team.slots.get(p1_slot)
    if not p1_obj:
        await query.edit_message_text("⛔ Player not found in your squad.")
        return

    p1_name = p1_obj.name

    # Build step 2 buttons — format: swap2|<match_id>|<p1_slot>|<p2_slot>
    buttons = _build_squad_buttons(team, f"swap2|{match_id}|{p1_slot}", exclude_slot=p1_slot)
    if not buttons:
        await query.edit_message_text("⛔ No other players to swap with.")
        return

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"swapcancel|{match_id}")])

    await query.edit_message_text(
        f"🔀 *Swap Positions — Step 2 of 2*\n\n"
        f"You selected: *{esc(p1_name)}* (currently in *{p1_slot}* slot)\n\n"
        f"Now pick the *second* player to swap with:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ─────────────────────────────────────────────────────────────
# Step 2: Player 2 chosen → execute the swap
# ─────────────────────────────────────────────────────────────

async def handle_swap_pick2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked the second player. Execute the position swap."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # Query may have timed out (e.g. stale callback after bot restart)

    # Format: swap2|<match_id>|<p1_slot>|<p2_slot>
    parts = query.data.split("|")
    if len(parts) < 4:
        await query.edit_message_text("⛔ Invalid selection. Please try again.")
        return

    match_id = parts[1]
    p1_slot = parts[2]  # Slot name for player 1
    p2_slot = parts[3]  # Slot name for player 2

    user_id = query.from_user.id
    match = await load_match_state(match_id)
    if not match:
        await query.edit_message_text("⛔ Match no longer exists.")
        return

    if match.state not in ("READY_CHECK", "DRAFTING"):
        await query.edit_message_text("⛔ Swap is no longer available.")
        return

    team = _get_user_team(match, user_id)
    if not team:
        await query.edit_message_text("⛔ You are not part of this match.")
        return

    if getattr(team, "swaps_used", 0) >= 1:
        await query.edit_message_text("⛔ You already used your swap.")
        return

    # Look up both players by their slot name
    p1_obj = team.slots.get(p1_slot)
    p2_obj = team.slots.get(p2_slot)

    if not p1_obj or not p2_obj:
        await query.edit_message_text("⛔ Could not find one or both players in your squad. Please try again.")
        return

    # Execute the swap
    team.slots[p1_slot] = p2_obj
    team.slots[p2_slot] = p1_obj
    team.swaps_used = 1

    await save_match_state(match)

    # Confirm to user in DM
    await query.edit_message_text(
        f"✅ *Swap Complete!*\n\n"
        f"• *{esc(p1_obj.name)}* is now in the *{p2_slot}* slot\n"
        f"• *{esc(p2_obj.name)}* is now in the *{p1_slot}* slot\n\n"
        f"Head back to the group to click *🚀 READY* when you're set!",
        parse_mode="Markdown"
    )

    # Silently refresh the group message to remove the Swap button for this user
    try:
        from handlers.draft import format_draft_board
        from utils.banners import get_banner_for_match

        board_text = format_draft_board(match, include_turn=False)
        a_status = "✅" if match.team_a.is_ready else "⏳"
        b_status = "✅" if match.team_b.is_ready else "⏳"
        ready_text = (
            f"{board_text}\n\n✅ *Draft Complete!*\n\n"
            f"{esc(match.team_a.owner_name)}: {a_status}\n"
            f"{esc(match.team_b.owner_name)}: {b_status}\n\n"
            f"Waiting for both..."
        )

        keyboard = [[InlineKeyboardButton("🚀 READY", callback_data=f"ready_{match.match_id}")]]

        # Keep Swap button visible if EITHER team still has their swap unconsumed
        a_swaps = getattr(match.team_a, 'swaps_used', 0)
        b_swaps = getattr(match.team_b, 'swaps_used', 0)
        if a_swaps < 1 or b_swaps < 1:
            bot_uname = context.bot.username
            swap_url = f"https://t.me/{bot_uname}?start=swap_{match.match_id}"
            keyboard.append([InlineKeyboardButton("🔀 Swap Positions (1 Left)", url=swap_url)])

        banner = await get_banner_for_match(match)

        await context.bot.edit_message_caption(
            chat_id=match.chat_id,
            message_id=match.draft_message_id,
            caption=ready_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Could not refresh group message after swap: {e}")


# ─────────────────────────────────────────────────────────────
# Cancel
# ─────────────────────────────────────────────────────────────

async def handle_swap_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User clicked Cancel during the swap flow."""
    query = update.callback_query
    await query.answer("Swap cancelled.", show_alert=False)
    await query.edit_message_text("❌ Swap cancelled. Your squad remains unchanged.")
