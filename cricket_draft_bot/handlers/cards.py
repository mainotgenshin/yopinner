# handlers/cards.py
"""
Card System handlers: /pack, /inventory, /mycards, /viewcard, /trade_card, /quest
All coin/card mutations are protected with per-user asyncio locks.
"""
import asyncio
import html
import logging
import time
import uuid
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

# ── Per-user anti-spam lock ───────────────────────────────────────────────────
_CARD_LOCKS: dict[int, asyncio.Lock] = {}
_CARD_CMD_COOLDOWNS: dict = {}

def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _CARD_LOCKS:
        if len(_CARD_LOCKS) > 500:
            for uid in list(_CARD_LOCKS.keys()):
                l = _CARD_LOCKS[uid]
                if not l.locked():
                    _CARD_LOCKS.pop(uid, None)
                    if len(_CARD_LOCKS) <= 250:
                        break
        _CARD_LOCKS[user_id] = asyncio.Lock()
    return _CARD_LOCKS[user_id]

def _check_card_cooldown(user_id: int, action: str, cooldown_secs: float = 2.0) -> bool:
    now = time.time()
    key = f"{user_id}_{action}"
    if now - _CARD_CMD_COOLDOWNS.get(key, 0) < cooldown_secs:
        return False
    _CARD_CMD_COOLDOWNS[key] = now
    if len(_CARD_CMD_COOLDOWNS) > 500:
        cutoff = now - 60
        for k in list(_CARD_CMD_COOLDOWNS.keys()):
            if _CARD_CMD_COOLDOWNS[k] < cutoff:
                _CARD_CMD_COOLDOWNS.pop(k, None)
    return True

# ── Display helpers ───────────────────────────────────────────────────────────
RARITY_EMOJI = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legend": "🟡", "mythical": "✨"}
PACK_EMOJI   = {"basic": "🟦", "premium": "🟣", "elite": "🟡"}
SPORT_EMOJI  = {"cricket": "🏏", "football": "⚽", "wwe": "🤼", "kabaddi": "🤸"}
SPORT_LABEL  = {"cricket": "Cricket", "football": "FIFA", "wwe": "WWE Men", "kabaddi": "PKL"}
FORMAT_LABEL = {"ipl": "IPL", "odi": "ODI", "test": "Test", "wwe": "WWE", "fifa": "FIFA", "pkl": "PKL"}
PACK_PRICES  = {"basic": 250, "premium": 550, "elite": 1200}
PACK_ODDS_TEXT = {
    "basic":   "60% Common | 35% Rare | 5% Epic | 0% Legend",
    "premium": "15% Common | 45% Rare | 35% Epic | 5% Legend",
    "elite":   "0% Common | 15% Rare | 55% Epic | 30% Legend",
}
CARDS_PER_PAGE    = 10
CARDS_PER_PAGE_DM = 20  # Larger page in private chats

async def get_user_sort(context_or_uid, user_id: int = None) -> tuple:
    """Return (criteria, order) for user. Reads from persistent DB cache."""
    uid = user_id if user_id is not None else context_or_uid
    from database import get_user_card_sort
    return await get_user_card_sort(uid)

def get_sort_label(criteria: str, order: str) -> str:
    order = (order or "desc").lower()
    if order in ("des", "descending"): order = "desc"
    if order in ("ascending",): order = "asc"
    criteria = (criteria or "rarity").lower()

    if criteria == "rarity":
        desc = "Legend ➔ Common" if order == "desc" else "Common ➔ Legend"
        return f"⭐ Rarity ({desc})"
    elif criteria == "ovr":
        desc = "Highest ➔ Lowest" if order == "desc" else "Lowest ➔ Highest"
        return f"📊 OVR ({desc})"
    elif criteria == "dupe":
        desc = "Most ➔ Fewest" if order == "desc" else "Fewest ➔ Most"
        return f"🔁 Dupes ({desc})"
    elif criteria == "name":
        desc = "A ➔ Z" if order == "asc" else "Z ➔ A"
        return f"🔤 Name ({desc})"
    elif criteria in ("ipl", "odi", "test", "fifa", "wwe", "pkl"):
        f_name = {"ipl": "IPL", "odi": "ODI", "test": "Test", "fifa": "FIFA", "wwe": "WWE", "pkl": "PKL"}.get(criteria, criteria.upper())
        return f"🏷️ {f_name} First"
    elif criteria == "format":
        return "📂 Grouped by Format"
    return f"{criteria.title()} ({order.upper()})"

def esc(t): return escape_markdown(str(t), version=1)

def _rarity_line(rarity: str, ovr: int, name: str, fmt: str) -> str:
    r_emoji = RARITY_EMOJI.get(rarity, "⚪")
    f_label = FORMAT_LABEL.get(fmt, fmt.upper())
    return f"{r_emoji} {name} ({f_label}) — OVR: {ovr}"

async def _get_raw_inv(user_id: int) -> dict:
    """Return the raw pack_inventory dict from DB."""
    from database import get_db
    db = get_db()
    doc = await db.users.find_one({"user_id": user_id}, {"pack_inventory": 1})
    return doc.get("pack_inventory", {}) if doc else {}

def _sort_cards(cards: list, criteria: str = "rarity", order: str = "desc") -> list:
    """Sort a card list by criteria and order ('asc' or 'desc')."""
    rarity_desc = {"mythical": 0, "legend": 1, "epic": 2, "rare": 3, "common": 4}
    rarity_asc  = {"common": 0, "rare": 1, "epic": 2, "legend": 3, "mythical": 4}
    format_order = {"ipl": 0, "odi": 1, "test": 2, "fifa": 3, "wwe": 4, "pkl": 5}

    criteria = (criteria or "rarity").lower()
    order = (order or "desc").lower()
    if order in ("des", "descending"): order = "desc"
    if order in ("ascending",): order = "asc"

    if criteria == "rarity":
        r_map = rarity_asc if order == "asc" else rarity_desc
        return sorted(cards, key=lambda c: (r_map.get(str(c.get("rarity", "common")).lower(), 9), c.get("name", "").lower()))
    elif criteria == "ovr":
        mult = 1 if order == "asc" else -1
        return sorted(cards, key=lambda c: (mult * int(c.get("ovr", 0) or 0), c.get("name", "").lower()))
    elif criteria == "dupe":
        mult = 1 if order == "asc" else -1
        return sorted(cards, key=lambda c: (mult * int(c.get("quantity", 1) or 1), rarity_desc.get(str(c.get("rarity", "common")).lower(), 9), c.get("name", "").lower()))
    elif criteria == "name":
        rev = (order == "desc")
        return sorted(cards, key=lambda c: c.get("name", "").lower(), reverse=rev)
    elif criteria in ("ipl", "odi", "test", "fifa", "wwe", "pkl"):
        r_map = rarity_asc if order == "asc" else rarity_desc
        return sorted(
            cards,
            key=lambda c: (
                0 if str(c.get("format", "")).lower() == criteria else 1,
                r_map.get(str(c.get("rarity", "common")).lower(), 9),
                -int(c.get("ovr", 0) or 0),
                c.get("name", "").lower()
            )
        )
    elif criteria == "format":
        r_map = rarity_asc if order == "asc" else rarity_desc
        return sorted(
            cards,
            key=lambda c: (
                format_order.get(str(c.get("format", "")).lower(), 99),
                r_map.get(str(c.get("rarity", "common")).lower(), 9),
                -int(c.get("ovr", 0) or 0),
                c.get("name", "").lower()
            )
        )
    return sorted(cards, key=lambda c: (rarity_desc.get(str(c.get("rarity", "common")).lower(), 9), c.get("name", "").lower()))

# ─────────────────────────────────────────────────────────────────────────────
# /pack
# ─────────────────────────────────────────────────────────────────────────────
async def handle_pack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat and update.effective_chat.type != "private":
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏏 Cricket", callback_data=f"pack_sport|{user.id}|cricket"),
        InlineKeyboardButton("⚽ FIFA",    callback_data=f"pack_sport|{user.id}|football"),
        InlineKeyboardButton("🤼 WWE Men", callback_data=f"pack_sport|{user.id}|wwe"),
        InlineKeyboardButton("🤸 PKL",     callback_data=f"pack_sport|{user.id}|kabaddi"),
    ]])
    await update.effective_message.reply_text(
        "🃏 *Pack Store*\nChoose a sport:",
        reply_markup=keyboard, parse_mode="Markdown"
    )

async def cb_pack_sport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, owner_id, sport = query.data.split("|")
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return
    await query.answer()
    sport_label = SPORT_LABEL.get(sport, sport.title())
    sport_emoji = SPORT_EMOJI.get(sport, "🃏")
    text = f"🃏 *Pack Store — {sport_emoji} {sport_label}*\n\nEach pack contains *3 cards* from this sport's players.\n\n"
    for tier in ["basic", "premium", "elite"]:
        text += f"{PACK_EMOJI[tier]} *{tier.title()} Pack* — {PACK_PRICES[tier]}🪙\n`{PACK_ODDS_TEXT[tier]}`\n\n"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🟦 Basic {PACK_PRICES['basic']}🪙",   callback_data=f"pack_tier|{owner_id}|{sport}|basic"),
            InlineKeyboardButton(f"🟣 Premium {PACK_PRICES['premium']}🪙", callback_data=f"pack_tier|{owner_id}|{sport}|premium"),
            InlineKeyboardButton(f"🟡 Elite {PACK_PRICES['elite']}🪙",   callback_data=f"pack_tier|{owner_id}|{sport}|elite"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"pack_back|{owner_id}")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def cb_pack_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, owner_id, sport, tier = query.data.split("|")
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return
    await query.answer()
    from database import get_card_coins
    balance = await get_card_coins(int(owner_id))
    price = PACK_PRICES[tier]
    text = (
        f"🛒 *Confirm Purchase*\n\n"
        f"{PACK_EMOJI[tier]} *{tier.title()} {SPORT_LABEL[sport]} Pack*\n"
        f"Cost: *{price}🪙* | Your balance: *{balance}🪙*\n\n"
    )
    if balance < price:
        text += "❌ *Insufficient card coins!*"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=f"pack_sport|{owner_id}|{sport}")]])
    else:
        text += "Confirm to add this pack to your inventory."
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"pack_confirm|{owner_id}|{sport}|{tier}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"pack_sport|{owner_id}|{sport}"),
        ]])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

