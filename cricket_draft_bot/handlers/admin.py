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
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)

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
        await save_player(player)
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [[InlineKeyboardButton("🎲 Generate Stats (ODI)", callback_data=f"gen_odi_{player_id}")]]
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



async def add_player_fifa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_playerfifa name=Mbappe sport=football overall=91 pac=97 ...
    """
    if not await check_admin(update): return

    text = update.message.text.replace('/add_playerfifa', '').strip()
    
    # Help Text if empty or missing "="
    if not text or "=" not in text:
        await update.message.reply_text(
            "⚠️ **FIFA Player Usage:**\n\n"
            "**Outfielder:**\n"
            "`/add_playerfifa name=Mbappe sport=football overall=91 pac=97 sho=90 pas=80 dri=92 def=36 phy=78 positions=ST,LW image=URL`\n\n"
            "**Goalkeeper:**\n"
            "`/add_playerfifa name=Yashin overall=94 div=95 han=90 kic=85 ref=96 spd=60 pos=93 positions=GK image=URL`\n"
            "*(You can also mix outfield stats like pac, sho if relevant)*",
            parse_mode="Markdown"
        )
        return

    try:
        import re
        
        # Regex to capture key=value pairs
        # Added 'positions' to the list of keys
        keys = "name|sport|overall|pac|sho|pas|dri|def|phy|div|han|kic|ref|spd|pos|image|positions"
        # Use raw string r'' to avoid SyntaxWarning with \s
        pattern = rf'({keys})\s*=\s*(.*?)(?=\s+(?:{keys})\s*=|$) '[:-1]
        
        raw_text = text + ' '
        matches = re.finditer(pattern, raw_text, re.IGNORECASE | re.DOTALL)
        parsed = {m.group(1).lower(): m.group(2).strip() for m in matches}
        
        # Defaults
        parsed['sport'] = parsed.get('sport', 'football')
        
        # Required Validation
        required = ['name', 'overall', 'image']
        # Relax requirement for pac/sho etc if GK stats provided?
        # Let's say if positions=GK, we might look for div/han/etc.
        # But simpler: check if ANY stats provided.
        # Original: required = ['name', 'overall', 'image', 'pac', 'sho', 'pas', 'dri', 'def', 'phy']
        # The user might provide GK stats INSTEAD of outfield stats.
        
        is_gk_stats = any(k in parsed for k in ['div', 'han', 'kic', 'ref', 'spd', 'pos'])
        
        if not is_gk_stats:
             # Standard outfield check
             missing = [k for k in ['pac', 'sho', 'pas', 'dri', 'def', 'phy'] if k not in parsed]
             if missing:
                  await update.message.reply_text(f"❌ Missing outfield stats: {', '.join(missing)}")
                  return
        
        missing_basic = [k for k in required if k not in parsed]
        if missing_basic:
             await update.message.reply_text(f"❌ Missing basic fields: {', '.join(missing_basic)}")
             return

        # Parsing Values
        name = parsed['name']
        overall = int(parsed['overall'])
        pac = int(parsed.get('pac', 0))
        sho = int(parsed.get('sho', 0))
        pas = int(parsed.get('pas', 0))
        dri = int(parsed.get('dri', 0))
        df = int(parsed.get('def', 0))
        phy = int(parsed.get('phy', 0))
        
        # GK Stats
        div = int(parsed.get('div', 0))
        han = int(parsed.get('han', 0))
        kic = int(parsed.get('kic', 0))
        ref = int(parsed.get('ref', 0))
        spd_gk = int(parsed.get('spd', 0))
        pos_gk = int(parsed.get('pos', 0))
        
        image_url = parsed['image']
        
        # Calculate Positions (Logic from import_fifa_26.py)
        # ST, CF: (PAC + SHO + DRI) / 3
        st_rating = int((pac + sho + dri) / 3)
        cf_rating = st_rating
        
        # LW, RW: (PAC + DRI + PAS) / 3
        wing_rating = int((pac + dri + pas) / 3)
        
        # CAM: (PAS + DRI + SHO) / 3
        cam_rating = int((pas + dri + sho) / 3)
        
        # CM: (PAS + DRI + PHY) / 3
        cm_rating = int((pas + dri + phy) / 3)
        
        # CDM: (PAS + DEF + PHY) / 3 (Approx logic)
        cdm_rating = int((pas + df + phy) / 3)
        
        # LB, RB: (PAC + DEF + PAS) / 3
        fullback_rating = int((pac + df + pas) / 3)
        
        # CB: (DEF + PHY) / 2
        cb_rating = int((df + phy) / 2)
        
        if is_gk_stats:
            # Calculate Average of PROVIDED stats
            provided_gk_stats = [v for v in [div, han, kic, ref, spd_gk, pos_gk] if v > 0]
            if provided_gk_stats:
                 gk_avg = int(sum(provided_gk_stats) / len(provided_gk_stats))
                 # Fill zeros with average? Or just use average as rating?
                 # If user misses 'pos', it's better to ignore it than avg in a 0.
                 gk_rating = gk_avg
            else:
                 gk_rating = overall
        else:
            # Fallback for outfielders or missing GK stats
            gk_rating = 0
            
        # If user explicitly said Position=GK, force GK rating to be at least Overall
        # This fixes the issue where missing stats drag down the rating
        if 'positions' in parsed and 'GK' in parsed['positions'].upper():
             if gk_rating < (overall - 5): 
                 gk_rating = overall
        
        fifa_stats = {
            "ST": st_rating, "CF": cf_rating,
            "LW": wing_rating, "RW": wing_rating,
            "CAM": cam_rating, 
            "CM": cm_rating,
            "CDM": cdm_rating,
            "LB": fullback_rating, "RB": fullback_rating,
            "CB": cb_rating,
            "GK": gk_rating,
            "PAC": pac, "SHO": sho, "PAS": pas, "DRI": dri, "DEF": df, "PHY": phy,
            "DIV": div, "HAN": han, "KIC": kic, "REF": ref, "SPD": spd_gk, "POS": pos_gk
        }
        
        # Helper to deduce best positions (Top 3)
        # Check if user provided explicit positions
        if 'positions' in parsed:
            raw_pos = parsed['positions'].upper().split(',')
            best_positions = [p.strip() for p in raw_pos if p.strip()]
        else:
            # Auto-calculate
            pos_map = {k: v for k, v in fifa_stats.items() if k in ["ST", "CF", "LW", "RW", "CAM", "CM", "CDM", "LB", "RB", "CB"]}
            sorted_pos = sorted(pos_map.items(), key=lambda item: item[1], reverse=True)
            best_positions = [x[0] for x in sorted_pos[:3]]
        
        
        # ID Generation
        clean_name = name.upper().replace(' ', '_')
        player_id = f"fifa_man_{clean_name[:10]}"
        
        # Image Store
        msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image_url, caption=f"Added {name}")
        image_file_id = msg.photo[-1].file_id
        
        player_doc = {
            "player_id": player_id,
            "name": name,
            "sport": "football",
            "role": "Footballer",
            "roles": ["Footballer"],
            "positions": best_positions,
            "overall": overall,
            "stats": {
                "fifa": fifa_stats
            },
            "image_file_id": image_file_id,
            "fifa_image_url": image_url,
            "source_db": "manual"
        }
        
        await save_player(player_doc)
        
        await update.message.reply_text(
            f"✅ **FIFA Player Added!**\n"
            f"Name: {name} (OVR: {overall})\n"
            f"Best Pos: {', '.join(best_positions)}\n"
            f"Stats: ST:{st_rating} CM:{cm_rating} CB:{cb_rating}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Add FIFA Error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def generate_player_stats(player_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False, mode='odi'):
    """
    Shared logic to generate stats.
    mode: 'odi' or 'ipl' or 'test'
    """
    from database import get_player, save_player
    p = await get_player(player_id)
    
    if not p:
        msg = f"Player not found ({player_id})"
        if is_callback:
            try: await update.callback_query.answer(msg, show_alert=True)
            except: pass
        else:
            await update.message.reply_text(msg)
        return

    # Notify user
    mode_label = "IPL" if mode == 'ipl' else ("Test" if mode == 'test' else "ODI")
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
        current_stats['odi'] = ai_stats.get('odi', ai_stats.get('international', {}))
        if 'api_reference' not in p: p['api_reference'] = {}
        p['api_reference']['provider'] = ai_stats.get('source_label', 'Seeded')
        
    p['stats'] = current_stats
    await save_player(p)
         
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

    await generate_player_stats(player_id, update, context, is_callback=False, mode='odi')

async def handle_gen_odi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # gen_odi_ID
    if not await can_manage_bot(update.effective_user.id):
        await query.answer("⛔ Admin Only", show_alert=True)
        return
    await generate_player_stats(player_id, update, context, is_callback=True, mode='odi')

async def handle_gen_ipl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # gen_ipl_ID
    if not await can_manage_bot(update.effective_user.id):
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
        
    await generate_player_stats(player_id, update, context, is_callback=True, mode='odi')

# Update add_player to include button
# This requires editing the add_player success block, which is lines 79-80.
# I will do that in a separate replacement call or include it here if ranges overlap.
# They don't overlap easily with this block replacing line 88+.
# Use MultiReplace? No, limited lines.
# I will implement generate_player_stats and update map_api first.


async def handle_remove_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeipl Virat Kohli
    Removes IPL stats/data for the specified player.
    """
    if not await check_admin(update): return
    
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usage: /removeipl Player Name")
        return
        
    from database import get_player_by_name, save_player
    
    player = await get_player_by_name(name)
    if not player:
        await update.message.reply_text(f"Player not found: {name}")
        return
        
    # Check if they even have IPL stats
    cleaned = False
    
    if 'stats' in player and 'ipl' in player['stats']:
        del player['stats']['ipl']
        cleaned = True
        
    # Remove top-level IPL fields if they exist
    for field in ['ipl_team', 'ipl_roles', 'ipl_image_file_id']:
        if field in player:
            del player[field]
            cleaned = True
            
    if cleaned:
        await save_player(player)
        await update.message.reply_text(f"✅ Removed IPL data for **{esc(player['name'])}**.", parse_mode="Markdown")
        logger.info(f"Admin {update.effective_user.id} removed IPL data for {player['name']}")
    else:
        await update.message.reply_text(f"⚠️ **{esc(player['name'])}** has no IPL data to remove.", parse_mode="Markdown")



