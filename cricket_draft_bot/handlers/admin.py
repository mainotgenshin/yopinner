# handlers/admin.py
from telegram import Update
from telegram.ext import ContextTypes
import logging
from database import save_player, save_match
from utils.images import download_image
import uuid
import os
from utils.permissions import check_admin, check_owner, can_manage_bot
from database import add_mod, remove_mod

logger = logging.getLogger(__name__)

async def add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_player name=Rohit Sharma roles=Captain,Hitting image=http...
    """
    if not await check_admin(update): return

    user_id = update.effective_user.id
    # Add admin check here if needed, for now assume anyone can add (or restricted by owner id in main?)
    # ideally config.admin_ids
    
    text = update.message.text.replace('/add_player', '').strip()
    if not text:
        await update.message.reply_text("Usage:\n/add_player name=X roles=A,B image=URL")
        return

    try:
        import re
        
        # Regex to capture key=value pairs, handling spaces and multi-word values
        # Looks for keywords (name, roles, image) followed by '=', then captures content until next keyword or end
        pattern = r'(name|roles|image)\s*=\s*(.*?)(?=\s+(?:name|roles|image)\s*=|$) '[:-1]
        
        # Implicit Name Handling:
        # If the text doesn't start with a keyword (name=, roles=, etc), assume the first part is the name
        raw_text = update.message.text.replace('/add_player', '').strip()
        
        if not re.match(r'^(name|roles|image)\s*=', raw_text, re.IGNORECASE):
            # User likely typed: "/add_player Virat Kohli roles=..."
            # We treat everything before the first keyword as the name
            # But simpler hack: just prepend "name=" and let the regex handle it
            raw_text = "name=" + raw_text

        # Add trailing space for regex lookahead
        raw_text += ' '
        
        matches = re.finditer(pattern, raw_text, re.IGNORECASE | re.DOTALL)
        parsed = {m.group(1).lower(): m.group(2).strip() for m in matches}
        
        if 'name' not in parsed or 'roles' not in parsed or 'image' not in parsed:
                raise ValueError("Missing required fields (name, roles, image)")
            
        name = parsed['name'].strip()
        # Handle comma-separated roles, stripping whitespace
        roles = [r.strip() for r in parsed['roles'].split(',') if r.strip()]
        
        # Validation: Check against ALLOWED POSITIONS
        from config import POSITIONS_T20
        # Normalize for comparison
        # Actually config.POSITIONS_T20 has titles "All-Rounder", "Captain" etc.
        # User input might vary slightly but we want strict.
        
        valid_roles = set(POSITIONS_T20)
        # Create a lowercase map for friendly suggestions
        valid_map = {r.lower(): r for r in valid_roles}
        # Add friendly aliases
        valid_map['all-rounder'] = "All Rounder" # legacy
        valid_map['all'] = "All Rounder"
        valid_map['all rounder'] = "All Rounder"
        valid_map['batting'] = "Top" # assumption? Or strict? 
        # Let's keep it strict but handle the "All Rounder" nuance.
        
        validated_roles = []
        for r in roles:
            clean_r = r.strip().lower()
            if clean_r in valid_map:
                validated_roles.append(valid_map[clean_r])
            else:
                 # Suggest valid roles
                 allowed_str = ", ".join(sorted(list(valid_roles)))
                 await update.message.reply_text(
                     f"❌ **Invalid Role:** `{r}`\n"
                     f"Allowed: {allowed_str}\n" 
                     f"**New Roles:** Top, Middle, All Rounder, Pacer, Spinner, Finisher, Fielder, Defence, Captain, WK",
                     parse_mode="Markdown"
                 )
                 return

        roles = validated_roles
        image_url = parsed['image'].strip()
            
        # ID Generation
        # IND_ROHIT style?
        clean_name = name.upper().replace(' ', '_')
        player_id = f"PL_{clean_name[:10]}"
        
        # Handle Image
        # We need to send it to Telegram to get a file_id, OR rely on URL.
        # Prompt says: "Download image... Store Telegram file_id"
        # To get file_id, we can send it to self (bot) or user?
        # Let's send to user, get the message, extract file_id.
        msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image_url, caption=f"Added {name}")
        image_file_id = msg.photo[-1].file_id
        
        player = {
            "player_id": player_id,
            "name": name,
            "roles": roles, # Intl / Default Roles
            "image_file_id": image_file_id,
            "ipl_roles": list(roles), # Seed IPL roles same as Intl initially? Or empty? User: "append every stats/roles... to ipl"
            "ipl_image_file_id": image_file_id,
            "api_reference": {},
            "stats": {} 
        }
        save_player(player)
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [[InlineKeyboardButton("🎲 Generate Stats (Intl)", callback_data=f"gen_intl_{player_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ Player Added!\nID: {player_id}\nName: {name}\nRoles: {roles}",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: {e}\n\n"
            "**Correct Syntax:**\n"
            "`/add_player name=Name roles=Role1,Role2 image=URL`"
        , parse_mode="Markdown")


async def generate_player_stats(player_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False, mode='intl'):
    """
    Shared logic to generate stats.
    mode: 'intl' or 'ipl'
    """
    from database import get_player, save_player
    p = get_player(player_id)
    
    if not p:
        msg = f"Player not found ({player_id})"
        if is_callback:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    # Notify user
    mode_label = "IPL" if mode == 'ipl' else "International"
    if is_callback:
        await update.callback_query.answer(f"🔄 Generating {mode_label} stats...", show_alert=False)
        await update.callback_query.message.reply_text(f"🔄 Fetching {mode_label} stats for {p['name']}... Please wait.")
    else:
        await update.message.reply_text(f"🔄 Fetching {mode_label} stats for {p['name']}... Please wait.")
    
    from utils.scraper import scrape_player_stats
    
    # Determine roles for scraping context
    roles_to_use = p.get('ipl_roles', []) if mode == 'ipl' else p.get('roles', [])
    if not roles_to_use: roles_to_use = p.get('roles', []) # Fallback
    
    # Use Seeded/Scraped Stats
    ai_stats = await scrape_player_stats(p['name'], roles_to_use)
    
    # Update Specific Mode Stats
    current_stats = p.get('stats', {})
    
    if mode == 'ipl':
        current_stats['ipl'] = ai_stats['ipl']
        # source label maybe separate?
        if 'api_reference' not in p: p['api_reference'] = {}
        p['api_reference']['ipl_provider'] = ai_stats.get('source_label', 'Seeded')
    else:
        current_stats['international'] = ai_stats['international']
        if 'api_reference' not in p: p['api_reference'] = {}
        p['api_reference']['provider'] = ai_stats.get('source_label', 'Seeded')
        
    p['stats'] = current_stats
    save_player(p)
         
    # Nicer output
    stat_data = current_stats.get(mode, {})
    
    summary = (
        f"✅ **{mode_label} Stats Generated for {p['name']}**\n"
        f"Source: {ai_stats.get('source_label', 'Unknown')}\n\n"
        f"Use /stats {p['name']} to see full details."
    )
    
    if is_callback:
         await update.callback_query.message.reply_text(summary, parse_mode="Markdown")
    else:
         await update.message.reply_text(summary, parse_mode="Markdown")
    


async def map_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /map_api player_id=IND_ROHIT
    """
    text = update.message.text.replace('/map_api', '').strip()
    if not text:
        await update.message.reply_text("**Usage:**\n`/map_api player_id=ID`", parse_mode="Markdown")
        return

    if not await check_admin(update): return

    player_id = text.split('=')[1].strip() if '=' in text else text.strip()
    player_id = text.split('=')[1].strip() if '=' in text else text.strip()
    await generate_player_stats(player_id, update, context, is_callback=False, mode='intl')