async def cb_pack_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, owner_id, sport, tier = query.data.split("|")
    user_id = query.from_user.id
    if str(user_id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return

    # ── DB-level atomic cooldown (primary spam-click guard) ──────────────────
    # asyncio.Lock only protects concurrent requests; DB cooldown protects
    # sequential spam clicks (each completes before the next starts).
    from database import try_acquire_action_cooldown
    allowed = await try_acquire_action_cooldown(user_id, "pack_buy", cooldown_seconds=5)
    if not allowed:
        await query.answer("⏳ Please wait before buying again!", show_alert=False)
        return
    # ── asyncio.Lock (secondary guard for truly concurrent requests) ─────────
    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        await query.answer()
        from database import deduct_card_coins, add_pack
        price = PACK_PRICES[tier]
        success, new_bal = await deduct_card_coins(user_id, price)
        if not success:
            await query.edit_message_text(f"❌ *Not enough coins!*\nNeed {price}🪙, have {new_bal}🪙.", parse_mode="Markdown")
            return
        pack_key = f"{tier}_{sport}"
        await add_pack(user_id, pack_key)
        await query.edit_message_text(
            f"✅ *{PACK_EMOJI[tier]} {tier.title()} {SPORT_LABEL[sport]} Pack* added to inventory!\n"
            f"Balance: *{new_bal}🪙* | Use /inventory to open packs.",
            parse_mode="Markdown"
        )


async def cb_pack_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, owner_id = query.data.split("|")
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return
    await query.answer()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏏 Cricket", callback_data=f"pack_sport|{owner_id}|cricket"),
        InlineKeyboardButton("⚽ FIFA",    callback_data=f"pack_sport|{owner_id}|football"),
        InlineKeyboardButton("🤼 WWE Men", callback_data=f"pack_sport|{owner_id}|wwe"),
        InlineKeyboardButton("🤸 PKL",     callback_data=f"pack_sport|{owner_id}|kabaddi"),
    ]])
    await query.edit_message_text("🃏 *Pack Store*\nChoose a sport:", reply_markup=keyboard, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /inventory
# ─────────────────────────────────────────────────────────────────────────────
async def handle_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat and update.effective_chat.type != "private":
        return
    from database import get_card_coins
    balance = await get_card_coins(user.id)
    raw_inv = await _get_raw_inv(user.id)
    # Count packs per sport+tier
    total_packs = sum(v for v in raw_inv.values() if isinstance(v, int) and v > 0)
    text = (
        f"💼 *Your Inventory*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Card Coins: *{balance}🪙*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Total Packs: *{total_packs}*\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    kb_rows = [[InlineKeyboardButton("📦 Your Packs", callback_data=f"inv_packs|{user.id}")]]
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="Markdown")

async def cb_inv_packs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of packs user owns with Open buttons."""
    query = update.callback_query
    _, owner_id = query.data.split("|")
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return
    await query.answer()
    raw_inv = await _get_raw_inv(int(owner_id))
    # Build pack list
    lines = ["📦 *Your Packs*\n━━━━━━━━━━━━━━━━━━"]
    buttons = []
    has_any = False
    for tier in ["basic", "premium", "elite"]:
        for sport in ["cricket", "football", "wwe", "kabaddi"]:
            key = f"{tier}_{sport}"
            qty = raw_inv.get(key, 0)
            if qty > 0:
                has_any = True
                label = f"{PACK_EMOJI[tier]} {tier.title()} {SPORT_LABEL[sport]} ×{qty}"
                lines.append(label)
                buttons.append([InlineKeyboardButton(
                    f"Open {PACK_EMOJI[tier]} {tier.title()} {SPORT_LABEL[sport]}",
                    callback_data=f"inv_open|{owner_id}|{tier}|{sport}"
                )])
    if not has_any:
        lines.append("You have no packs. Use /pack to buy some!")
    text = "\n".join(lines)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, parse_mode="Markdown")

async def cb_inv_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open a pack — draw 3 cards. Anti-spam locked."""
    query = update.callback_query
    _, owner_id, tier, sport = query.data.split("|")
    user_id = query.from_user.id
    if str(user_id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return

    # ── DB-level atomic cooldown (primary spam-click guard) ──────────────────
    from database import try_acquire_action_cooldown
    allowed = await try_acquire_action_cooldown(user_id, "pack_open", cooldown_seconds=5)
    if not allowed:
        await query.answer("⏳ Please wait before opening another pack!", show_alert=False)
        return
    # ── asyncio.Lock (secondary guard for truly concurrent requests) ─────────
    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        await query.answer("Opening pack...")
        from database import use_pack, draw_pack_cards, add_card_to_user, get_user_card, increment_quest_progress, count_user_cards
        pack_key = f"{tier}_{sport}"
        consumed = await use_pack(user_id, pack_key)
        if not consumed:
            await query.answer("❌ No pack of that type!", show_alert=True); return
        drawn = await draw_pack_cards(tier, sport, 3)
        if not drawn:
            # Refund if pool empty
            from database import add_pack
            await add_pack(user_id, pack_key)
            await query.answer("❌ Card pool is empty. Pack refunded.", show_alert=True); return
        # Add cards to user collection
        card_lines = []
        cards_obtained = 0
        for card in drawn:
            existing = await get_user_card(user_id, card["player_id"], card["format"])
            new_qty = await add_card_to_user(user_id, card["player_id"], card["format"])
            r_emoji = RARITY_EMOJI.get(card["rarity"], "⚪")
            f_label = FORMAT_LABEL.get(card["format"], card["format"].upper())
            line = f"{r_emoji} *{esc(card['name'])}* ({f_label}) — OVR: {card['ovr']}"
            if existing is None:
                line += "  ✨ NEW!"
            elif new_qty > 1:
                line += f"  +1 duplicate"
            card_lines.append(line)
            cards_obtained += 1
        # Quest progress
        await increment_quest_progress(user_id, "cards_obtained", cards_obtained)
        total = await count_user_cards(user_id)
        sport_name = SPORT_LABEL[sport]
        text = (
            f"🎊 *{PACK_EMOJI[tier]} {tier.title()} {sport_name} Pack Opened!*\n"
            f"━━━━━━━━━━━━━━━━━━\n" +
            "\n".join(card_lines) +
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"Collection: *{total} cards*"
        )
        await query.edit_message_text(text, parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /mycards
# ─────────────────────────────────────────────────────────────────────────────
async def handle_mycards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id if user else (update.effective_chat.id if update.effective_chat else 0)
    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    sport_filter = None
    if context.args:
        arg = context.args[0].lower()
        if arg in ("ipl", "odi", "test", "cricket"):
            sport_filter = "cricket"
        elif arg in ("fifa", "football"):
            sport_filter = "football"
        elif arg in ("wwe",):
            sport_filter = "wwe"
        elif arg in ("pkl", "kabaddi"):
            sport_filter = "kabaddi"

    await _show_mycards(update.effective_message, user_id, user_id,
                        sport_filter=sport_filter, page=0, edit=False,
                        is_dm=is_dm)

async def _show_mycards(message_or_query, owner_id: int, viewer_id: int,
                        sport_filter, page: int, edit: bool,
                        is_dm: bool = False, context=None):
    from database import get_user_cards
    cards = await get_user_cards(owner_id, sport_filter=sport_filter)

    # Determine sort from persistent user preference
    crit, order = await get_user_sort(owner_id)
    cards = _sort_cards(cards, crit, order)

    page_size  = CARDS_PER_PAGE_DM if is_dm else CARDS_PER_PAGE
    total_unique = len(cards)
    start        = page * page_size
    page_cards   = cards[start:start + page_size]
    total_pages  = max(1, (total_unique + page_size - 1) // page_size)

    sf = (sport_filter or "all").lower()
    sport_name = SPORT_LABEL.get(sf, sf.title()) if sf != "all" else ""

    if not cards:
        if sf != "all":
            text = (
                f"🃏 <b>Your Collection ({html.escape(sport_name)})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"No {html.escape(sport_name)} cards yet!\n"
                f"Use /pack to buy {html.escape(sport_name)} packs."
            )
        else:
            text = "🃏 <b>Your Collection</b>\n━━━━━━━━━━━━━━━━━━\nNo cards yet! Use /pack to buy packs."
    else:
        filter_tag = f" ({html.escape(sport_name)})" if sport_name else ""
        sort_label = html.escape(get_sort_label(crit, order))
        lines = [f"🃏 <b>Your Collection</b>{filter_tag}  <i>{sort_label}</i>\n━━━━━━━━━━━━━━━━━━"]
        for idx, c in enumerate(page_cards, start=start + 1):
            r_emoji = RARITY_EMOJI.get(c.get("rarity"), "⚪")
            f_label = FORMAT_LABEL.get(c.get("format"), str(c.get("format", "")).upper())
            qty_str = f" ×{c['quantity']}" if c.get("quantity", 1) > 1 else ""
            ovr_str = f" OVR {c['ovr']}" if crit == "ovr" and c.get("ovr") else ""
            c_name = html.escape(str(c.get('name', 'Unknown')))
            lines.append(f"<code>{idx}.</code> {r_emoji} {c_name} ({f_label}){qty_str}{ovr_str}")
        lines.append(f"━━━━━━━━━━━━━━━━━━\nPage {page+1}/{total_pages}")
        text = "\n".join(lines)

    # Navigation buttons
    nav = []
    if total_pages > 10 and page > 0:
        nav.append(InlineKeyboardButton("⏮ -10", callback_data=f"mc_page|{owner_id}|{sf}|{max(0, page-10)}"))
    if total_pages > 5 and page > 0:
        nav.append(InlineKeyboardButton("⏪ -5", callback_data=f"mc_page|{owner_id}|{sf}|{max(0, page-5)}"))
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"mc_page|{owner_id}|{sf}|{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"mc_page|{owner_id}|{sf}|{page+1}"))
    if total_pages > 5 and page < total_pages - 1:
        nav.append(InlineKeyboardButton("+5 ⏩", callback_data=f"mc_page|{owner_id}|{sf}|{min(total_pages-1, page+5)}"))
    if total_pages > 10 and page < total_pages - 1:
        nav.append(InlineKeyboardButton("+10 ⏭", callback_data=f"mc_page|{owner_id}|{sf}|{min(total_pages-1, page+10)}"))

    filters = [
        InlineKeyboardButton("All", callback_data=f"mc_page|{owner_id}|all|0"),
        InlineKeyboardButton("🏏",  callback_data=f"mc_page|{owner_id}|cricket|0"),
        InlineKeyboardButton("⚽",  callback_data=f"mc_page|{owner_id}|football|0"),
        InlineKeyboardButton("🤼",  callback_data=f"mc_page|{owner_id}|wwe|0"),
        InlineKeyboardButton("🤸",  callback_data=f"mc_page|{owner_id}|kabaddi|0"),
    ]
    collections_row = [InlineKeyboardButton("📊 Collections", callback_data=f"mc_collections|{owner_id}")]
    rows = [filters]
    if nav:
        rows.append(nav)
    rows.append(collections_row)
    kb = InlineKeyboardMarkup(rows)

    if edit:
        try:
            if kb:
                await message_or_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            else:
                await message_or_query.edit_message_text(text, parse_mode="HTML")
        except Exception as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                pass
            else:
                logger.warning(f"_show_mycards HTML edit error: {e}, falling back to plain text")
                try:
                    if kb:
                        await message_or_query.edit_message_text(text, reply_markup=kb)
                    else:
                        await message_or_query.edit_message_text(text)
                except Exception:
                    pass
    else:
        try:
            if kb:
                await message_or_query.reply_text(text, reply_markup=kb, parse_mode="HTML")
            else:
                await message_or_query.reply_text(text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"_show_mycards reply error: {e}, falling back to plain text")
            if kb:
                await message_or_query.reply_text(text, reply_markup=kb)
            else:
                await message_or_query.reply_text(text)


async def cb_mc_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    parts = query.data.split("|")
    if len(parts) < 4:
        await query.answer()
        return
    _, owner_id, sport_str, page_str = parts

    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    page_cd = 0.3 if is_dm else 1.2

    if not _check_card_cooldown(query.from_user.id, "mc_page", page_cd):
        msg = "Slow down!" if is_dm else "⏳ Please wait a moment between page turns."
        await query.answer(msg, show_alert=False)
        return

    if str(owner_id) not in ("1087968824", "777000") and str(query.from_user.id) != str(owner_id):
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    await query.answer()
    sport_filter = None if sport_str == "all" else sport_str
    try:
        oid_int = int(owner_id)
        if oid_int in (1087968824, 777000):
            oid_int = query.from_user.id
    except (ValueError, TypeError):
        oid_int = query.from_user.id

    await _show_mycards(query, oid_int, query.from_user.id,
                        sport_filter, int(page_str), edit=True,
                        is_dm=is_dm, context=context)


async def cb_mc_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sort button callback (backward-compat if old message button clicked)."""
    query = update.callback_query
    if not query:
        return
    parts = query.data.split("|")
    if len(parts) < 4:
        await query.answer()
        return
    _, owner_id, new_sort, sport_str = parts

    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    sort_cd = 0.3 if is_dm else 1.2
    if not _check_card_cooldown(query.from_user.id, "mc_sort", sort_cd):
        await query.answer("Slow down!", show_alert=False)
        return

    if str(owner_id) not in ("1087968824", "777000") and str(query.from_user.id) != str(owner_id):
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    try:
        oid_int = int(owner_id)
        if oid_int in (1087968824, 777000):
            oid_int = query.from_user.id
    except (ValueError, TypeError):
        oid_int = query.from_user.id

    _, cur_order = await get_user_sort(oid_int)
    from database import save_user_card_sort
    await save_user_card_sort(oid_int, new_sort, cur_order)
    await query.answer(f"Sorted by: {get_sort_label(new_sort, cur_order)}")
    sport_filter = None if sport_str == "all" else sport_str
    await _show_mycards(query, oid_int, query.from_user.id,
                        sport_filter, 0, edit=True,
                        is_dm=is_dm, context=context)


async def handle_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sort [criteria] [asc|des]
    Examples:
      /sort rarity
      /sort rarity asc
      /sort asc
      /sort des
      /sort ovr
      /sort ovr des
      /sort name
      /sort name des
      /sort dupe
    """
    user = update.effective_user
    if not user:
        return
    if not _check_card_cooldown(user.id, "sort", 2.0):
        return
    args = [a.lower() for a in (context.args or [])]

    crit_keys = {
        "rarity", "ovr", "dupe", "duplicate", "dupes", "name", "alphabetical",
        "ipl", "odi", "test", "fifa", "wwe", "pkl", "kabaddi", "format", "formats"
    }
    dir_keys  = {"asc", "ascending", "des", "desc", "descending"}

    crit_alias = {
        "duplicate": "dupe", "dupes": "dupe", "alphabetical": "name",
        "kabaddi": "pkl", "formats": "format"
    }
    dir_alias  = {"ascending": "asc", "des": "desc", "descending": "desc"}

    cur_crit, cur_order = await get_user_sort(user.id)

    if not args:
        cur_label = get_sort_label(cur_crit, cur_order)
        await update.effective_message.reply_text(
            f"📊 <b>Card Sorting Options</b>\n\n"
            f"Current: <b>{cur_label}</b>\n\n"
            f"<b>General Criteria:</b>\n"
            f"• <code>/sort rarity</code> — Legend ➔ Common (default)\n"
            f"• <code>/sort rarity asc</code> — Common ➔ Legend\n"
            f"• <code>/sort ovr</code> — Highest OVR first\n"
            f"• <code>/sort ovr asc</code> — Lowest OVR first\n"
            f"• <code>/sort dupe</code> — Most duplicates first\n"
            f"• <code>/sort name</code> — A ➔ Z\n\n"
            f"<b>Format Criteria:</b>\n"
            f"• <code>/sort format</code> — Group by format (IPL ➔ ODI ➔ Test ➔ FIFA ➔ WWE ➔ PKL)\n"
            f"• <code>/sort ipl</code> — IPL cards priority on top\n"
            f"• <code>/sort odi</code> — ODI cards priority on top\n"
            f"• <code>/sort test</code> — Test cards priority on top\n"
            f"• <code>/sort fifa</code> — FIFA cards priority on top\n"
            f"• <code>/sort wwe</code> — WWE cards priority on top\n"
            f"• <code>/sort pkl</code> — PKL cards priority on top\n\n"
            f"<b>Direction Only:</b>\n"
            f"• <code>/sort asc</code> — Ascending order\n"
            f"• <code>/sort des</code> — Descending order\n\n"
            f"<i>Sort applies across /mycards, /multi_sell, and trade.</i>",
            parse_mode="HTML"
        )
        return

    new_crit = None
    new_dir  = None

    for arg in args:
        if arg in crit_keys:
            new_crit = crit_alias.get(arg, arg)
        elif arg in dir_keys:
            new_dir = dir_alias.get(arg, arg)

    if not new_crit and not new_dir:
        await update.effective_message.reply_text(
            "❌ Unknown sort option.\nUse <code>/sort rarity</code>, <code>/sort ovr</code>, <code>/sort dupe</code>, <code>/sort name</code>, <code>/sort format</code>, or specific formats: <code>ipl</code>, <code>odi</code>, <code>test</code>, <code>fifa</code>, <code>wwe</code>, <code>pkl</code>.",
            parse_mode="HTML"
        )
        return

    # If only direction given, keep current criteria
    if new_dir and not new_crit:
        new_crit = cur_crit

    # If only criteria given, default direction:
    # name defaults to 'asc', others default to 'desc'
    if new_crit and not new_dir:
        new_dir = "asc" if new_crit == "name" else "desc"

    from database import save_user_card_sort
    await save_user_card_sort(user.id, new_crit, new_dir)

    label = get_sort_label(new_crit, new_dir)
    await update.effective_message.reply_text(
        f"✅ Sort set to: <b>{label}</b>\n\n<i>Your /mycards, /multi_sell, and trades will now display in this order.</i>",
        parse_mode="HTML"
    )


async def _get_collections_text(owner_id: int) -> str:
    from database import get_user_cards, get_catalog_totals
    all_cards = await get_user_cards(owner_id)
    cat_totals = await get_catalog_totals()
    cat_fmt_rarity = cat_totals.get("by_format_rarity", {})
    cat_fmt_total  = cat_totals.get("by_format", {})
    cat_grand      = cat_totals.get("grand_total", 0)

    # Group by sport → format → rarity
    FORMAT_TO_SPORT = {"ipl": "Cricket", "odi": "Cricket", "test": "Cricket",
                       "fifa": "FIFA", "wwe": "WWE", "pkl": "PKL"}
    SPORT_EMOJI_MAP = {"Cricket": "🏏", "FIFA": "⚽", "WWE": "🤼", "PKL": "🤸"}
    FORMAT_ORDER = ["ipl", "odi", "test", "pkl", "fifa", "wwe"]
    RARITY_ORDER = ["legend", "epic", "rare", "common"]

    # Unique owned cards per format and rarity
    counts: dict = {}
    for card in all_cards:
        fmt = card.get("format", "")
        rarity = card.get("rarity", "common")
        if fmt not in counts:
            counts[fmt] = {"common": 0, "rare": 0, "epic": 0, "legend": 0}
        counts[fmt][rarity] = counts[fmt].get(rarity, 0) + 1

    lines = ["📊 <b>Your Collections</b>", "━━━━━━━━━━━━━━━━━━"]

    for fmt in FORMAT_ORDER:
        tot_fmt = cat_fmt_total.get(fmt, 0)
        if fmt not in counts and tot_fmt == 0:
            continue
        sport = FORMAT_TO_SPORT.get(fmt, fmt)
        emoji = SPORT_EMOJI_MAP.get(sport, "🃏")
        f_label = FORMAT_LABEL.get(fmt, fmt.upper())
        fmt_counts = counts.get(fmt, {})
        fmt_owned = sum(fmt_counts.get(r, 0) for r in RARITY_ORDER)

        lines.append(f"\n{emoji} <b>{f_label}</b>")
        for r in RARITY_ORDER:
            owned_r = fmt_counts.get(r, 0)
            tot_r = cat_fmt_rarity.get((fmt, r), 0)
            if owned_r > 0:
                lines.append(f"  {RARITY_EMOJI.get(r, '⚪')} {r.title()}: <b>{owned_r}/{tot_r}</b>")
        lines.append(f"  Total: <b>{fmt_owned}/{tot_fmt} cards</b>")

    from database import get_all_editions
    all_editions = await get_all_editions()
    tot_editions = len(all_editions)
    owned_ed_ids = {c.get("edition_id") for c in all_cards if c.get("rarity") == "mythical" or c.get("edition_id")}
    owned_ed_ids.discard(None)
    user_editions = len(owned_ed_ids)

    lines.append(f"\n✨ <b>Special Editions</b>")
    lines.append(f"  Owned: <b>{user_editions} / {tot_editions}</b>")

    grand_unique = len(all_cards)
    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"🃏 Grand Total: <b>{grand_unique}/{cat_grand} cards</b>")
    return "\n".join(lines)


async def handle_collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command /collections - view collection statistics."""
    user = update.effective_user
    if not user:
        return
    text = await _get_collections_text(user.id)
    try:
        await update.effective_message.reply_text(text, parse_mode="HTML")
    except Exception:
        await update.effective_message.reply_text(text)


async def cb_mc_collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show full collection breakdown: rarity counts per format with total available n."""
    query = update.callback_query
    if not query:
        return
    parts = query.data.split("|")
    if len(parts) < 2:
        await query.answer()
        return
    _, owner_id = parts[:2]
    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    coll_cd = 0.3 if is_dm else 1.2
    if not _check_card_cooldown(query.from_user.id, "mc_collections", coll_cd):
        await query.answer("Slow down!", show_alert=False)
        return

    if str(owner_id) not in ("1087968824", "777000") and str(query.from_user.id) != str(owner_id):
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    await query.answer()

    try:
        oid_int = int(owner_id)
        if oid_int in (1087968824, 777000):
            oid_int = query.from_user.id
    except (ValueError, TypeError):
        oid_int = query.from_user.id

    text = await _get_collections_text(oid_int)
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Cards", callback_data=f"mc_page|{owner_id}|all|0")]])
    try:
        await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"cb_mc_collections edit error: {e}, falling back to plain text")
            try:
                await query.edit_message_text(text, reply_markup=back_kb)
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# /viewcard
# ─────────────────────────────────────────────────────────────────────────────
async def handle_viewcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args:
        try:
            await update.effective_message.reply_text("Usage: /viewcard <player name>")
        except Exception:
            pass
        return
    name_query = " ".join(args).strip()
    if len(name_query) < 2:
        try:
            await update.effective_message.reply_text(
                "⚠️ *Search query too short.* Please type at least 2 characters (e.g. `/viewcard AB` or `/viewcard Rohit`).",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return

    from database import get_user_cards, get_db
    cards = await get_user_cards(user.id)
    if not cards:
        try:
            await update.effective_message.reply_text("❌ You don't own any cards yet. Use /pack to get some!", parse_mode="Markdown")
        except Exception:
            pass
        return

    q_lower = name_query.lower()

    # 1. Exact match priority
    exact_matches = [c for c in cards if c["name"].lower() == q_lower]
    if exact_matches:
        matching = exact_matches
    else:
        # 2. Starts-with first, then general substring
        starts_with = [c for c in cards if c["name"].lower().startswith(q_lower)]
        contains = [c for c in cards if q_lower in c["name"].lower() and not c["name"].lower().startswith(q_lower)]
        matching = starts_with + contains

    if not matching:
        try:
            await update.effective_message.reply_text(f"❌ You don't own any card matching *{esc(name_query)}*.", parse_mode="Markdown")
        except Exception:
            pass
        return

    # Preserve order of relevance while getting unique player names
    distinct_names = []
    seen = set()
    for c in matching:
        if c["name"] not in seen:
            seen.add(c["name"])
            distinct_names.append(c["name"])

    if len(distinct_names) > 1:
        # Multiple different players matched — show clickable buttons (capped to top 8)
        MAX_MATCHES = 8
        total_found = len(distinct_names)
        display_names = distinct_names[:MAX_MATCHES]

        buttons = []
        for n in display_names:
            # Find this player's cards to show format info in button
            player_cards_for_name = [c for c in matching if c["name"] == n]
            if len(player_cards_for_name) == 1:
                c = player_cards_for_name[0]
                label = f"{esc(n)} ({FORMAT_LABEL.get(c['format'], c['format'].upper())})"
                buttons.append([InlineKeyboardButton(label, callback_data=f"vc_fmt|{user.id}|{c['player_id']}|{c['format']}")])
            else:
                label = f"{esc(n)} ({len(player_cards_for_name)} formats)"
                buttons.append([InlineKeyboardButton(label, callback_data=f"vc_name|{user.id}|{player_cards_for_name[0]['player_id']}")])

        if total_found > MAX_MATCHES:
            header_text = (
                f"🔍 Found *{total_found}* players matching *{esc(name_query)}*.\n"
                f"Showing top {MAX_MATCHES} (type more letters to narrow down):\n\n"
                f"Tap a name to view:"
            )
        else:
            header_text = f"🔍 Multiple matches for *{esc(name_query)}*:\nTap a name to view:"

        try:
            await update.effective_message.reply_text(
                header_text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return

    # One player name — check formats
    player_name = distinct_names[0]
    player_cards = [c for c in matching if c["name"] == player_name]
    bot_username = context.bot.username if context and context.bot else ""
    if len(player_cards) == 1:
        # Single format — show directly
        try:
            await _show_card_detail(update.effective_message, user.id, player_cards[0], edit=False, bot_username=bot_username)
        except Exception:
            pass
    else:
        # Multiple formats — ask which one
        buttons = []
        for c in player_cards:
            ed_id = c.get("edition_id")
            if ed_id:
                lbl = f"✨ {c.get('edition', 'Special Edition').title()} — OVR {c.get('ovr', 90)}"
                cb_data = f"vc_fmt|ed|{ed_id}"
            else:
                lbl = f"{FORMAT_LABEL.get(c['format'], c['format'].upper())} — {RARITY_EMOJI.get(c['rarity'], '⚪')} OVR {c['ovr']}"
                cb_data = f"vc_fmt|{c['player_id']}|{c['format']}"
            buttons.append([InlineKeyboardButton(lbl, callback_data=cb_data)])
        try:
            await update.effective_message.reply_text(
                f"Which *{esc(player_name)}* card do you want to view?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        except Exception:
            pass

async def cb_vc_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a player name from the disambiguation list — show their formats."""
    query = update.callback_query
    _, owner_id, player_id = query.data.split("|")
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return
    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    cd = 0.3 if is_dm else 1.2
    if not _check_card_cooldown(query.from_user.id, "vc_name", cd):
        await query.answer("⏳ Please wait a moment.", show_alert=False)
        return
    await query.answer()
    from database import get_user_cards
    cards = await get_user_cards(int(owner_id))
    player_cards = [c for c in cards if c["player_id"] == player_id]
    if not player_cards:
        await query.edit_message_text("❌ Card not found in your collection.")
        return
    bot_username = context.bot.username if context and context.bot else ""
    if len(player_cards) == 1:
        await _show_card_detail(query, int(owner_id), player_cards[0], edit=True, bot_username=bot_username)
    else:
        buttons = []
        for c in player_cards:
            ed_id = c.get("edition_id")
            if ed_id:
                lbl = f"✨ {c.get('edition', 'Special Edition').title()} — OVR {c.get('ovr', 90)}"
                cb_data = f"vc_fmt|ed|{ed_id}"
            else:
                lbl = f"{FORMAT_LABEL.get(c['format'], c['format'].upper())} — {RARITY_EMOJI.get(c['rarity'], '⚪')} OVR {c['ovr']}"
                cb_data = f"vc_fmt|{c['player_id']}|{c['format']}"
            buttons.append([InlineKeyboardButton(lbl, callback_data=cb_data)])
        player_name = player_cards[0]["name"]
        await query.edit_message_text(
            f"Which *{esc(player_name)}* card do you want to view?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )


async def cb_vc_fmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    user_id = query.from_user.id
    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    cd = 0.3 if is_dm else 1.2
    if not _check_card_cooldown(user_id, "vc_fmt", cd):
        await query.answer("⏳ Please wait a moment.", show_alert=False)
        return
    await query.answer()

    if parts[1] == "ed":
        ed_id = parts[2]
        player_id = None
        fmt = None
    elif len(parts) == 3:
        ed_id = "base"
        player_id = parts[1]
        fmt = parts[2]
    else:
        owner_id = parts[1]
        if str(user_id) != owner_id:
            await query.answer("⛔ Not your menu.", show_alert=True); return
        player_id = parts[2]
        fmt = parts[3]
        ed_id = parts[4] if len(parts) >= 5 else "base"

    from database import get_user_cards
    cards = await get_user_cards(user_id)
    if ed_id != "base":
        card = next((c for c in cards if c.get("edition_id") == ed_id), None)
    else:
        card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt and not c.get("edition_id")), None)
        if not card:
            card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt), None)

    if not card:
        try:
            await query.edit_message_caption("❌ Card not found in your collection.")
        except Exception:
            try:
                await query.edit_message_text("❌ Card not found in your collection.")
            except Exception:
                pass
        return

    bot_username = context.bot.username if context and context.bot else ""
    await _show_card_detail(query, user_id, card, edit=True, bot_username=bot_username)

async def _show_card_detail(msg_or_query, owner_id: int, card: dict, edit: bool, bot_username: str = ""):
    from database import validate_fav_card
    # validate_fav_card auto-clears stale fav if card was traded/sold
    fav = await validate_fav_card(owner_id)
    card_ed_id = card.get("edition_id")
    is_fav = fav and fav.get("player_id") == card["player_id"] and fav.get("format") == card["format"] and fav.get("edition_id") == card_ed_id
    is_mythical = (card.get("rarity") == "mythical" or bool(card_ed_id))
    r_emoji = "✨" if is_mythical else RARITY_EMOJI.get(card["rarity"], "⚪")
    f_label = FORMAT_LABEL.get(card["format"], card["format"].upper())
    SELL_VALUES = {"common": 25, "rare": 75, "epic": 200, "legend": 600}
    sell_val = SELL_VALUES.get(card["rarity"], 25)
    is_last_copy = card["quantity"] <= 1
    fav_label = "💔 Remove Fav" if is_fav else "❤️ Set as Fav"

    # Ultra-compact callback data guaranteed < 45 bytes (strictly within Telegram 64-byte limit)
    if card_ed_id:
        fav_cb = f"vc_unfav|ed|{card_ed_id}" if is_fav else f"vc_fav|ed|{card_ed_id}"
    else:
        fav_cb = f"vc_unfav|{card['player_id']}|{card['format']}" if is_fav else f"vc_fav|{card['player_id']}|{card['format']}"

    fav_warning = ""
    fav_warning_html = ""
    if is_fav and is_last_copy:
        fav_warning = "\n\n⭐ *Fav Card* — Remove fav to sell or trade this card."
        fav_warning_html = "\n\n⭐ <b>Fav Card</b> — Remove fav to sell or trade this card."
    ed_str = f"🏆 Edition: *{esc(card['edition'])}*\n" if card.get("edition") else ""
    title_ico = "✨" if is_mythical else "🃏"
    text = (
        f"{title_ico} *{esc(card['name'])}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{ed_str}"
        f"📋 Format: *{f_label}*\n"
        f"{r_emoji} Rarity: *{card['rarity'].title()}*\n"
        f"⭐ OVR: *{card['ovr']}*\n"
        f"📦 Owned: *{card['quantity']}×*"
        f"{fav_warning}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    chat = getattr(msg_or_query, 'chat', None) or getattr(getattr(msg_or_query, 'message', None), 'chat', None)
    is_dm = (chat.type == "private") if chat else False

    if is_dm:
        kb_rows = [[InlineKeyboardButton(fav_label, callback_data=fav_cb)]]
        if not is_mythical:
            kb_rows.append([InlineKeyboardButton(f"💰 Sell for {sell_val}🪙", callback_data=f"vc_sell|{card['player_id']}|{card['format']}|{sell_val}")])
        kb = InlineKeyboardMarkup(kb_rows)
    else:
        if not bot_username:
            try:
                bot = getattr(msg_or_query, 'get_bot', lambda: None)()
                bot_username = getattr(bot, 'username', '') or ""
            except Exception:
                pass
        if bot_username:
            if card_ed_id:
                start_param = f"vc_ed_{card_ed_id}"
            else:
                start_param = f"vc_{card['player_id']}_{card['format']}"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📬 Manage in DM", url=f"https://t.me/{bot_username}?start={start_param}")
            ]])
        else:
            kb = None
    image = card.get("image")  # may be a URL or a file_id

    # Determine if image is a web URL (usable for href preview) or a file_id
    image_is_url = bool(image and str(image).startswith("http"))

    import html
    name_html = html.escape(card.get("name", "Card"))
    # Build HTML-compatible text (href requires HTML parse mode)
    ed_str_html = f"🏆 Edition: <b>{html.escape(str(card['edition']))}</b>\n" if card.get("edition") else ""
    text_html = (
        f"{title_ico} <b>{name_html}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{ed_str_html}"
        f"📋 Format: <b>{f_label}</b>\n"
        f"{r_emoji} Rarity: <b>{card['rarity'].title()}</b>\n"
        f"⭐ OVR: <b>{card['ovr']}</b>\n"
        f"📦 Owned: <b>{card['quantity']}×</b>"
        f"{fav_warning_html}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    if edit:
        is_photo = bool(getattr(msg_or_query, 'message', None) and msg_or_query.message.photo)
        if image_is_url:
            # href approach: edit existing message to text with hidden image preview
            href_text = f'<a href="{image}">&#8205;</a>' + text_html
            try:
                if is_photo:
                    # Convert photo message to text message via send + delete pattern
                    # (edit_message_media still works but is slow — we use text edit for speed)
                    await msg_or_query.edit_message_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
                else:
                    await msg_or_query.edit_message_text(
                        href_text, reply_markup=kb, parse_mode="HTML",
                        disable_web_page_preview=False
                    )
            except Exception:
                # Fallback: plain text
                await msg_or_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        elif is_photo:
            await msg_or_query.edit_message_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
        elif image:
            from telegram import InputMediaPhoto
            try:
                await msg_or_query.edit_message_media(
                    media=InputMediaPhoto(media=image, caption=text, parse_mode="Markdown"),
                    reply_markup=kb
                )
            except Exception:
                await msg_or_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            await msg_or_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        if image_is_url:
            href_text = f'<a href="{image}">&#8205;</a>' + text_html
            try:
                await msg_or_query.reply_text(
                    href_text, reply_markup=kb, parse_mode="HTML",
                    disable_web_page_preview=False
                )
                return
            except Exception:
                pass
        elif image:
            try:
                await msg_or_query.reply_photo(photo=image, caption=text, reply_markup=kb, parse_mode="Markdown")
                return
            except Exception:
                pass
        await msg_or_query.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def handle_viewcard_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    """Deep link: /start vc_<player_id>_<format> or vc_ed_<edition_id> from group chat to manage card in DM."""
    user = update.effective_user
    if not user or not payload:
        return
    from database import get_user_cards, get_player
    cards = await get_user_cards(user.id)
    if payload.startswith("ed_"):
        edition_id = payload[3:]
        card = next((c for c in cards if c.get("edition_id") == edition_id), None)
        if not card or card.get("quantity", 0) < 1:
            await update.effective_message.reply_text("❌ You don't own this Special Edition card in your collection!")
            return
    else:
        parts = payload.rsplit("_", 1)
        if len(parts) != 2:
            await update.effective_message.reply_text("❌ Invalid card link.")
            return
        player_id, fmt = parts[0], parts[1].lower()
        card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt and not c.get("edition_id")), None)
        if not card:
            card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt), None)
        if not card or card.get("quantity", 0) < 1:
            p = await get_player(player_id)
            p_name = html.escape(p.get("name", "this card")) if p else "this card"
            await update.effective_message.reply_text(
                f"❌ You don't own <b>{p_name}</b> ({FORMAT_LABEL.get(fmt, fmt.upper())}) in your collection!",
                parse_mode="HTML"
            )
            return
    bot_username = context.bot.username if context and context.bot else ""
    await _show_card_detail(update.effective_message, user.id, card, edit=False, bot_username=bot_username)


async def cb_vc_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please manage cards in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    user_id = query.from_user.id

    if parts[1] == "ed":
        ed_id = parts[2]
        player_id = None
        fmt = None
    elif len(parts) == 3:
        ed_id = "base"
        player_id = parts[1]
        fmt = parts[2]
    else:
        owner_id = parts[1]
        if str(user_id) != owner_id:
            await query.answer("⛔ Not your menu.", show_alert=True); return
        player_id = parts[2]
        fmt = parts[3]
        ed_id = parts[4] if len(parts) >= 5 else "base"

    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        from database import set_fav_card, get_user_cards
        cards = await get_user_cards(user_id)
        if ed_id != "base":
            card = next((c for c in cards if c.get("edition_id") == ed_id), None)
        else:
            card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt and not c.get("edition_id")), None)
            if not card:
                card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt), None)
        if not card or card["quantity"] < 1:
            await query.answer("❌ You no longer own this card.", show_alert=True); return
        await set_fav_card(user_id, card["player_id"], card["format"], None if ed_id == "base" else ed_id)
        f_label = FORMAT_LABEL.get(card["format"], card["format"].upper())
        await query.answer(f"❤️ {card['name']} ({f_label}) set as favorite!", show_alert=True)
        bot_username = context.bot.username if context and context.bot else ""
        await _show_card_detail(query, user_id, card, edit=True, bot_username=bot_username)

async def cb_vc_unfav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please manage cards in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    user_id = query.from_user.id

    if parts[1] == "ed":
        ed_id = parts[2]
        player_id = None
        fmt = None
    elif len(parts) == 3:
        ed_id = "base"
        player_id = parts[1]
        fmt = parts[2]
    else:
        owner_id = parts[1]
        if str(user_id) != owner_id:
            await query.answer("⛔ Not your menu.", show_alert=True); return
        player_id = parts[2]
        fmt = parts[3]
        ed_id = parts[4] if len(parts) >= 5 else "base"

    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        from database import clear_fav_card, get_user_cards
        await clear_fav_card(user_id)
        cards = await get_user_cards(user_id)
        if ed_id != "base":
            card = next((c for c in cards if c.get("edition_id") == ed_id), None)
        else:
            card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt and not c.get("edition_id")), None)
            if not card:
                card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt), None)
        await query.answer("💔 Removed from favorites.", show_alert=True)
        if card:
            bot_username = context.bot.username if context and context.bot else ""
            await _show_card_detail(query, user_id, card, edit=True, bot_username=bot_username)

async def cb_vc_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show sell confirmation."""
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please manage and sell cards in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    user_id = query.from_user.id
    if len(parts) == 4:
        _, player_id, fmt, sell_val_str = parts
    else:
        _, owner_id, player_id, fmt, sell_val_str = parts
        if str(user_id) != owner_id:
            await query.answer("⛔ Not your menu.", show_alert=True); return

    from database import get_user_cards, get_fav_card
    cards = await get_user_cards(user_id)
    card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt and not c.get("edition_id")), None)
    if not card:
        card = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt), None)
    if not card:
        if query.message and query.message.photo:
            await query.answer(); await query.edit_message_caption(caption="❌ Card not found."); return
        else:
            await query.answer(); await query.edit_message_text("❌ Card not found."); return
    if card.get("rarity") == "mythical" or card.get("edition_id"):
        await query.answer("✨ Special Edition cards cannot be sold!", show_alert=True)
        return
    # Check fav BEFORE answering (query can only be answered once)
    fav = await get_fav_card(user_id)
    is_fav = fav and fav.get("player_id") == player_id and fav.get("format") == fmt
    if is_fav and card["quantity"] <= 1:
        await query.answer(
            "⭐ This is your Fav Card!\n\nTap '💔 Remove Fav' first, then you can sell it.",
            show_alert=True
        )
        return
    await query.answer()  # answered here, after all popup-answer paths are done
    f_label = FORMAT_LABEL.get(fmt, fmt.upper())
    text = (
        f"💰 *Sell Confirmation*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Sell 1× *{esc(card['name'])}* ({f_label})\n"
        f"Earn: *{sell_val_str}🪙*\n"
        f"Remaining copies: {card['quantity'] - 1}×\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm Sell", callback_data=f"vc_sell_ok|{player_id}|{fmt}|{sell_val_str}"),
        InlineKeyboardButton("❌ Cancel",       callback_data=f"vc_fmt|{player_id}|{fmt}"),
    ]])
    # Handle photo messages (viewcard sends photo+caption)
    if query.message and query.message.photo:
        await query.edit_message_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