async def handle_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /clearcache
    Manually clears the player data cache.
    """
    logger.info(f"Command /clearcache invoked by {update.effective_user.id}")
    if not await check_admin(update): return
    
    from database import clear_player_cache
    clear_player_cache()
    
    await update.message.reply_text("✅ Player cache cleared successfully.", parse_mode="Markdown")

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
        
        p = await get_player(player_id)
        if not p:
            # Fallback: Try by name
            from database import get_player_by_name
            p = await get_player_by_name(player_id) # player_id variable holds the search text here
            
            if not p:
                await update.message.reply_text(f"❌ Player not found by ID or Name: '{player_id}'")
                return
            
            # Found by name, update player_id to the actual ID found
            player_id = p['player_id']

        if await delete_player(player_id):
            await update.message.reply_text(f"✅ Player *{esc(p['name'])}* (`{player_id}`) has been removed.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Failed to remove player (DB Error).")
            
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")



async def get_player_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats Player Name [sport=wwe|cricket|football]"""
    if not await check_admin(update): return

    import re
    text = update.message.text.replace('/stats', '').strip()
    if not text:
        await update.message.reply_text(
            "**Usage:** `/stats Player Name` or `/stats Player Name sport=wwe`",
            parse_mode="Markdown"
        )
        return

    # Parse optional sport= flag
    sport_match = re.search(r'\bsport=(\w+)', text, re.IGNORECASE)
    sport_filter = sport_match.group(1).lower() if sport_match else None
    if sport_filter == 'fifa': sport_filter = 'football'
    if sport_match:
        text = re.sub(r'\s*\bsport=\S+', '', text, flags=re.IGNORECASE).strip()

    from database import search_players_by_name
    results = await search_players_by_name(text, sport_filter)
    
    if not results:
        hint = f" (sport={sport_filter})" if sport_filter else ""
        await update.message.reply_text(
            f"❌ Player matching `{esc(text)}`{hint} not found.", parse_mode="Markdown"
        )
        return
        
    if len(results) > 1:
        # Check if one is an exact match
        exact_matches = [r for r in results if r['name'].lower() == text.lower()]
        if len(exact_matches) == 1:
            p = exact_matches[0]
        else:
            names = [f"`{esc(r['name'])}`" for r in results[:5]]
            if len(results) > 5: names.append("...")
            
            await update.message.reply_text(
                f"⚠️ Multiple players found matching `{esc(text)}`:\n"
                f"{', '.join(names)}\n\n"
                f"Please type the full name to be more specific.",
                parse_mode="Markdown"
            )
            return
    else:
        p = results[0]

    stats  = p.get('stats', {})
    sport  = p.get('sport', 'cricket')

    # ── WWE ──────────────────────────────────────────────────────────────────
    if sport == 'wwe':
        ws = stats.get('wwe', {})
        def w(k): return ws.get(k, 'N/A')
        msg = (
            f"🤼 *{esc(p['name'])}* (WWE)\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💪 Power:        {w('power')}\n"
            f"⚡ Speed:        {w('speed')}\n"
            f"🧠 Technique:    {w('technique')}\n"
            f"🔋 Stamina:      {w('stamina')}\n"
            f"🛡 Durability:  {w('durability')}\n"
            f"🎤 Charisma:     {w('charisma')}\n"
            f"🥊 Aggression:   {w('aggression')}\n"
            f"📋 Intelligence: {w('intelligence')}\n"
            f"🪂 Aerial:       {w('aerial')}\n"
            f"🔒 Submission:   {w('submission')}"
        )
        img = p.get('image_file_id') or p.get('wwe_image_url')
        if img:
            try:
                await update.message.reply_photo(photo=img, caption=msg, parse_mode="Markdown")
                return
            except Exception:
                pass
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # ── FIFA ─────────────────────────────────────────────────────────────────
    if sport == 'football':
        fs = stats.get('fifa', {})
        def f(k): return fs.get(k, 0)
        msg = (
            f"⚽ *{esc(p['name'])}* (FIFA)\n"
            f"🏅 Overall: {p.get('overall', 'N/A')}  "
            f"📍 {', '.join(p.get('positions', []))}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🏃 ST: {f('ST')}  CF: {f('CF')}  GK: {f('GK')}\n"
            f"🎯 LW: {f('LW')}  RW: {f('RW')}  CAM: {f('CAM')}\n"
            f"🧠 CM: {f('CM')}  CDM: {f('CDM')}\n"
            f"🛡 CB: {f('CB')}  LB: {f('LB')}  RB: {f('RB')}"
        )
        sent = False
        for img_key in ('image_file_id', 'fifa_image_url'):
            if p.get(img_key) and not sent:
                try:
                    await update.message.reply_photo(
                        photo=p[img_key], caption=msg, parse_mode="Markdown"
                    )
                    sent = True
                except Exception:
                    pass
        if not sent:
            await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # ── Cricket ───────────────────────────────────────────────────────────────
    roles      = p.get('roles', [])
    roles_up   = [r.upper() for r in roles]
    has_wk     = "WK" in roles_up
    has_def    = "DEFENCE" in roles_up

    def format_stats(data):
        if not data:              return "N/A"
        if isinstance(data, int): return str(data)
        def g(k): return data.get(k, 'N/A')
        parts = [
            f"🧠 Captain: {g('leadership')}",
            f"🏏 Top:     {g('batting_power')}",
            f"🛡 Middle: {g('batting_control')}",
        ]
        if has_def: parts.append(f"🧱 Defence:  {g('batting_defence')}")
        if has_wk:  parts.append(f"🧤 WK:       {g('wicket_keeping')}")
        parts += [
            f"✨ All Round: {g('all_round')}",
            f"💥 Finisher:  {g('finishing')}",
            f"⚡ Pacer:     {g('bowling_pace')}",
            f"🌀 Spinner:   {g('bowling_spin')}",
            f"🤾 Fielding:  {g('fielding')}",
        ]
        return "\n".join(parts)

    intl_display = format_stats(stats.get('odi', {}))

    msg = (
        f"📊 <b>{p['name']}</b>\n"
        f"<i>ODI Stats</i>\n"
        f"{intl_display}\n\n"
        f"Roles: {', '.join(roles)}"
    )
    # Also build a Markdown version for photo caption (player names in captions are safe)
    md_msg = (
        f"📊 *{esc(p['name'])}*\n"
        f"*ODI Stats*\n{intl_display}\n\n"
        f"Roles: {esc(', '.join(roles))}"
    )

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    btn_row = [InlineKeyboardButton("🏏 IPL Stats", callback_data=f"view_ipl_{p['player_id']}")]
    if p.get('stats', {}).get('test'):
        btn_row.append(InlineKeyboardButton("🧪 Test Stats", callback_data=f"view_test_{p['player_id']}"))
    kb = InlineKeyboardMarkup([btn_row])

    if p.get('image_file_id'):
        try:
            await update.message.reply_photo(
                photo=p['image_file_id'], caption=md_msg,
                reply_markup=kb, parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Photo send failed for {p['name']}: {e}")
    # Fallback: plain HTML — never crashes on special chars
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")



async def handle_view_ipl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # view_ipl_ID
    
    from database import get_player
    p = await get_player(player_id)
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
        f"🇮🇳 *IPL Stats for {esc(p['name'])}*\n\n"
        f"{stats_display}\n\n"
        f"Roles: {', '.join(ipl_roles)}"
    )
    
    # Back button
    keyboard = [[InlineKeyboardButton("🔙 Back to ODI", callback_data=f"view_odi_{player_id}")]]
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