async def handle_gen_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # gen_intl_ID
    if not can_manage_bot(update.effective_user.id):
        await query.answer("⛔ Admin Only", show_alert=True)
        return
    await generate_player_stats(player_id, update, context, is_callback=True, mode='intl')

async def handle_gen_ipl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # gen_ipl_ID
    if not can_manage_bot(update.effective_user.id):
        await query.answer("⛔ Admin Only", show_alert=True)
        return
    await generate_player_stats(player_id, update, context, is_callback=True, mode='ipl')

# Deprecated map_stats callback for backward compatibility or alias to intl?
async def handle_map_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Map old map_ calls to intl
    query = update.callback_query
    try:
        player_id = query.data.split('_', 1)[1] 
    except:
        player_id = "UNKNOWN"
        
    await generate_player_stats(player_id, update, context, is_callback=True, mode='intl')

# Update add_player to include button
# This requires editing the add_player success block, which is lines 79-80.
# I will do that in a separate replacement call or include it here if ranges overlap.
# They don't overlap easily with this block replacing line 88+.
# Use MultiReplace? No, limited lines.
# I will implement generate_player_stats and update map_api first.


async def remove_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeplayer player_id=IND_KOHLI
    """
    text = update.message.text.replace('/removeplayer', '').strip()
    if not text:
        await update.message.reply_text("**Usage:**\n`/removeplayer player_id=ID`", parse_mode="Markdown")
        return
        
    if not await check_admin(update): return
        
    try:
        player_id = text.split('=')[1].strip() if '=' in text else text.strip()
        from database import delete_player, get_player
        
        p = get_player(player_id)
        if not p:
            # Fallback: Try by name
            from database import get_player_by_name
            p = get_player_by_name(player_id) # player_id variable holds the search text here
            
            if not p:
                await update.message.reply_text(f"❌ Player not found by ID or Name: '{player_id}'")
                return
            
            # Found by name, update player_id to the actual ID found
            player_id = p['player_id']

        if delete_player(player_id):
            await update.message.reply_text(f"✅ Player **{p['name']}** (`{player_id}`) has been removed.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Failed to remove player (DB Error).")
            
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def player_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /playerlist - Shows paginated list by nation
    """
    if not await check_admin(update): return

    from database import get_all_players
    players = get_all_players()
    
    if not players:
        await update.message.reply_text("No players found.")
        return
        
    # Group by Nation (derived from ID prefix usually, e.g. IND_...)
    # Or assuming manual grouping? 
    # Let's derive from ID prefix if possible, or just list all sorted.
    # Requirement: "grouped by nation"
    # Current ID format: PL_NAME...
    # Prompt example ID: IND_ROHIT. 
    # Let's try to infer from ID first part.
    
    grouped = {}
    for p in players:
        pid = p['player_id']
        nation = pid.split('_')[0] if '_' in pid else "OTHERS"
        if nation not in grouped:
            grouped[nation] = []
        grouped[nation].append(p)
        
    # Flatten for pagination, but keep headers?
    # Better: List of lines.
    lines = []
    for nation in sorted(grouped.keys()):
        lines.append(f"🚩 **{nation}**")
        for p in grouped[nation]:
            # Check if provider exists (scraped/seeded) or old ID logic
            mapped = "✅" if p.get('api_reference', {}).get('provider') or p.get('api_reference', {}).get('international_id') else "❌"
            lines.append(f"• {p['name']} (`{p['player_id']}`) - {', '.join(p['roles'][:2])} - Map: {mapped}")
        lines.append("") # Spacer
        
    # Pagination
    PAGE_SIZE = 10
    total_pages = (len(lines) + PAGE_SIZE - 1) // PAGE_SIZE
    
    # Store lines in context (bad practice for stateless, but okay for simple bot)
    # Better: Pass page number in callback and regenerate lines (db overhead but stateless)
    # Since we can't pass huge list in callback, we re-query DB.
    
    await show_player_page(update, context, 0)

async def show_player_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    from database import get_all_players
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    players = get_all_players()
    grouped = {}
    for p in players:
        pid = p['player_id']
        nation = pid.split('_')[0] if '_' in pid else "OTHERS"
        if nation not in grouped:
            grouped[nation] = []
        grouped[nation].append(p)
    
    lines = []
    for nation in sorted(grouped.keys()):
        lines.append(f"🚩 **{nation}**")
        for p in grouped[nation]:
            mapped = "✅" if p.get('api_reference', {}).get('provider') or p.get('api_reference', {}).get('international_id') else "❌"
            lines.append(f"• {p['name']} (`{p['player_id']}`)")
            lines.append(f"   Roles: {', '.join(p['roles'])}")
            lines.append(f"   Map: {mapped}")
        lines.append("")
        
    PAGE_SIZE = 15 # Lines per page approximately 
    # Actually this logic cuts nations in half.
    # Let's just paginate raw players? "Displays all added players grouped by nation"
    # If we paginate the text lines, it's easiest.
    
    total_lines = len(lines)
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_content = lines[start_idx:end_idx]
    
    text = f"**📋 Player List (Page {page + 1})**\n\n" + "\n".join(page_content)
    
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"plist_{page-1}"))
    if end_idx < total_lines:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"plist_{page+1}"))
        
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def handle_playerlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    page = int(data.split('_')[1])
    await show_player_page(update, context, page)