_PROCESSED_SELL_MESSAGES: set[str] = set()

async def cb_vc_sell_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute sell with anti-spam lock and per-message duplicate suppression."""
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please manage and sell cards in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    user_id = query.from_user.id
    if len(parts) == 4:
        _, player_id, fmt, sell_val_str = parts
    else:
        _, owner_id, player_id, fmt, sell_val_str = parts
        if str(user_id) != owner_id:
            await query.answer("⛔ Not your menu.", show_alert=True); return

    msg_id = query.message.message_id if query.message else None
    sell_key = f"{user_id}_{msg_id}"
    if msg_id and sell_key in _PROCESSED_SELL_MESSAGES:
        await query.answer("⚠️ Sale already processed!", show_alert=False)
        return
    if msg_id:
        _PROCESSED_SELL_MESSAGES.add(sell_key)
        if len(_PROCESSED_SELL_MESSAGES) > 3000:
            _PROCESSED_SELL_MESSAGES.clear()

    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        from database import get_user_card, get_player, get_fav_card, remove_card_from_user, add_card_coins, increment_quest_progress
        fav = await get_fav_card(user_id)
        is_fav = fav and fav.get("player_id") == player_id and fav.get("format") == fmt
        if is_fav:
            ucard = await get_user_card(user_id, player_id, fmt)
            if not ucard or ucard.get("quantity", 0) <= 1:
                if msg_id:
                    _PROCESSED_SELL_MESSAGES.discard(sell_key)
                await query.answer(
                    "⭐ This is your Fav Card!\n\nTap '💔 Remove Fav' in /viewcard first, then sell.",
                    show_alert=True
                )
                return

        # Atomically remove exactly 1 copy (returns remaining quantity, or -1 on failure)
        remaining = await remove_card_from_user(user_id, player_id, fmt)
        if remaining < 0:
            await query.answer("❌ Card not available to sell.", show_alert=True)
            return

        try:
            await query.answer()
        except Exception:
            pass

        sell_val = int(sell_val_str)
        new_bal = await add_card_coins(user_id, sell_val)
        await increment_quest_progress(user_id, "cards_sold", 1)
        
        p_doc = await get_player(player_id)
        p_name = p_doc.get("name", "Card") if p_doc else "Card"
        f_label = FORMAT_LABEL.get(fmt, fmt.upper())
        import html as _html
        name_safe = _html.escape(p_name)
        success_text = (
            f"✅ Sold <b>{name_safe}</b> ({f_label}) for <b>{sell_val}🪙</b>!\n"
            f"Balance: <b>{new_bal}🪙</b> | Remaining copies: <b>{remaining}×</b>"
        )
        if query.message and query.message.photo:
            try:
                await query.edit_message_caption(caption=success_text, parse_mode="HTML")
            except Exception:
                pass
        else:
            try:
                await query.edit_message_text(success_text, parse_mode="HTML")
            except Exception:
                pass



# ─────────────────────────────────────────────────────────────────────────────
# /trade_card
# ─────────────────────────────────────────────────────────────────────────────
async def handle_trade_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate a trade by replying to the target user's message."""
    user = update.effective_user
    if not user:
        return
    if not _check_card_cooldown(user.id, "trade_cmd", 3.0):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("♻️ Reply to the target user's message to trade.\nExample: reply to their message then send /trade_card")
        return
    target = msg.reply_to_message.from_user
    if target.is_bot or target.id == user.id:
        await msg.reply_text("❌ You can't trade with a bot or yourself.")
        return
    from database import is_user_banned
    if await is_user_banned(target.id):
        await msg.reply_text("❌ Target user is banned.")
        return
    from database import get_user_active_trade, get_user_cards

    # Check if either user already has an active trade
    my_trade = await get_user_active_trade(user.id)
    if my_trade:
        await msg.reply_text("❌ You already have an active trade. Cancel it first or wait for it to expire (5 min).")
        return
    their_trade = await get_user_active_trade(target.id)
    if their_trade:
        await msg.reply_text(f"❌ {esc(target.first_name)} already has an active trade.")
        return
    # Check target has cards (excluding Mythical/Edition cards)
    target_cards = await get_user_cards(target.id)
    target_cards = [c for c in target_cards if c.get("rarity") != "mythical" and not c.get("edition_id")]
    if not target_cards:
        await msg.reply_text(f"❌ {esc(target.first_name)} has no cards to trade.")
        return
    # Show initiator's cards to pick from (excluding Mythical/Edition cards)
    my_cards = await get_user_cards(user.id)
    my_cards = [c for c in my_cards if c.get("rarity") != "mythical" and not c.get("edition_id")]
    if not my_cards:
        await msg.reply_text("❌ You have no cards to offer in a trade.")
        return
    crit, order = await get_user_sort(user.id)
    my_cards = _sort_cards(my_cards, crit, order)
    # Show card picker for initiator (paginated, page 0)
    await _show_trade_picker(msg, user.id, target.id, target.first_name, my_cards, page=0, edit=False)