async def handle_view_odi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when user clicks 'Back to ODI' from IPL/Test view."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
    query = update.callback_query
    player_id = query.data.split('_', 2)[2]

    from database import get_player
    p = await get_player(player_id)
    if not p:
        await query.answer("Player not found", show_alert=True)
        return

    stats = p.get('stats', {}).get('odi', {})
    intl_img = p.get('image_file_id')
    roles = p.get('roles', [])
    roles_up = [r.upper() for r in roles]
    has_wk  = "WK" in roles_up
    has_def = "DEFENCE" in roles_up

    # Same format_stats as /stats command
    def format_stats(data):
        if not data:              return "N/A"
        if isinstance(data, int): return str(data)
        def g(k): return data.get(k, 'N/A')
        parts = [
            f"🧠 Captain: {g('leadership')}",
            f"🏏 Top:     {g('batting_power')}",
            f"🛡 Middle: {g('batting_control')}",
        ]
        if has_def: parts.append(f"🧱 Defence:  {g('batting_defence')}")
        if has_wk:  parts.append(f"🧤 WK:       {g('wicket_keeping')}")
        parts += [
            f"✨ All Round: {g('all_round')}",
            f"💥 Finisher:  {g('finishing')}",
            f"⚡ Pacer:     {g('bowling_pace')}",
            f"🌀 Spinner:   {g('bowling_spin')}",
            f"🤾 Fielding:  {g('fielding')}",
        ]
        return "\n".join(parts)

    intl_display = format_stats(stats)
    caption = (
        f"📊 *{esc(p['name'])}*\n"
        f"*ODI Stats*\n{intl_display}\n\n"
        f"Roles: {esc(', '.join(roles))}"
    )

    btn_row = [InlineKeyboardButton("🏏 IPL Stats", callback_data=f"view_ipl_{player_id}")]
    if p.get('stats', {}).get('test'):
        btn_row.append(InlineKeyboardButton("🧪 Test Stats", callback_data=f"view_test_{player_id}"))
    kb = InlineKeyboardMarkup([btn_row])

    try:
        if intl_img:
            await query.message.edit_media(
                media=InputMediaPhoto(media=intl_img, caption=caption, parse_mode="Markdown"),
                reply_markup=kb
            )
        else:
            await query.edit_message_caption(caption=caption, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"handle_view_odi_callback edit error: {e}")
        await query.answer()


