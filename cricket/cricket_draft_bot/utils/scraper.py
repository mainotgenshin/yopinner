import httpx
from bs4 import BeautifulSoup
import logging
import random
import re

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0',
}

def normalize_stat(value, min_val, max_val):
    if value < min_val: return 50
    if value > max_val: return 99
    # Linear map: (val - min) / (max - min) * (99 - 50) + 50
    pct = (value - min_val) / (max_val - min_val)
    return int(50 + (pct * 49))

def get_deterministic_stats(player_name, roles):
# ... (rest of get_deterministic_stats is assumed safe if unchanged) ...
    random.seed(player_name.lower())
    is_bowler = "Spin" in roles or "Pace" in roles
    is_batter = "Hitting" in roles or "Captain" in roles or "WK" in roles
    
    bat_avg_ipl = random.randint(25, 40) if is_batter else random.randint(5, 15)
    bat_sr_ipl = random.randint(130, 160) if is_batter else random.randint(80, 110)
    bowl_avg = random.randint(20, 30) if is_bowler else random.randint(40, 60)
    
    bat_pow = normalize_stat(bat_sr_ipl, 120, 160)
    bat_ctrl = normalize_stat(bat_avg_ipl, 25, 50)
    bowl_rat = normalize_stat(40 - bowl_avg, 10, 25)
    
    leadership = random.randint(80, 95) if "Captain" in roles else random.randint(40, 70)
    
    return {
        "ipl": {
            "leadership": leadership,
            "batting_power": bat_pow,
            "batting_control": bat_ctrl,
            "bowling_pace": bowl_rat if "Pace" in roles else 20,
            "bowling_spin": bowl_rat if "Spin" in roles else 20,
            "all_round": (bat_pow + bowl_rat) // 2,
            "finishing": int((bat_pow * 0.4) + (bat_ctrl * 0.2) + (random.randint(70, 95) * 0.4)), # High clutch built-in
            "fielding": random.randint(60, 90),
            "clutch": random.randint(70, 95),
            "wicket_keeping": bat_ctrl # Initialize with Batting Control (High for WKs usually)
        },
        "international": {
            "leadership": leadership,
            "batting_power": bat_pow - 5,
            "batting_control": bat_ctrl + 5,
            "bowling_pace": bowl_rat if "Pace" in roles else 20,
            "bowling_spin": bowl_rat if "Spin" in roles else 20,
            "all_round": (bat_pow + bowl_rat) // 2,
            "finishing": int((bat_pow * 0.4) + (bat_ctrl * 0.2) + (random.randint(70, 95) * 0.4)),
            "fielding": random.randint(60, 90),
            "clutch": random.randint(70, 95),
            "wicket_keeping": bat_ctrl + 5 # International boost same as batting control
        },
        "source": "Deterministic (Seeded)"
    }