async def _show_trade_picker(msg_or_q, initiator_id: int, target_id: int, target_name: str, cards: list, page: int, edit: bool):
    start = page * 8
    page_cards = cards[start:start + 8]
    total_pages = max(1, (len(cards) + 7) // 8)
    text = f"♻️ *Trade* — Choose a card to offer {esc(target_name)}:\n(Page {page+1}/{total_pages})"
    buttons = [
        [InlineKeyboardButton(
            f"{RARITY_EMOJI.get(c['rarity'], '⚪')} {c['name']} ({FORMAT_LABEL.get(c['format'], c['format'])})",
            callback_data=f"tr_offer|{initiator_id}|{target_id}|{c['player_id']}|{c['format']}"
        )]
        for c in page_cards
    ]
    nav = []
    if total_pages > 10 and page > 0:
        nav.append(InlineKeyboardButton("⏮ -10", callback_data=f"tr_page|{initiator_id}|{target_id}|{max(0, page-10)}"))
    if total_pages > 5 and page > 0:
        nav.append(InlineKeyboardButton("⏪ -5", callback_data=f"tr_page|{initiator_id}|{target_id}|{max(0, page-5)}"))
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tr_page|{initiator_id}|{target_id}|{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tr_page|{initiator_id}|{target_id}|{page+1}"))
    if total_pages > 5 and page < total_pages - 1:
        nav.append(InlineKeyboardButton("+5 ⏩", callback_data=f"tr_page|{initiator_id}|{target_id}|{min(total_pages-1, page+5)}"))
    if total_pages > 10 and page < total_pages - 1:
        nav.append(InlineKeyboardButton("+10 ⏭", callback_data=f"tr_page|{initiator_id}|{target_id}|{min(total_pages-1, page+10)}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"tr_cancel|{initiator_id}")])
    kb = InlineKeyboardMarkup(buttons)
    if edit:
        await msg_or_q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_q.reply_text(text, reply_markup=kb, parse_mode="Markdown")

async def cb_tr_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, initiator_id, target_id, page_str = query.data.split("|")
    if str(query.from_user.id) != initiator_id:
        await query.answer("⛔ Not your trade.", show_alert=True); return
    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    page_cd = 0.3 if is_dm else 1.2
    if not _check_card_cooldown(query.from_user.id, "tr_page", page_cd):
        await query.answer("⏳ Please wait a moment between page turns.", show_alert=False)
        return
    await query.answer()
    from database import get_user_cards, get_db
    db = get_db()
    target_id_int = int(target_id)
    cards = await get_user_cards(int(initiator_id))
    cards = [c for c in cards if c.get("rarity") != "mythical" and not c.get("edition_id")]
    crit, order = await get_user_sort(int(initiator_id))
    cards = _sort_cards(cards, crit, order)
    await _show_trade_picker(query, int(initiator_id), target_id_int, f"User {target_id}", cards, int(page_str), edit=True)

async def cb_tr_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiator picked card to offer. Edit message to show target's card picker (single-msg flow)."""
    query = update.callback_query
    _, initiator_id, target_id, player_id, fmt = query.data.split("|")
    if str(query.from_user.id) != initiator_id:
        await query.answer("⛔ Not your trade.", show_alert=True); return
    # DB-level cooldown
    from database import try_acquire_action_cooldown
    if not await try_acquire_action_cooldown(int(initiator_id), "trade_offer", cooldown_seconds=3):
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    lock = _get_lock(int(initiator_id))
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        from database import get_user_cards, get_user_active_trade, create_trade, get_fav_card
        # Re-check active trade
        my_trade = await get_user_active_trade(int(initiator_id))
        if my_trade:
            await query.answer("❌ You already have an active trade.", show_alert=True); return
        cards = await get_user_cards(int(initiator_id))
        offered = next((c for c in cards if c["player_id"] == player_id and c["format"] == fmt), None)
        if not offered:
            await query.answer("❌ Card not in your collection.", show_alert=True); return
        if offered.get("rarity") == "mythical" or offered.get("edition_id"):
            await query.answer("✨ Special Edition cards cannot be traded.", show_alert=True)
            return
        # Fav protection — check BEFORE answering
        fav = await get_fav_card(int(initiator_id))
        is_fav = fav and fav.get("player_id") == player_id and fav.get("format") == fmt
        if is_fav and offered["quantity"] <= 1:
            await query.answer(
                "⭐ This is your Fav Card!\n\nTap '💔 Remove Fav' in /viewcard first, then trade.",
                show_alert=True
            )
            return
        await query.answer()  # safe to answer now
        # Check target has cards of same rarity
        target_cards = await get_user_cards(int(target_id))
        matching_rarity = [c for c in target_cards if c["rarity"] == offered["rarity"] and c.get("rarity") != "mythical" and not c.get("edition_id")]
        if not matching_rarity:
            await query.edit_message_text(
                f"❌ The other user has no *{offered['rarity'].title()}* cards to trade with yours.",
                parse_mode="Markdown"
            ); return
        # Create trade record
        trade_id = str(uuid.uuid4())[:8]
        trade_data = {
            "trade_id":          trade_id,
            "initiator_id":      int(initiator_id),
            "target_id":         int(target_id),
            "offered_player_id": player_id,
            "offered_format":    fmt,
            "offered_rarity":    offered["rarity"],
            "offered_name":      offered["name"],
            "status":            "awaiting_target_pick",
            "chat_id":           query.message.chat_id,
            "message_id":        query.message.message_id,
            "created_at":        time.time(),
        }
        await create_trade(trade_data)
        f_label = FORMAT_LABEL.get(fmt, fmt.upper())
        r_emoji = RARITY_EMOJI.get(offered["rarity"], "⚪")
        # ── Show target's paginated picker (page 0) ──────────────────────────
        crit, order = await get_user_sort(int(target_id))
        matching_rarity = _sort_cards(matching_rarity, crit, order)
        header = (
            f"♻️ *Trade Request*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*{esc(query.from_user.first_name)}* is offering:\n"
            f"{r_emoji} *{esc(offered['name'])}* ({f_label}) — OVR {offered['ovr']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pick one of your *{offered['rarity'].title()}* cards to trade back:"
        )
        await _show_target_picker(query, trade_id, target_id, matching_rarity, header, page=0)

async def _show_target_picker(query, trade_id: str, target_id: str, cards: list, header: str, page: int):
    """Render the target's card picker with pagination."""
    TPAGE_SIZE = 8
    total_pages = max(1, (len(cards) + TPAGE_SIZE - 1) // TPAGE_SIZE)
    page_cards = cards[page * TPAGE_SIZE:(page + 1) * TPAGE_SIZE]
    pick_buttons = [
        [InlineKeyboardButton(
            f"{RARITY_EMOJI.get(c['rarity'],'')}{c['name']} ({FORMAT_LABEL.get(c['format'],c['format'])})",
            callback_data=f"tr_pick|{target_id}|{trade_id}|{c['player_id']}|{c['format']}"
        )]
        for c in page_cards
    ]
    nav = []
    if total_pages > 10 and page > 0:
        nav.append(InlineKeyboardButton("⏮ -10", callback_data=f"tr_tpage|{target_id}|{trade_id}|{max(0, page-10)}"))
    if total_pages > 5 and page > 0:
        nav.append(InlineKeyboardButton("⏪ -5", callback_data=f"tr_tpage|{target_id}|{trade_id}|{max(0, page-5)}"))
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tr_tpage|{target_id}|{trade_id}|{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tr_tpage|{target_id}|{trade_id}|{page+1}"))
    if total_pages > 5 and page < total_pages - 1:
        nav.append(InlineKeyboardButton("+5 ⏩", callback_data=f"tr_tpage|{target_id}|{trade_id}|{min(total_pages-1, page+5)}"))
    if total_pages > 10 and page < total_pages - 1:
        nav.append(InlineKeyboardButton("+10 ⏭", callback_data=f"tr_tpage|{target_id}|{trade_id}|{min(total_pages-1, page+10)}"))
    if nav:
        pick_buttons.append(nav)

    pick_buttons.append([InlineKeyboardButton("❌ Decline", callback_data=f"tr_decline|{target_id}|{trade_id}")])
    page_note = f" (Page {page+1}/{total_pages})" if total_pages > 1 else ""
    await query.edit_message_text(
        header + page_note,
        reply_markup=InlineKeyboardMarkup(pick_buttons),
        parse_mode="Markdown"
    )

async def cb_tr_tpage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Target-side pagination for the trade card picker."""
    query = update.callback_query
    _, target_id, trade_id, page_str = query.data.split("|")
    if str(query.from_user.id) != target_id:
        await query.answer("⛔ Not your trade.", show_alert=True); return
    chat = update.effective_chat
    is_dm = (chat.type == "private") if chat else False
    page_cd = 0.3 if is_dm else 1.2
    if not _check_card_cooldown(query.from_user.id, "tr_tpage", page_cd):
        await query.answer("⏳ Please wait a moment between page turns.", show_alert=False)
        return
    await query.answer()
    from database import get_trade, get_user_cards
    trade = await get_trade(trade_id)
    if not trade or trade["status"] != "awaiting_target_pick":
        await query.answer("❌ Trade expired or cancelled.", show_alert=True); return
    if time.time() - trade["created_at"] > 300:
        from database import get_db
        await get_db().active_trades.delete_one({"trade_id": trade_id})
        await query.answer("❌ Trade expired (5 min timeout).", show_alert=True); return
    target_cards = await get_user_cards(int(target_id))
    matching_rarity = [c for c in target_cards if c["rarity"] == trade["offered_rarity"] and c.get("rarity") != "mythical" and not c.get("edition_id")]
    crit, order = await get_user_sort(int(target_id))
    matching_rarity = _sort_cards(matching_rarity, crit, order)
    r_emoji = RARITY_EMOJI.get(trade["offered_rarity"], "⚪")
    f_label = FORMAT_LABEL.get(trade.get("offered_format", ""), trade.get("offered_format", ""))
    header = (
        f"♻️ *Trade Request*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Offered: {r_emoji} *{esc(trade['offered_name'])}* ({f_label})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Pick one of your *{trade['offered_rarity'].title()}* cards to trade back:"
    )
    await _show_target_picker(query, trade_id, target_id, matching_rarity, header, page=int(page_str))

async def cb_tr_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Target picked their card. Edit message to show summary+confirm (single-msg flow)."""
    query = update.callback_query
    _, target_id, trade_id, player_id, fmt = query.data.split("|")
    if str(query.from_user.id) != target_id:
        await query.answer("⛔ Not your trade.", show_alert=True); return
    # DB-level cooldown
    from database import try_acquire_action_cooldown
    if not await try_acquire_action_cooldown(int(target_id), "trade_pick", cooldown_seconds=3):
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    lock = _get_lock(int(target_id))
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        from database import get_trade, update_trade, get_user_cards, get_fav_card
        trade = await get_trade(trade_id)
        if not trade or trade["status"] != "awaiting_target_pick":
            await query.answer("❌ This trade has expired or been cancelled.", show_alert=True); return
        # Check expiry
        if time.time() - trade["created_at"] > 300:
            from database import cancel_trade
            await cancel_trade(trade_id)
            await query.answer("❌ Trade expired (5 min timeout).", show_alert=True); return
        # Verify target still has the card
        target_cards = await get_user_cards(int(target_id))
        their_card = next((c for c in target_cards if c["player_id"] == player_id and c["format"] == fmt), None)
        if not their_card:
            await query.answer("❌ You no longer have that card.", show_alert=True); return
        if their_card.get("rarity") == "mythical" or their_card.get("edition_id"):
            await query.answer("✨ Special Edition cards cannot be traded.", show_alert=True)
            return
        # Fav protection — check BEFORE answering silently
        fav = await get_fav_card(int(target_id))
        is_fav = fav and fav.get("player_id") == player_id and fav.get("format") == fmt
        if is_fav and their_card["quantity"] <= 1:
            await query.answer(
                "⭐ That's your Fav Card!\n\nTap '💔 Remove Fav' in /viewcard first, then trade.",
                show_alert=True
            )
            return
        await query.answer()  # silent ack after all popup paths handled
        # Update trade with target's pick
        await update_trade(trade_id, {
            "requested_player_id": player_id,
            "requested_format":    fmt,
            "requested_name":      their_card["name"],
            "status":              "awaiting_confirmation"
        })
        r_off = RARITY_EMOJI.get(trade["offered_rarity"], "⚪")
        r_req = RARITY_EMOJI.get(their_card["rarity"], "⚪")
        off_fl = FORMAT_LABEL.get(trade["offered_format"], trade["offered_format"])
        req_fl = FORMAT_LABEL.get(fmt, fmt)
        # ── SINGLE MESSAGE: edit M1 to show trade summary + Accept/Decline for initiator ──
        confirm_buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept",  callback_data=f"tr_confirm|{trade['initiator_id']}|{trade_id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"tr_decline|{trade['initiator_id']}|{trade_id}"),
        ]])
        await query.edit_message_text(
            f"♻️ *Trade Summary*\n━━━━━━━━━━━━━━━━━━\n"
            f"You give: {r_off} *{esc(trade['offered_name'])}* ({off_fl})\n"
            f"You get:  {r_req} *{esc(their_card['name'])}* ({req_fl})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Do you accept?",
            reply_markup=confirm_buttons,
            parse_mode="Markdown"
        )

async def cb_tr_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiator accepts trade — execute atomically."""
    query = update.callback_query
    _, initiator_id, trade_id = query.data.split("|")
    user_id = query.from_user.id
    if str(user_id) != initiator_id:
        await query.answer("⛔ Not your trade.", show_alert=True); return
    # DB-level cooldown — prevents spam confirm clicks showing repeated error messages
    from database import try_acquire_action_cooldown
    if not await try_acquire_action_cooldown(user_id, "trade_confirm", cooldown_seconds=5):
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        await query.answer()
        from database import get_trade, update_trade, get_user_cards, remove_card_from_user, add_card_to_user, increment_quest_progress, get_db
        # ── ATOMIC STATUS LOCK: claim the trade before doing any card operations ──
        # This prevents any double-execution even in edge-case race conditions.
        db = get_db()
        claimed = await db.active_trades.find_one_and_update(
            {"trade_id": trade_id, "status": "awaiting_confirmation"},
            {"$set": {"status": "completing"}},
            return_document=False
        )
        if not claimed:
            # Either already completing, completed, expired, or cancelled
            await query.edit_message_text("❌ Trade already completed or expired."); return
        trade = claimed  # contains the pre-update document
        if time.time() - trade["created_at"] > 300:
            await db.active_trades.update_one({"trade_id": trade_id}, {"$set": {"status": "expired"}})
            await query.edit_message_text("❌ Trade expired (5 min timeout)."); return
        # Verify both parties still have the cards
        init_id  = trade["initiator_id"]
        tgt_id   = trade["target_id"]
        off_pid  = trade["offered_player_id"]
        off_fmt  = trade["offered_format"]
        req_pid  = trade["requested_player_id"]
        req_fmt  = trade["requested_format"]
        init_cards = await get_user_cards(init_id)
        tgt_cards  = await get_user_cards(tgt_id)
        init_has = next((c for c in init_cards if c["player_id"] == off_pid and c["format"] == off_fmt), None)
        tgt_has  = next((c for c in tgt_cards  if c["player_id"] == req_pid and c["format"] == req_fmt), None)
        if not init_has or not tgt_has:
            from database import get_db
            await get_db().active_trades.delete_one({"trade_id": trade_id})
            await query.edit_message_text("❌ Trade cancelled: one party no longer has the card."); return
        if init_has.get("rarity") == "mythical" or init_has.get("edition_id") or tgt_has.get("rarity") == "mythical" or tgt_has.get("edition_id"):
            from database import get_db
            await get_db().active_trades.delete_one({"trade_id": trade_id})
            await query.edit_message_text("❌ Trade cancelled: Special Edition cards cannot be traded."); return
        # Execute trade
        await asyncio.gather(
            remove_card_from_user(init_id, off_pid, off_fmt),
            remove_card_from_user(tgt_id,  req_pid, req_fmt),
        )
        await asyncio.gather(
            add_card_to_user(init_id, req_pid, req_fmt),
            add_card_to_user(tgt_id,  off_pid, off_fmt),
        )
        await asyncio.gather(
            increment_quest_progress(init_id, "cards_traded", 1),
            increment_quest_progress(tgt_id,  "cards_traded", 1),
        )
        await db.active_trades.delete_one({"trade_id": trade_id})
        off_fl = FORMAT_LABEL.get(off_fmt, off_fmt)
        req_fl = FORMAT_LABEL.get(req_fmt, req_fmt)
        await query.edit_message_text(
            f"✅ *Trade Complete!*\n"
            f"You gave: *{esc(trade['offered_name'])}* ({off_fl})\n"
            f"You got:  *{esc(trade['requested_name'])}* ({req_fl})",
            parse_mode="Markdown"
        )

async def cb_tr_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, user_id_str, trade_id = query.data.split("|")
    if str(query.from_user.id) != user_id_str:
        await query.answer("⛔ Not your trade.", show_alert=True); return
    # DB-level cooldown
    from database import try_acquire_action_cooldown
    if not await try_acquire_action_cooldown(int(user_id_str), "trade_decline", cooldown_seconds=5):
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    lock = _get_lock(int(user_id_str))
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        await query.answer()
        from database import cancel_trade
        await cancel_trade(trade_id)
        # Single message flow — just edit M1 to show declined result (no second message needed)
        await query.edit_message_text("❌ *Trade Declined.*", parse_mode="Markdown")

async def cb_tr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, initiator_id = query.data.split("|")
    if str(query.from_user.id) != initiator_id:
        await query.answer("⛔ Not your menu.", show_alert=True); return
    await query.answer()
    from database import get_user_active_trade, cancel_trade
    trade = await get_user_active_trade(int(initiator_id))
    if trade:
        await cancel_trade(trade["trade_id"])
    await query.edit_message_text("❌ Trade cancelled.")

# ─────────────────────────────────────────────────────────────────────────────
# /quest
# ─────────────────────────────────────────────────────────────────────────────
async def handle_quest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat and update.effective_chat.type != "private":
        return
    from database import get_daily_quests, QUEST_DEFINITIONS
    quests = await get_daily_quests(user.id)
    reset_at = quests.get("reset_at", 0)
    now = time.time()
    secs_left = max(0, int(reset_at - now))
    hours = secs_left // 3600
    mins  = (secs_left % 3600) // 60
    claimed = quests.get("claimed", [])
    lines = [f"📋 *Daily Quests*", f"Resets in: *{hours}h {mins}m*", "━━━━━━━━━━━━━━━━━━"]
    has_claimable = False
    for key, defn in QUEST_DEFINITIONS.items():
        progress = quests.get(defn["field"], 0)
        target   = defn["target"]
        done     = progress >= target
        already  = key in claimed
        if done and not already:
            has_claimable = True
            status = "✅ COMPLETED"
        elif already:
            status = "🏆 Claimed"
        else:
            status = f"({min(progress, target)}/{target})"
        lines.append(f"{status} {defn['label']} → *+{defn['reward']}🪙*")
    lines.append("━━━━━━━━━━━━━━━━━━")
    text = "\n".join(lines)
    kb = None
    if has_claimable:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🎁 Claim Rewards", callback_data=f"quest_claim|{user.id}"
        )]])
    try:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await update.effective_message.reply_text(text, reply_markup=kb)

async def cb_quest_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, owner_id = query.data.split("|")
    user_id = query.from_user.id
    if str(user_id) != owner_id:
        await query.answer("⛔ Not your quests.", show_alert=True); return
    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Please wait a moment...", show_alert=False); return
    async with lock:
        await query.answer()
        from database import claim_quest_rewards, get_card_coins
        coins_earned, claimed_keys = await claim_quest_rewards(user_id)
        if not claimed_keys:
            await query.answer("No rewards to claim right now.", show_alert=True); return
        new_bal = await get_card_coins(user_id)
        await query.edit_message_text(
            f"🎁 *Rewards Claimed!*\n"
            f"Earned: *+{coins_earned}🪙*\n"
            f"New balance: *{new_bal}🪙*\n\n"
            f"Come back tomorrow for new quests!",
            parse_mode="Markdown"
        )

# ─────────────────────────────────────────────────────────────────────────────
# /ggive — Gift coins to another user
# Usage: reply to target user's message, then /ggive <amount>
# ─────────────────────────────────────────────────────────────────────────────
async def handle_ggive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    sender = update.effective_user
    if not msg.reply_to_message:
        await msg.reply_text("💸 Reply to the target user's message and use:\n/ggive <amount>")
        return
    target = msg.reply_to_message.from_user
    if target.is_bot or target.id == sender.id:
        await msg.reply_text("❌ You can't send coins to a bot or yourself.")
        return
    from database import is_user_banned
    if await is_user_banned(target.id):
        await msg.reply_text("❌ Target user is banned.")
        return

    args = context.args
    if not args:
        await msg.reply_text("💸 Usage: /ggive <amount>\nExample: /ggive 100")
        return
    raw_amount = args[0].strip()
    if not raw_amount.isdecimal():
        await msg.reply_text("❌ Please enter a valid number (e.g. /ggive 100).")
        return
    try:
        amount = int(raw_amount)
    except (ValueError, TypeError):
        await msg.reply_text("❌ Please enter a valid number (e.g. /ggive 100).")
        return
    if amount < 1:
        await msg.reply_text("❌ Amount must be at least 1🪙.")
        return
    lock = _get_lock(sender.id)
    if lock.locked():
        await msg.reply_text("⏳ Please wait a moment before transferring again.")
        return
    async with lock:
        from database import get_card_coins, deduct_card_coins, add_card_coins
        sender_balance = await get_card_coins(sender.id)
        if sender_balance < amount:
            await msg.reply_text(
                f"❌ Insufficient balance.\n"
                f"You have *{sender_balance}🪙*, trying to send *{amount}🪙*.",
                parse_mode="Markdown"
            )
            return
        success, new_sender_bal = await deduct_card_coins(sender.id, amount)
        if not success:
            await msg.reply_text("❌ Transfer failed — insufficient balance.", parse_mode="Markdown")
            return
        await add_card_coins(target.id, amount)
        await msg.reply_text(
            f"✅ Sent *{amount}🪙* to {esc(target.first_name)}!\n"
            f"Your balance: *{new_sender_bal}🪙*",
            parse_mode="Markdown"
        )

# # ─────────────────────────────────────────────────────────────────────────────
# /multi_sell
# ─────────────────────────────────────────────────────────────────────────────
_PENDING_MULTI_SELLS: dict[str, dict] = {}
_COMPLETED_MULTI_SELLS: set[str] = set()

async def handle_multi_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Sell multiple cards by their numbered position in /mycards (all mode).
    Shows confirmation preview with cards count, coins payout, and Confirm/Cancel buttons.
    Usage:
      /multi_sell 1-20   → preview selling cards numbered 1 through 20
      /multi_sell 1 3 5  → preview selling cards at positions 1, 3, 5
    """
    user = update.effective_user
    if not user:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return
    if not _check_card_cooldown(user.id, "multi_sell", 3.0):
        return
    args = context.args

    if not args:
        await update.effective_message.reply_text(
            "📦 *Multi-Sell*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Usage:\n"
            "  `/multi_sell 1-10` — sell cards #1 to #10\n"
            "  `/multi_sell 1 3 5` — sell cards #1, #3, #5\n\n"
            "_Card numbers match the /mycards list (All tab)_",
            parse_mode="Markdown"
        )
        return

    # Parse indices
    indices: set[int] = set()
    try:
        if len(args) == 1 and "-" in args[0]:
            # Range: "1-20"
            parts = args[0].split("-")
            start, end = int(parts[0]), int(parts[1])
            if start < 1 or end < start or (end - start) > 200:
                raise ValueError
            indices = set(range(start, end + 1))
        else:
            # Space-separated numbers
            for a in args:
                indices.add(int(a))
    except (ValueError, IndexError):
        await update.effective_message.reply_text(
            "❌ Invalid format.\nUse `/multi_sell 1-10` or `/multi_sell 1 3 5`",
            parse_mode="Markdown"
        )
        return

    if not indices:
        await update.effective_message.reply_text("❌ No valid card numbers provided.")
        return

    # Fetch current cards (using active user sort order)
    from database import get_user_cards, get_fav_card
    crit, order = await get_user_sort(user.id)
    all_cards = await get_user_cards(user.id, sport_filter=None)
    all_cards = _sort_cards(all_cards, crit, order)

    SELL_VALUES = {"common": 25, "rare": 75, "epic": 200, "legend": 600}

    fav = await get_fav_card(user.id)
    fav_key = (fav.get("player_id"), fav.get("format")) if fav else (None, None)

    total_coins = 0
    cards_to_sell = []
    skipped_fav = 0
    skipped_missing = 0
    rarity_counts: dict[str, int] = {}

    for idx in sorted(indices):
        pos = idx - 1  # 0-indexed
        if pos < 0 or pos >= len(all_cards):
            skipped_missing += 1
            continue
        card = all_cards[pos]
        if card.get("rarity") == "mythical" or card.get("edition_id"):
            skipped_missing += 1
            continue
        pid, fmt = card["player_id"], card["format"]
        # Protect favorite card if last copy
        if (pid, fmt) == fav_key and card.get("quantity", 1) <= 1:
            skipped_fav += 1
            continue
        rarity = card.get("rarity", "common")
        sell_val = SELL_VALUES.get(rarity, 25)
        total_coins += sell_val
        rarity_counts[rarity] = rarity_counts.get(rarity, 0) + 1
        cards_to_sell.append(card)

    if not cards_to_sell:
        lines = [
            "📦 *Multi-Sell Preview*",
            "━━━━━━━━━━━━━━━━━━",
            "❌ No valid cards to sell.",
        ]
        if skipped_fav:
            lines.append(f"⭐ Skipped *{skipped_fav}* fav card(s) — remove fav first to sell.")
        if skipped_missing:
            lines.append(f"⚠️ Skipped *{skipped_missing}* invalid/missing position(s).")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # Auto-prune sessions older than 5 minutes
    now = time.time()
    expired = [k for k, v in _PENDING_MULTI_SELLS.items() if now - v.get("created_at", 0) > 300]
    for k in expired:
        _PENDING_MULTI_SELLS.pop(k, None)

    # Store pending session
    sell_id = uuid.uuid4().hex[:8]
    _PENDING_MULTI_SELLS[sell_id] = {
        "user_id": user.id,
        "indices": sorted(indices),
        "created_at": now,
    }

    # Format preview message
    lines = [
        "📦 *Multi-Sell Confirmation*",
        "━━━━━━━━━━━━━━━━━━",
        f"🃏 Cards selected: *{len(cards_to_sell)}*",
        f"💰 Total payout: *+{total_coins}🪙*",
    ]
    # Rarity breakdown
    breakdown = []
    for r in ["legend", "epic", "rare", "common"]:
        cnt = rarity_counts.get(r, 0)
        if cnt > 0:
            r_emoji = RARITY_EMOJI.get(r, "⚪")
            val = cnt * SELL_VALUES[r]
            breakdown.append(f"{r_emoji} {r.title()}: *{cnt}* (+{val}🪙)")
    if breakdown:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.extend(breakdown)

    if skipped_fav or skipped_missing:
        lines.append("━━━━━━━━━━━━━━━━━━")
        if skipped_fav:
            lines.append(f"⭐ Skipped *{skipped_fav}* fav card(s) (protected)")
        if skipped_missing:
            lines.append(f"⚠️ Skipped *{skipped_missing}* invalid position(s)")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("Are you sure you want to sell these cards?")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Confirm Sell (+{total_coins}🪙)", callback_data=f"msell_ok|{user.id}|{sell_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"msell_cancel|{user.id}|{sell_id}"),
        ]
    ])
    await update.effective_message.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")


