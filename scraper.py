import time
import threading
import requests
import re
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cureq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from loguru import logger
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

import config
from ratelimit import RateLimiter
from runtime import (STOP, interruptible_sleep, request_stop, Interrupted,
                     iter_done, install_sigint, restore_sigint)
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
        # KRYTYCZNE: sesja HTTP NIE moze byc wspoldzielona miedzy watkami.
        # curl_cffi.Session opakowuje jeden uchwyt libcurl i nie jest thread-safe;
        # przy >2 watkach rownolegle zadania mieszaja swoje odpowiedzi
        # (watek A dostaje HTML zadany przez watek B) -> ksiazka X zapisuje
        # autorow ksiazki Y. Dlatego kazdy watek dostaje WLASNA sesje.
        self._thread_local = threading.local()
        # Globalny limiter szybkosci wspoldzielony przez WSZYSTKIE watki tego
        # scrapera - to on (a nie liczba watkow) chroni przed HTTP 429.
        self.rate_limiter = RateLimiter(requests_per_second=config.REQUESTS_PER_SECOND)

    def get_http_session(self):
        """Zwraca sesje HTTP zwiazana z biezacym watkiem (thread-local)."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = cureq.Session(impersonate="chrome120")
            self._thread_local.session = session
        return session

    def request(self, url: str):
        """GET z globalnym rate-limitingiem oraz backoff dla 429/503.

        Zwraca obiekt odpowiedzi (status 200/404/...). Bledy przejsciowe
        (429, 503, wyjatki sieciowe) sa ponawiane z rosnacym odstepem;
        respektowany jest naglowek Retry-After. Po wyczerpaniu prob - wyjatek.
        """
        last_exc = None
        for attempt in range(config.MAX_HTTP_RETRIES + 1):
            if STOP.is_set():
                raise Interrupted()
            self.rate_limiter.wait()
            if STOP.is_set():
                raise Interrupted()
            try:
                resp = self.get_http_session().get(
                    url, headers=config.get_random_headers(), timeout=15
                )
            except Exception as e:  # noqa: BLE001 - sieciowe bledy przejsciowe
                last_exc = e
                interruptible_sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue

            if resp.status_code in (429, 503):
                self.rate_limiter.penalize()
                retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                wait_s = None
                if retry_after:
                    try:
                        wait_s = float(retry_after)
                    except (TypeError, ValueError):
                        wait_s = None
                if wait_s is None:
                    wait_s = min(2 ** attempt, 60) + random.uniform(0, 1)
                logger.warning(
                    f"HTTP {resp.status_code} dla {url} - czekam {wait_s:.1f}s "
                    f"(proba {attempt + 1}/{config.MAX_HTTP_RETRIES})"
                )
                interruptible_sleep(wait_s)
                continue

            # Sukces (lub trwaly status jak 404) - powoli wracamy do tempa bazowego.
            self.rate_limiter.recover()
            return resp

        if last_exc:
            raise last_exc
        raise ValueError("Przekroczono limit prob (HTTP 429/503)")

    def fetch(self, url: str) -> str:
        response = self.request(url)
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
            book = Book(
                external_id=data['external_id'], slug=data['slug'], url=data['url'], type=data['type'],
                title=data['title'], original_title=data['original_title'], description=data['description'],
                avg_rating=data['avg_rating'], pages=data['pages'], duration_minutes=data.get('duration_minutes'),
                release_date=data['release_date'], premiere_date=data['premiere_date'], isbn=data['isbn'], 
                translator=data['translator'], format=data['format'], volume_number=data.get('volume_number')
            )
            db_session.add(book)
            db_session.flush()

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

        # Autorzy - PRZEBUDOWA calej listy na podstawie sparsowanych danych.
        # Dzieki temu ponowne scrapowanie USUWA bledne/stare powiazania zamiast
        # je akumulowac (book.authors.append nigdy nie kasowal starych wpisow).
        desired_authors = []
        seen_authors = set()
        for auth_data in data.get('authors', []):
            name = auth_data.get('name')
            if not name or name in seen_authors:
                continue
            seen_authors.add(name)
            author = db_session.query(Author).filter_by(name=name).first()
            if not author:
                author = Author(name=name)
                db_session.add(author)
                db_session.flush()
            desired_authors.append(author)

        # Nadpisujemy tylko gdy faktycznie cos sparsowano - chroni przed
        # przypadkowym wyczyszczeniem autorow przy chwilowym bledzie parsowania.
        if desired_authors:
            book.authors = desired_authors

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
        # Zbierz ext_id wszystkich kandydatow-ksiazek i sprawdz istnienie JEDNYM
        # zapytaniem (zamiast osobnego SELECT na kazdy link - przy stronach-listach
        # to setki zapytan). Reszta (listy/autorzy/kategorie) przechodzi dalej.
        candidates = []
        ext_ids = set()
        for item in links_data:
            match = re.search(r"/(ksiazka|audiobook)/(\d+)/", item["url"])
            if match:
                ext_id = int(match.group(2))
                item["_ext_id"] = ext_id
                ext_ids.add(ext_id)
            candidates.append(item)

        existing = set()
        if ext_ids:
            rows = db_session.query(Book.external_id).filter(Book.external_id.in_(ext_ids)).all()
            existing = {r[0] for r in rows}

        valid_links = []
        for item in candidates:
            ext_id = item.pop("_ext_id", None)
            if ext_id is not None and ext_id in existing:
                continue
            valid_links.append(item)
        if valid_links:
            add_many_to_queue(valid_links)

    def process_single_item(self, item_dict):
        item_id = item_dict["id"]
        url = item_dict["url"]
        try:
            # request() zapewnia globalny rate-limit + backoff dla 429/503
            # oraz sesje per-watek.
            res = self.request(url)
            if res.status_code == 404:
                mark_status(item_id, "archived_error")
                return f"Pudlo 404: {url.split('/')[-1]}"
            res.raise_for_status()
            html_text = res.text
            with get_session() as db_session:
                if item_dict["type"] == "book":
                    self.process_book_page(url, html_text, db_session)
                    if self.mode == "spider":
                        discovered_links = extract_links(html_text)
                        self.enqueue_spider_links(discovered_links, db_session)
                elif item_dict["type"] == "list":
                    discovered_links = extract_links(html_text)
                    self.enqueue_spider_links(discovered_links, db_session)
            mark_status(item_id, "completed")
            return f"Zapisano: {url.split('/')[-1][:30]}"
        except Interrupted:
            # Przerwano (Ctrl+C) - zostaw status 'processing' (run_queue wznowi).
            return "Przerwano"
        except Exception as e:
            logger.error(f"Blad dla {url}: {e}")
            # Bledy przejsciowe -> retry/dead-letter (mark_queue_failed),
            # zamiast bezpowrotnego statusu "error".
            mark_queue_failed(item_id)
            log_error(url, str(e))
            return f"Blad: {url.split('/')[-1][:20]}"

    def run_queue(self):
        # Odzyskiwanie zadan zablokowanych w stanie 'processing' po wczesniejszym
        # przerwaniu (Ctrl+C / crash) - inaczej zostalyby utracone na zawsze.
        with get_session() as s:
            s.query(ScrapeQueue).filter_by(status='processing').update({"status": "pending"})
            s.commit()
            total_tasks = s.query(ScrapeQueue).filter_by(status='pending').count()
        if total_tasks == 0:
            print("Kolejka jest pusta!")
            return
        with Progress(
            SpinnerColumn(),
            TextColumn(f"[bold green]LubimyCzytac ([blue]{self.max_workers} Watkow[/blue]):[/bold green] {{task.description}}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[cyan]Pozostalo: {task.remaining}"),
        ) as progress:
            task = progress.add_task("Rozgrzewanie pajakow...", total=total_tasks)
            processed_count = 0
            # JEDEN executor na cale uruchomienie. NIE uzywamy 'with', bo jego
            # wyjscie czeka na wszystkie zakolejkowane zadania (przy Ctrl+C =
            # wiszenie). Sterujemy zamknieciem recznie.
            executor = ThreadPoolExecutor(max_workers=self.max_workers)
            prev_sigint = install_sigint()
            try:
                while not STOP.is_set():
                    batch = get_batch_queue(limit=self.max_workers * 4)
                    if not batch:
                        break
                    futures = {executor.submit(self.process_single_item, item): item for item in batch}
                    try:
                        for future in iter_done(futures):
                            status_msg = future.result()
                            processed_count += 1
                            progress.update(task, description=status_msg, completed=processed_count)
                    except KeyboardInterrupt:
                        request_stop()
                    if STOP.is_set():
                        progress.console.print("\n[bold red]Zatrzymywanie (Ctrl+C)... porzucam kolejke paczki.[/bold red]")
                        for f in futures:
                            f.cancel()
                        break
                    with get_session() as session:
                        new_pending = session.query(ScrapeQueue).filter_by(status='pending').count()
                    progress.update(task, total=processed_count + new_pending)
            finally:
                restore_sigint(prev_sigint)
                executor.shutdown(wait=False, cancel_futures=True)

    def seed_start_urls(self):
        starts = ["https://lubimyczytac.pl/katalog", "https://lubimyczytac.pl/ksiazki/nowosci"]
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
                response = self.request(url_to_check)
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
            except requests.exceptions.RequestException as e:
                logger.error(f"Blad sieci przy ID {current_id}: {e}. Czekam 60s przed ponowna proba.")
                time.sleep(60)
                current_id -= 1
            except Exception as e:
                logger.error(f"Nieoczekiwany blad przy ID {current_id}: {e}")
                log_error(url_to_check, str(e))

    def _process_gap_id(self, current_id: int, progress, task) -> str:
        url_to_check = f"https://lubimyczytac.pl/ksiazka/{current_id}/a"
        status_msg = ""
        try:
            # request() = sesja per-watek + globalny rate-limit + backoff 429/503
            # + poprawne naglowki (wczesniej brakowalo headerow -> latwiejsza blokada).
            response = self.request(url_to_check)
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
        except Interrupted:
            # Przerwano (Ctrl+C) - nie zapisujemy 404 ani bledu, ID zostanie
            # ponownie sprawdzone przy nastepnym uruchomieniu.
            status_msg = f"Przerwano: ID {current_id}"
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
            executor = ThreadPoolExecutor(max_workers=self.max_workers)
            prev_sigint = install_sigint()
            batch_size = 5000
            done = saved = miss = err = 0
            try:
                for i in range(0, len(missing_ids), batch_size):
                    if STOP.is_set():
                        break
                    batch = missing_ids[i : i + batch_size]
                    futures = {executor.submit(self._process_gap_id, cid, progress, task): cid for cid in batch}
                    try:
                        for future in iter_done(futures):
                            msg = future.result()
                            done += 1
                            if msg.startswith("Zapisano"): saved += 1
                            elif msg.startswith("Pudlo"): miss += 1
                            elif msg.startswith("Blad"): err += 1
                            if done % 200 == 0:
                                logger.info(f"Latanie dziur: {done}/{len(missing_ids)} | "
                                            f"zapisane={saved} 404={miss} bledy={err} | "
                                            f"tempo={self.rate_limiter.current_rps:.2f} zad/s")
                    except KeyboardInterrupt:
                        request_stop()
                    if STOP.is_set():
                        progress.console.print("\n[bold red]Zatrzymywanie (Ctrl+C)... porzucam reszte ID.[/bold red]")
                        for f in futures:
                            f.cancel()
                        break
            finally:
                restore_sigint(prev_sigint)
                executor.shutdown(wait=False, cancel_futures=True)

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
            executor = ThreadPoolExecutor(max_workers=self.max_workers)
            prev_sigint = install_sigint()
            batch_size = 5000
            done = saved = miss = err = 0
            try:
                for i in range(0, len(missing_ids), batch_size):
                    if STOP.is_set():
                        break
                    batch = missing_ids[i : i + batch_size]
                    futures = {executor.submit(self._process_gap_id, cid, progress, task): cid for cid in batch}
                    try:
                        for future in iter_done(futures):
                            msg = future.result()
                            done += 1
                            if msg.startswith("Zapisano"): saved += 1
                            elif msg.startswith("Pudlo"): miss += 1
                            elif msg.startswith("Blad"): err += 1
                            if done % 200 == 0:
                                logger.info(f"Skaner ID: {done}/{len(missing_ids)} | "
                                            f"zapisane={saved} 404={miss} bledy={err} | "
                                            f"tempo={self.rate_limiter.current_rps:.2f} zad/s")
                    except KeyboardInterrupt:
                        request_stop()
                    if STOP.is_set():
                        progress.console.print("\n[bold red]Zatrzymywanie skanera ID (Ctrl+C)...[/bold red]")
                        for f in futures:
                            f.cancel()
                        break
            finally:
                restore_sigint(prev_sigint)
                executor.shutdown(wait=False, cancel_futures=True)
