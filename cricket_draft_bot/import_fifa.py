import csv
import logging
from database import get_db, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CSV_FILE = "players_fifa22.csv"

def import_fifa_players():
    init_db()
    db = get_db()
    collection = db.players
    
    logger.info(f"Reading {CSV_FILE}...")
    
    count = 0
    updated = 0
    
    try:
        with open(CSV_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                # Map fields
                player_id = f"fifa_{row['ID']}"
                
                # Parse positions string "RW,ST,CF" -> ["RW", "ST", "CF"]
                positions_str = row.get('Positions', '')
                positions = [p.strip() for p in positions_str.split(',')] if positions_str else []
                
                # Stats mapping
                stats = {
                    "ST": int(row.get('STRating') or 0) if row.get('STRating').isdigit() else 0,
                    "LW": int(row.get('LWRating') or 0) if row.get('LWRating').isdigit() else 0,
                    "CF": int(row.get('CFRating') or 0) if row.get('CFRating').isdigit() else 0,
                    "RW": int(row.get('RWRating') or 0) if row.get('RWRating').isdigit() else 0,
                    "CAM": int(row.get('CAMRating') or 0) if row.get('CAMRating').isdigit() else 0,
                    "CM": int(row.get('CMRating') or 0) if row.get('CMRating').isdigit() else 0,
                    "LB": int(row.get('LBRating') or 0) if row.get('LBRating').isdigit() else 0,
                    "CB": int(row.get('CBRating') or 0) if row.get('CBRating').isdigit() else 0,
                    "RB": int(row.get('RBRating') or 0) if row.get('RBRating').isdigit() else 0,
                    "GK": int(row.get('GKRating') or 0) if row.get('GKRating').isdigit() else 0,
                }
                
                overall = int(row['Overall'])
                
                player_doc = {
                    "player_id": player_id,
                    "name": row['Name'],
                    "full_name": row['FullName'],
                    "sport": "football",
                    "mode": "fifa",
                    "overall": overall,
                    "position": row['BestPosition'],
                    "positions": positions,
                    "stats": {
                        "fifa": stats 
                    },
                    "fifa_image_url": row['PhotoUrl'],
                    "image_file_id": None 
                }
                
                # OPTIMIZATION: Only import if Overall > 80
                if overall <= 80:
                    continue

                # Upsert
                collection.update_one(
                    {"player_id": player_id},
                    {"$set": player_doc},
                    upsert=True
                )
                count += 1
                if count % 1000 == 0:
                    logger.info(f"Processed {count} players...")
                    
        logger.info(f"Import complete. Total processed: {count}")
        
    except Exception as e:
        logger.error(f"Import failed: {e}")

if __name__ == "__main__":
    import_fifa_players()