async def get_player_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /stats (player name)
    """
    if not await check_admin(update): return

    text = update.message.text.replace('/stats', '').strip()
    if not text:
        await update.message.reply_text("**Usage:**\n`/stats (Player Name)`", parse_mode="Markdown")
        return
        
    from database import get_player_by_name
    p = get_player_by_name(text)
    
    if not p:
        await update.message.reply_text(f"❌ Player matching '{text}' not found.", parse_mode="Markdown")
        return
        
    stats = p.get('stats', {})
    
    # Check if new structure (dict with ipl/international keys containing sub-dicts)
    ipl_data = stats.get('ipl', {})
    intl_data = stats.get('international', {})
    
    # Handle old structure fallback (int) vs new structure (dict)
    # Helpers for display
    has_wk = "WK" in [r.upper() for r in p['roles']]
    has_defence = "DEFENCE" in [r.upper() for r in p['roles']]
    
    def format_stats(data):
        if isinstance(data, int): return str(data)
        parts = []
        
        # Core Stats (Renamed to match Roles)
        parts.append(f"🧠 Captain: {data.get('leadership')}")
        parts.append(f"🏏 Top: {data.get('batting_power')}")
        parts.append(f"🛡️ Middle: {data.get('batting_control')}")
        
        if has_defence:
             parts.append(f"🧱 Defence: {data.get('batting_defence', 50)}")
        
        # Wicket Keeping
        if has_wk:
             parts.append(f"🧤 WK: {data.get('wicket_keeping', 50)}")

        parts.append(f"💥 Finisher: {data.get('finishing')}")
        parts.append(f"⚡ Pacer: {data.get('bowling_pace', 20)}")
        parts.append(f"🌀 Spinner: {data.get('bowling_spin', 20)}")
        parts.append(f"✨ All Rounder: {data.get('all_round')}")
        parts.append(f"👟 Fielder: {data.get('fielding')}")
        
        return "\n".join(parts) if parts else "N/A"
        
    ipl_display = format_stats(ipl_data)
    intl_display = format_stats(intl_data)

    msg = (
        f"📊 **Stats for {p['name']}**\n"
        f"ID: `{p['player_id']}`\n\n"
        f"**International**\n{intl_display}\n\n"
        f"Roles: {', '.join(p['roles'])}\n"
        f"Source: {p.get('api_reference', {}).get('provider', 'Unknown')}"
    )

    # Sanitize handled by careful construction, skipping blind replacement to allow bolding
    # msg = msg.replace('*', '').replace('_', '').replace('`', '')
    

    # Sanitize handled by careful construction
    
    # IPL Toggle Button
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [[InlineKeyboardButton("🏏 View IPL Stats", callback_data=f"view_ipl_{p['player_id']}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if p.get('image_file_id'):
        try:
            await update.message.reply_photo(photo=p['image_file_id'], caption=msg, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to send photo for {p['name']}: {e}")
            await update.message.reply_text(msg + "\n\n⚠️ (Image failed to load)", reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg, reply_markup=reply_markup)
        

async def handle_view_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # view_intl_ID
    
    from database import get_player
    p = get_player(player_id)
    if not p: return

    # Intl Default
    stats = p.get('stats', {}).get('international', {})
    
    # Handle intl is default so stats might be messy if stored differently
    # But new run_fix/migrate standardizes 'international' key. 
    # Fallback to current stats if missing?
    # Actually fallback to root level if old schema, but migration handles it.
    
    if not stats: stats = p.get('stats', {}) # Fallback
    
    intl_img = p.get('image_file_id')
    roles = p.get('roles', [])
    
    def format_stats_local(data):
        if isinstance(data, int): return str(data)
        parts = []
        parts.append(f"🧠 Cap: {data.get('leadership')}")
        parts.append(f"🏏 Top: {data.get('batting_power')}")
        parts.append(f"🛡️ Mid: {data.get('batting_control')}")
        # Show all relevant for Intl
        parts.append(f"💥 Fin: {data.get('finishing')}")
        parts.append(f"⚡ Pace: {data.get('bowling_pace')}")
        parts.append(f"🌀 Spin: {data.get('bowling_spin')}")
        parts.append(f"✨ All: {data.get('all_round')}")
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)
        
    stats_display = format_stats_local(stats)
    
    caption = f"📊 **Stats for {p['name']}**\n(International)\n\n{stats_display}\n\nRoles: {', '.join(roles)}"
    
    keyboard = [[InlineKeyboardButton("🏏 View IPL Stats", callback_data=f"view_ipl_{player_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    from telegram import InputMediaPhoto
    try:
        if intl_img:
             await query.message.edit_media(
                media=InputMediaPhoto(media=intl_img, caption=caption, parse_mode="Markdown"),
                reply_markup=reply_markup
            )
        else:
             await query.edit_message_caption(caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        pass


async def handle_view_ipl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # view_ipl_ID
    
    from database import get_player
    p = get_player(player_id)
    if not p:
        await query.answer("Player not found", show_alert=True)
        return
        
    # Check IPL Stats
    stats = p.get('stats', {}).get('ipl', {})
    if not stats:
        await query.answer("No IPL Stats available", show_alert=True)
        return

    # Check IPL Image
    ipl_img = p.get('ipl_image_file_id', p.get('image_file_id'))
    
    # Check IPL Roles
    ipl_roles = p.get('ipl_roles', p.get('roles', []))
    
    # Format Stats
    def format_stats_local(data):
        if isinstance(data, int): return str(data)
        parts = []
        # ... Reuse formatting logic or duplicate ...
        # (Since previous helper was inside function, I'll allow simple duplication or improved shared helper later)
        # Just quick format for IPL view:
        parts.append(f"🧠 Cap: {data.get('leadership')}")
        parts.append(f"🏏 Top: {data.get('batting_power')}")
        parts.append(f"🛡️ Mid: {data.get('batting_control')}")
        if "DEFENCE" in [r.upper() for r in ipl_roles]: parts.append(f"🧱 Def: {data.get('batting_defence')}")
        if "WK" in [r.upper() for r in ipl_roles]: parts.append(f"🧤 WK: {data.get('wicket_keeping')}")
        parts.append(f"💥 Fin: {data.get('finishing')}")
        parts.append(f"⚡ Pace: {data.get('bowling_pace')}")
        parts.append(f"🌀 Spin: {data.get('bowling_spin')}")
        parts.append(f"✨ All: {data.get('all_round')}")
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)

    stats_display = format_stats_local(stats)
    
    caption = (
        f"🇮🇳 **IPL Stats for {p['name']}**\n\n"
        f"{stats_display}\n\n"
        f"Roles: {', '.join(ipl_roles)}"
    )
    
    # Back button
    keyboard = [[InlineKeyboardButton("🔙 Back to International", callback_data=f"view_intl_{player_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    from telegram import InputMediaPhoto
    try:
        # Edit Message Media is tricky if type changes, but here photo->photo.
        if ipl_img:
            await query.message.edit_media(
                media=InputMediaPhoto(media=ipl_img, caption=caption, parse_mode="Markdown"),
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_caption(caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"IPL View Edit Error: {e}")
        await query.answer("Error switching view", show_alert=True)

async def handle_view_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # view_intl_ID
    
    from database import get_player
    p = get_player(player_id)
    if not p: return

    # Intl Default
    stats = p.get('stats', {}).get('international', {})
    if not stats: stats = p.get('stats', {}) # Fallback for legacy schema
    
    intl_img = p.get('image_file_id')
    roles = p.get('roles', [])
        
    def format_stats_local(data):
        if isinstance(data, int): return str(data)
        parts = []
        parts.append(f"🧠 Cap: {data.get('leadership')}")
        parts.append(f"🏏 Top: {data.get('batting_power')}")
        parts.append(f"🛡️ Mid: {data.get('batting_control')}")
        # Show all relevant for Intl
        parts.append(f"💥 Fin: {data.get('finishing')}")
        parts.append(f"⚡ Pace: {data.get('bowling_pace')}")
        parts.append(f"🌀 Spin: {data.get('bowling_spin')}")
        parts.append(f"✨ All: {data.get('all_round')}")
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)
        
    stats_display = format_stats_local(stats)
    
    caption = f"📊 **Stats for {p['name']}**\n(International)\n\n{stats_display}\n\nRoles: {', '.join(roles)}"
    
    keyboard = [[InlineKeyboardButton("🏏 View IPL Stats", callback_data=f"view_ipl_{player_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    from telegram import InputMediaPhoto
    try:
        if intl_img:
             await query.message.edit_media(
                media=InputMediaPhoto(media=intl_img, caption=caption, parse_mode="Markdown"),
                reply_markup=reply_markup
            )
        else:
             await query.edit_message_caption(caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        pass





async def reset_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    from database import clear_all_matches
    clear_all_matches()
    await update.message.reply_text("✅ All active matches have been cleared from the database.")

async def modify_stat_generic(update: Update, context: ContextTypes.DEFAULT_TYPE, stat_key: str, command_name: str):
    """
    Generic helper to modify a stat.
    Syntax: /command PlayerName +10
    """
    if not await check_admin(update): return

    text = update.message.text.replace(f'/{command_name}', '').strip()
    
    # Simple regex to split Name and Value
    # Assumes value is at the end like "+10" or "-5" or "50"
    import re
    match = re.search(r'^(.*)\s+([+-]?\d+)$', text)
    
    if not match:
        await update.message.reply_text(
            f"⚠️ **Invalid Syntax**\n\n"
            f"Usage: `/{command_name} Player Name +10`\n"
            f"Example: `/{command_name} Dhoni +5` or `/{command_name} Kohli -2`",
            parse_mode="Markdown"
        )
        return
    
    player_name = match.group(1).strip()
    delta = int(match.group(2))
    
    from database import get_player_by_name, save_player
    p = get_player_by_name(player_name)
    
    if not p:
        await update.message.reply_text(f"❌ Player '{player_name}' not found.")
        return
        
    # Update Stats
    stats = p.get('stats', {})
    modes = ['ipl', 'international']
    
    changes = []
    
    for mode in modes:
        if mode not in stats: stats[mode] = {}
        
        # Get current value (default 50)
        current = stats[mode].get(stat_key, 50)
        
        # Handle int/string issues just in case
        if isinstance(current, str) and current.isdigit(): current = int(current)
        
        new_val = max(1, min(100, current + delta)) # Clamp 1-100
        stats[mode][stat_key] = new_val
        
        changes.append(f"{mode.upper()}: {current} -> {new_val}")
        
    p['stats'] = stats
    save_player(p)
    
    await update.message.reply_text(
        f"✅ Updated {p['name']} ({stat_key})\n"
        f"{' | '.join(changes)}"
    )

async def change_cap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "leadership", "changecap")

async def change_wk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "wicket_keeping", "changewk")

async def change_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "batting_power", "changetop")

async def change_middle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "batting_control", "changemiddle")

async def change_defence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "batting_defence", "changedefence")

async def change_pacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "bowling_pace", "changepacer")
    
async def change_spinner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "bowling_spin", "changespinner")
    
async def change_allrounder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "all_round", "changeallrounder")
    
async def change_finisher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "finishing", "changefinisher")

async def change_fielder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await modify_stat_generic(update, context, "fielding", "changefielder")

async def add_mod_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mod 123456789
    Restricted to OWNER only.
    """
    if not await check_owner(update): return
    
    try:
        target_id = int(context.args[0])
        add_mod(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` is now a Moderator.", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/mod <user_id>`", parse_mode="Markdown")

async def remove_mod_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unmod 123456789
    Restricted to OWNER only.
    """
    if not await check_owner(update): return
    
    try:
        target_id = int(context.args[0])
        remove_mod(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` removed from Moderators.", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/unmod <user_id>`", parse_mode="Markdown")

async def set_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setstats PlayerName format=ipl cap=90 top=80 mid=85 ...
    """
    if not await check_admin(update): return

    text = update.message.text.replace('/setstats', '').strip()
    if not text:
        await update.message.reply_text(
            "❌ **Invalid Syntax**\n"
            "Usage: `/setstats [Name] [format=all|ipl|intl] cap=90 top=85 ...`\n"
            "Keys: cap, wk, top, mid, def, all (or allrounder), pacer, spinner, fin, field",
            parse_mode="Markdown"
        )
        return

    # Parse arguments
    # Strategy: Split by space, identify key=value pairs.
    # Everything that is NOT a key=value pair is part of the Name.
    # Note: Name might contain spaces.
    
    parts = text.split()
    name_parts = []
    kwargs = {}
    
    for part in parts:
        if '=' in part:
            k, v = part.split('=', 1)
            kwargs[k.lower()] = v
        else:
            name_parts.append(part)
            
    player_name = " ".join(name_parts)
    
    if not player_name:
         await update.message.reply_text(
             "❌ **Player name is missing.**\n"
             "Usage: `/setstats [Name] [format=all|ipl|intl] cap=90 ...`",
             parse_mode="Markdown"
         )
         return

    from database import get_player_by_name, save_player
    p = get_player_by_name(player_name)
    
    if not p:
        await update.message.reply_text(f"❌ Player '{player_name}' not found.")
        return
        
    # Mapping
    key_map = {
        'cap': 'leadership',
        'wk': 'wicket_keeping',
        'top': 'batting_power',
        'mid': 'batting_control',
        'def': 'batting_defence',
        'pacer': 'bowling_pace',
        'spinner': 'bowling_spin',
        'all': 'all_round',
        'allrounder': 'all_round',
        'fin': 'finishing',
        'field': 'fielding'
    }
    
    # Check for invalid keys? Or just ignore/warn?
    # Let's process valid keys.
    
    target_format = kwargs.get('format', 'all').lower()
    if target_format not in ['ipl', 'intl', 'all']:
        await update.message.reply_text(
            "❌ **Invalid format.**\n"
            "Usage: `/setstats [Name] [format=all|ipl|intl] cap=90 ...`",
            parse_mode="Markdown"
        )
        return
        
    modes = ['ipl', 'international'] if target_format == 'all' else [ 'ipl' if target_format == 'ipl' else 'international' ]
    
    stats = p.get('stats', {})
    changes = []
    
    has_updates = False
    
    for k, v in kwargs.items():
        if k == 'format': continue
        if k not in key_map:
            # warn or ignore? Let's ignore extra keys but maybe warn if it looks like a stat
            continue
            
        stat_key = key_map[k]
        try:
            val = int(v)
            val = max(1, min(100, val)) # Clamp
            
            for mode in modes:
                if mode not in stats: stats[mode] = {}
                old_val = stats[mode].get(stat_key, 50)
                stats[mode][stat_key] = val
                changes.append(f"{mode.upper()} {k}: {old_val} -> {val}")
                has_updates = True
                
        except ValueError:
            await update.message.reply_text(
                f"❌ **Invalid value for {k}: {v}**\n"
                "Must be an integer (1-100).\n"
                "Usage: `/setstats [Name] cap=90 ...`",
                parse_mode="Markdown"
            )
            return

    if not has_updates:
        await update.message.reply_text(
            "⚠️ **No valid stats to update found.**\n"
            "Usage: `/setstats [Name] [format=all] cap=90 hit=80 ...`\n"
            "Keys: cap, wk, hit, pace, spin, all, fin, field",
            parse_mode="Markdown"
        )
        return

    p['stats'] = stats
    save_player(p)
    
    # Summary
    summary = "\n".join(changes)
    if len(summary) > 4000: summary = summary[:4000] + "..."
    
    await update.message.reply_text(
        f"✅ **Updated Stats for {p['name']}**\n"
        f"Format: {target_format}\n\n"
        f"{summary}",
        parse_mode="Markdown"
    )

async def check_role_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /check [role] [mode]
    Lists all players with the role and their stats in that mode.
    """
    if not await check_admin(update): return
    
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: `/check [role] [mode]`\nExample: `/check hitting intl`", parse_mode="Markdown")
        return
        
    target_role_query = args[0] # e.g. "hitting"
    target_mode = args[1].lower() # e.g. "intl" or "ipl"
    
    if target_mode not in ['ipl', 'intl', 'international']:
         await update.message.reply_text("Mode must be `ipl` or `intl`.", parse_mode="Markdown")
         return
         
    if target_mode == 'intl': target_mode = 'international'
    
    # Map role query to actual role name
    # We need to map "hitting" -> "Top", "pace" -> "Pacer" etc.
    # And map role to STAT KEY from config.ROLE_STATS_MAP
    from config import ROLE_STATS_MAP, POSITIONS_T20, POSITIONS_TEST
    
    # First, identify the canonical Role Name from input
    # Helper to fuzzy match input to keys in ROLE_STATS_MAP or POSITIONS
    # ROLE_STATS_MAP keys are "Captain", "WK", "Top" etc.
    
    canonical_role = None
    stat_key = None
    
    # Pre-defined aliases for user convenience
    input_alias = target_role_query.lower()
    alias_map = {
        "hitting": "Top",
        "batting": "Top",
        "pace": "Pacer",
        "spin": "Spinner",
        "all": "All Rounder",
        "all-rounder": "All Rounder",
        "field": "Fielder",
        "fielding": "Fielder",
        "def": "Defence",
        "defence": "Defence",
        "middle": "Middle",
        "top": "Top",
        "wk": "WK",
        "cap": "Captain"
    }

    if input_alias in alias_map:
        canonical_role = alias_map[input_alias]
    else:
        # Try direct match in keys
        for r in ROLE_STATS_MAP.keys():
            if r.lower() == input_alias:
                canonical_role = r
                break
    
    if canonical_role and canonical_role in ROLE_STATS_MAP:
        stat_key = ROLE_STATS_MAP[canonical_role]
            
    if not stat_key:
         await update.message.reply_text(f"❌ Unknown role: `{target_role_query}`.\nAvailable: {', '.join(ROLE_STATS_MAP.keys())}", parse_mode="Markdown")
         return
         
    from database import get_all_players
    players = get_all_players()
    
    results = []
    
    for p in players:
        # Check if player has this role in their list
        # p['roles'] is a list of strings
        if canonical_role in p['roles']:
             # Get Stat
             stats = p.get('stats', {}).get(target_mode, {})
             val = stats.get(stat_key, 0) # Default 0 if missing
             results.append((p['name'], val))
             
    # Sort Descending
    results.sort(key=lambda x: x[1], reverse=True)
    
    if not results:
        await update.message.reply_text(f"No players found with role **{canonical_role}**.", parse_mode="Markdown")
        return
        
    lines = [f"📊 **{canonical_role} ({target_mode.upper()})**"]
    for idx, (name, score) in enumerate(results, 1):
        lines.append(f"{idx}. {name}: **{score}**")
        
    msg = "\n".join(lines)
    if len(msg) > 4000: msg = msg[:4000] + "..."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def fix_roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /fix_roles - Alias for /migrate_roles
    """
    if not await check_admin(update): return
    await migrate_roles_command(update, context)
    
    # Merge all valid positions for lookup
    # POSITIONS_T20 has "WK", "Captain", "All Rounder" etc.
    valid_map = {r.lower(): r for r in POSITIONS_T20 + POSITIONS_TEST}
    
    for p in players:
        changed = False
        new_roles = []
        for r in p['roles']:
            # Normalizer Logic
            r_clean = r.strip()
            # If already valid, keep
            if r_clean in POSITIONS_T20 or r_clean in POSITIONS_TEST:
                if r_clean not in new_roles: new_roles.append(r_clean)
                continue
                
            # Maps
            norm = r_clean.lower()
            mapped = None
            
            if norm == 'hitting': mapped = 'Top'
            elif norm == 'pacex': mapped = 'Pacer' # Typo safety?
            elif norm == 'pace': mapped = 'Pacer'
            elif norm == 'spin': mapped = 'Spinner'
            elif norm == 'all-rounder': mapped = 'All Rounder'
            elif norm == 'all': mapped = 'All Rounder'
            elif norm == 'fielding': mapped = 'Fielder'
            elif norm in valid_map: mapped = valid_map[norm]
            
            if mapped:
                changed = True
                if mapped not in new_roles: new_roles.append(mapped)
            else:
                # Keep unknown? Or drop? Let's keep to be safe, but warn user manually checking could help
                new_roles.append(r_clean)
                
        if changed:
            p['roles'] = new_roles
            save_player(p)
            updated_count += 1
            
    await update.message.reply_text(f"✅ Migration Complete. Updated {updated_count} players.")

async def migrate_roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /migrate_roles - Updates ALL players:
    1. Maps Old -> New Roles (Hitting -> Top)
    2. Normalizes Aliases
    3. Enforces Exclusivity (Removes Top if All Rounder/Finisher present)
    """
    if not await check_admin(update): return
    
    from database import get_all_players, save_player
    from config import POSITIONS_T20, POSITIONS_TEST
    
    players = get_all_players()
    total_updated = 0
    cleaned_top_count = 0
    
    # Valid Canonical Roles
    valid_roles = set(POSITIONS_T20 + POSITIONS_TEST)
    valid_map = {r.lower(): r for r in valid_roles}
    
    # Aliases & Legacy Mapping
    aliases = {
        "hitting": "Top",
        "batting": "Top",
        "pace": "Pacer",
        "bowling": "Pacer",
        "spin": "Spinner",
        "all-rounder": "All Rounder",
        "all-round": "All Rounder",
        "all": "All Rounder",
        "fielding": "Fielder",
        "field": "Fielder",
        "defence": "Defence",
        "def": "Defence",
        "middle": "Middle",
        "wk": "WK",
        "keeper": "WK",
        "wicketkeeper": "WK",
        "captain": "Captain",
        "cap": "Captain",
        "finisher": "Finisher"
    }

    for p in players:
        current_roles = p.get('roles', [])
        new_roles = []
        is_modified = False
        
        # Step 1: Normalize & Map
        for r in current_roles:
            r_clean = r.strip()
            r_lower = r_clean.lower()
            
            canonical = None
            
            if r_lower in valid_map:
                canonical = valid_map[r_lower]
            elif r_lower in aliases:
                canonical = aliases[r_lower]
            else:
                canonical = r_clean # Keep unknown?
            
            new_roles.append(canonical)
            
        # Step 2: Dedupe
        unique_roles = []
        for r in new_roles:
            if r not in unique_roles: unique_roles.append(r)
        
        # Check against original to see if modified so far
        if unique_roles != current_roles:
            is_modified = True
        
        # Step 3: Enforce Exclusivity (Remove 'Top' if 'All Rounder' or 'Finisher' exists)
        if "Top" in unique_roles:
            if "All Rounder" in unique_roles or "Finisher" in unique_roles:
                unique_roles.remove("Top")
                is_modified = True
                cleaned_top_count += 1
                
        if is_modified:
            p['roles'] = unique_roles
            save_player(p)
            total_updated += 1
            
    await update.message.reply_text(
        f"✅ **Migration & Cleanup Complete**\n"
        f"👥 Total Players Scanned: {len(players)}\n"
        f"📝 Updated Records: {total_updated}\n"
        f"🧹 Removed 'Top' from {cleaned_top_count} All-Rounders/Finishers."
    , parse_mode="Markdown")

async def set_roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setroles Name roles=Captain,WK
    /setroles player_id=ID roles=Captain,WK
    """
    if not await check_admin(update): return

    # Remove command prefix (support both variants)
    text = update.message.text
    if text.startswith('/set_roles'):
        text = text.replace('/set_roles', '', 1).strip()
    else:
        text = text.replace('/setroles', '', 1).strip()
    
    if not text:
        await update.message.reply_text(
            "❌ **Usage:** `/setroles [Name/ID] roles=Role1,Role2`\n"
            "Example: `/setroles Virat Kohli roles=Captain,Hitting`",
            parse_mode="Markdown"
        )
        return

    # Strategy: Look for 'roles=' keyword. Everything before it is the identifier (Name or ID).
    import re
    # Match: (Identifier) roles=(Roles)
    # This allows spaces in identifier.
    match = re.search(r'^(.*?)\s+roles\s*=\s*(.*)$', text, re.IGNORECASE | re.DOTALL)
    
    identifier = None
    roles_str = None
    
    if match:
        identifier = match.group(1).strip()
        roles_str = match.group(2).strip()
    else:
        # Fallback: maybe they used explicit player_id= syntax without spaces?
        # Or maybe reversed order? Let's Stick to "Identifier roles=..." as primary.
        # Check if they used the old strict syntax "player_id=X roles=Y" which might not match if they have extra params
        
        # Try finding 'roles=' anywhere
        roles_match = re.search(r'roles\s*=\s*([^=\n]+)', text, re.IGNORECASE)
        if roles_match:
            roles_str = roles_match.group(1).strip()
            # Remove the roles part from text to find identifier
            # This is tricky if order varies.
            # Let's rely on the simple split first. If that failed, it means pattern didn't match.
            # Maybe they didn't put a space before roles? "Name roles=..."
            pass
            
    if not identifier or not roles_str:
         # Try parsing explicit player_id= if present
         pid_match = re.search(r'player_id\s*=\s*([^\s]+)', text, re.IGNORECASE)
         rm_match = re.search(r'roles\s*=\s*(.+)', text, re.IGNORECASE)
         
         if pid_match and rm_match:
             identifier = pid_match.group(1).strip()
             roles_str = rm_match.group(1).strip()
         else:
             await update.message.reply_text(
                 "❌ **Parsing Error**\n"
                 "Usage: `/setroles [Name] roles=A,B`",
                 parse_mode="Markdown"
             )
             return

    # Clean identifier (remove player_id= if user typed it manually in the first part)
    if identifier.lower().startswith('player_id='):
        identifier = identifier.split('=', 1)[1].strip()

    from database import get_player, get_player_by_name, save_player
    
    # Try ID first
    p = get_player(identifier)
    if not p:
        # Try Name
        p = get_player_by_name(identifier)
    
    if not p:
        await update.message.reply_text(f"❌ Player not found: `{identifier}`", parse_mode="Markdown")
        return
        
    # Validate Roles
    from config import POSITIONS_T20
    valid_map = {r.lower(): r for r in POSITIONS_T20}
    
    raw_roles = [r.strip() for r in roles_str.split(',') if r.strip()]
    final_roles = []
    invalid_roles = []
    
    for r in raw_roles:
        r_lower = r.lower()
        if r_lower in valid_map:
            final_roles.append(valid_map[r_lower])
        elif r_lower in ["all-round", "all-rounder", "all", "all round", "all rounder"]: 
             final_roles.append("All Rounder")
        elif r_lower in ["wk", "keeper", "wicketkeeper"]:
             final_roles.append("WK")
        elif r_lower in ["batting", "batsman", "hitting"]: # Support legacy 'Hitting' as input mapping
             final_roles.append("Top")
        elif r_lower in ["bowling", "bowler", "pace", "pacer"]:
             final_roles.append("Pacer") 
        elif r_lower in ["spin", "spinner"]:
             final_roles.append("Spinner")
        elif r_lower in ["fielding", "fielder", "field"]:
             final_roles.append("Fielder")
        elif r_lower in ["defence", "def"]:
             final_roles.append("Defence")
        elif r_lower in ["middle", "mid"]:
             final_roles.append("Middle")
        elif r_lower in ["captain", "cap"]:
             final_roles.append("Captain")
        elif r_lower in ["finisher", "fin"]:
             final_roles.append("Finisher")
        else:
            invalid_roles.append(r)
            
    if invalid_roles:
        await update.message.reply_text(
            f"❌ **Invalid Roles:** {', '.join(invalid_roles)}\n"
            f"Allowed: {', '.join(sorted(POSITIONS_T20))}",
            parse_mode="Markdown"
        )
        return
        
    p['roles'] = final_roles
    save_player(p)
    
    await update.message.reply_text(
        f"✅ **Updated Roles for {p['name']}**\n"
        f"New Roles: {', '.join(final_roles)}",
        parse_mode="Markdown"
    )

async def add_role_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_role [Player] [Role] - Appends a role
    """
    if not await check_admin(update): return
    
    text = update.message.text.replace('/add_role', '').strip()
    if not text:
        await update.message.reply_text("❌ **Usage:** `/add_role [Name] [Role]`\nExample: `/add_role Matt Henry Defence`", parse_mode="Markdown")
        return
        
    # Strategy: Last word is likely the role
    parts = text.rsplit(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("❌ **Usage:** `/add_role [Name] [Role]`", parse_mode="Markdown")
        return
        
    identifier = parts[0].strip()
    role_input = parts[1].strip()
    
    # Normalize Role
    from config import POSITIONS_T20, POSITIONS_TEST
    valid_map = {r.lower(): r for r in POSITIONS_T20 + POSITIONS_TEST}
    
    aliases = {
        "hitting": "Top", "batting": "Top",
        "pace": "Pacer", "bowling": "Pacer",
        "spin": "Spinner",
        "all-rounder": "All Rounder", "all": "All Rounder",
        "fielding": "Fielder", "field": "Fielder",
        "defence": "Defence", "def": "Defence",
        "middle": "Middle", "mid": "Middle",
        "wk": "WK", "keeper": "WK",
        "captain": "Captain", "cap": "Captain",
        "finisher": "Finisher", "fin": "Finisher"
    }

    target_role = None
    ri_lower = role_input.lower()
    
    if ri_lower in valid_map:
        target_role = valid_map[ri_lower]
    elif ri_lower in aliases:
        target_role = aliases[ri_lower]
    else:
        await update.message.reply_text(f"❌ Unknown role: `{role_input}`\nAllowed: {', '.join(sorted(POSITIONS_T20))}", parse_mode="Markdown")
        return
        
    # Find Player
    from database import get_player, get_player_by_name, save_player
    p = get_player(identifier) or get_player_by_name(identifier)
    
    if not p:
        await update.message.reply_text(f"❌ Player not found: `{identifier}`", parse_mode="Markdown")
        return
        
    current_roles = p.get('roles', [])
    if target_role in current_roles:
         await update.message.reply_text(f"⚠️ {p['name']} already has role **{target_role}**.", parse_mode="Markdown")
         return
         
    current_roles.append(target_role)
    p['roles'] = current_roles
    save_player(p)
    
    await update.message.reply_text(f"✅ Added **{target_role}** to **{p['name']}**.\nCurrent Roles: {', '.join(current_roles)}", parse_mode="Markdown")

async def rem_role_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rem_role [Player] [Role] - Removes a role
    """
    if not await check_admin(update): return
    
    text = update.message.text.replace('/rem_role', '').strip()
    if not text:
        await update.message.reply_text("❌ **Usage:** `/rem_role [Name] [Role]`\nExample: `/rem_role Matt Henry Defence`", parse_mode="Markdown")
        return
        
    # Strategy: Last word is likely the role
    parts = text.rsplit(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("❌ **Usage:** `/rem_role [Name] [Role]`", parse_mode="Markdown")
        return
        
    identifier = parts[0].strip()
    role_input = parts[1].strip()
    
    # Normalize Role
    from config import POSITIONS_T20, POSITIONS_TEST
    valid_map = {r.lower(): r for r in POSITIONS_T20 + POSITIONS_TEST}
    
    aliases = {
        "hitting": "Top", "batting": "Top",
        "pace": "Pacer", "bowling": "Pacer",
        "spin": "Spinner",
        "all-rounder": "All Rounder", "all": "All Rounder",
        "fielding": "Fielder", "field": "Fielder",
        "defence": "Defence", "def": "Defence",
        "middle": "Middle", "mid": "Middle",
        "wk": "WK", "keeper": "WK",
        "captain": "Captain", "cap": "Captain",
        "finisher": "Finisher", "fin": "Finisher"
    }

    target_role = None
    ri_lower = role_input.lower()
    
    if ri_lower in valid_map:
        target_role = valid_map[ri_lower]
    elif ri_lower in aliases:
        target_role = aliases[ri_lower]
    else:
        # If removing, maybe they typed strict name
        target_role = role_input 
        
    # Find Player
    from database import get_player, get_player_by_name, save_player
    p = get_player(identifier) or get_player_by_name(identifier)
    
    if not p:
        await update.message.reply_text(f"❌ Player not found: `{identifier}`", parse_mode="Markdown")
        return
        
    current_roles = p.get('roles', [])
    if target_role not in current_roles:
         await update.message.reply_text(f"⚠️ {p['name']} does not have role **{target_role}**.", parse_mode="Markdown")
         return
         
    current_roles.remove(target_role)
    p['roles'] = current_roles
    save_player(p)
    
    await update.message.reply_text(f"✅ Removed **{target_role}** from **{p['name']}**.\nCurrent Roles: {', '.join(current_roles)}", parse_mode="Markdown")

async def non_role_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nonrolefix
    Sets stats for unassigned roles to random(40, 60) for all players.
    """
    if not await check_admin(update): return

    await update.message.reply_text("🔄 **Starting Non-Role Stat Fix...**\nChecking all players...")
    
    from database import get_all_players, save_player
    from config import ROLE_STATS_MAP
    import random

    players = get_all_players()
    count = 0
    updated_players = 0
    
    for p in players:
        current_roles = [r.lower() for r in p.get('roles', [])]
        stats = p.get('stats', {})
        modes = ['ipl', 'international']
        
        has_change = False
        
        for role_name, stat_key in ROLE_STATS_MAP.items():
            # If player matches role (case-insensitive), skip
            if role_name.lower() in current_roles:
                continue
                
            # Player does NOT have this role -> Randomize generic stat
            new_val = random.randint(40, 60)
            
            for mode in modes:
                if mode not in stats: stats[mode] = {}
                
                # Check if we need to update
                # (Optional: Only update if not already in range? 
                # Prompt says: "make the not assigned roles of players stats will be random numbers b/w 40-60"
                # Implies we force it always.)
                
                stats[mode][stat_key] = new_val
                has_change = True
                
        if has_change:
            p['stats'] = stats
            save_player(p)
            updated_players += 1
            
        count += 1
        
    await update.message.reply_text(
        f"✅ **Non-Role Fix Value Completed!**\n\n"
        f"Processed: {count} players\n"
        f"Updated: {updated_players} players\n"
        f"Unassigned stats set to 40-60 range."
    )

async def list_mods_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mods
    Lists all moderator IDs.
    Accesible by Mods/Owner.
    """
    if not await check_admin(update): return
    
    from database import get_all_mods
    mod_ids = get_all_mods()
    
    if not mod_ids:
        await update.message.reply_text("ℹ️ No moderators found.")
        return
        
    # Format list
    msg = "👮 **Moderator List**\n"
    for mid in mod_ids:
         msg += f"• `{mid}`\n"
         
    await update.message.reply_text(msg, parse_mode="Markdown")



async def run_fix_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /run_fix_now - Runs the logic from run_fix_now.py
    """
    if not await check_admin(update): return
    
    await update.message.reply_text("🔄 **Running Stat Fix (with Backup)...**\nThis may take a moment.")
    
    # Run logic in executor to avoid blocking main loop if heavy (though 50 players is fast)
    # But since it's blocking DB calls, direct call is okay-ish or better use thread.
    # For simplicity, call directly, but catch output?
    # run_fix_directly prints to stdout. We modified it?
    # No, I didn't modify it to return a string.
    # To properly reply to telegram, I should capture the output or modify run_fix_directly to return msg.
    # Let's modify run_fix_now (via import) to return msg?
    # Or just capture stdout?
    # Simpler: Import the logic and run it, ignoringprints, but recreate the summary logic here?
    # No, cleaner to change run_fix_directly to return summary.
    # But I already wrote the file.
    # I'll just assume it works and print a generic success message, OR
    # I can capture stdout.
    
    from io import StringIO
    import sys
    from run_fix_now import run_fix_directly
    
    old_stdout = sys.stdout
    sys.stdout = mystdout = StringIO()
    
    try:
        run_fix_directly()
    except Exception as e:
        sys.stdout = old_stdout
        await update.message.reply_text(f"❌ Error running fix: {e}")
        return
        
    sys.stdout = old_stdout
    output = mystdout.getvalue()
    
    # Filter output? Just send last few lines or full log if small.
    # Output might be large if many players update.
    # Only show summary lines?
    summary_lines = [line for line in output.split('\n') if "Processed" in line or "Updated" in line or "DONE" in line or "Backup" in line]
    summary_text = "\n".join(summary_lines)
    
    if len(summary_text) == 0: summary_text = "Completed (No summary captured)."
    
    await update.message.reply_text(f"✅ **Execution Complete**\n\n{summary_text}")

async def revert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /revert - Reverts changes from players_backup.json
    """
    if not await check_admin(update): return
    
    await update.message.reply_text("🔄 **Reverting Changes...**")
    
    from run_fix_now import revert_stats
    success, msg = revert_stats()
    
    if success:
        await update.message.reply_text(f"✅ {msg}")
    else:
        await update.message.reply_text(f"❌ {msg}")


async def add_player_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_playeripl name=X roles=A,B image=URL
    Adds/Updates player in IPL pool specifically.
    """
    if not await check_admin(update): return
    
    text = update.message.text.replace('/add_playeripl', '').strip()
    
    # 1. Parse
    import re
    if not re.match(r'^(name|roles|image)\s*=', text, re.IGNORECASE):
         text = "name=" + text
    text += ' '
    pattern = r'(name|roles|image)\s*=\s*(.*?)(?=\s+(?:name|roles|image)\s*=|$) '[:-1]
    matches = re.finditer(pattern, text, re.IGNORECASE | re.DOTALL)
    parsed = {m.group(1).lower(): m.group(2).strip() for m in matches}
    
    if 'name' not in parsed or 'roles' not in parsed or 'image' not in parsed:
        await update.message.reply_text("Missing fields. Usage: /add_playeripl name=X roles=A,B image=URL")
        return
        
    name = parsed['name'].strip()
    roles_str = parsed['roles']
    image_url = parsed['image'].strip()
    
    # Normalize Roles
    from config import POSITIONS_T20
    valid_map = {r.lower(): r for r in POSITIONS_T20} # IPL is T20
    aliases = {"hitting": "Top", "keeper": "WK", "cap": "Captain", "pace": "Pacer", "spin": "Spinner", "all": "All Rounder"}
    
    final_roles = []
    for r in roles_str.split(','):
        r = r.strip()
        rl = r.lower()
        if rl in valid_map: final_roles.append(valid_map[rl])
        elif rl in aliases: final_roles.append(aliases[rl])
        else: final_roles.append(r) # Keep fallback
        
    # Get Player
    from database import get_player_by_name, save_player, get_player
    clean_name = name.upper().replace(' ', '_')
    player_id = f"PL_{clean_name[:10]}"
    
    p = get_player(player_id) or get_player_by_name(name)
    
    if not p:
        p = {
            "player_id": player_id,
            "name": name,
            "roles": [],
            "image_file_id": None,
            "ipl_roles": final_roles,
            "ipl_image_file_id": None,
            "stats": {"ipl": {}} 
        }
    else:
        p['ipl_roles'] = final_roles
        
    msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image_url, caption=f"Added IPL Data for {name}")
    file_id = msg.photo[-1].file_id
    p['ipl_image_file_id'] = file_id
    
    save_player(p)
    
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [[InlineKeyboardButton("🎲 Generate Stats (IPL)", callback_data=f"gen_ipl_{player_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(f"✅ IPL Data Updated for **{name}**\nRoles: {final_roles}", reply_markup=reply_markup)

async def add_role_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    # Basic implementation: simple role append
    text = update.message.text.replace('/add_roleipl', '').strip()
    # Format: Player Role
    if not text:
        await update.message.reply_text("Usage: /add_roleipl [Name] [Role]")
        return
        
    parts = text.rsplit(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /add_roleipl [Name] [Role]")
        return
        
    name = parts[0].strip()
    role_input = parts[1].strip()
    
    # Normalize
    from config import POSITIONS_T20
    valid_map = {r.lower(): r for r in POSITIONS_T20}
    aliases = {"hitting": "Top", "keeper": "WK", "cap": "Captain", "pace": "Pacer", "spin": "Spinner", "all": "All Rounder"}
    
    ri_lower = role_input.lower()
    target_role = valid_map.get(ri_lower, aliases.get(ri_lower, role_input))
    
    from database import get_player_by_name, save_player
    p = get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
        
    current = p.get('ipl_roles', [])
    if target_role not in current:
        current.append(target_role)
        p['ipl_roles'] = current
        save_player(p)
        await update.message.reply_text(f"✅ Added {target_role} to {p['name']} (IPL).\nIPL Roles: {', '.join(current)}")
    else:
        await update.message.reply_text(f"⚠️ {p['name']} already has {target_role} in IPL.")

async def rem_role_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    text = update.message.text.replace('/rem_roleipl', '').strip()
    
    parts = text.rsplit(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /rem_roleipl [Name] [Role]")
        return
    name = parts[0].strip()
    role_input = parts[1].strip()
    
    from database import get_player_by_name, save_player
    p = get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    
    # Normalize input slightly or check rough match?
    # Strict match first or loop
    current = p.get('ipl_roles', [])
    
    # Try case-insensitive remove
    found = None
    for r in current:
        if r.lower() == role_input.lower():
            found = r
            break
            
    if found:
        current.remove(found)
        p['ipl_roles'] = current
        save_player(p)
        await update.message.reply_text(f"✅ Removed {found} from {p['name']} (IPL).\nIPL Roles: {', '.join(current)}")
    else:
         await update.message.reply_text(f"⚠️ Role {role_input} not found in {p['name']}'s IPL roles.")

async def update_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /update_image name [format=ipl] url
    """
    if not await check_admin(update): return
    
    text = update.message.text.replace('/update_image', '').strip()
    
    import re
    fmt_match = re.search(r'format=(ipl|intl)', text, re.IGNORECASE)
    target_format = fmt_match.group(1).lower() if fmt_match else 'intl'
    clean_text = re.sub(r'format=(ipl|intl)', '', text, flags=re.IGNORECASE).strip()
    
    parts = clean_text.rsplit(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /update_image [Name] format=[ipl|intl] [URL]")
        return
        
    name = parts[0].strip()
    url = parts[1].strip()
    
    from database import get_player_by_name, save_player
    p = get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
        
    try:
        msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=f"Updated {target_format.upper()} Image for {p['name']}")
        fid = msg.photo[-1].file_id
        
        if target_format == 'ipl':
            p['ipl_image_file_id'] = fid
        else:
            p['image_file_id'] = fid
            
        save_player(p)
        await update.message.reply_text(f"✅ Image updated.")
    except Exception as e:
         await update.message.reply_text(f"❌ Failed: {e}")

async def enable_ipl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_db
    db = get_db()
    db.system_config.update_one({"key": "ipl_mode"}, {"$set": {"enabled": True}}, upsert=True)
    await update.message.reply_text("✅ IPL Mode ENABLED.")

async def disable_ipl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_db
    db = get_db()
    db.system_config.update_one({"key": "ipl_mode"}, {"$set": {"enabled": False}}, upsert=True)
    await update.message.reply_text("⛔ IPL Mode DISABLED.")

async def player_list_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /playerlist_ipl - Shows paginated list of valid IPL players
    """
    if not await check_admin(update): return

    from database import get_all_players
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    players = get_all_players()
    
    # Filter for IPL Players (Must have ipl_roles)
    ipl_players = [p for p in players if p.get('ipl_roles')]
    
    if not ipl_players:
        await update.message.reply_text("No IPL players found.")
        return
        
    await show_player_page_ipl(update, context, 0, ipl_players)

async def show_player_page_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, players=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    # Fetch if not passed (for callback)
    if players is None:
        from database import get_all_players
        all_p = get_all_players()
        players = [p for p in all_p if p.get('ipl_roles')]

    # Prepare Lines
    grouped = {}
    for p in players:
        pid = p['player_id']
        nation = pid.split('_')[0] if '_' in pid else "OTHERS"
        if nation not in grouped: grouped[nation] = []
        grouped[nation].append(p)
        
    lines = []
    lines.append(f"🏆 **IPL Player Pool** ({len(players)})")
    
    for nation in sorted(grouped.keys()):
        lines.append(f"🚩 **{nation}**")
        for p in grouped[nation]:
            mapped = "✅" if p.get('api_reference', {}).get('ipl_provider') or p.get('stats', {}).get('ipl') else "❌"
            roles = p.get('ipl_roles', [])
            lines.append(f"• {p['name']} - {', '.join(roles[:2])} - Stats: {mapped}")
        lines.append("")
        
    PAGE_SIZE = 15
    total_lines = len(lines)
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_content = lines[start_idx:end_idx]
    
    if not page_content:
         text = "End of list."
    else:
         text = "\n".join(page_content)
    
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"plipl_{page-1}"))
    if end_idx < total_lines:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"plipl_{page+1}"))
        
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def handle_playerlist_ipl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    page = int(data.split('_')[1])
    await show_player_page_ipl(update, context, page)
