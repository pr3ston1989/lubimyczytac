"""Naprawa juz zapisanych autorow ksiazek (migracja danych).

KONTEKST: wadliwy parser dopisywal do KAZDEJ ksiazki wszystkich autorow
napotkanych na stronie (widgety "Inne wydania"/"Podobne"/rekomendacje), wiec
praktycznie WSZYSTKIE rekordy maja nadmiarowych autorow. Jednoczesnie czesc
ksiazek ma autorow kilku naprawde - dlatego nie da sie typowac bledow po liczbie
autorow. Trzeba zweryfikowac KAZDA ksiazke wzgledem zrodla.

Zrodlem prawdy jest atrybut ``data-ga-book-authors`` (ustawiany przez serwis),
z fallbackiem do naglowka ``span.author`` / ``.book__author`` - patrz
parser.extract_authors. To pozwala poprawnie obsluzyc takze ksiazki wieloautorskie.

Zasady:
  * NIE usuwamy bazy; NIE scrapujemy katalogu - pobieramy tylko strony konkretnych
    ksiazek po zapisanym ``book.url`` (lub z cache, jesli jest).
  * Pobieranie idzie przez Scraper.request() => globalny rate-limit + backoff
    dla HTTP 429/503 + Retry-After + naglowki + sesja per-watek.
  * WZNAWIALNOSC: postep zapisywany jest w dodatkowej tabeli
    ``author_repair_progress`` (tworzonej nieinwazyjnie). Ponowne uruchomienie
    POMIJA ksiazki juz przetworzone (status fixed/ok) i NIE pobiera ich ponownie.
    Mozna wiec naprawiac partiami (``--limit``) przez kilka dni.
  * Opcjonalny ``--save-cache`` zapisuje pobrany HTML do data/html_cache, dzieki
    czemu kolejne przebiegi/weryfikacje sa darmowe.

BEZPIECZENSTWO WATKOW: watki tylko POBIERAJA i PARSUJA (zwracaja nazwiska);
zapisy do bazy robi wylacznie watek glowny (jedna sesja).

Uzycie:
    python repair_authors.py --dry-run --limit 200     # podglad na probce
    python repair_authors.py --workers 8 --save-cache  # naprawa calej bazy
    python repair_authors.py --workers 8               # wznowienie po przerwaniu
    python repair_authors.py --recheck                 # zweryfikuj ponownie wszystko
    python repair_authors.py --cache-only              # bez sieci (tylko cache)

429 / tempo: reguluje config.REQUESTS_PER_SECOND (env REQUESTS_PER_SECOND).
    Liczba --workers powyzej tego, co przepuszcza limiter, nie przyspieszy,
    ale nie wywola 429.

Wynik:
    * repair_log.csv  -> timestamp, book_id, external_id, url, stary/nowy autor, zrodlo
    * tabela author_repair_progress (postep + historia w bazie)
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

import config
from database import get_session, init_db
from models import Book, Author
from parser import extract_book_info

CACHE_DIR = os.path.join("data", "html_cache")
LOG_PATH = "repair_log.csv"
console = Console()

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS author_repair_progress (
    book_id      INTEGER PRIMARY KEY,
    external_id  INTEGER,
    status       TEXT,        -- 'fixed' | 'ok' | 'skipped'
    old_authors  TEXT,
    new_authors  TEXT,
    source       TEXT,
    skip_reason  TEXT,
    checked_at   TEXT
)
"""


def ensure_progress_table(session):
    session.execute(text(PROGRESS_DDL))
    session.commit()


def load_processed_ids(session) -> set:
    """book_id juz przetworzone pomyslnie (status fixed/ok) - do pominiecia."""
    rows = session.execute(
        text("SELECT book_id FROM author_repair_progress WHERE status IN ('fixed','ok')")
    ).fetchall()
    return {r[0] for r in rows}


