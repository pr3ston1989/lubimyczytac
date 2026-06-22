import os
import hashlib
import requests
from PIL import Image
from io import BytesIO
from loguru import logger
from typing import Optional, Dict, Any
import config

def download_cover(url: str, book_type: str, external_id: str) -> Optional[Dict[str, Any]]:
    if not url:
        return None
        
    try:
        response = requests.get(url, headers=config.get_random_headers(), timeout=10)
        response.raise_for_status()
        
        image_data = response.content
        size_bytes = len(image_data)
        sha256_hash = hashlib.sha256(image_data).hexdigest()
        
        # Okreslanie sciezki (books vs audiobooks)
        folder = "audiobooks" if book_type == "audiobook" else "books"
        filename = f"{external_id}_{sha256_hash[:12]}.jpg"
        save_dir = os.path.join("media", "covers", folder)
        local_path = os.path.join(save_dir, filename)
        
        # TWORZENIE FOLDERU (jesli nie istnieje) - zapobiega FileNotFoundError
        os.makedirs(save_dir, exist_ok=True)
        
        # Weryfikacja i pobieranie wymiarow
        img = Image.open(BytesIO(image_data))
        img = img.convert("RGB") # wymuszenie JPG
        width, height = img.size
        
        # Zapis fizyczny
        img.save(local_path, "JPEG")
        
        return {
            "source_url": url,
            "local_path": local_path,
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "sha256": sha256_hash
        }
    except Exception as e:
        # Bez polskich znakow, aby uniknac bledu kodowania w logach
        logger.error(f"Nie udalo sie pobrac okladki {url}: {e}")
        return None