# utils/images.py
import requests
import logging

logger = logging.getLogger(__name__)

def download_image(url: str, save_path: str) -> bool:
    """
    Downloads an image from a URL and saves it locally.
    Returns True if successful, False otherwise.
    """
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Failed to download image: {e}")
        return False