def record_progress(session, r: dict, status: str):
    session.execute(
        text("""
            INSERT INTO author_repair_progress
                (book_id, external_id, status, old_authors, new_authors, source, skip_reason, checked_at)
            VALUES (:book_id, :external_id, :status, :old_authors, :new_authors, :source, :skip_reason, :checked_at)
            ON CONFLICT(book_id) DO UPDATE SET
                status=excluded.status, old_authors=excluded.old_authors,
                new_authors=excluded.new_authors, source=excluded.source,
                skip_reason=excluded.skip_reason, checked_at=excluded.checked_at
        """),
        {
            "book_id": r["book_id"],
            "external_id": r["external_id"],
            "status": status,
            "old_authors": "; ".join(r["current"]),
            "new_authors": "; ".join(r["correct"]) if r["correct"] else "",
            "source": r["source"],
            "skip_reason": r["skip_reason"],
            "checked_at": datetime.utcnow().isoformat(),
        },
    )


def load_cached_html(book_type: str, external_id: int):
    path = os.path.join(CACHE_DIR, f"{book_type}_{external_id}.html")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), path
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Nie udalo sie odczytac cache {path}: {e}")
    return None, None


def save_cached_html(book_type: str, external_id: int, html: str):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, f"{book_type}_{external_id}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Nie zapisano cache dla {external_id}: {e}")


def compute_correct_authors(snapshot: dict, cache_only: bool, save_cache: bool, scraper):
    """Liczy poprawnych autorow dla jednej ksiazki (BEZ dotykania bazy)."""
    result = {
        "book_id": snapshot["id"],
        "external_id": snapshot["external_id"],
        "url": snapshot["url"],
        "current": snapshot["current"],
        "correct": None,
        "source": None,
        "skip_reason": None,
    }

    html, cache_path = load_cached_html(snapshot["type"], snapshot["external_id"])
    source = None
    if html is not None:
        source = f"cache:{os.path.basename(cache_path)}"
    elif cache_only:
        result["skip_reason"] = "brak-cache"
        return result
    else:
        if not snapshot["url"]:
            result["skip_reason"] = "brak-url"
            return result
        try:
            resp = scraper.request(snapshot["url"])
        except Exception as e:  # noqa: BLE001
            result["skip_reason"] = f"blad-pobierania: {str(e)[:80]}"
            return result
        if resp.status_code == 404:
            result["skip_reason"] = "404-usunieta"
            return result
        if resp.status_code != 200:
            result["skip_reason"] = f"http-{resp.status_code}"
            return result
        html = resp.text
        source = "network"
        if save_cache:
            save_cached_html(snapshot["type"], snapshot["external_id"], html)

    data = extract_book_info(html, snapshot["url"])
    if not data:
        result["skip_reason"] = "parse-failed"
        return result

    names = sorted({a["name"] for a in data.get("authors", []) if a.get("name")})
    if not names:
        # Nie nadpisujemy pustym zbiorem - lepiej oznaczyc do recznego przegladu.
        result["skip_reason"] = "brak-autora-w-zrodle"
        return result
    result["correct"] = names
    result["source"] = source
    return result


def load_snapshots(processed_ids: set, limit):
    """Lekkie migawki ksiazek (plain dict), z pominieciem juz przetworzonych."""
    snapshots = []
    with get_session() as session:
        for book in session.query(Book).order_by(Book.id.asc()).all():
            if book.id in processed_ids:
                continue
            snapshots.append({
                "id": book.id,
                "external_id": book.external_id,
                "url": book.url,
                "type": book.type,
                "current": sorted(a.name for a in book.authors),
            })
            if limit and len(snapshots) >= limit:
                break
    return snapshots


def apply_fix(session, book_id: int, correct_names: list):
    book = session.get(Book, book_id)
    if book is None:
        return
    desired = []
    for name in correct_names:
        author = session.query(Author).filter_by(name=name).first()
        if not author:
            author = Author(name=name)
            session.add(author)
            session.flush()
        desired.append(author)
    book.authors = desired


