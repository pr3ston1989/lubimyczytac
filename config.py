import os
import random
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/database.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MIN_DELAY = float(os.getenv("MIN_DELAY", 1.0))
MAX_DELAY = float(os.getenv("MAX_DELAY", 3.0))

# Maksymalna liczba stron paginacji generowanych z jednej listy (wydawnictwo,
# kategoria, autor, tag itd.). Chroni przed wygenerowaniem dziesiatek tysiecy
# adresow z bardzo duzych kategorii. Wartosc 0 = brak limitu (generuj wszystkie
# strony az do data-maxpage). Mozna nadpisac zmienna srodowiskowa MAX_LIST_PAGES.
MAX_LIST_PAGES = int(os.getenv("MAX_LIST_PAGES", 0))

# Rotujące User-Agenty
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
]

def get_random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://lubimyczytac.pl/",
    }

BASE_URL = "https://lubimyczytac.pl"