async def scrape_player_stats(player_name: str, roles: list) -> dict:
    """
    Attempts to fetch real stats from Wikipedia.
    Falls back to deterministic generation if failed.
    """
    stats_found = {}
    soup = None # Initialize soup safely
    
    try:
        search_term = player_name.replace("_", " ") # Ensure spaces for search
        search_url = "https://en.wikipedia.org/w/index.php"
        params = {'search': search_term, 'title': 'Special:Search', 'go': 'Go'}
        
        async with httpx.AsyncClient(headers=HEADERS, timeout=10.0, follow_redirects=True) as client:

            try:
                # Allow redirects!
                r = await client.get(search_url, params=params)
                
                # Check if we landed on a valid page
                final_url = str(r.url) # httpx URL object to string
                
                if "Special:Search" not in final_url and "Wikipedia does not have an article" not in r.text:
                    if "cricketer" in r.text.lower() or "cricket" in r.text.lower():
                        soup = BeautifulSoup(r.text, 'html.parser')
                        stats_found["source"] = f"Wikipedia (via Search)"
                else:
                     # Search failed, try appending "cricketer" to search?
                     params['search'] = search_term + " cricketer"
                     r = await client.get(search_url, params=params)
                     if "Special:Search" not in str(r.url) and "cricket" in r.text.lower():
                          soup = BeautifulSoup(r.text, 'html.parser')
                          stats_found["source"] = f"Wikipedia (via Search+)"
                
            except Exception as e:
                logger.error(f"Search Request Failed: {e}")
                soup = None



                
        if soup:
            # Remove class filter to find ALL tables (some stats tables aren't wikitables)
            tables = soup.find_all('table')
            
            def parse_val(text):
                # Handle scientific notation or huge numbers gracefully
                try:
                    # Filter out purely non-numeric junk but keep 'e' for scientific if present (unlikely in cricket stats but defensive)
                    # Actually, for cricket stats, 'e' is garbage.
                    clean = re.sub(r'[^\d.]', '', text) 
                    val = float(clean)
                    if val > 600: return 0.0 # Sanity Cap for any stat
                    return val
                except:
                    return 0.0

            debug_reasons = []
            
            for table in tables:
                headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
                
                # Column Indexing
                idx_avg = -1
                idx_sr = -1
                idx_bowl_avg = -1
                
                # Regular Indexing
                for i, h_raw in enumerate(headers):
                    h = h_raw.replace('.', '') # Handle Ave. or S.R.
                    if h in ['ave', 'avg', 'average']: idx_avg = i
                    elif h.startswith('av'): idx_avg = i
                    
                    if h in ['sr', 's/r', 'st', 'strike rate', 'strikerate']: idx_sr = i
                    elif h.startswith('sr'): idx_sr = i

                    if h in ['ave', 'avg', 'average']: idx_bowl_avg = i
                
                is_stats_candidate = (idx_avg != -1 or idx_sr != -1)
                
                # ALSO Check for Transposed / Summary Table (Format in Header like 'T20I')
                idx_t20_col = -1
                for i, h in enumerate(headers):
                    if 't20' in h or 'ipl' in h:
                        idx_t20_col = i
                        is_stats_candidate = True # Enable looking inside
                        break
                
                # Debug info
                if headers: 
                    # Dump full headers to identify mismatch
                    debug_reasons.append(f"{headers}")

                if is_stats_candidate:
                    # Check for Transposed / Summary Table (Format in Header)
                    idx_t20_col = -1
                    for i, h in enumerate(headers):
                        if 't20' in h or 'ipl' in h:
                            idx_t20_col = i
                            break
                    
                    if idx_t20_col != -1:
                        rows = table.find_all('tr')
                        for row in rows:
                            cells = row.find_all(['td', 'th'])
                            if not cells: continue
                            
                            label = cells[0].get_text(strip=True).lower().replace('.', '')
                            val = 0.0
                            if len(cells) > idx_t20_col:
                                val = parse_val(cells[idx_t20_col].get_text(strip=True))
                            
                            if val > 0:
                                if label in ['ave', 'avg', 'average', 'bowling average']:
                                    if 10 < val < 60: stats_found["bowl_avg"] = val
                                    if 10 < val < 100: stats_found["avg_ipl"] = val
                                    
                                if label in ['sr', 'strike rate', 'strikerate']:
                                    if 50 < val < 400: stats_found["sr_ipl"] = val

                    # Regular Row-Based Table Logic (Only run if missing stats)
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['td', 'th'])
                        # Skip header rows (len(cells) usually matches headers)
                        if len(cells) < 3: continue
                        
                        row_text = row.get_text(strip=True).lower()
                        
                        # PRIORITY: T20I > T20 > IPL
                        if "t20" in row_text or "ipl" in row_text:
                            # Extract using indices
                            # Note: indices might need adjustment if row has 'th' (row header) that offsets 'td'
                            # Usually wikitable rows are: [th(Year), td(Mat), td(Runs)...] OR [td(Year)...]
                            # Safest is to map by index but watch out for colspan. ignoring colspan for now.
                            
                            current_vals = [c.get_text(strip=True) for c in cells]
                            
                            # Helper to safely get value by index
                            def get_idx_val(idx, row_vals):
                                if 0 <= idx < len(row_vals):
                                    return parse_val(row_vals[idx])
                                return 0.0

                            # Try to get stats
                            v_avg = get_idx_val(idx_avg, current_vals) if idx_avg != -1 else 0
                            v_sr = get_idx_val(idx_sr, current_vals) if idx_sr != -1 else 0
                            
                            # Heuristic: If we found valid-looking stats, lock them in
                            if 50 < v_sr < 400: 
                                stats_found["sr_ipl"] = v_sr
                            if v_avg > 5:
                                stats_found["avg_ipl"] = v_avg

                    if "t20" not in str(rows).lower():
                         pass
                else:
                    pass

                # Bowling Check (Inside Loop)
                # Look for 'wkts'/'wickets' AND 'ave'/'avg'
                idx_bowl_avg = -1
                for i, h in enumerate(headers):
                    if h in ['ave', 'avg', 'average']: idx_bowl_avg = i
                
                kw_wkts = ['wkts', 'wickets', 'w']
                has_wkts = any(x in headers for x in kw_wkts)
                
                if has_wkts and idx_bowl_avg != -1:
                     rows = table.find_all('tr')
                     for row in rows:
                        cells = row.find_all(['td', 'th'])
                        if len(cells) < 3: continue
                        
                        row_text = row.get_text(strip=True).lower()
                        if "t20" in row_text or "ipl" in row_text:
                             # Extract specific column
                             current_vals = [c.get_text(strip=True) for c in cells]
                             v_bowl_avg = get_idx_val(idx_bowl_avg, current_vals)
                             
                             # Heuristic for Bowling Avg (usually 15-40 for good players, up to 60)
                             if 10 < v_bowl_avg < 60:
                                 stats_found["bowl_avg"] = v_bowl_avg
            
            # After Loop: Check if we found anything
            if not stats_found.get('avg_ipl') and not stats_found.get('bowl_avg'):
                 if debug_reasons:
                     stats_found['debug'] = "; ".join(debug_reasons[:10]) # Expanded limit
                 else:
                     stats_found['debug'] = "No valid stats tables matched"

    except Exception as e:
        logger.error(f"Scraper Error: {e}")

    # Merge
    random.seed(player_name.lower())
    is_bowler = "Spin" in roles or "Pace" in roles
    is_batter = "Hitting" in roles or "Captain" in roles or "WK" in roles
    
    # Defaults (Seeded) - TUNED HIGHER
    bat_avg_ipl = random.randint(30, 45) if is_batter else random.randint(10, 20)
    bat_sr_ipl = random.randint(135, 165) if is_batter else random.randint(90, 120)
    bowl_avg = random.randint(18, 28) if is_bowler else random.randint(45, 60)
    
    # Override
    if "avg_ipl" in stats_found: bat_avg_ipl = stats_found["avg_ipl"]
    if "sr_ipl" in stats_found: bat_sr_ipl = stats_found["sr_ipl"]
    if "bowl_avg" in stats_found and is_bowler: bowl_avg = stats_found["bowl_avg"]
    
    bat_pow = normalize_stat(bat_sr_ipl, 120, 160)
    bat_ctrl = normalize_stat(bat_avg_ipl, 25, 45)
    
    if bowl_avg < 15: bowl_rat = 95
    elif bowl_avg > 50: bowl_rat = 40
    else:
        pct = (bowl_avg - 15) / (50 - 15)
        bowl_rat = int(95 - (pct * 55))
        
    leadership = random.randint(80, 95) if "Captain" in roles else random.randint(40, 70)
    
    source_label = stats_found.get("source", "Seeded Engine")
    
    # Validation: Only claim Wikipedia if we actually used the numbers
    if "avg_ipl" not in stats_found and "sr_ipl" not in stats_found and "bowl_avg" not in stats_found:
        if "Wikipedia" in source_label:
            debug_info = stats_found.get('debug', 'Unknown reason')
            # Truncate debug info to avoid Telegram Message Capture Too Long error
            # If it's a huge dump of headers, we just want a snippet
            if len(debug_info) > 100:
                debug_info = debug_info[:100] + "... (Truncated)"
                
            source_label = f"Seeded (Wiki Found - Unrecognized Table Format: {debug_info})"
    else:
        # Success Debug Check
        raw_debug = []
        if "avg_ipl" in stats_found: raw_debug.append(f"Avg={stats_found['avg_ipl']}")
        if "sr_ipl" in stats_found: raw_debug.append(f"SR={stats_found['sr_ipl']}")
        if "bowl_avg" in stats_found: raw_debug.append(f"BowlAvg={stats_found['bowl_avg']}")
        if raw_debug:
            source_label += f" [{', '.join(raw_debug)}]"
    
    return {
        "source_label": source_label, 
        "ipl": {
            "leadership": leadership,
            "batting_power": bat_pow,
            "batting_control": bat_ctrl,
            "bowling_pace": bowl_rat if "Pace" in roles else 20,
            "bowling_spin": bowl_rat if "Spin" in roles else 20,
            "all_round": (bat_pow + bowl_rat) // 2,
            "finishing": int((bat_pow * 0.4) + (bat_ctrl * 0.2) + (stats_found.get('sr_ipl', 135) * 0.1) + 20),
            "fielding": random.randint(60, 90),
            "clutch": random.randint(70, 95),
            "wicket_keeping": bat_ctrl
        },
        "international": {
            "leadership": leadership + 2,
            "batting_power": bat_pow - 5,
            "batting_control": bat_ctrl + 5,
            "bowling_pace": bowl_rat if "Pace" in roles else 20,
            "bowling_spin": bowl_rat if "Spin" in roles else 20,
            "all_round": (bat_pow + bowl_rat) // 2,
            "finishing": int((bat_pow * 0.4) + (bat_ctrl * 0.2) + (stats_found.get('sr_ipl', 135) * 0.1) + 15),
            "fielding": random.randint(60, 90),
            "clutch": random.randint(70, 95),
            "wicket_keeping": bat_ctrl + 5
        },
        "source": source_label
    }
