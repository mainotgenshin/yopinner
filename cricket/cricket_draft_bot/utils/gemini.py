# utils/gemini.py
import google.generativeai as genai
import logging
import json
import os
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# Configure API
if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_KEY_HERE":
    genai.configure(api_key=GEMINI_API_KEY)

async def generate_player_stats(player_name: str, roles: list) -> dict:
    """
    Generates realistic granular cricket stats for a given player using Gemini.
    """
    if not GEMINI_API_KEY or GEMINI_API_KEY.startswith("YOUR"):
        logger.warning("Gemini API Key missing")
        # Fallback to random if no key
        import random
        return {
            "ipl": {
                "leadership": random.randint(50, 90),
                "batting_power": random.randint(50, 90),
                "batting_control": random.randint(50, 90),
                "bowling_pace": random.randint(10, 40),
                "bowling_spin": random.randint(10, 40),
                "all_round": random.randint(40, 70),
                "fielding": random.randint(60, 90),
                "clutch": random.randint(50, 90)
            },
            "international": {
                "leadership": random.randint(50, 90),
                "batting_power": random.randint(50, 90),
                "batting_control": random.randint(50, 90),
                "bowling_pace": random.randint(10, 40),
                "bowling_spin": random.randint(10, 40),
                "all_round": random.randint(40, 70),
                "fielding": random.randint(60, 90),
                "clutch": random.randint(50, 90)
            }
        }

    try:
        # User has access to 2.0-flash
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        prompt = f"""
You are a cricket analyst AI generating fictional gameplay stats.

These stats are NOT real career statistics.
They are balanced, intuitive values for a strategy game.

PLAYER DETAILS:
Name: {player_name}
Primary Roles: {', '.join(roles)}

IMPORTANT CLARIFICATION:
"Clutch" is NOT a role or position.
It is an internal performance modifier only.

STAT GENERATION RULES (STRICT):
1. Stat range: 0–100
2. Do NOT use flat or average values (no 50s)
3. Player must have strong strengths (≥80) and clear weaknesses (≤40)
4. Bowling stats MUST be very low for non-bowlers
5. Captain role boosts leadership
6. Hitting role boosts batting_power (especially in IPL)
7. IPL favors aggression and impact
8. International favors control and pressure handling
9. IPL and International stats MUST differ meaningfully

GENERATE THESE STATS:
- leadership
- batting_power
- batting_control
- bowling_pace
- bowling_spin
- all_round
- fielding
- clutch (internal modifier only)

OUTPUT FORMAT (JSON ONLY):
{{
  "ipl": {{
    "leadership": number,
    "batting_power": number,
    "batting_control": number,
    "bowling_pace": number,
    "bowling_spin": number,
    "all_round": number,
    "fielding": number,
    "clutch": number
  }},
  "international": {{
    "leadership": number,
    "batting_power": number,
    "batting_control": number,
    "bowling_pace": number,
    "bowling_spin": number,
    "all_round": number,
    "fielding": number,
    "clutch": number
  }}
}}

If stats appear generic or averaged, regenerate them.
"""
        
        response = model.generate_content(prompt)
        text = response.text
        
        # Clean potential markdown code blocks
        if "```" in text:
            text = text.replace("```json", "").replace("```", "")
            
        data = json.loads(text.strip())
        return data
        
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        # Return fallback structure
        return {"error": str(e), "ipl": {}, "international": {}}