async def handle_view_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when user clicks 'Test Stats' button."""
    query = update.callback_query
    player_id = query.data.split('_', 2)[2]  # view_test_ID
    from database import get_player
    p = await get_player(player_id)
    if not p:
        await query.answer("Player not found", show_alert=True)
        return
    stats = p.get('stats', {}).get('test', {})
    if not stats:
        await query.answer("No Test Stats available", show_alert=True)
        return
    test_img = p.get('test_image_url') or p.get('image_file_id')
    test_roles = p.get('test_roles', p.get('roles', []))
    def fmt(data):
        if isinstance(data, int): return str(data)
        parts = []
        parts.append(f"🧠 Cap: {data.get('leadership')}")
        parts.append(f"🏏 Top: {data.get('batting_power')}")
        parts.append(f"🛡️ Mid: {data.get('batting_control')}")
        parts.append(f"🧱 Def: {data.get('batting_defence')}")
        if "WK" in [r.upper() for r in test_roles]: parts.append(f"🧤 WK: {data.get('wicket_keeping')}")
        parts.append(f"✨ All: {data.get('all_round')}")
        parts.append(f"⚡ Pace: {data.get('bowling_pace')}")
        parts.append(f"🌀 Spin: {data.get('bowling_spin')}")
        parts.append(f"👟 Field: {data.get('fielding')}")
        return '\n'.join(parts)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
    keyboard = [[InlineKeyboardButton("🔙 Back to ODI", callback_data=f"view_odi_{player_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = f"🏏 *Test Stats for {esc(p['name'])}*\n\n{fmt(stats)}\n\nRoles: {', '.join(test_roles)}"
    try:
        if test_img:
            await query.message.edit_media(media=InputMediaPhoto(media=test_img, caption=caption, parse_mode='Markdown'), reply_markup=reply_markup)
        else:
            await query.message.edit_caption(caption=caption, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"handle_view_test_callback error: {e}")
    await query.answer()






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
    p = await get_player_by_name(player_name)
    
    if not p:
        await update.message.reply_text(f"❌ Player '{player_name}' not found.")
        return
        
    # Update Stats
    stats = p.get('stats', {})
    modes = ['ipl', 'odi', 'test']
    
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
    await save_player(p)
    
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
        await add_mod(target_id)
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
        await remove_mod(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` removed from Moderators.", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/unmod <user_id>`", parse_mode="Markdown")

async def set_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setstats Name [sport=wwe|cricket] [format=all|ipl|odi|test] stat=value ..."""
    if not await check_admin(update): return

    text = update.message.text.replace('/setstats', '').strip()
    if not text:
        await update.message.reply_text(
            "**Usage:**\n"
            "`/setstats Name format=ipl cap=90 top=85`\n"
            "`/setstats Name format=odi cap=90 top=85`\n"
            "`/setstats Name format=test cap=90 top=85`\n"
            "`/setstats Name sport=wwe power=95 speed=80`\n"
            "Cricket keys: cap, wk, top, mid, def, all, pacer, spin, fin, field\n"
            "WWE keys: power, speed, tech, stamina, dur, char, agg, intel, aerial, sub",
            parse_mode="Markdown"
        )
        return

    # Parse key=value pairs — name is everything without '='
    parts = text.split()
    name_parts, kwargs = [], {}
    for part in parts:
        if '=' in part:
            k, v = part.split('=', 1)
            kwargs[k.lower()] = v
        else:
            name_parts.append(part)

    player_name = " ".join(name_parts)
    if not player_name:
        await update.message.reply_text("❌ Player name missing.", parse_mode="Markdown")
        return

    sport_filter = kwargs.pop('sport', None)
    if sport_filter:
        sport_filter = sport_filter.lower()
        if sport_filter == 'fifa': sport_filter = 'football'

    from database import search_players_by_name, save_player
    results = await search_players_by_name(player_name, sport_filter)

    if not results:
        hint = f" (sport={sport_filter})" if sport_filter else ""
        await update.message.reply_text(f"❌ Player '{player_name}'{hint} not found.")
        return

    if len(results) > 1:
        # Check for exact match first
        exact = [r for r in results if r['name'].lower() == player_name.lower()]
        if len(exact) == 1:
            p = exact[0]
        else:
            names = [f"`{r['name']}` (sport={r.get('sport','?')})" for r in results[:5]]
            if len(results) > 5:
                names.append("...")
            await update.message.reply_text(
                f"⚠️ Multiple players found matching `{player_name}`:\n"
                + "\n".join(names)
                + "\n\nPlease be more specific or add `sport=cricket/football/wwe` to target the right player.",
                parse_mode="Markdown"
            )
            return
    else:
        p = results[0]

    sport = p.get('sport', 'cricket')
    stats = p.get('stats', {})
    changes = []

    # ── WWE path ─────────────────────────────────────────────────────────────
    if sport == 'wwe':
        wwe_key_map = {
            'power': 'power', 'speed': 'speed',
            'technique': 'technique', 'tech': 'technique',
            'stamina': 'stamina', 'stam': 'stamina',
            'durability': 'durability', 'dur': 'durability',
            'charisma': 'charisma', 'char': 'charisma',
            'aggression': 'aggression', 'agg': 'aggression',
            'intelligence': 'intelligence', 'intel': 'intelligence',
            'aerial': 'aerial',
            'submission': 'submission', 'sub': 'submission',
        }
        wwe_stats = stats.get('wwe', {})
        has_updates = False
        for k, v in kwargs.items():
            if k == 'format': continue
            sk = wwe_key_map.get(k)
            if not sk:
                continue
            try:
                val = max(1, min(100, int(v)))
                old = wwe_stats.get(sk, 'N/A')
                wwe_stats[sk] = val
                changes.append(f"WWE {sk}: {old} → {val}")
                has_updates = True
            except ValueError:
                await update.message.reply_text(f"❌ Invalid value for {k}: `{v}` (must be 1-100)", parse_mode="Markdown")
                return
        if not has_updates:
            await update.message.reply_text(
                "⚠️ No valid WWE stats found.\n"
                "Keys: power, speed, tech, stamina, dur, char, agg, intel, aerial, sub",
                parse_mode="Markdown"
            )
            return
        stats['wwe'] = wwe_stats

    # ── Cricket path ──────────────────────────────────────────────────────────
    else:
        cricket_key_map = {
            'cap': 'leadership', 'wk': 'wicket_keeping',
            'top': 'batting_power', 'mid': 'batting_control', 'def': 'batting_defence',
            'pacer': 'bowling_pace', 'pace': 'bowling_pace',
            'spinner': 'bowling_spin', 'spin': 'bowling_spin',
            'all': 'all_round', 'allrounder': 'all_round',
            'fin': 'finishing', 'field': 'fielding',
        }
        target_format = kwargs.get('format', 'all').lower()
        if target_format not in ['ipl', 'odi', 'test', 'all']:
            await update.message.reply_text("❌ format must be `ipl`, `odi`, `test`, or `all`.", parse_mode="Markdown")
            return
        if target_format == 'all':
            modes = ['ipl', 'odi', 'test']
        elif target_format == 'ipl':
            modes = ['ipl']
        elif target_format == 'odi':
            modes = ['odi']
        else:
            modes = ['test']
        has_updates = False
        for k, v in kwargs.items():
            if k in ('format', 'sport'): continue
            sk = cricket_key_map.get(k)
            if not sk: continue
            try:
                val = max(1, min(100, int(v)))
                for mode in modes:
                    if mode not in stats: stats[mode] = {}
                    old = stats[mode].get(sk, 'N/A')
                    stats[mode][sk] = val
                    changes.append(f"{mode.upper()} {k}: {old} → {val}")
                    has_updates = True
            except ValueError:
                await update.message.reply_text(f"❌ Invalid value for {k}: `{v}` (must be 1-100)", parse_mode="Markdown")
                return
        if not has_updates:
            await update.message.reply_text(
                "⚠️ No valid stats found.\nKeys: cap, wk, top, mid, def, all, pacer, spin, fin, field",
                parse_mode="Markdown"
            )
            return

    p['stats'] = stats
    await save_player(p)

    summary = "\n".join(changes)
    await update.message.reply_text(
        f"✅ *Updated {esc(p['name'])}*\n\n{summary}",
        parse_mode="Markdown"
    )



async def check_role_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/check [role/stat] [mode]  — paginated, 30/page"""
    if not await check_admin(update): return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: `/check [role] [mode]`\n"
            "Cricket: `/check middle odi` · `/check wk ipl`\n"
            "WWE: `/check power wwe` · `/check sub wwe`",
            parse_mode="Markdown"
        )
        return

    role_query = args[0].lower()
    mode       = args[1].lower()
    await _render_check(update, None, role_query, mode, 0)


async def _render_check(update, cb_query, role_query: str, mode: str, page: int):
    """Shared renderer for /check command and its pagination callbacks."""
    PAGE_SIZE = 30
    from database import get_db
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    db = get_db()

    # ── WWE ──────────────────────────────────────────────────────────────────
    if mode == "wwe":
        wwe_alias = {
            "power": "power", "speed": "speed",
            "technique": "technique", "tech": "technique",
            "stamina": "stamina", "stam": "stamina",
            "durability": "durability", "dur": "durability",
            "charisma": "charisma", "char": "charisma",
            "aggression": "aggression", "agg": "aggression",
            "intelligence": "intelligence", "intel": "intelligence",
            "aerial": "aerial",
            "submission": "submission", "sub": "submission",
        }
        stat_key = wwe_alias.get(role_query)
        if not stat_key:
            err = (f"❌ Unknown WWE stat: `{role_query}`\n"
                   "Available: power, speed, technique, stamina, durability, "
                   "charisma, aggression, intelligence, aerial, submission")
            if cb_query: await cb_query.edit_message_text(err, parse_mode="Markdown")
            else:        await update.message.reply_text(err, parse_mode="Markdown")
            return

        # Efficient projection — only name + the needed stat
        cursor = db.players.find(
            {"sport": "wwe"},
            {"name": 1, f"stats.wwe.{stat_key}": 1, "_id": 0}
        )
        docs    = [d async for d in cursor]
        results = [(d["name"], d.get("stats", {}).get("wwe", {}).get(stat_key, 0)) for d in docs]
        results.sort(key=lambda x: x[1], reverse=True)
        display_label = f"{stat_key.capitalize()} (WWE)"

    # ── Cricket/IPL ───────────────────────────────────────────────────────────
    else:
        if mode in ('odi', 'intl'): db_mode = 'odi'
        elif mode == 'ipl': db_mode = 'ipl'
        elif mode == 'test': db_mode = 'test'
        else:
            err = "Mode must be `ipl`, `odi`, `test`, or `wwe`."
            if cb_query: await cb_query.edit_message_text(err, parse_mode="Markdown")
            else:        await update.message.reply_text(err, parse_mode="Markdown")
            return

        from config import ROLE_STATS_MAP
        alias_map = {
            "hitting": "Top", "batting": "Top", "pace": "Pacer",
            "spin": "Spinner", "all": "All Rounder", "allrounder": "All Rounder",
            "field": "Fielder", "fielding": "Fielder", "def": "Defence",
            "defence": "Defence", "middle": "Middle", "mid": "Middle",
            "top": "Top", "wk": "WK", "cap": "Captain", "captain": "Captain",
            "fin": "Finisher", "finisher": "Finisher", "pacer": "Pacer",
            "spinner": "Spinner", "ar": "All Rounder",
        }

        canonical_role = alias_map.get(role_query) or next(
            (r for r in ROLE_STATS_MAP if r.lower() == role_query), None
        )
        if not canonical_role or canonical_role not in ROLE_STATS_MAP:
            err = f"❌ Unknown role: `{role_query}`.\nAvailable: {', '.join(ROLE_STATS_MAP.keys())}"
            if cb_query: await cb_query.edit_message_text(err, parse_mode="Markdown")
            else:        await update.message.reply_text(err, parse_mode="Markdown")
            return

        stat_key = ROLE_STATS_MAP[canonical_role]

        # Efficient DB query — filter 0 at DB level, project only what's needed
        cursor = db.players.find(
            {f"stats.{db_mode}.{stat_key}": {"$gt": 0}},
            {"name": 1, f"stats.{db_mode}.{stat_key}": 1, "_id": 0}
        )
        docs    = [d async for d in cursor]
        results = [(d["name"], d.get("stats", {}).get(db_mode, {}).get(stat_key, 0)) for d in docs]
        results.sort(key=lambda x: x[1], reverse=True)
        display_label = f"{canonical_role} ({mode.upper()})"

    # ── Pagination ────────────────────────────────────────────────────────────
    total       = len(results)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    start       = page * PAGE_SIZE
    page_rows   = results[start: start + PAGE_SIZE]

    lines = [f"📊 *{display_label}* — {total} entries\n"]
    for i, (name, score) in enumerate(page_rows, start + 1):
        lines.append(f"{i}. {esc(name)}: *{score}*")
    lines.append(f"\n_Page {page + 1}/{total_pages}_")
    msg = "\n".join(lines)

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(
            "⬅️ Prev", callback_data=f"chk_{role_query}_{mode}_{page - 1}"
        ))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton(
            "Next ➡️", callback_data=f"chk_{role_query}_{mode}_{page + 1}"
        ))
    kb = InlineKeyboardMarkup([buttons]) if buttons else None

    if cb_query:
        await cb_query.edit_message_text(msg, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=kb, parse_mode="Markdown")


async def handle_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /check pagination — callback_data format: chk_ROLE_MODE_PAGE"""
    query = update.callback_query
    await query.answer()
    try:
        # Split into exactly 4 parts: chk, role, mode, page
        _, role_query, mode, page_str = query.data.split("_", 3)
        await _render_check(update, query, role_query, mode, int(page_str))
    except Exception as e:
        logger.warning(f"check callback parse error: {e}")


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
from telegram.helpers import escape_markdown