async def cb_msell_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback when user clicks Confirm Sell."""
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please manage and sell cards in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Invalid request.", show_alert=True)
        return
    _, owner_id, sell_id = parts
    user_id = query.from_user.id
    if str(user_id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    if sell_id in _COMPLETED_MULTI_SELLS:
        await query.answer("Already processed.", show_alert=False)
        return

    session = _PENDING_MULTI_SELLS.pop(sell_id, None)
    if not session or (time.time() - session.get("created_at", 0) > 300):
        await query.answer("⌛ This sell request has expired. Please run /multi_sell again.", show_alert=True)
        try:
            await query.edit_message_text("⌛ *Multi-Sell Expired*\nPlease run `/multi_sell` again.", parse_mode="Markdown")
        except Exception:
            pass
        return

    _COMPLETED_MULTI_SELLS.add(sell_id)
    if len(_COMPLETED_MULTI_SELLS) > 200:
        _COMPLETED_MULTI_SELLS.pop()

    await query.answer("Selling cards...")

    from database import get_user_cards, get_fav_card, remove_card_from_user, add_card_coins, increment_quest_progress
    indices = session.get("indices", [])
    rarity_order = {"legend": 0, "epic": 1, "rare": 2, "common": 3}
    SELL_VALUES = {"common": 25, "rare": 75, "epic": 200, "legend": 600}

    lock = _get_lock(user_id)
    async with lock:
        # Re-fetch cards to ensure snapshot is fully accurate
        crit, order = await get_user_sort(user_id)
        all_cards = await get_user_cards(user_id, sport_filter=None)
        all_cards = _sort_cards(all_cards, crit, order)

        fav = await get_fav_card(user_id)
        fav_key = (fav.get("player_id"), fav.get("format")) if fav else (None, None)

        total_coins = 0
        sold_count = 0
        skipped_fav = 0
        skipped_missing = 0

        for idx in indices:
            pos = idx - 1  # 0-indexed
            if pos < 0 or pos >= len(all_cards):
                skipped_missing += 1
                continue
            card = all_cards[pos]
            if card.get("rarity") == "mythical" or card.get("edition_id"):
                skipped_missing += 1
                continue
            pid, fmt = card["player_id"], card["format"]
            # Skip fav card if last copy
            if (pid, fmt) == fav_key and card.get("quantity", 1) <= 1:
                skipped_fav += 1
                continue
            sell_val = SELL_VALUES.get(card.get("rarity", "common"), 25)
            remaining = await remove_card_from_user(user_id, pid, fmt)
            if remaining < 0:
                skipped_missing += 1
                continue
            total_coins += sell_val
            sold_count += 1

        if total_coins > 0:
            new_bal = await add_card_coins(user_id, total_coins)
            # Track quest progress
            try:
                await increment_quest_progress(user_id, "cards_sold", sold_count)
            except Exception:
                pass
        else:
            new_bal = None

    lines = ["💰 *Multi-Sell Complete*", "━━━━━━━━━━━━━━━━━━"]
    if sold_count > 0:
        lines.append(f"✅ Sold *{sold_count}* card(s) for *+{total_coins}🪙*")
        if new_bal is not None:
            lines.append(f"💰 New Balance: *{new_bal}🪙*")
    else:
        lines.append("❌ No cards were sold.")
    if skipped_fav:
        lines.append(f"⭐ Skipped *{skipped_fav}* fav card(s) — remove fav first to sell.")
    if skipped_missing:
        lines.append(f"⚠️ Skipped *{skipped_missing}* invalid/missing position(s).")

    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


async def cb_msell_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback when user clicks Cancel."""
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please manage cards in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Invalid request.", show_alert=True)
        return
    _, owner_id, sell_id = parts
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    _PENDING_MULTI_SELLS.pop(sell_id, None)
    await query.answer("Cancelled")
    await query.edit_message_text(
        "❌ *Multi-Sell Cancelled*\n━━━━━━━━━━━━━━━━━━\nNo cards were sold.",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Special Edition Offer System (/offer) — DM Only
# ═══════════════════════════════════════════════════════════════════════════

async def handle_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /offer — View and claim active Special Edition offer (DM Only).
    """
    user = update.effective_user
    if not user:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return  # Silent ignore in groups

    from database import get_active_offer, get_user_cards, validate_fav_card
    import time
    offer = await get_active_offer()
    now = time.time()
    if not offer or now >= offer.get("expires_at", 0):
        await update.effective_message.reply_text("ℹ️ No offer currently available.")
        return

    claimed_users = offer.get("claimed_users", [])
    already_claimed = user.id in claimed_users

    name = offer.get("player_name", "Unknown")
    edition = offer.get("edition", "Special Edition")
    ovr = offer.get("ovr", 90)
    fmt = offer.get("format", "odi").upper()
    req_rarity = offer.get("submit_rarity", "legend").lower()
    req_count = offer.get("submit_count", 20)
    image_url = offer.get("image_url", "")
    rem_secs = max(0, int(offer.get("expires_at", 0) - now))
    rem_days = rem_secs // 86400
    rem_hours = (rem_secs % 86400) // 3600

    r_emoji = RARITY_EMOJI.get(req_rarity, "🟡")

    import html
    name_html = html.escape(str(name))
    edition_html = html.escape(str(edition))

    if already_claimed:
        msg = (
            f"✨ *Special Edition: {esc(name)} ({esc(edition)})*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 Format: *{fmt}* | ⭐ OVR: *{ovr}*\n"
            f"💎 Rarity: ✨ *Mythical*\n\n"
            f"✅ *You have already claimed this Special Edition card!*\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        msg_html = (
            f"✨ <b>Special Edition: {name_html} ({edition_html})</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 Format: <b>{fmt}</b> | ⭐ OVR: <b>{ovr}</b>\n"
            f"💎 Rarity: ✨ <b>Mythical</b>\n\n"
            f"✅ <b>You have already claimed this Special Edition card!</b>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        if image_url and image_url.startswith("http"):
            href_text = f'<a href="{image_url}">&#8205;</a>' + msg_html
            try:
                await update.effective_message.reply_text(
                    href_text, parse_mode="HTML", disable_web_page_preview=False
                )
                return
            except Exception:
                pass
        await update.effective_message.reply_text(msg, parse_mode="Markdown")
        return

    # Calculate user's eligible cards
    all_cards = await get_user_cards(user.id)
    fav = await validate_fav_card(user.id)
    total_eligible = 0
    for c in all_cards:
        if c.get("rarity") != req_rarity or c.get("edition_id"):
            continue
        qty = c.get("quantity", 1)
        if fav and c.get("player_id") == fav.get("player_id") and c.get("format") == fav.get("format"):
            qty -= 1
        if qty > 0:
            total_eligible += qty

    has_enough = (total_eligible >= req_count)

    msg = (
        f"✨ *Special Edition Offer: {esc(name)} ({esc(edition)})*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Format: *{fmt}* | ⭐ OVR: *{ovr}*\n"
        f"💎 Rarity: ✨ *Mythical*\n\n"
        f"📥 *Requirement:* Submit *{req_count} {req_rarity.title()}* cards\n"
        f"📊 *Your Progress:* *{total_eligible}/{req_count}* {r_emoji} {req_rarity.title()} Cards\n"
        f"⏳ *Time Remaining:* {rem_days}d {rem_hours}h\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_(Cards from any sport qualify. Duplicates are consumed first; your favorite card is safe.)_"
    )

    if has_enough:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎁 Claim Edition Card", callback_data=f"offer_claim|{user.id}")
        ]])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"❌ Need {req_count - total_eligible} more {req_rarity.title()}s", callback_data="offer_insuf")
        ]])

    msg_html = (
        f"✨ <b>Special Edition Offer: {name_html} ({edition_html})</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Format: <b>{fmt}</b> | ⭐ OVR: <b>{ovr}</b>\n"
        f"💎 Rarity: ✨ <b>Mythical</b>\n\n"
        f"📥 <b>Requirement:</b> Submit <b>{req_count} {req_rarity.title()}</b> cards\n"
        f"📊 <b>Your Progress:</b> <b>{total_eligible}/{req_count}</b> {r_emoji} {req_rarity.title()} Cards\n"
        f"⏳ <b>Time Remaining:</b> {rem_days}d {rem_hours}h\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>(Cards from any sport qualify. Duplicates are consumed first; your favorite card is safe.)</i>"
    )

    if image_url and image_url.startswith("http"):
        href_text = f'<a href="{image_url}">&#8205;</a>' + msg_html
        try:
            await update.effective_message.reply_text(
                href_text, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=False
            )
            return
        except Exception:
            pass

    await update.effective_message.reply_text(msg, reply_markup=kb, parse_mode="Markdown")


async def cb_offer_insuf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("❌ You don't have enough cards to claim this offer yet. Open more packs or trade!", show_alert=True)


async def cb_offer_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 of claim: Show confirmation popup with Confirm and Cancel buttons."""
    query = update.callback_query
    parts = query.data.split("|")
    owner_id = parts[1]
    user_id = query.from_user.id
    if str(user_id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    from database import get_active_offer
    import time
    offer = await get_active_offer()
    if not offer or time.time() >= offer.get("expires_at", 0):
        await query.answer("❌ This offer has expired.", show_alert=True)
        return

    req_rarity = offer.get("submit_rarity", "legend").title()
    req_count = offer.get("submit_count", 20)
    name = offer.get("player_name", "")
    edition = offer.get("edition", "")

    confirm_text = (
        f"⚠️ *Confirm Card Exchange*\n\n"
        f"Are you sure you want to sacrifice *{req_count} {req_rarity}* cards to claim ✨ *{esc(name)} ({esc(edition)})*?\n\n"
        f"🛡️ *Safeguards:*\n"
        f"• Your favorite card is strictly protected.\n"
        f"• Duplicate copies are consumed first before unique singles.\n\n"
        f"This action is permanent and cannot be undone."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Exchange", callback_data=f"offer_confirm|{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"offer_cancel|{user_id}")]
    ])

    if query.message and query.message.photo:
        await query.edit_message_caption(caption=confirm_text, reply_markup=kb, parse_mode="Markdown")
    else:
        await query.edit_message_text(text=confirm_text, reply_markup=kb, parse_mode="Markdown")


