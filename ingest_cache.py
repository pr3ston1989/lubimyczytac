"""Wczytanie cache HTML do bazy (scalanie pracy z wielu maszyn, BEZ sieci).

Po co: gdy pracujesz na kilku maszynach (np. serwer robi naprawe autorow, a PC
puszcza pajaka dodajacego nowe ksiazki), kazda z nich zapisuje pobrany HTML do
``data/html_cache/`` (scraper robi to automatycznie; naprawa - z flaga
``--save-cache``). Zamiast ryzykownego laczenia plikow SQLite, zlewasz wszystkie
pliki cache w jedno miejsce i uruchamiasz TEN skrypt na jednej bazie-master.

Co robi dla KAZDEGO pliku cache:
  * parsuje zapisany HTML (bez sieci),
  * jesli ksiazki nie ma w bazie - DODAJE ja (z poprawnymi autorami),
  * jesli jest - AKTUALIZUJE autorow/kategorie/recenzje (naprawiony parser).
Okladki sa pomijane (tryb offline).

Cechy: strumieniowy (stala pamiec), WZNAWIALNY (tabela cache_ingest_progress),
przerywalny (Ctrl+C), z paskiem postepu i logiem.

Uzycie:
    python3 ingest_cache.py                  # wczytaj caly cache do data/database.db
    python3 ingest_cache.py --limit 50000    # partiami
    python3 ingest_cache.py --recheck        # przetworz takze juz wczytane
    python3 ingest_cache.py --cache-dir /inna/sciezka/html_cache
"""

import argparse
import os
import sys

from loguru import logger
from rich.console import Console
from rich.progress import (Progress, SpinnerColumn, TextColumn, BarColumn,
                           TaskProgressColumn, TimeRemainingColumn)
from sqlalchemy import text

import config
from database import get_session, init_db
from scraper import Scraper

console = Console()
DEFAULT_CACHE_DIR = os.path.join("data", "html_cache")

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS cache_ingest_progress (
    fname       TEXT PRIMARY KEY,
    status      TEXT,
    checked_at  TEXT
)
"""


def parse_filename(fname: str):
    """'ksiazka_245373.html' -> ('ksiazka', 245373). Zwraca None gdy nie pasuje."""
    if not fname.endswith(".html"):
        return None
    stem = fname[:-5]
    if "_" not in stem:
        return None
    btype, ext = stem.rsplit("_", 1)
    if btype not in ("ksiazka", "audiobook") or not ext.isdigit():
        return None
    return btype, int(ext)


def ensure_progress(session):
    session.execute(text(PROGRESS_DDL))
    session.commit()


def load_done(session) -> set:
    rows = session.execute(text("SELECT fname FROM cache_ingest_progress")).fetchall()
    return {r[0] for r in rows}


def main():
    parser = argparse.ArgumentParser(description="Wczytaj cache HTML do bazy (offline scalanie).")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="Katalog z plikami cache.")
    parser.add_argument("--limit", type=int, default=0, help="Maks. plikow w tym uruchomieniu (0 = wszystkie).")
    parser.add_argument("--batch", type=int, default=500, help="Co ile rekordow commit.")
    parser.add_argument("--recheck", action="store_true", help="Przetworz takze juz wczytane pliki.")
    parser.add_argument("--log-file", default="logs/ingest.log", help="Plik logu (rotowany).")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.log_file) or ".", exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(args.log_file, rotation="20 MB", retention="14 days", level="INFO", encoding="utf-8")

    if not os.path.isdir(args.cache_dir):
        console.print(f"[red]Brak katalogu cache: {args.cache_dir}[/red]")
        sys.exit(1)

    init_db()
    with get_session() as session:
        ensure_progress(session)
        done = set() if args.recheck else load_done(session)

    all_files = sorted(f for f in os.listdir(args.cache_dir) if f.endswith(".html"))
    todo = [f for f in all_files if f not in done]
    if args.limit:
        todo = todo[:args.limit]
    total = len(todo)
    logger.info(f"Cache: {len(all_files)} plikow | do wczytania: {total} "
                f"(pominieto wczytane: {len(all_files) - len(todo) if not args.recheck else 0})")
    if total == 0:
        console.print("[green]Nic do wczytania.[/green]")
        return

    scraper = Scraper(mode="ingest")
    added = updated = skipped = 0
    processed = 0
    use_bar = console.is_terminal

    progress_cm = Progress(
        SpinnerColumn(),
        TextColumn("[bold green]Ingest cache:[/bold green] {task.description}"),
        BarColumn(), TaskProgressColumn(),
        TextColumn("[cyan]ok:{task.fields[ok]} skip:{task.fields[skip]}"),
        TimeRemainingColumn(),
    ) if use_bar else None

    def run(progress=None, task=None):
        nonlocal added, updated, skipped, processed
        session = get_session()
        try:
            for i, fname in enumerate(todo, 1):
                meta = parse_filename(fname)
                if meta is None:
                    skipped += 1
                    continue
                btype, ext = meta
                path = os.path.join(args.cache_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        html = fh.read()
                    url = f"{config.BASE_URL}/{btype}/{ext}/x"
                    scraper.process_book_page(url, html, session, download_covers=False)
                    session.execute(
                        text("INSERT INTO cache_ingest_progress(fname,status,checked_at) "
                             "VALUES(:f,'ok',datetime('now')) "
                             "ON CONFLICT(fname) DO UPDATE SET status='ok', checked_at=datetime('now')"),
                        {"f": fname},
                    )
                    updated += 1
                except Exception as e:  # noqa: BLE001
                    session.rollback()
                    skipped += 1
                    logger.debug(f"Pomijam {fname}: {str(e)[:120]}")
                processed += 1
                if processed % args.batch == 0:
                    session.commit()
                    if progress is not None:
                        progress.update(task, completed=processed, description=f"...{fname}",
                                        ok=updated, skip=skipped)
                    pct = processed * 100.0 / total
                    logger.info(f"Postep: {processed}/{total} ({pct:.1f}%) | wczytane={updated} pominiete={skipped}")
                elif progress is not None:
                    progress.update(task, completed=processed)
            session.commit()
        except KeyboardInterrupt:
            session.commit()
            logger.warning("Przerwano (Ctrl+C). Postep zapisany - uruchom ponownie, aby wznowic.")
        finally:
            session.close()

    if use_bar:
        with progress_cm as progress:
            task = progress.add_task("start", total=total, ok=0, skip=0)
            run(progress, task)
    else:
        run()

    logger.info(f"Zakonczono. Wczytane/zaktualizowane: {updated}, pominiete: {skipped}, razem: {processed}")
    console.print(f"\n[bold green]Gotowe.[/bold green] wczytane={updated} pominiete={skipped} razem={processed}\n")


if __name__ == "__main__":
    main()