def esc(t):
    return escape_markdown(str(t), version=1)

logger = logging.getLogger(__name__)



async def handle_remove_ipl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeipl Virat Kohli
    Removes IPL stats/data for the specified player.
    """
    if not await check_admin(update): return
    
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usage: /removeipl Player Name")
        return
        
    from database import get_player_by_name, save_player
    
    player = await get_player_by_name(name)
    if not player:
        await update.message.reply_text(f"Player not found: {name}")
        return
        
    # Check if they even have IPL stats
    cleaned = False
    
    if 'stats' in player and 'ipl' in player['stats']:
        del player['stats']['ipl']
        cleaned = True
        
    # Remove top-level IPL fields if they exist
    for field in ['ipl_team', 'ipl_roles', 'ipl_image_file_id']:
        if field in player:
            del player[field]
            cleaned = True
            
    if cleaned:
        await save_player(player)
        await update.message.reply_text(f"✅ Removed IPL data for **{esc(player['name'])}**.", parse_mode="Markdown")
        logger.info(f"Admin {update.effective_user.id} removed IPL data for {player['name']}")
    else:
        await update.message.reply_text(f"⚠️ **{esc(player['name'])}** has no IPL data to remove.", parse_mode="Markdown")






async def add_player_wwe(update, context):
    """/add_playerwwe name=Cena image=URL power=95 speed=60 ..."""
    from telegram.ext import ContextTypes
    from telegram import Update
    if not await check_admin(update): return

    import re
    text = update.message.text.replace("/add_playerwwe", "").strip()
    if not text or "=" not in text:
        await update.message.reply_text(
            "**WWE Player Usage:**\n"
            "`/add_playerwwe name=John Cena image=URL power=95 speed=60 "
            "technique=75 stamina=90 durability=95 charisma=98 "
            "aggression=85 intelligence=80 aerial=40 submission=65`",
            parse_mode="Markdown"
        )
        return

    try:
        keys = "name|image|power|speed|technique|stamina|durability|charisma|aggression|intelligence|aerial|submission"
        pattern = rf"({keys})\s*=\s*(.*?)(?=\s+(?:{keys})\s*=|$) "[:-1]
        raw = text + " "
        matches = re.finditer(pattern, raw, re.IGNORECASE | re.DOTALL)
        parsed = {m.group(1).lower(): m.group(2).strip() for m in matches}

        if "name" not in parsed or "image" not in parsed:
            await update.message.reply_text("❌ name= and image= are required.")
            return

        name      = parsed["name"]
        image_url = parsed["image"]
        stat_keys = ["power","speed","technique","stamina","durability",
                     "charisma","aggression","intelligence","aerial","submission"]
        wwe_stats = {}
        for sk in stat_keys:
            try:    wwe_stats[sk] = max(1, min(100, int(parsed.get(sk, 70))))
            except: wwe_stats[sk] = 70

        clean  = name.upper().replace(" ","_").replace(".","").replace("'","")
        pid    = f"wwe_{clean[:20]}"

        msg = await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=image_url, caption=f"Adding WWE: {name}"
        )
        fid = msg.photo[-1].file_id

        from config import POSITIONS_WWE
        from database import save_player
        doc = {
            "player_id": pid, "name": name, "full_name": name,
            "sport": "wwe", "roles": list(POSITIONS_WWE),
            "stats": {"wwe": wwe_stats},
            "image_file_id": fid, "wwe_image_url": image_url,
        }
        await save_player(doc)
        await update.message.reply_text(
            f"✅ WWE Superstar added!\nID: `{pid}`\nName: {esc(name)}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"add_playerwwe error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


async def remove_player_wwe(update, context):
    """/remove_playerwwe Name"""
    if not await check_admin(update): return
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        await update.message.reply_text("Usage: /remove_playerwwe Name")
        return
    from database import get_player_by_name_and_sport, delete_player
    p = await get_player_by_name_and_sport(name, "wwe")
    if not p:
        await update.message.reply_text(f"❌ WWE superstar not found: {name}")
        return
    if await delete_player(p["player_id"]):
        await update.message.reply_text(f"✅ Removed {esc(p['name'])} from WWE roster.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ DB error removing player.")


async def update_image_wwe(update, context):
    """/update_imagewwe Name URL"""
    if not await check_admin(update): return
    text = update.message.text.replace("/update_imagewwe", "").strip()
    parts = text.rsplit(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /update_imagewwe Name URL")
        return
    name, url = parts[0].strip(), parts[1].strip()
    from database import get_player_by_name_and_sport, save_player
    p = await get_player_by_name_and_sport(name, "wwe")
    if not p:
        await update.message.reply_text(f"❌ WWE superstar not found: {name}")
        return
    try:
        msg = await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=url, caption=f"Updated image: {p['name']}"
        )
        p["image_file_id"]  = msg.photo[-1].file_id
        p["wwe_image_url"]  = url
        await save_player(p)
        await update.message.reply_text(f"✅ Image updated for {esc(p['name'])}.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# MOD MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

async def list_mods_handler(update, context):
    """/mods — Lists all moderator IDs."""
    if not await check_admin(update): return
    from database import get_all_mods
    mod_ids = await get_all_mods()
    if not mod_ids:
        await update.message.reply_text("ℹ️ No moderators found.")
        return
    msg = "*👮 Moderator List*\n"
    for mid in mod_ids:
        msg += f"• `{mid}`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
# ROLE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

async def fix_roles_command(update, context):
    """/fix_roles — Alias for /migrate_roles."""
    if not await check_admin(update): return
    await migrate_roles_command(update, context)


async def migrate_roles_command(update, context):
    """/migrate_roles — Normalizes all player roles to canonical names."""
    if not await check_admin(update): return
    from database import get_all_players, save_player
    from config import POSITIONS_T20, POSITIONS_TEST
    players = await get_all_players()
    valid_roles = set(POSITIONS_T20 + POSITIONS_TEST)
    valid_map = {r.lower(): r for r in valid_roles}
    aliases = {
        "hitting": "Top", "batting": "Top", "pace": "Pacer",
        "bowling": "Pacer", "spin": "Spinner",
        "all-rounder": "All Rounder", "all-round": "All Rounder", "all": "All Rounder",
        "fielding": "Fielder", "field": "Fielder",
        "defence": "Defence", "def": "Defence", "middle": "Middle",
        "wk": "WK", "keeper": "WK", "wicketkeeper": "WK",
        "captain": "Captain", "cap": "Captain", "finisher": "Finisher"
    }
    total_updated = cleaned_top_count = 0
    for p in players:
        current_roles = p.get("roles", [])
        new_roles = []
        for r in current_roles:
            rl = r.strip().lower()
            canonical = valid_map.get(rl) or aliases.get(rl) or r.strip()
            new_roles.append(canonical)
        unique_roles = list(dict.fromkeys(new_roles))
        is_modified = unique_roles != current_roles
        if "Top" in unique_roles and ("All Rounder" in unique_roles or "Finisher" in unique_roles):
            unique_roles.remove("Top")
            is_modified = True
            cleaned_top_count += 1
        if is_modified:
            p["roles"] = unique_roles
            await save_player(p)
            total_updated += 1
    await update.message.reply_text(
        f"✅ *Migration Complete*\n"
        f"Scanned: {len(players)}  Updated: {total_updated}  "
        f"Top removed: {cleaned_top_count}",
        parse_mode="Markdown"
    )


async def add_role_command(update, context):
    """/add_role Name Role"""
    if not await check_admin(update): return
    text = update.message.text.replace("/add_role", "").strip()
    if not text:
        await update.message.reply_text("Usage: `/add_role Name Role`", parse_mode="Markdown")
        return
    parts = text.rsplit(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: `/add_role Name Role`", parse_mode="Markdown")
        return
    name, role_input = parts[0].strip(), parts[1].strip()
    from config import POSITIONS_T20, POSITIONS_TEST
    valid_map = {r.lower(): r for r in POSITIONS_T20 + POSITIONS_TEST}
    aliases = {"hitting":"Top","batting":"Top","pace":"Pacer","spin":"Spinner",
               "all":"All Rounder","fielding":"Fielder","field":"Fielder",
               "defence":"Defence","def":"Defence","middle":"Middle",
               "wk":"WK","keeper":"WK","captain":"Captain","cap":"Captain","finisher":"Finisher","fin":"Finisher"}
    target_role = valid_map.get(role_input.lower()) or aliases.get(role_input.lower())
    if not target_role:
        await update.message.reply_text(f"❌ Unknown role: `{role_input}`", parse_mode="Markdown")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text(f"❌ Player not found: {name}")
        return
    current = p.get("roles", [])
    if target_role in current:
        await update.message.reply_text(f"⚠️ Already has role *{target_role}*.", parse_mode="Markdown")
        return
    current.append(target_role)
    p["roles"] = current
    await save_player(p)
    await update.message.reply_text(f"✅ Added *{target_role}* to *{esc(p['name'])}*.", parse_mode="Markdown")


async def rem_role_command(update, context):
    """/rem_role Name Role"""
    if not await check_admin(update): return
    text = update.message.text.replace("/rem_role", "").strip()
    # Known roles — match from end of input to support multi-word roles like "All Rounder"
    KNOWN_ROLES = ["All Rounder", "Captain", "WK", "Top", "Middle", "Finisher", "Pacer", "Spinner", "Fielder"]
    role_input, name = None, None
    text_lower = text.lower()
    for role in sorted(KNOWN_ROLES, key=len, reverse=True):  # longest first
        if text_lower.endswith(role.lower()):
            role_input = role
            name = text[:len(text) - len(role)].strip()
            break
    if not role_input or not name:
        await update.message.reply_text("Usage: `/rem_role Name Role`\nRoles: Captain, WK, Top, Middle, All Rounder, Finisher, Pacer, Spinner, Fielder", parse_mode="Markdown")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text(f"❌ Player not found: {name}")
        return
    current = p.get("roles", [])
    found = next((r for r in current if r.lower() == role_input.lower()), None)
    if not found:
        await update.message.reply_text(f"⚠️ Role `{role_input}` not found.", parse_mode="Markdown")
        return
    current.remove(found)
    p["roles"] = current
    await save_player(p)
    await update.message.reply_text(f"✅ Removed *{found}* from *{esc(p['name'])}*.", parse_mode="Markdown")


async def non_role_fix(update, context):
    """/nonrolefix — Sets unassigned role stats to 40-60 for all players."""
    if not await check_admin(update): return
    from database import get_all_players, save_player
    from config import ROLE_STATS_MAP
    import random
    players = await get_all_players()
    updated = 0
    for p in players:
        roles_lower = [r.lower() for r in p.get("roles", [])]
        stats = p.get("stats", {})
        changed = False
        for role_name, stat_key in ROLE_STATS_MAP.items():
            if role_name.lower() in roles_lower:
                continue
            val = random.randint(40, 60)
            for mode in ["ipl", "odi", "test"]:
                if mode not in stats: stats[mode] = {}
                stats[mode][stat_key] = val
                changed = True
        if changed:
            p["stats"] = stats
            await save_player(p)
            updated += 1
    await update.message.reply_text(
        f"✅ Non-role fix done. Updated {updated}/{len(players)} players."
    )


async def run_fix_now_command(update, context):
    """/run_fix_now — Runs the stat fix script."""
    if not await check_admin(update): return
    await update.message.reply_text("🔄 Running stat fix...")
    from io import StringIO
    import sys as _sys
    try:
        from run_fix_now import run_fix_directly
        old = _sys.stdout
        _sys.stdout = buf = StringIO()
        run_fix_directly()
        _sys.stdout = old
        out = buf.getvalue()
        summary = "\n".join([l for l in out.split("\n") if any(k in l for k in ["Processed","Updated","DONE","Backup"])])
        await update.message.reply_text(f"✅ Done.\n{summary or 'Completed.'}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def revert_command(update, context):
    """/revert — Reverts stats from backup."""
    if not await check_admin(update): return
    try:
        from run_fix_now import revert_stats
        success, msg = revert_stats()
        await update.message.reply_text(f"{'✅' if success else '❌'} {msg}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ═══════════════════════════════════════════════════════════════════
# IPL SPECIFIC COMMANDS
# ═══════════════════════════════════════════════════════════════════

async def add_player_ipl(update, context):
    """/add_playeripl name=X roles=A,B image=URL"""
    if not await check_admin(update): return
    import re
    text = update.message.text.replace("/add_playeripl", "").strip()
    if not re.match(r"^(name|roles|image)\s*=", text, re.IGNORECASE):
        text = "name=" + text
    text += " "
    pattern = r"(name|roles|image)\s*=\s*(.*?)(?=\s+(?:name|roles|image)\s*=|$) "[:-1]
    parsed = {m.group(1).lower(): m.group(2).strip()
              for m in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL)}
    if not all(k in parsed for k in ["name", "roles", "image"]):
        await update.message.reply_text("Usage: /add_playeripl name=X roles=A,B image=URL")
        return
    name, image_url = parsed["name"], parsed["image"]
    from config import POSITIONS_T20
    valid_map = {r.lower(): r for r in POSITIONS_T20}
    aliases = {"hitting":"Top","keeper":"WK","cap":"Captain","pace":"Pacer","spin":"Spinner","all":"All Rounder"}
    final_roles = [valid_map.get(r.strip().lower(), aliases.get(r.strip().lower(), r.strip()))
                   for r in parsed["roles"].split(",") if r.strip()]
    clean = name.upper().replace(" ", "_")
    pid = f"PL_{clean[:10]}"
    from database import get_player_by_name, save_player, get_player
    p = await get_player(pid) or await get_player_by_name(name)
    if not p:
        p = {"player_id": pid, "name": name, "roles": [], "image_file_id": None,
             "ipl_roles": final_roles, "ipl_image_file_id": None, "stats": {"ipl": {}}}
    else:
        p["ipl_roles"] = final_roles
    msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image_url, caption=f"IPL: {name}")
    p["ipl_image_file_id"] = msg.photo[-1].file_id
    await save_player(p)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Generate Stats (IPL)", callback_data=f"gen_ipl_{pid}")]])
    await update.message.reply_text(f"✅ IPL data updated for *{esc(name)}*", reply_markup=kb, parse_mode="Markdown")


async def add_role_ipl(update, context):
    if not await check_admin(update): return
    text = update.message.text.replace("/add_roleipl", "").strip()
    parts = text.rsplit(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /add_roleipl Name Role")
        return
    name, role_input = parts[0].strip(), parts[1].strip()
    from config import POSITIONS_T20
    valid_map = {r.lower(): r for r in POSITIONS_T20}
    aliases = {"hitting":"Top","keeper":"WK","cap":"Captain","pace":"Pacer","spin":"Spinner","all":"All Rounder"}
    target = valid_map.get(role_input.lower(), aliases.get(role_input.lower(), role_input))
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    current = p.get("ipl_roles", [])
    if target not in current:
        current.append(target)
        p["ipl_roles"] = current
        await save_player(p)
        await update.message.reply_text(f"✅ Added {target} to {p['name']} (IPL).")
    else:
        await update.message.reply_text(f"⚠️ Already has {target} in IPL.")


async def rem_role_ipl(update, context):
    if not await check_admin(update): return
    text = update.message.text.replace("/rem_roleipl", "").strip()
    # Known roles — match from end of input to support multi-word roles like "All Rounder"
    KNOWN_ROLES = ["All Rounder", "Captain", "WK", "Top", "Middle", "Finisher", "Pacer", "Spinner", "Fielder"]
    role_input, name = None, None
    text_lower = text.lower()
    for role in sorted(KNOWN_ROLES, key=len, reverse=True):  # longest first
        if text_lower.endswith(role.lower()):
            role_input = role
            name = text[:len(text) - len(role)].strip()
            break
    if not role_input or not name:
        await update.message.reply_text("Usage: /rem_roleipl Name Role\nRoles: Captain, WK, Top, Middle, All Rounder, Finisher, Pacer, Spinner, Fielder")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    current = p.get("ipl_roles", [])
    found = next((r for r in current if r.lower() == role_input.lower()), None)
    if found:
        current.remove(found)
        p["ipl_roles"] = current
        await save_player(p)
        await update.message.reply_text(f"✅ Removed {found} from {p['name']} (IPL).")
    else:
        await update.message.reply_text(f"⚠️ Role {role_input} not found for {name} (IPL).")


async def add_role_test(update, context):
    if not await check_admin(update): return
    text = update.message.text.replace("/add_roletest", "").strip()
    KNOWN_ROLES = ["All Rounder", "Captain", "WK", "Top", "Middle", "Defence", "Pacer", "Spinner", "Fielder"]
    role_input, name = None, None
    text_lower = text.lower()
    for role in sorted(KNOWN_ROLES, key=len, reverse=True):
        if text_lower.endswith(role.lower()):
            role_input = role
            name = text[:len(text) - len(role)].strip()
            break
    if not role_input or not name:
        await update.message.reply_text("Usage: /add_roletest Name Role\nRoles: Captain, WK, Top, Middle, Defence, All Rounder, Pacer, Spinner, Fielder")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    current = p.get("test_roles", [])
    if role_input in current:
        await update.message.reply_text(f"Already has {role_input}.")
        return
    current.append(role_input)
    p["test_roles"] = current
    await save_player(p)
    await update.message.reply_text(f"✅ Added {role_input} to {p['name']} (Test).")


async def rem_role_test(update, context):
    if not await check_admin(update): return
    text = update.message.text.replace("/rem_roletest", "").strip()
    KNOWN_ROLES = ["All Rounder", "Captain", "WK", "Top", "Middle", "Defence", "Pacer", "Spinner", "Fielder"]
    role_input, name = None, None
    text_lower = text.lower()
    for role in sorted(KNOWN_ROLES, key=len, reverse=True):
        if text_lower.endswith(role.lower()):
            role_input = role
            name = text[:len(text) - len(role)].strip()
            break
    if not role_input or not name:
        await update.message.reply_text("Usage: /rem_roletest Name Role\nRoles: Captain, WK, Top, Middle, Defence, All Rounder, Pacer, Spinner, Fielder")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    current = p.get("test_roles", [])
    found = next((r for r in current if r.lower() == role_input.lower()), None)
    if found:
        current.remove(found)
        p["test_roles"] = current
        await save_player(p)
        await update.message.reply_text(f"✅ Removed {found} from {p['name']} (Test).")
    else:
        await update.message.reply_text(f"⚠️ Role {role_input} not found for {name} (Test).")


async def rem_player_odi(update, context):
    """/rem_playerodi Name — removes ODI stats from a player"""
    if not await check_admin(update): return
    name = update.message.text.replace("/rem_playerodi", "").strip()
    if not name:
        await update.message.reply_text("Usage: /rem_playerodi Player Name")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    stats = p.get("stats", {})
    if "odi" not in stats:
        await update.message.reply_text(f"{p['name']} has no ODI stats.")
        return
    del stats["odi"]
    p["stats"] = stats
    await save_player(p)
    await update.message.reply_text(f"✅ ODI stats removed from {p['name']}. They are no longer in ODI pool.")


async def rem_player_test(update, context):
    """/rem_playertest Name — removes Test stats from a player"""
    if not await check_admin(update): return
    name = update.message.text.replace("/rem_playertest", "").strip()
    if not name:
        await update.message.reply_text("Usage: /rem_playertest Player Name")
        return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    stats = p.get("stats", {})
    if "test" not in stats:
        await update.message.reply_text(f"{p['name']} has no Test stats.")
        return
    del stats["test"]
    p["stats"] = stats
    p["test_roles"] = []
    await save_player(p)
    await update.message.reply_text(f"✅ Test stats removed from {p['name']}. They are no longer in Test pool.")


async def add_player_test(update, context):
    """/add_playertest name=X roles=A,B image=URL
    
    Adds or updates a player's Test mode entry.
    Roles: Captain, WK, Top, Middle, Defence, All Rounder, Pacer, Spinner, Fielder
    Use /setstats name=X format=test ... to set stats separately.
    """
    if not await check_admin(update): return
    import re
    text = update.message.text.replace("/add_playertest", "").strip()
    # Allow bare name if no key= prefix given
    if not re.match(r"^(name|roles|image)\s*=", text, re.IGNORECASE):
        text = "name=" + text
    text += " "
    pattern = r"(name|roles|image)\s*=\s*(.*?)(?=\s+(?:name|roles|image)\s*=|$) "[:-1]
    parsed = {m.group(1).lower(): m.group(2).strip()
              for m in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL)}
    if "name" not in parsed:
        await update.message.reply_text(
            "Usage: /add_playertest name=X roles=A,B image=URL\n"
            "Roles: Captain, WK, Top, Middle, Defence, All Rounder, Pacer, Spinner, Fielder"
        )
        return
    name = parsed["name"]
    image_url = parsed.get("image", "")

    # Parse roles — same valid set as Test positions
    VALID_TEST_ROLES = {"Captain", "WK", "Top", "Middle", "Defence", "All Rounder", "Pacer", "Spinner", "Fielder"}
    role_aliases = {
        "cap": "Captain", "keeper": "WK", "wk": "WK", "ar": "All Rounder",
        "allrounder": "All Rounder", "pace": "Pacer", "spin": "Spinner",
        "def": "Defence", "defence": "Defence", "field": "Fielder"
    }
    valid_map = {r.lower(): r for r in VALID_TEST_ROLES}
    raw_roles = [r.strip() for r in parsed.get("roles", "").split(",") if r.strip()]
    final_roles = [valid_map.get(r.lower(), role_aliases.get(r.lower(), r)) for r in raw_roles]
    final_roles = [r for r in final_roles if r in VALID_TEST_ROLES]

    from database import get_player_by_name, save_player
    import re as _re
    p = await get_player_by_name(name)
    if not p:
        slug = _re.sub(r'[^a-z0-9]', '_', name.lower()).strip('_')
        p = {
            "player_id": f"TEST_{slug.upper()[:12]}",
            "name": name, "sport": "cricket",
            "roles": [], "ipl_roles": [], "test_roles": [],
            "stats": {}
        }
    p.setdefault("stats", {})
    p.setdefault("test_roles", [])
    if final_roles:
        p["test_roles"] = final_roles
    if image_url:
        p["test_image_url"] = image_url
    if "test" not in p["stats"]:
        # Placeholder stats — admin must run /setstats to fill in values
        p["stats"]["test"] = {
            "leadership": 0, "wicket_keeping": 0, "batting_power": 0,
            "batting_control": 0, "batting_defence": 0, "all_round": 0,
            "bowling_pace": 0, "bowling_spin": 0, "fielding": 0
        }
    await save_player(p)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    pid = p["player_id"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Generate Stats (Test)", callback_data=f"gen_test_{pid}")]])
    roles_txt = esc(", ".join(final_roles) if final_roles else "None (use /add_roletest to add)")
    await update.message.reply_text(
        f"✅ Test player *{esc(name)}* added/updated.\nRoles: {roles_txt}\n"
        f"Use `/setstats name={esc(name)} format=test cap=80 ...` to set stats.",
        reply_markup=kb, parse_mode="Markdown"
    )




async def update_image_command(update, context):
    """/update_image Name [format=ipl|odi|test] URL"""
    if not await check_admin(update): return
    import re
    text = update.message.text.replace("/update_image", "").strip()
    fmt_m = re.search(r"format=(ipl|odi|test)", text, re.IGNORECASE)
    target_format = fmt_m.group(1).lower() if fmt_m else "odi"
    clean = re.sub(r"format=(ipl|odi|test)", "", text, flags=re.IGNORECASE).strip()
    parts = clean.rsplit(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /update_image Name format=[ipl|odi|test] URL")
        return
    name, url = parts[0].strip(), parts[1].strip()
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text("Player not found.")
        return
    try:
        msg = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=f"Updated {target_format.upper()} image for {p['name']}")
        fid = msg.photo[-1].file_id
        if target_format == "ipl":
            p["ipl_image_file_id"] = fid
        elif target_format == "test":
            p["test_image_url"] = url
        else:
            p["image_file_id"] = fid
        await save_player(p)
        await update.message.reply_text(f"✅ Image updated for *{esc(p['name'])}*.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


async def enable_ipl_command(update, context):
    from database import get_db
    get_db().system_config.update_one({"key": "ipl_mode"}, {"$set": {"enabled": True}}, upsert=True)
    await update.message.reply_text("✅ IPL Mode ENABLED.")


async def disable_ipl_command(update, context):
    from database import get_db
    get_db().system_config.update_one({"key": "ipl_mode"}, {"$set": {"enabled": False}}, upsert=True)
    await update.message.reply_text("⛔ IPL Mode DISABLED.")


async def player_list_ipl(update, context):
    """/playerlist_ipl — Paginated IPL player list."""
    if not await check_admin(update): return
    from database import get_all_players
    players = await get_all_players()
    ipl = [p for p in players if p.get("ipl_roles")]
    if not ipl:
        await update.message.reply_text("No IPL players found.")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    lines = [f"🏆 *IPL Pool* ({len(ipl)} players)"]
    for p in ipl[:30]:
        roles = ", ".join(p.get("ipl_roles", [])[:2])
        lines.append(f"• {esc(p['name'])} — {roles}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
# BANNER & BROADCAST
# ═══════════════════════════════════════════════════════════════════

async def handle_banner(update, context):
    """/banner <mode> <url>  (mode: ipl|odi|test|fifa|wwe|all)"""
    if not await check_admin(update): return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/banner <mode> <url>`\nModes: ipl · odi · test · fifa · wwe · all",
            parse_mode="Markdown"
        )
        return
    mode, url = args[0].lower(), args[1].strip()
    valid_modes = {"ipl", "odi", "test", "fifa", "wwe", "all"}
    if mode not in valid_modes:
        await update.message.reply_text(f"❌ Invalid mode `{mode}`.", parse_mode="Markdown")
        return
    from database import set_banner
    if mode == "all":
        for m in ("ipl", "odi", "test", "fifa", "wwe"):
            await set_banner(m, url)
        await update.message.reply_text(f"✅ All banners updated!", parse_mode="Markdown")
    else:
        await set_banner(mode, url)
        await update.message.reply_text(f"✅ `{mode.upper()}` banner updated!", parse_mode="Markdown")


async def get_current_banner(mode: str) -> str:
    from database import get_banner
    from config import DRAFT_BANNER_IPL, DRAFT_BANNER_ODI, DRAFT_BANNER_TEST, DRAFT_BANNER_FIFA, DRAFT_BANNER_WWE
    defaults = {"ipl": DRAFT_BANNER_IPL, "odi": DRAFT_BANNER_ODI, "test": DRAFT_BANNER_TEST, "intl": DRAFT_BANNER_ODI, "fifa": DRAFT_BANNER_FIFA, "wwe": DRAFT_BANNER_WWE}
    override = await get_banner(mode)
    return override if override else defaults.get(mode, DRAFT_BANNER_ODI)


async def handle_broadcast(update, context):
    """/broadcast message"""
    if not await check_admin(update): return
    msg = update.message.text.replace("/broadcast", "").strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast [message]")
        return
    from database import get_all_chats, get_db
    chats = await get_all_chats()
    if not chats:
        await update.message.reply_text("❌ No active chats found.")
        return
    status = await update.message.reply_text(f"📢 Broadcasting to {len(chats)} chats...")
    import asyncio, re
    from telegram.error import Forbidden
    async def _broadcast():
        success = failed = 0
        html = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", msg)
        text_out = f"📢 <b>Announcement</b>\n\n{html}"
        for chat_id in chats:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text_out, parse_mode="HTML")
                success += 1
                await asyncio.sleep(0.05)
            except Forbidden:
                await get_db().chats.delete_one({"chat_id": chat_id})
                failed += 1
            except Exception as e:
                logger.warning(f"Broadcast fail {chat_id}: {e}")
                failed += 1
        try:
            await status.edit_text(f"✅ Broadcast done. Sent: {success} ✅  Failed: {failed} ❌")
        except Exception:
            pass
    asyncio.ensure_future(_broadcast())


# ═══════════════════════════════════════════════════════════════════
# FIFA IMAGE / REMOVE
# ═══════════════════════════════════════════════════════════════════

async def update_image_fifa(update, context):
    """/update_imagefifa Name URL"""
    if not await check_admin(update): return
    text = (update.message.caption or update.message.text or "").replace("/update_imagefifa", "").strip()
    photo_fid = None
    if update.message.photo:
        photo_fid = update.message.photo[-1].file_id
    name = text
    if not photo_fid:
        parts = text.split()
        if parts and parts[-1].startswith("http"):
            url = parts[-1]
            name = " ".join(parts[:-1])
            try:
                m = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=f"Updated: {name}")
                photo_fid = m.photo[-1].file_id
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to load image: {e}")
                return
    from database import get_player_by_name, save_player
    p = await get_player_by_name(name)
    if not p:
        await update.message.reply_text(f"❌ Player not found: {name}")
        return
    if photo_fid:
        p["image_file_id"] = photo_fid
        await save_player(p)
        await update.message.reply_text(f"✅ Updated FIFA image for *{esc(p['name'])}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ No photo found. Attach a photo or provide URL.")


async def remove_player_fifa(update, context):
    """/removeplayerfifa Name or ID"""
    if not await check_admin(update): return
    text = update.message.text.replace("/removeplayerfifa", "").strip()
    if not text:
        await update.message.reply_text("Usage: /removeplayerfifa Name")
        return
    from database import delete_player
    deleted = await delete_player(text)
    if deleted:
        await update.message.reply_text(f"✅ Removed '{text}'.")
    else:
        await update.message.reply_text(f"❌ '{text}' not found.")