async def cb_offer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel offer exchange confirmation."""
    query = update.callback_query
    parts = query.data.split("|")
    owner_id = parts[1]
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True)
        return
    await query.answer("Exchange cancelled.")
    if query.message and query.message.photo:
        await query.edit_message_caption(caption="❌ Exchange cancelled. Your cards were not modified.")
    else:
        await query.edit_message_text("❌ Exchange cancelled. Your cards were not modified.")


async def cb_offer_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 of claim: Atomic execution with anti-spam lock."""
    query = update.callback_query
    parts = query.data.split("|")
    owner_id = parts[1]
    user_id = query.from_user.id
    if str(user_id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True)
        return

    # Anti-spam cooldown check
    from database import try_acquire_action_cooldown, get_active_offer, get_user_cards, validate_fav_card, remove_card_from_user, add_edition_card_to_user, get_db
    import time
    if not await try_acquire_action_cooldown(user_id, "offer_claim", cooldown_seconds=5):
        await query.answer("⏳ Processing, please wait...", show_alert=False)
        return

    lock = _get_lock(user_id)
    if lock.locked():
        await query.answer("⏳ Processing your request...", show_alert=False)
        return

    async with lock:
        await query.answer()
        offer = await get_active_offer()
        if not offer or time.time() >= offer.get("expires_at", 0):
            await query.edit_message_text("❌ This offer has expired.")
            return

        claimed_users = offer.get("claimed_users", [])
        if user_id in claimed_users:
            await query.edit_message_text("✅ You have already claimed this Special Edition card!")
            return

        req_rarity = offer.get("submit_rarity", "legend").lower()
        req_count = offer.get("submit_count", 20)

        all_cards = await get_user_cards(user_id)
        fav = await validate_fav_card(user_id)

        # Select cards to sacrifice (duplicates first, protecting fav)
        eligible = []
        for c in all_cards:
            if c.get("rarity") != req_rarity or c.get("edition_id"):
                continue
            qty = c.get("quantity", 1)
            if fav and c.get("player_id") == fav.get("player_id") and c.get("format") == fav.get("format"):
                qty -= 1
            if qty > 0:
                eligible.append({
                    "player_id": c["player_id"],
                    "format": c["format"],
                    "name": c.get("name", ""),
                    "available": qty,
                })

        total_avail = sum(e["available"] for e in eligible)
        if total_avail < req_count:
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=f"❌ You no longer have {req_count} {req_rarity.title()} cards available.")
            else:
                await query.edit_message_text(f"❌ You no longer have {req_count} {req_rarity.title()} cards available.")
            return

        # Phase 1: Deduct duplicates down to 1
        to_consume = []
        remaining = req_count
        eligible.sort(key=lambda x: x["available"], reverse=True)

        for e in eligible:
            if remaining <= 0:
                break
            if e["available"] > 1:
                dupes = e["available"] - 1
                take = min(dupes, remaining)
                to_consume.append({"player_id": e["player_id"], "format": e["format"], "count": take})
                e["available"] -= take
                remaining -= take

        # Phase 2: Deduct singles if needed
        if remaining > 0:
            for e in eligible:
                if remaining <= 0:
                    break
                if e["available"] > 0:
                    take = min(e["available"], remaining)
                    found = next((tc for tc in to_consume if tc["player_id"] == e["player_id"] and tc["format"] == e["format"]), None)
                    if found:
                        found["count"] += take
                    else:
                        to_consume.append({"player_id": e["player_id"], "format": e["format"], "count": take})
                    e["available"] -= take
                    remaining -= take

        # Execute deductions
        for tc in to_consume:
            await remove_card_from_user(user_id, tc["player_id"], tc["format"], quantity=tc["count"])

        # Grant Special Edition card with atomic rollback safety
        edition_id = offer["edition_id"]
        try:
            success, grant_msg = await add_edition_card_to_user(user_id, edition_id)
            if not success:
                # Rollback deducted cards
                for tc in to_consume:
                    await add_card_to_user(user_id, tc["player_id"], tc["format"], quantity=tc["count"])
                err_msg = f"❌ Failed to claim card: {grant_msg}\n\nYour sacrificed cards have been restored to your inventory."
                if query.message and query.message.photo:
                    await query.edit_message_caption(caption=err_msg)
                else:
                    await query.edit_message_text(text=err_msg)
                return
        except Exception as _grant_err:
            logger.error(f"Failed to grant edition card to user {user_id}: {_grant_err}", exc_info=True)
            for tc in to_consume:
                try:
                    await add_card_to_user(user_id, tc["player_id"], tc["format"], quantity=tc["count"])
                except Exception:
                    pass
            err_msg = "❌ An unexpected error occurred while claiming. Your sacrificed cards have been restored to your inventory."
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=err_msg)
            else:
                await query.edit_message_text(text=err_msg)
            return

        # Mark user in claimed_users
        db = get_db()
        await db.system_config.update_one(
            {"key": "active_offer"},
            {"$addToSet": {"claimed_users": user_id}}
        )

        name = offer.get("player_name", "")
        edition = offer.get("edition", "")
        success_text = (
            f"🎉 *CONGRATULATIONS!* 🎉\n\n"
            f"You successfully sacrificed *{req_count} {req_rarity.title()}* cards and claimed:\n"
            f"✨ *{esc(name)} ({esc(edition)})*!\n\n"
            f"⭐ Rarity: ✨ *Mythical*\n"
            f"🏆 OVR: *{offer.get('ovr', 90)}*\n\n"
            f"Check out your new mythical card in `/mycards` or `/viewcard {name}`!"
        )

        if query.message and query.message.photo:
            await query.edit_message_caption(caption=success_text, parse_mode="Markdown")
        else:
            await query.edit_message_text(text=success_text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════
# Referral & Growth System (/share, /referrals)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /share and /referrals — View personal referral link, stats, and milestone ladder (DM Only).
    """
    user = update.effective_user
    if not user:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return  # Silent ignore in groups

    bot_username = getattr(context.bot, "username", "") or ""
    if not bot_username:
        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username
        except Exception:
            pass

    ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    from database import get_user_referral_stats
    stats = await get_user_referral_stats(user.id)

    total_inv = stats["total_invited"]
    qualified = stats["total_qualified"]
    coins = stats["coins_earned"]
    pos = stats["pos_in_cycle"]
    next_m = stats["next_milestone"]
    reward_desc = stats["reward_desc"]

    text = (
        f"👥 *Player Referral Program*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Invite your friends to the bot and earn Card Coins & exclusive packs!\n\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"📊 *Your Stats:*\n"
        f"• Friends Joined: *{total_inv}*\n"
        f"• Qualified Friends (Level 2+): *{qualified}*\n"
        f"• Card Coins Earned: *{coins:,} 🪙*\n\n"
        f"🎯 *Next Milestone:* *{pos}/{next_m}* Qualified Friends\n"
        f"🎁 *Reward:* {reward_desc}\n\n"
        f"🏆 *Milestone Rewards Ladder:*\n"
        f"• 3 Friends ➔ 300 🪙 + 1 Premium Pack\n"
        f"• 7 Friends ➔ 750 🪙 + 1 Elite Pack\n"
        f"• 15 Friends ➔ 2,000 🪙 + 2 Elite Packs\n"
        f"_(Loops infinitely every 15 friends!)_\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    import urllib.parse
    share_msg = f"Join me on Cricket Bot, draft your dream team, and claim your welcome bonus!"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote(share_msg)}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share to Friends", url=share_url)],
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data=f"ref_refresh|{user.id}")]
    ])

    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def cb_ref_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat and update.effective_chat.type != "private":
        await query.answer("📬 Please check referrals in DM!", show_alert=True)
        return
    parts = query.data.split("|")
    owner_id = parts[1]
    if str(query.from_user.id) != owner_id:
        await query.answer("⛔ Not your menu.", show_alert=True)
        return
    await query.answer("Stats updated!")
    bot_username = getattr(context.bot, "username", "") or ""
    ref_link = f"https://t.me/{bot_username}?start=ref_{owner_id}"
    from database import get_user_referral_stats
    stats = await get_user_referral_stats(int(owner_id))

    total_inv = stats["total_invited"]
    qualified = stats["total_qualified"]
    coins = stats["coins_earned"]
    pos = stats["pos_in_cycle"]
    next_m = stats["next_milestone"]
    reward_desc = stats["reward_desc"]

    text = (
        f"👥 *Player Referral Program*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Invite your friends to the bot and earn Card Coins & exclusive packs!\n\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"📊 *Your Stats:*\n"
        f"• Friends Joined: *{total_inv}*\n"
        f"• Qualified Friends (Level 2+): *{qualified}*\n"
        f"• Card Coins Earned: *{coins:,} 🪙*\n\n"
        f"🎯 *Next Milestone:* *{pos}/{next_m}* Qualified Friends\n"
        f"🎁 *Reward:* {reward_desc}\n\n"
        f"🏆 *Milestone Rewards Ladder:*\n"
        f"• 3 Friends ➔ 300 🪙 + 1 Premium Pack\n"
        f"• 7 Friends ➔ 750 🪙 + 1 Elite Pack\n"
        f"• 15 Friends ➔ 2,000 🪙 + 2 Elite Packs\n"
        f"_(Loops infinitely every 15 friends!)_\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    import urllib.parse
    share_msg = f"Join me on Cricket Bot, draft your dream team, and claim your welcome bonus!"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote(share_msg)}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share to Friends", url=share_url)],
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data=f"ref_refresh|{owner_id}")]
    ])

    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            pass