def repair(dry_run, cache_only, limit, workers, recheck, save_cache):
    init_db()

    scraper = None
    if not cache_only:
        try:
            from scraper import Scraper
            scraper = Scraper(mode="repair", max_workers=workers)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Nie mozna uzyc sieci ({e}); tryb cache-only.")
            cache_only = True

    with get_session() as session:
        ensure_progress_table(session)
        processed = set() if recheck else load_processed_ids(session)

    snapshots = load_snapshots(processed, limit)
    total = len(snapshots)
    logger.info(f"Do sprawdzenia: {total} ksiazek "
                f"(pominieto juz przetworzone: {len(processed)}).")
    if total == 0:
        console.print("[green]Brak ksiazek do przetworzenia (wszystko juz naprawione lub pusto).[/green]")
        return

    # --- Etap 1: pobranie + parsowanie (rownolegle, bez bazy) ---
    results = []
    if cache_only or workers <= 1:
        for snap in snapshots:
            results.append(compute_correct_authors(snap, cache_only, save_cache, scraper))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(compute_correct_authors, snap, cache_only, save_cache, scraper)
                       for snap in snapshots]
            for fut in as_completed(futures):
                results.append(fut.result())

    # --- Etap 2: analiza + zapis (tylko watek glowny) ---
    changed = ok = skipped = 0
    log_rows = []

    with get_session() as session:
        for r in results:
            if r["correct"] is None:
                skipped += 1
                if not dry_run:
                    record_progress(session, r, "skipped")
                continue

            if r["correct"] == r["current"]:
                ok += 1
                if not dry_run:
                    record_progress(session, r, "ok")
                continue

            # Wykryto roznice -> korekta.
            log_rows.append({
                "book_id": r["book_id"], "external_id": r["external_id"], "url": r["url"],
                "old_authors": "; ".join(r["current"]), "new_authors": "; ".join(r["correct"]),
                "source": r["source"],
            })
            if not dry_run:
                apply_fix(session, r["book_id"], r["correct"])
                record_progress(session, r, "fixed")
                changed += 1
        if not dry_run:
            session.commit()

    _write_log(log_rows)
    _print_summary(total, ok, changed, skipped, dry_run, log_rows)


def _write_log(log_rows):
    if not log_rows:
        return
    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "book_id", "external_id", "url",
                                               "old_authors", "new_authors", "source"])
        if write_header:
            writer.writeheader()
        ts = datetime.utcnow().isoformat()
        for row in log_rows:
            writer.writerow({"timestamp": ts, **row})


def _print_summary(total, ok, changed, skipped, dry_run, log_rows):
    table = Table(title="Naprawa autorow - podsumowanie", header_style="bold magenta")
    table.add_column("Metryka", style="cyan")
    table.add_column("Wartosc", style="green", justify="right")
    table.add_row("Sprawdzone w tym przebiegu", str(total))
    table.add_row("Bez zmian (juz poprawne)", str(ok))
    table.add_row("Wykryte do korekty", str(len(log_rows)))
    table.add_row("Zaktualizowane rekordy", str(changed if not dry_run else 0))
    table.add_row("Pominiete (404/blad/brak autora)", str(skipped))
    table.add_row("Tryb", "DRY-RUN (bez zapisu)" if dry_run else "ZAPIS")
    console.print("\n")
    console.print(table)
    if log_rows:
        console.print(f"\nSzczegolowy log zmian: [bold]{LOG_PATH}[/bold]\n")
        preview = Table(title="Przyklady korekt (max 15)", header_style="bold yellow")
        for col in ("ID", "ext_id", "Stary autor", "Nowy autor", "Zrodlo"):
            preview.add_column(col)
        for row in log_rows[:15]:
            preview.add_row(str(row["book_id"]), str(row["external_id"]),
                            row["old_authors"][:45] or "(brak)",
                            row["new_authors"][:45] or "(brak)", row["source"])
        console.print(preview)


def main():
    parser = argparse.ArgumentParser(description="Naprawa autorow ksiazek w istniejacej bazie.")
    parser.add_argument("--dry-run", action="store_true", help="Tylko raport, bez zapisu (nie zapisuje tez postepu).")
    parser.add_argument("--cache-only", action="store_true", help="Tylko cache HTML, bez sieci.")
    parser.add_argument("--limit", type=int, default=0, help="Maks. liczba ksiazek w tym przebiegu (0 = wszystkie).")
    parser.add_argument("--workers", type=int, default=1, help="Watki pobierajace (zapisy zawsze 1-watkowe).")
    parser.add_argument("--recheck", action="store_true", help="Ignoruj postep - zweryfikuj ponownie wszystkie.")
    parser.add_argument("--save-cache", action="store_true", help="Zapisuj pobrany HTML do cache (tansze kolejne przebiegi).")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO")

    repair(
        dry_run=args.dry_run,
        cache_only=args.cache_only,
        limit=args.limit if args.limit > 0 else None,
        workers=max(1, args.workers),
        recheck=args.recheck,
        save_cache=args.save_cache,
    )


if __name__ == "__main__":
    main()
