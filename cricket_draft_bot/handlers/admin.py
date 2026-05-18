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

async def generate_player_stats(player_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False, mode='intl'):
    """
    Shared logic to generate stats.
    mode: 'intl' or 'ipl'
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

    await generate_player_stats(player_id, update, context, is_callback=False, mode='intl')

async def handle_gen_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # gen_intl_ID
    if not await can_manage_bot(update.effective_user.id):
        await query.answer("⛔ Admin Only", show_alert=True)
        return
    await generate_player_stats(player_id, update, context, is_callback=True, mode='intl')

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
        
    await generate_player_stats(player_id, update, context, is_callback=True, mode='intl')

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

    from database import get_player_by_name, get_player_by_name_and_sport
    p = (await get_player_by_name_and_sport(text, sport_filter)
         if sport_filter else await get_player_by_name(text))

    if not p:
        hint = f" (sport={sport_filter})" if sport_filter else ""
        await update.message.reply_text(
            f"❌ Player matching `{esc(text)}`{hint} not found.", parse_mode="Markdown"
        )
        return

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

    intl_display = format_stats(stats.get('international', {}))

    msg = (
        f"📊 *{esc(p['name'])}*\n"
        f"ID: `{p['player_id']}`\n\n"
        f"*International Stats*\n{intl_display}\n\n"
        f"Roles: {esc(', '.join(roles))}\n"
        f"Source: {p.get('api_reference', {}).get('provider', 'Unknown')}"
    )

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏏 IPL Stats", callback_data=f"view_ipl_{p['player_id']}")
    ]])

    if p.get('image_file_id'):
        try:
            await update.message.reply_photo(
                photo=p['image_file_id'], caption=msg,
                reply_markup=kb, parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Photo send failed for {p['name']}: {e}")
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="Markdown")
async def handle_view_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # view_intl_ID
    
    from database import get_player
    p = await get_player(player_id)
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
        parts.append(f"🧤 WK: {data.get('wicket_keeping')}") # Added WK stat line
        parts.append(f"⚡ Pace: {data.get('bowling_pace')}")
        parts.append(f"🌀 Spin: {data.get('bowling_spin')}")
        parts.append(f"✨ All: {data.get('all_round')}")
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)
        
    stats_display = format_stats_local(stats)
    
    caption = f"📊 *Stats for {esc(p['name'])}*\n(International)\n\n{stats_display}\n\nRoles: {', '.join(roles)}"
    
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
    p = await get_player(player_id)
    if not p: return

    # Intl Default
    stats = p.get('stats', {}).get('international', {})
    # if not stats: stats = p.get('stats', {}) # Fallback REMOVED to prevent ghost stats!
    
    intl_img = p.get('image_file_id')
    roles = p.get('roles', [])
        
    def format_stats_local(data):
        if not data: return "N/A"
        if isinstance(data, int): return str(data)
        parts = []
        
        def g(k): return data.get(k) if data.get(k) is not None else "N/A"

        parts.append(f"🧠 Cap: {g('leadership')}")
        parts.append(f"🏏 Top: {g('batting_power')}")
        parts.append(f"🛡️ Mid: {g('batting_control')}")
        # Show all relevant for Intl
        parts.append(f"💥 Fin: {g('finishing')}")
        parts.append(f"⚡ Pace: {g('bowling_pace')}")
        parts.append(f"🌀 Spin: {g('bowling_spin')}")
        parts.append(f"✨ All: {g('all_round')}")
        parts.append(f"👟 Field: {g('fielding')}")
        return "\n".join(parts)
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)
        
    stats_display = format_stats_local(stats)
    
    caption = f"📊 *Stats for {esc(p['name'])}*\n(International)\n\n{stats_display}\n\nRoles: {', '.join(roles)}"
    
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
    p = await get_player_by_name(player_name)
    
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
    """/setstats Name [sport=wwe|cricket] [format=all|ipl|intl] stat=value ..."""
    if not await check_admin(update): return

    text = update.message.text.replace('/setstats', '').strip()
    if not text:
        await update.message.reply_text(
            "**Usage:**\n"
            "`/setstats Name format=ipl cap=90 top=85`\n"
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

    from database import get_player_by_name, get_player_by_name_and_sport, save_player
    p = (await get_player_by_name_and_sport(player_name, sport_filter)
         if sport_filter else await get_player_by_name(player_name))

    if not p:
        hint = f" (sport={sport_filter})" if sport_filter else ""
        await update.message.reply_text(f"❌ Player '{player_name}'{hint} not found.")
        return

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
        if target_format not in ['ipl', 'intl', 'all']:
            await update.message.reply_text("❌ format must be `ipl`, `intl`, or `all`.", parse_mode="Markdown")
            return
        modes = ['ipl', 'international'] if target_format == 'all' else [
            'ipl' if target_format == 'ipl' else 'international'
        ]
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
            "Cricket: `/check middle intl` · `/check wk ipl`\n"
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
        if mode == 'intl': db_mode = 'international'
        elif mode == 'ipl': db_mode = 'ipl'
        else:
            err = "Mode must be `ipl`, `intl`, or `wwe`."
            if cb_query: await cb_query.edit_message_text(err, parse_mode="Markdown")
            else:        await update.message.reply_text(err, parse_mode="Markdown")
            return

        from config import ROLE_STATS_MAP
        alias_map = {
            "hitting": "Top", "batting": "Top", "pace": "Pacer",
            "spin": "Spinner", "all": "All Rounder", "allrounder": "All Rounder",
            "field": "Fielder", "fielding": "Fielder", "def": "Defence",
            "defence": "Defence", "middle": "Middle", "top": "Top",
            "wk": "WK", "cap": "Captain", "captain": "Captain",
            "fin": "Finisher", "finisher": "Finisher",
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

async def generate_player_stats(player_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False, mode='intl'):
    """
    Shared logic to generate stats.
    mode: 'intl' or 'ipl'
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

    await generate_player_stats(player_id, update, context, is_callback=False, mode='intl')

