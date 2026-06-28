import time
import re
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cureq
from loguru import logger
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn

import threading

import config
from database import get_session
from sqlalchemy import func
from models import Book, Author, Publisher, Series, Category, Cover, Review, ScrapeQueue
from parser import extract_book_info, extract_links, extract_reviews
from downloader import download_cover
from progress import add_to_queue, get_next_in_queue, mark_queue_status, mark_queue_failed, log_error, add_many_to_queue, get_batch_queue, mark_status

CACHE_DIR = "data/html_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

class Scraper:
    def __init__(self, mode="full-scan", max_workers=5):
        self.mode = mode
        self.max_workers = max_workers
        self.session = cureq.Session(impersonate="chrome120")
        # Adaptive throttle - wspoldzielony miedzy watkami
        self._throttle_lock = threading.Lock()
        self._delay_multiplier = 1.0  # mnoznik opoznienia (rosnie przy bledach)
        self._consecutive_errors = 0
        self._consecutive_ok = 0

    def fetch(self, url: str) -> str:
        delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
        time.sleep(delay)
        response = self.session.get(url, headers=config.get_random_headers(), timeout=15)
        if response.status_code == 404:
            raise ValueError("HTTP 404 - Strona nie istnieje")
        response.raise_for_status()
        return response.text

    def process_book_page(self, url: str, html: str, db_session):
        data = extract_book_info(html, url)
        if not data or not data.get('title'):
            raise ValueError("Nie udalo sie sparsowac detali")

        cache_path = os.path.join(CACHE_DIR, f"{data['type']}_{data['external_id']}.html")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            logger.warning(f"Nie udalo sie zapisac cache HTML: {e}")

        book = db_session.query(Book).filter_by(external_id=data['external_id']).first()
        if not book:
            try:
                book = Book(
                    external_id=data['external_id'], slug=data['slug'], url=data['url'], type=data['type'],
                    title=data['title'], original_title=data['original_title'], description=data['description'],
                    avg_rating=data['avg_rating'], pages=data['pages'], duration_minutes=data.get('duration_minutes'),
                    release_date=data['release_date'], premiere_date=data['premiere_date'], isbn=data['isbn'], 
                    translator=data['translator'], format=data['format'], volume_number=data.get('volume_number')
                )
                db_session.add(book)
                db_session.flush()
            except Exception as e:
                if "UNIQUE constraint" in str(e) or "IntegrityError" in str(e):
                    db_session.rollback()
                    book = db_session.query(Book).filter_by(external_id=data['external_id']).first()
                    if not book:
                        raise
                else:
                    raise
        else:
            # Aktualizacja istniejacego rekordu - uzupelniaj brakujace pola, aktualizuj rating
            if data['avg_rating'] is not None:
                book.avg_rating = data['avg_rating']
            if data['description'] and not book.description:
                book.description = data['description']
            if data['pages'] and not book.pages:
                book.pages = data['pages']
            if data['isbn'] and not book.isbn:
                book.isbn = data['isbn']
            if data['release_date'] and not book.release_date:
                book.release_date = data['release_date']
            if data['original_title'] and not book.original_title:
                book.original_title = data['original_title']
            if data.get('duration_minutes') and not book.duration_minutes:
                book.duration_minutes = data['duration_minutes']
            if data['translator'] and not book.translator:
                book.translator = data['translator']

        if data.get('cover_url'):
            if not db_session.query(Cover).filter_by(book_id=book.id).first():
                cover_meta = download_cover(data['cover_url'], data['type'], str(data['external_id']))
                if cover_meta:
                    db_session.add(Cover(book_id=book.id, **cover_meta))

        if data.get('publisher'):
            pub = db_session.query(Publisher).filter_by(name=data['publisher']['name']).first()
            if not pub:
                pub = Publisher(name=data['publisher']['name'])
                db_session.add(pub)
                db_session.flush()
            book.publisher = pub

        if data.get('series'):
            ser = db_session.query(Series).filter_by(name=data['series']['name']).first()
            if not ser:
                ser = Series(name=data['series']['name'])
                db_session.add(ser)
                db_session.flush()
            book.series = ser

        seen_authors = set()
        for auth_data in data.get('authors', []):
            name = auth_data.get('name')
            if not name or name in seen_authors: continue
            seen_authors.add(name)
            author = db_session.query(Author).filter_by(name=name).first()
            if not author:
                author = Author(name=name)
                db_session.add(author)
                db_session.flush()
            if author not in book.authors:
                book.authors.append(author)

        seen_cats = set()
        for cat_data in data.get('categories', []):
            name = cat_data.get('name')
            if not name or name in seen_cats: continue
            seen_cats.add(name)
            category = db_session.query(Category).filter_by(name=name).first()
            if not category:
                category = Category(name=name)
                db_session.add(category)
                db_session.flush()
            if category not in book.categories:
                book.categories.append(category)

        reviews_data = extract_reviews(html)
        for rev_data in reviews_data:
            existing_review = db_session.query(Review).filter_by(
                book_id=book.id, 
                username=rev_data['username'],
                full_text=rev_data['full_text']
            ).first()
            if not existing_review:
                review = Review(book_id=book.id, **rev_data)
                db_session.add(review)

        db_session.commit()

    def enqueue_spider_links(self, links_data: list, db_session):
        if not links_data:
            return
        
        # Zbierz wszystkie external_id ksiazek do sprawdzenia
        book_ids_to_check = {}
        non_book_links = []
        for item in links_data:
            match = re.search(r"/(ksiazka|audiobook)/(\d+)/", item["url"])
            if match:
                book_ids_to_check[int(match.group(2))] = item
            else:
                non_book_links.append(item)
        
        # Batch query z chunkowaniem (SQLite limit ~999 parametrow)
        existing_ids = set()
        if book_ids_to_check:
            ids_list = list(book_ids_to_check.keys())
            CHUNK_SIZE = 500
            for i in range(0, len(ids_list), CHUNK_SIZE):
                chunk = ids_list[i:i + CHUNK_SIZE]
                existing_ids.update(
                    r[0] for r in db_session.query(Book.external_id)
                    .filter(Book.external_id.in_(chunk))
                    .all()
                )
        
        valid_links = list(non_book_links)
        for ext_id, item in book_ids_to_check.items():
            if ext_id not in existing_ids:
                valid_links.append(item)
        
        if valid_links:
            add_many_to_queue(valid_links)

    def _throttle_success(self):
        """Zglos sukces - stopniowo zmniejszaj delay jesli bylo dobrze."""
        with self._throttle_lock:
            self._consecutive_errors = 0
            self._consecutive_ok += 1
            # Po 20 sukcesach pod rzad, zmniejsz mnoznik (min 1.0)
            if self._consecutive_ok >= 20 and self._delay_multiplier > 1.0:
                self._delay_multiplier = max(1.0, self._delay_multiplier * 0.8)
                self._consecutive_ok = 0
                logger.debug(f"Throttle DOWN: multiplier={self._delay_multiplier:.2f}")

    def _throttle_error(self, is_rate_limit: bool = False):
        """Zglos blad - zwieksz delay."""
        with self._throttle_lock:
            self._consecutive_ok = 0
            self._consecutive_errors += 1
            if is_rate_limit:
                # 429/503 - agresywne zwolnienie
                self._delay_multiplier = min(10.0, self._delay_multiplier * 2.0)
                logger.warning(f"Throttle UP (rate limit): multiplier={self._delay_multiplier:.2f}")
            elif self._consecutive_errors >= 3:
                # 3+ bledy pod rzad - lagodne zwolnienie
                self._delay_multiplier = min(5.0, self._delay_multiplier * 1.5)
                logger.warning(f"Throttle UP (errors): multiplier={self._delay_multiplier:.2f}")

    def _get_delay(self) -> float:
        """Oblicz aktualny delay z uwzglednieniem mnoznika."""
        base_delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
        return base_delay * self._delay_multiplier

    def process_single_item(self, item_dict):
        item_id = item_dict["id"]
        url = item_dict["url"]
        try:
            delay = self._get_delay()
            time.sleep(delay)
            # Kazdy watek tworzy wlasna sesje HTTP (curl_cffi nie jest thread-safe)
            with cureq.Session(impersonate="chrome120") as local_session:
                res = local_session.get(url, timeout=15, allow_redirects=True)
            
            # Rate limiting detection
            if res.status_code in (429, 503):
                self._throttle_error(is_rate_limit=True)
                # Czekaj i ponow
                time.sleep(30 * self._delay_multiplier)
                mark_queue_failed(item_id)
                return f"Rate limit {res.status_code}: {url.split('/')[-1][:20]}"
            
            if res.status_code == 404:
                self._throttle_success()
                mark_status(item_id, "archived_error")
                return f"Pudlo 404: {url.split('/')[-1]}"
            # Dla stron paginacyjnych: jesli serwer przekierowal na inna strone
            # (np. /katalog bez ?page=), to oznacza koniec paginacji
            final_url_str = str(res.url) if hasattr(res, 'url') and res.url else url
            if item_dict["type"] == "list" and "page=" in url and "page=" not in final_url_str:
                self._throttle_success()
                mark_status(item_id, "archived_error")
                return f"Koniec paginacji: {url.split('/')[-1]}"
            res.raise_for_status()
            with get_session() as db_session:
                if item_dict["type"] == "book":
                    # Uzyj finalnego URL po ewentualnych redirectach
                    final_url = str(res.url) if hasattr(res, 'url') and res.url else url
                    self.process_book_page(final_url, res.text, db_session)
                    if self.mode == "spider":
                        discovered_links = extract_links(res.text, base_url=final_url)
                        self.enqueue_spider_links(discovered_links, db_session)
                elif item_dict["type"] == "list":
                    discovered_links = extract_links(res.text, base_url=url)
                    self.enqueue_spider_links(discovered_links, db_session)
            self._throttle_success()
            mark_status(item_id, "completed")
            return f"Zapisano: {url.split('/')[-1][:30]}"
        except Exception as e:
            err_str = str(e).lower()
            is_network = any(x in err_str for x in ['timeout', 'connection', 'reset', 'refused', 'ssl', '429', '503'])
            self._throttle_error(is_rate_limit=('429' in err_str or '503' in err_str))
            if is_network:
                logger.warning(f"Blad sieci dla {url.split('/')[-1][:25]}: {str(e)[:60]} (retry, delay x{self._delay_multiplier:.1f})")
            else:
                logger.error(f"Blad dla {url}: {e}")
            mark_queue_failed(item_id)
            return f"Blad: {url.split('/')[-1][:20]}"

    def run_queue(self):
        # Cleanup: przywroc status "pending" elementom ktore utknely w "processing"
        # (np. po crash programu)
        with get_session() as s:
            stuck = s.query(ScrapeQueue).filter_by(status='processing').count()
            if stuck > 0:
                s.query(ScrapeQueue).filter_by(status='processing').update({"status": "pending"})
                s.commit()
                logger.info(f"Przywrocono {stuck} zadan ze statusu 'processing' do 'pending'.")
            total_tasks = s.query(ScrapeQueue).filter_by(status='pending').count()
        if total_tasks == 0:
            print("Kolejka jest pusta!")
            return
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold green]{task.description}[/bold green]"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[cyan]|"),
            TaskProgressColumn(),
            TextColumn("[cyan]|"),
            TimeElapsedColumn(),
            TextColumn("[cyan]→"),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task(
                f"[{self.max_workers}T] Delay {config.MIN_DELAY}-{config.MAX_DELAY}s",
                total=total_tasks
            )
            processed_count = 0
            while True:
                batch = get_batch_queue(limit=self.max_workers)
                if not batch:
                    break 
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self.process_single_item, item): item for item in batch}
                    for future in as_completed(futures):
                        status_msg = future.result()
                        processed_count += 1
                        with get_session() as session:
                            new_pending = session.query(ScrapeQueue).filter_by(status='pending').count()
                            progress.update(task, completed=processed_count, total=processed_count + new_pending)

    def run_update(self, id_lookahead: int = 2000):
        """
        Tryb aktualizacji - szybkie przechwycenie nowosci bez pelnego crawla.
        
        Strategia:
        1. Scrapuj strony nowosci/zapowiedzi (kilka stron paginacji)
        2. Sprawdz ID powyżej max known ID (szukaj nowych wpisow)
        3. Przetworz znalezione linki z kolejki
        
        Idealny do codziennego/tygodniowego crona.
        """
        logger.info(f"[Update] Rozpoczynam aktualizacje bazy o nowosci...")
        
        # 1. Dodaj strony nowosci do kolejki
        update_seeds = [
            "https://lubimyczytac.pl/ksiazki/nowosci",
            "https://lubimyczytac.pl/ksiazki/zapowiedzi",
            "https://lubimyczytac.pl/katalog/ostatnio-wydane",
            "https://lubimyczytac.pl/ksiazki/nowosci-i-zapowiedzi",
        ]
        with get_session() as session:
            for url in update_seeds:
                existing = session.query(ScrapeQueue).filter_by(url=url).first()
                if existing:
                    existing.status = "pending"
                else:
                    session.add(ScrapeQueue(url=url, type="list", priority=10))
            session.commit()
        
        # 2. Sprawdz nowe ID powyżej max known
        with get_session() as session:
            max_id = session.query(func.max(Book.external_id)).scalar() or 0
            existing_books = set(r[0] for r in session.query(Book.external_id).filter(
                Book.external_id >= max_id - 100  # ostatnie 100 tez sprawdz (mogly byc puste)
            ).all())
            dead_urls = session.query(ScrapeQueue.url).filter(
                ScrapeQueue.status == 'archived_error'
            ).all()
            dead_ids = set()
            for url_tuple in dead_urls:
                match = re.search(r"/ksiazka/(\d+)/", url_tuple[0])
                if match:
                    dead_ids.add(int(match.group(1)))
        
        # Generuj ID do sprawdzenia: od (max_id - 100) do (max_id + lookahead)
        start_id = max(1, max_id - 100)
        end_id = max_id + id_lookahead
        ids_to_check = [
            cid for cid in range(start_id, end_id + 1)
            if cid not in existing_books and cid not in dead_ids
        ]
        
        if ids_to_check:
            logger.info(f"[Update] Dodaje {len(ids_to_check)} ID do sprawdzenia ({start_id}-{end_id})...")
            id_links = [
                {"url": f"https://lubimyczytac.pl/ksiazka/{cid}/a", "type": "book", "priority": 8}
                for cid in ids_to_check
            ]
            add_many_to_queue(id_links)
        
        # 3. Uruchom kolejke
        self.mode = "spider"
        logger.info("[Update] Uruchamiam przetwarzanie kolejki...")
        self.run_queue()
        
        logger.info("[Update] Aktualizacja zakonczona.")

    def seed_start_urls(self):
        starts = [
            # Główne katalogi
            "https://lubimyczytac.pl/katalog",
            "https://lubimyczytac.pl/katalog/ksiazki",
            "https://lubimyczytac.pl/ksiazki/nowosci",
            "https://lubimyczytac.pl/ksiazki/zapowiedzi",
            "https://lubimyczytac.pl/ksiazki/nowosci-i-zapowiedzi",
            "https://lubimyczytac.pl/katalog/ostatnio-wydane",
            # Popularne autorzy i cykle
            "https://lubimyczytac.pl/autorzy",
            "https://lubimyczytac.pl/cykle",
            "https://lubimyczytac.pl/serie",
            # Tagi - glowna strona z lista tagow
            "https://lubimyczytac.pl/ksiazki/tagi",
            # Strony zbiorcze z linkami do ksiazek
            "https://lubimyczytac.pl/ksiazki/kategorie",
            "https://lubimyczytac.pl/patronaty",
            "https://lubimyczytac.pl/autorzy/popularni",
            # Kategorie - wszystkie glowne podkategorie
            "https://lubimyczytac.pl/kategoria/beletrystyka/fantasy-science-fiction",
            "https://lubimyczytac.pl/kategoria/beletrystyka/horror",
            "https://lubimyczytac.pl/kategoria/beletrystyka/klasyka",
            "https://lubimyczytac.pl/kategoria/beletrystyka/kryminal-sensacja-thriller",
            "https://lubimyczytac.pl/kategoria/beletrystyka/literatura-mlodziezowa",
            "https://lubimyczytac.pl/kategoria/beletrystyka/literatura-obyczajowa-romans",
            "https://lubimyczytac.pl/kategoria/beletrystyka/literatura-piekna",
            "https://lubimyczytac.pl/kategoria/beletrystyka/powiesc-historyczna",
            "https://lubimyczytac.pl/kategoria/beletrystyka/powiesc-przygodowa",
            "https://lubimyczytac.pl/kategoria/beletrystyka/romantasy",
            "https://lubimyczytac.pl/kategoria/literatura-faktu/biografia-autobiografia-pamietnik",
            "https://lubimyczytac.pl/kategoria/literatura-faktu/reportaz",
            "https://lubimyczytac.pl/kategoria/literatura-faktu/literatura-podroznicza",
            "https://lubimyczytac.pl/kategoria/literatura-faktu/publicystyka-literacka-eseje",
            "https://lubimyczytac.pl/kategoria/literatura-popularnonaukowa/historia",
            "https://lubimyczytac.pl/kategoria/literatura-popularnonaukowa/popularnonaukowa",
            "https://lubimyczytac.pl/kategoria/literatura-popularnonaukowa/nauki-spoleczne-psychologia-socjologia-itd",
            "https://lubimyczytac.pl/kategoria/literatura-popularnonaukowa/biznes-finanse",
            "https://lubimyczytac.pl/kategoria/literatura-popularnonaukowa/filozofia-etyka",
            "https://lubimyczytac.pl/kategoria/literatura-popularnonaukowa/zdrowie-medycyna",
            "https://lubimyczytac.pl/kategoria/literatura-dziecieca/literatura-dziecieca",
            "https://lubimyczytac.pl/kategoria/literatura-dziecieca/bajki",
            "https://lubimyczytac.pl/kategoria/komiksy/komiksy",
            "https://lubimyczytac.pl/kategoria/poezja-dramat-satyra/poezja",
            "https://lubimyczytac.pl/kategoria/pozostale/poradniki",
            "https://lubimyczytac.pl/kategoria/pozostale/religia",
            "https://lubimyczytac.pl/kategoria/pozostale/hobby",
            # Popularne tagi - bezposrednie listy ksiazek po tagu
            "https://lubimyczytac.pl/ksiazki/t/romans",
            "https://lubimyczytac.pl/ksiazki/t/fantasy",
            "https://lubimyczytac.pl/ksiazki/t/manga",
            "https://lubimyczytac.pl/ksiazki/t/historia",
            "https://lubimyczytac.pl/ksiazki/t/thriller",
            "https://lubimyczytac.pl/ksiazki/t/horror",
            "https://lubimyczytac.pl/ksiazki/t/science%20fiction",
            "https://lubimyczytac.pl/ksiazki/t/biografia",
            "https://lubimyczytac.pl/ksiazki/t/klasyka",
            "https://lubimyczytac.pl/ksiazki/t/dla%20dzieci",
            "https://lubimyczytac.pl/ksiazki/t/young%20adult",
            "https://lubimyczytac.pl/ksiazki/t/komiks",
        ]
        with get_session() as session:
            for url in starts:
                existing = session.query(ScrapeQueue).filter_by(url=url).first()
                if existing:
                    existing.status = "pending"
                else:
                    session.add(ScrapeQueue(url=url, type="list", priority=10))
            session.commit()

    def run_daemon(self):
        logger.info("[!] Uruchamiam tryb DAEMON-IDS. Dzialam non-stop weryfikujac nowe ID.")
        with get_session() as session:
            max_id = session.query(func.max(Book.external_id)).scalar() or 0
        current_id = max_id
        miss_counter = 0
        logger.info(f"Ostatnie znane ID w bazie to {current_id}. Rozpoczynam poszukiwania...")
        while True:
            current_id += 1
            url_to_check = f"https://lubimyczytac.pl/ksiazka/{current_id}/a"
            try:
                delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
                time.sleep(delay)
                response = self.session.get(url_to_check, headers=config.get_random_headers(), timeout=15)
                if response.status_code == 404:
                    miss_counter += 1
                    logger.info(f"ID {current_id} - Puste (Pudlo {miss_counter}/50)")
                    if miss_counter >= 50:
                        logger.warning("Napotkano 50 pustych ID pod rzad.")
                        time.sleep(12 * 3600)
                        current_id -= 50 
                        miss_counter = 0
                    continue
                response.raise_for_status()
                final_url = response.url
                miss_counter = 0
                with get_session() as db_session:
                    if "/ksiazka/" in final_url or "/audiobook/" in final_url:
                        match = re.search(r"/(ksiazka|audiobook)/(\d+)/", final_url)
                        if match:
                            ext_id = int(match.group(2))
                            if not db_session.query(Book).filter_by(external_id=ext_id).first():
                                logger.info(f"Nowe ID: {ext_id}! Parsuje: {final_url}")
                                self.process_book_page(final_url, response.text, db_session)
                            else:
                                logger.debug(f"Ksiazka {ext_id} juz jest w bazie. Pomijam.")
            except Exception as e:
                err_str = str(e).lower()
                # Bledy sieciowe / HTTP - tymczasowe, ponow probe
                if any(x in err_str for x in ['timeout', 'connection', 'network', 'reset', 'refused', 'ssl']):
                    logger.error(f"Blad sieci przy ID {current_id}: {e}. Czekam 60s przed ponowna proba.")
                    time.sleep(60)
                    current_id -= 1
                else:
                    # Inne bledy (parsowanie, logika) - loguj i idz dalej
                    logger.error(f"Nieoczekiwany blad przy ID {current_id}: {e}")
                    log_error(url_to_check, str(e))

    def _process_gap_id(self, current_id: int, progress, task) -> str:
        url_to_check = f"https://lubimyczytac.pl/ksiazka/{current_id}/a"
        status_msg = ""
        try:
            delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
            time.sleep(delay)
            with cureq.Session(impersonate="chrome120") as local_session:
                response = local_session.get(url_to_check, timeout=15)
                status_code = response.status_code
                final_url = response.url
                html_text = response.text
            if status_code == 404:
                with get_session() as db_session:
                    existing_item = db_session.query(ScrapeQueue).filter_by(url=url_to_check).first()
                    if existing_item:
                        existing_item.status = "archived_error"
                        existing_item.retry_count = 3
                    else:
                        error_item = ScrapeQueue(url=url_to_check, type="book", status="archived_error", retry_count=3)
                        db_session.add(error_item)
                    db_session.commit()
                status_msg = f"Pudlo 404: ID {current_id}"
            else:
                if status_code != 200:
                    raise ValueError(f"HTTP {status_code}")
                with get_session() as db_session:
                    if "/ksiazka/" in final_url or "/audiobook/" in final_url:
                        try:
                            self.process_book_page(final_url, html_text, db_session)
                            status_msg = f"Zapisano: ID {current_id}"
                        except Exception as e:
                            if "UNIQUE constraint failed" in str(e) or "IntegrityError" in str(e):
                                db_session.rollback()
                                status_msg = f"Duplikat (scalono): ID {current_id}"
                            else:
                                raise e
                    else:
                        status_msg = f"Pominieto (inny link): ID {current_id}"
        except Exception as e:
            logger.error(f"Blad przy lataniu ID {current_id}: {str(e)[:100]}")
            status_msg = f"Blad sieci: ID {current_id}"
        finally:
            progress.update(task, advance=1, description=status_msg)
        return status_msg

    def run_gap_filler(self):
        logger.info("[!] Uruchamiam skanowanie dziur w bazie (Gap Filler)...")
        with get_session() as session:
            max_id = session.query(func.max(Book.external_id)).scalar() or 0
            if max_id == 0:
                logger.info("Baza jest pusta. Uzyj najpierw trybu daemon-ids.")
                return
            existing_books = set(r[0] for r in session.query(Book.external_id).all())
            dead_urls = session.query(ScrapeQueue.url).filter(ScrapeQueue.status == 'archived_error').all()
            dead_ids = set()
            for url_tuple in dead_urls:
                match = re.search(r"/ksiazka/(\d+)/", url_tuple[0])
                if match:
                    dead_ids.add(int(match.group(1)))

        all_possible_ids = set(range(1, max_id))
        missing_ids = sorted(list(all_possible_ids - existing_books - dead_ids))
        if not missing_ids:
            logger.info("[OK] Brak dziur w bazie! Wszystko jest kompletne.")
            return
        logger.info(f"Znaleziono {len(missing_ids)} brakujacych ID. Rozpoczynam pobieranie...")
        
        with Progress(
            SpinnerColumn(),
            TextColumn(f"[bold green]Latanie Dziur ([blue]{self.max_workers} Watkow[/blue]):[/bold green] {{task.description}}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[cyan]Pozostalo: {task.remaining}"),
        ) as progress:
            task = progress.add_task("Uruchamianie...", total=len(missing_ids))
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                batch_size = 5000
                try:
                    for i in range(0, len(missing_ids), batch_size):
                        batch = missing_ids[i : i + batch_size]
                        futures = {executor.submit(self._process_gap_id, cid, progress, task): cid for cid in batch}
                        for future in as_completed(futures):
                            future.result() 
                except KeyboardInterrupt:
                    progress.console.print("\n[bold red]Zatrzymywanie pajakow (to moze zajac kilka sekund)...[/bold red]")
                    for future in futures:
                        future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise

    def harvest_links_from_cache(self):
        """
        Przeszukuje zapisane pliki HTML w katalogu cache (data/html_cache/)
        i wyciaga z nich linki do ksiazek/audiobookow oraz stron katalogowych.
        Nowe linki sa dodawane do kolejki scrape'owania (ScrapeQueue).
        
        Zwraca:
            int: Laczna liczba nowych linkow dodanych do kolejki.
        """
        logger.info("[Cache Harvester] Rozpoczynam ekstrakcje linkow z plikow cache...")

        if not os.path.isdir(CACHE_DIR):
            logger.warning(f"Katalog cache '{CACHE_DIR}' nie istnieje.")
            return 0

        cache_files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".html")]
        total_files = len(cache_files)

        if total_files == 0:
            logger.info("[Cache Harvester] Katalog cache jest pusty.")
            return 0

        logger.info(f"[Cache Harvester] Znaleziono {total_files} plikow HTML do przeanalizowania.")

        all_discovered_links = []
        seen_urls = set()

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold green]Cache Harvester:[/bold green] {task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[cyan]Pozostalo: {task.remaining}"),
        ) as progress:
            task = progress.add_task("Analizowanie plikow cache...", total=total_files)

            for filename in cache_files:
                filepath = os.path.join(CACHE_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        html_content = f.read()

                    links_data = extract_links(html_content)

                    for link in links_data:
                        url = link["url"]
                        if url not in seen_urls:
                            seen_urls.add(url)
                            all_discovered_links.append(link)

                except Exception as e:
                    logger.warning(f"Blad przy odczycie pliku cache '{filename}': {e}")

                progress.update(task, advance=1, description=f"Przetworzono: {filename[:35]}")

        logger.info(f"[Cache Harvester] Wyekstrahowano {len(all_discovered_links)} unikalnych linkow z cache.")

        total_new_links = 0
        if all_discovered_links:
            with get_session() as db_session:
                # Batch query: zbierz wszystkie external_id do sprawdzenia
                book_ext_ids = {}
                for link in all_discovered_links:
                    if link["type"] == "book":
                        match = re.search(r"/(ksiazka|audiobook)/(\d+)/", link["url"])
                        if match:
                            book_ext_ids[int(match.group(2))] = link

                # Batch query z chunkowaniem (SQLite limit ~999 parametrow)
                existing_ids = set()
                if book_ext_ids:
                    ids_list = list(book_ext_ids.keys())
                    CHUNK_SIZE = 500
                    for i in range(0, len(ids_list), CHUNK_SIZE):
                        chunk = ids_list[i:i + CHUNK_SIZE]
                        existing_ids.update(
                            r[0] for r in db_session.query(Book.external_id)
                            .filter(Book.external_id.in_(chunk))
                            .all()
                        )

                filtered_links = []
                for link in all_discovered_links:
                    if link["type"] == "book":
                        match = re.search(r"/(ksiazka|audiobook)/(\d+)/", link["url"])
                        if match and int(match.group(2)) in existing_ids:
                            continue
                    filtered_links.append(link)

                total_new_links = len(filtered_links)
                if filtered_links:
                    add_many_to_queue(filtered_links)
                    logger.info(f"[Cache Harvester] Dodano {total_new_links} nowych linkow do kolejki.")
                else:
                    logger.info("[Cache Harvester] Wszystkie znalezione linki juz istnieja w bazie lub kolejce.")

        logger.info(f"[Cache Harvester] Zakończono. Nowe linki w kolejce: {total_new_links}")
        return total_new_links

    def run_custom_id_scanner(self, start_id: int, direction: str = "up", count: int = 20000):
        logger.info(f"[!] Uruchamiam wielowatkowy skaner ID. Start: {start_id}, Kierunek: {direction}, Ilosc: {count}")
        with get_session() as session:
            existing_books = set(r[0] for r in session.query(Book.external_id).all())
            dead_urls = session.query(ScrapeQueue.url).filter(ScrapeQueue.status == 'archived_error').all()
            dead_ids = set()
            for url_tuple in dead_urls:
                match = re.search(r"/ksiazka/(\d+)/", url_tuple[0])
                if match:
                    dead_ids.add(int(match.group(1)))

        if direction == "up":
            if count == 0:
                # "Do końca" w górę: generujemy paczkę np. 2 milionów potencjalnych ID w przód
                target_ids = list(range(start_id, start_id + 2000000))
            else:
                target_ids = list(range(start_id, start_id + count))
        else:
            if count == 0:
                # "Do końca" w dół: schodzimy do najniższego możliwego ID, czyli 1
                target_ids = list(range(start_id, 0, -1))
            else:
                # max(0, ...) zapewnia, że nie miniemy zera przy standardowym odliczaniu
                target_ids = list(range(start_id, max(0, start_id - count), -1))

        missing_ids = [cid for cid in target_ids if cid not in existing_books and cid not in dead_ids]
        if not missing_ids:
            logger.info("[OK] Wszystkie ID w tym przedziale sa juz pobrane lub zweryfikowane jako 404.")
            return

        logger.info(f"Do sprawdzenia pozostalo {len(missing_ids)} adresow ID. Rozpoczynam skanowanie...")
        with Progress(
            SpinnerColumn(),
            TextColumn(f"[bold green]Skaner ID ([blue]{self.max_workers} Watkow[/blue]):[/bold green] {{task.description}}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[cyan]Pozostalo: {task.remaining}"),
        ) as progress:
            task = progress.add_task("Skanowanie...", total=len(missing_ids))
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                batch_size = 5000
                try:
                    for i in range(0, len(missing_ids), batch_size):
                        batch = missing_ids[i : i + batch_size]
                        futures = {executor.submit(self._process_gap_id, cid, progress, task): cid for cid in batch}
                        for future in as_completed(futures):
                            future.result()
                except KeyboardInterrupt:
                    progress.console.print("\n[bold red]Zatrzymywanie skanera ID...[/bold red]")
                    for future in futures:
                        future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