async def handle_gen_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # gen_intl_ID
    if not await can_manage_bot(update.effective_user.id):
        await query.answer("⛔ Admin Only", show_alert=True)
        return
    await generate_player_stats(player_id, update, context, is_callback=True, mode='intl')

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
        
    await generate_player_stats(player_id, update, context, is_callback=True, mode='intl')

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

    from database import get_player_by_name, get_player_by_name_and_sport
    p = (await get_player_by_name_and_sport(text, sport_filter)
         if sport_filter else await get_player_by_name(text))

    if not p:
        hint = f" (sport={sport_filter})" if sport_filter else ""
        await update.message.reply_text(
            f"❌ Player matching `{esc(text)}`{hint} not found.", parse_mode="Markdown"
        )
        return

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

    intl_display = format_stats(stats.get('international', {}))

    msg = (
        f"📊 *{esc(p['name'])}*\n"
        f"ID: `{p['player_id']}`\n\n"
        f"*International Stats*\n{intl_display}\n\n"
        f"Roles: {esc(', '.join(roles))}\n"
        f"Source: {p.get('api_reference', {}).get('provider', 'Unknown')}"
    )

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏏 IPL Stats", callback_data=f"view_ipl_{p['player_id']}")
    ]])

    if p.get('image_file_id'):
        try:
            await update.message.reply_photo(
                photo=p['image_file_id'], caption=msg,
                reply_markup=kb, parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Photo send failed for {p['name']}: {e}")
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="Markdown")
async def handle_view_intl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    player_id = query.data.split('_', 2)[2] # view_intl_ID
    
    from database import get_player
    p = await get_player(player_id)
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
        parts.append(f"🧤 WK: {data.get('wicket_keeping')}") # Added WK stat line
        parts.append(f"⚡ Pace: {data.get('bowling_pace')}")
        parts.append(f"🌀 Spin: {data.get('bowling_spin')}")
        parts.append(f"✨ All: {data.get('all_round')}")
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)
        
    stats_display = format_stats_local(stats)
    
    caption = f"📊 *Stats for {esc(p['name'])}*\n(International)\n\n{stats_display}\n\nRoles: {', '.join(roles)}"
    
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
    p = await get_player(player_id)
    if not p: return

    # Intl Default
    stats = p.get('stats', {}).get('international', {})
    # if not stats: stats = p.get('stats', {}) # Fallback REMOVED to prevent ghost stats!
    
    intl_img = p.get('image_file_id')
    roles = p.get('roles', [])
        
    def format_stats_local(data):
        if not data: return "N/A"
        if isinstance(data, int): return str(data)
        parts = []
        
        def g(k): return data.get(k) if data.get(k) is not None else "N/A"

        parts.append(f"🧠 Cap: {g('leadership')}")
        parts.append(f"🏏 Top: {g('batting_power')}")
        parts.append(f"🛡️ Mid: {g('batting_control')}")
        # Show all relevant for Intl
        parts.append(f"💥 Fin: {g('finishing')}")
        parts.append(f"⚡ Pace: {g('bowling_pace')}")
        parts.append(f"🌀 Spin: {g('bowling_spin')}")
        parts.append(f"✨ All: {g('all_round')}")
        parts.append(f"👟 Field: {g('fielding')}")
        return "\n".join(parts)
        parts.append(f"👟 Field: {data.get('fielding')}")
        return "\n".join(parts)
        
    stats_display = format_stats_local(stats)
    
    caption = f"📊 *Stats for {esc(p['name'])}*\n(International)\n\n{stats_display}\n\nRoles: {', '.join(roles)}"
    
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
    p = await get_player_by_name(player_name)
    
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
    """/setstats Name [sport=wwe|cricket] [format=all|ipl|intl] stat=value ..."""
    if not await check_admin(update): return

    text = update.message.text.replace('/setstats', '').strip()
    if not text:
        await update.message.reply_text(
            "**Usage:**\n"
            "`/setstats Name format=ipl cap=90 top=85`\n"
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

    from database import get_player_by_name, get_player_by_name_and_sport, save_player
    p = (await get_player_by_name_and_sport(player_name, sport_filter)
         if sport_filter else await get_player_by_name(player_name))

    if not p:
        hint = f" (sport={sport_filter})" if sport_filter else ""
        await update.message.reply_text(f"❌ Player '{player_name}'{hint} not found.")
        return

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
        if target_format not in ['ipl', 'intl', 'all']:
            await update.message.reply_text("❌ format must be `ipl`, `intl`, or `all`.", parse_mode="Markdown")
            return
        modes = ['ipl', 'international'] if target_format == 'all' else [
            'ipl' if target_format == 'ipl' else 'international'
        ]
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





# ═══════════════════════════════════════════════════════════════════
# WWE ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════

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
