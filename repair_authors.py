"""Naprawa juz zapisanych danych ksiazek (migracja danych) - wersja STRUMIENIOWA.

KONTEKST: wadliwy parser dopisywal do KAZDEJ ksiazki wszystkich autorow
napotkanych na stronie (widgety/rekomendacje/recenzje), wiec praktycznie
WSZYSTKIE rekordy maja nadmiarowych autorow. Czesc ksiazek ma jednak kilku
autorow naprawde - dlatego trzeba zweryfikowac KAZDA ksiazke wzgledem zrodla.

Zrodlo prawdy: JSON-LD (@type=Book) -> data-ga-book-authors -> naglowek
(patrz parser.extract_authors). Opcjonalnie naprawiamy tez wydawce.

PAMIEC (wazne): skrypt NIE laduje calej bazy do RAM. Przechodzi przez ksiazki
OKNAMI po ID (Book.id > last_id LIMIT window), przetwarza okno, zapisuje je i
zwalnia. Dzieki temu zuzycie pamieci jest stale, niezaleznie od liczby ksiazek
(testowane logicznie dla 300k+ rekordow).

WZNAWIALNOSC: postep w tabeli author_repair_progress (CREATE TABLE IF NOT EXISTS).
Pominiecie juz przetworzonych realizowane jest w zapytaniu SQL (NOT IN), wiec
nie trzymamy zbioru ID w pamieci.

429 / tempo: globalny limiter (Scraper.request, AIMD). Reguluj --rps.

Uzycie:
    python repair_authors.py --dry-run --limit 50
    python repair_authors.py --workers 8 --rps 2 --save-cache --fix-publisher
    python repair_authors.py --workers 8 --rps 2            # wznowienie po Ctrl+C
    python repair_authors.py --recheck                      # weryfikuj od nowa
    python repair_authors.py --cache-only                   # bez sieci
"""

import argparse
import csv
import os
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.progress import (Progress, SpinnerColumn, TextColumn, BarColumn,
                           TaskProgressColumn, TimeRemainingColumn)
from sqlalchemy import select, text, func

import config
from runtime import (STOP, request_stop, Interrupted, iter_done,
                     install_sigint, restore_sigint)
from database import get_session, init_db
from models import Book, Author, Publisher, book_authors
from parser import extract_book_info

CACHE_DIR = os.path.join("data", "html_cache")
LOG_PATH = "repair_log.csv"
console = Console()

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS author_repair_progress (
    book_id      INTEGER PRIMARY KEY,
    external_id  INTEGER,
    status       TEXT,
    old_authors  TEXT,
    new_authors  TEXT,
    source       TEXT,
    skip_reason  TEXT,
    checked_at   TEXT
)
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_progress_table(session):
    session.execute(text(PROGRESS_DDL))
    session.commit()


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
            "book_id": r["book_id"], "external_id": r["external_id"], "status": status,
            "old_authors": "; ".join(r["current"]),
            "new_authors": "; ".join(r["correct"]) if r["correct"] else "",
            "source": r["source"], "skip_reason": r["skip_reason"], "checked_at": _now(),
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


def compute_correct_data(snapshot: dict, cache_only: bool, save_cache: bool, scraper):
    """Pobiera + parsuje JEDNA ksiazke (bez bazy). Zwraca dict wyniku.
    HTML nie jest przechowywany po sparsowaniu - oszczednosc pamieci."""
    result = {
        "book_id": snapshot["id"], "external_id": snapshot["external_id"], "url": snapshot["url"],
        "current": snapshot["current"], "current_publisher": snapshot["current_publisher"],
        "correct": None, "correct_publisher": None, "source": None, "skip_reason": None,
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
    del html  # zwolnij od razu
    if not data:
        result["skip_reason"] = "parse-failed"
        return result

    names = sorted({a["name"] for a in data.get("authors", []) if a.get("name")})
    if not names:
        result["skip_reason"] = "brak-autora-w-zrodle"
        return result
    result["correct"] = names
    result["correct_publisher"] = (data.get("publisher") or {}).get("name")
    result["source"] = source
    return result


def count_remaining(recheck: bool) -> int:
    """Szybki licznik do paska postepu/ETA (bez ladowania rekordow)."""
    with get_session() as session:
        total = session.execute(select(func.count(Book.id))).scalar() or 0
        if recheck:
            return total
        done = session.execute(
            text("SELECT COUNT(*) FROM author_repair_progress WHERE status IN ('fixed','ok')")
        ).scalar() or 0
        return max(0, total - done)


def iter_windows(recheck: bool, limit, window: int):
    """Generator OKIEN ksiazek (lista lekkich dict). Stala pamiec.

    Pomijanie juz przetworzonych realizowane w SQL (NOT IN), wiec nie trzymamy
    zbioru ID w RAM. Autorzy i wydawca doczytywani sa hurtem dla calego okna.
    """
    last_id = 0
    yielded = 0
    skip_clause = "" if recheck else (
        " AND books.id NOT IN (SELECT book_id FROM author_repair_progress "
        "WHERE status IN ('fixed','ok'))"
    )
    while True:
        if limit and yielded >= limit:
            return
        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, external_id, url, type FROM books "
                    "WHERE id > :last_id" + skip_clause +
                    " ORDER BY id ASC LIMIT :win"
                ),
                {"last_id": last_id, "win": window},
            ).all()

            if not rows:
                return
            last_id = rows[-1][0]
            ids = [r[0] for r in rows]

            # Autorzy calego okna - jedno zapytanie.
            auth_map = {}
            for bid, name in session.execute(
                select(book_authors.c.book_id, Author.name)
                .select_from(book_authors.join(Author, Author.id == book_authors.c.author_id))
                .where(book_authors.c.book_id.in_(ids))
            ).all():
                auth_map.setdefault(bid, []).append(name)

            # Wydawca calego okna - jedno zapytanie.
            pub_map = {}
            for bid, pname in session.execute(
                select(Book.id, Publisher.name)
                .select_from(Book.__table__.join(Publisher.__table__,
                             Book.publisher_id == Publisher.id, isouter=True))
                .where(Book.id.in_(ids))
            ).all():
                pub_map[bid] = pname

        batch = []
        for bid, ext, url, typ in rows:
            if limit and yielded >= limit:
                break
            batch.append({
                "id": bid, "external_id": ext, "url": url, "type": typ,
                "current": sorted(set(auth_map.get(bid, []))),
                "current_publisher": pub_map.get(bid),
            })
            yielded += 1
        if batch:
            yield batch


def apply_author_fix(session, book_id: int, correct_names: list):
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


def apply_publisher_fix(session, book_id: int, pub_name: str):
    book = session.get(Book, book_id)
    if book is None or not pub_name:
        return
    pub = session.query(Publisher).filter_by(name=pub_name).first()
    if not pub:
        pub = Publisher(name=pub_name)
        session.add(pub)
        session.flush()
    book.publisher = pub


def repair(dry_run, cache_only, limit, workers, recheck, save_cache, fix_publisher, rps, window):
    if rps:
        config.REQUESTS_PER_SECOND = rps
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

    total_est = count_remaining(recheck)
    if limit:
        total_est = min(total_est, limit)
    logger.info(f"Do sprawdzenia (szac.): {total_est} ksiazek | okno={window} | "
                f"watki={workers} | tempo~{config.REQUESTS_PER_SECOND}/s | wydawca={fix_publisher}")
    if total_est == 0:
        console.print("[green]Brak ksiazek do przetworzenia.[/green]")
        return

    counters = {"changed": 0, "ok": 0, "skipped": 0, "pub_changed": 0}
    preview_rows = []
    processed = 0

    use_bar = console.is_terminal
    progress_cm = Progress(
        SpinnerColumn(),
        TextColumn("[bold green]Naprawa:[/bold green] {task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[cyan]fix:{task.fields[fixed]} ok:{task.fields[ok]} skip:{task.fields[skip]}"),
        TimeRemainingColumn(),
    ) if use_bar else nullcontext()

    def _rate():
        return f" | tempo {scraper.rate_limiter.current_rps:.2f}/s" if scraper else ""

    # Jeden executor na cale uruchomienie. Bez 'with' - sterujemy zamknieciem
    # recznie, by Ctrl+C nie czekal na wszystkie zakolejkowane zadania.
    executor = ThreadPoolExecutor(max_workers=workers) if not (cache_only or workers <= 1) else None
    prev_sigint = install_sigint()

    with progress_cm as progress:
        task = progress.add_task("start", total=total_est, fixed=0, ok=0, skip=0) if use_bar else None
        try:
            for batch in iter_windows(recheck, limit, window):
                if STOP.is_set():
                    break
                # --- Etap 1: pobranie + parsowanie rownolegle (bez bazy) ---
                results = []
                if executor is None:
                    for snap in batch:
                        if STOP.is_set():
                            break
                        results.append(compute_correct_data(snap, cache_only, save_cache, scraper))
                        if use_bar:
                            progress.update(task, advance=1)
                else:
                    futs = [executor.submit(compute_correct_data, s, cache_only, save_cache, scraper)
                            for s in batch]
                    try:
                        for fut in iter_done(futs):
                            results.append(fut.result())
                            if use_bar:
                                progress.update(task, advance=1)
                    except (KeyboardInterrupt, Interrupted):
                        request_stop()
                    if STOP.is_set():
                        # Ctrl+C: porzuc niezakolejkowane, zapisz to co juz gotowe.
                        for f in futs:
                            f.cancel()

                # --- Etap 2: zapis tego okna (tylko watek glowny) ---
                chunk_log = []
                with get_session() as session:
                    for r in results:
                        if r["correct"] is None:
                            counters["skipped"] += 1
                            if not dry_run:
                                record_progress(session, r, "skipped")
                            continue
                        author_diff = r["correct"] != r["current"]
                        pub_diff = (fix_publisher and r["correct_publisher"]
                                    and r["correct_publisher"] != r["current_publisher"])
                        if not author_diff and not pub_diff:
                            counters["ok"] += 1
                            if not dry_run:
                                record_progress(session, r, "ok")
                            continue
                        if author_diff:
                            chunk_log.append({
                                "book_id": r["book_id"], "external_id": r["external_id"], "url": r["url"],
                                "field": "authors", "old": "; ".join(r["current"]),
                                "new": "; ".join(r["correct"]), "source": r["source"],
                            })
                            if not dry_run:
                                apply_author_fix(session, r["book_id"], r["correct"])
                            counters["changed"] += 1
                            if len(preview_rows) < 15:
                                preview_rows.append((r["external_id"], "; ".join(r["current"]),
                                                     "; ".join(r["correct"])))
                        if pub_diff:
                            chunk_log.append({
                                "book_id": r["book_id"], "external_id": r["external_id"], "url": r["url"],
                                "field": "publisher", "old": r["current_publisher"] or "",
                                "new": r["correct_publisher"], "source": r["source"],
                            })
                            if not dry_run:
                                apply_publisher_fix(session, r["book_id"], r["correct_publisher"])
                            counters["pub_changed"] += 1
                        if not dry_run:
                            record_progress(session, r, "fixed")
                    if not dry_run:
                        session.commit()

                _write_log(chunk_log)
                processed += len(batch)
                if use_bar:
                    progress.update(task, description=f"...ID {batch[-1]['external_id']}",
                                    fixed=counters["changed"], ok=counters["ok"], skip=counters["skipped"])
                pct = processed * 100.0 / total_est if total_est else 0
                logger.info(f"Postep: {processed}/{total_est} ({pct:.1f}%) | fix={counters['changed']} "
                            f"ok={counters['ok']} skip={counters['skipped']}{_rate()}")
        except KeyboardInterrupt:
            request_stop()
            logger.warning("Przerwano (Ctrl+C). Postep tego i poprzednich okien zapisany - "
                           "uruchom ponownie, aby wznowic.")
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            restore_sigint(prev_sigint)

    _print_summary(processed, counters, dry_run, fix_publisher, preview_rows)


def _write_log(log_rows):
    if not log_rows:
        return
    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "book_id", "external_id", "url",
                                               "field", "old", "new", "source"])
        if write_header:
            writer.writeheader()
        ts = _now()
        for row in log_rows:
            writer.writerow({"timestamp": ts, **row})


def _print_summary(processed, c, dry_run, fix_publisher, preview_rows):
    table = Table(title="Naprawa - podsumowanie", header_style="bold magenta")
    table.add_column("Metryka", style="cyan")
    table.add_column("Wartosc", style="green", justify="right")
    table.add_row("Przetworzone w tym uruchomieniu", str(processed))
    table.add_row("Bez zmian (juz poprawne)", str(c["ok"]))
    table.add_row("Poprawieni autorzy", str(c["changed"] if not dry_run else 0) + (" (dry-run)" if dry_run else ""))
    if fix_publisher:
        table.add_row("Poprawiony wydawca", str(c["pub_changed"] if not dry_run else 0) + (" (dry-run)" if dry_run else ""))
    table.add_row("Pominiete (404/blad/brak autora)", str(c["skipped"]))
    table.add_row("Tryb", "DRY-RUN (bez zapisu)" if dry_run else "ZAPIS")
    console.print("\n")
    console.print(table)
    if preview_rows:
        console.print(f"\nSzczegolowy log zmian: [bold]{LOG_PATH}[/bold]\n")
        preview = Table(title="Przyklady korekt autorow (max 15)", header_style="bold yellow")
        preview.add_column("ext_id", justify="right")
        preview.add_column("Stary autor", style="red")
        preview.add_column("Nowy autor", style="green")
        for ext_id, old, new in preview_rows:
            preview.add_row(str(ext_id), old[:50] or "(brak)", new[:50] or "(brak)")
        console.print(preview)


def main():
    parser = argparse.ArgumentParser(description="Naprawa autorow (i opcjonalnie wydawcy) - strumieniowa, niskopamieciowa.")
    parser.add_argument("--dry-run", action="store_true", help="Tylko raport, bez zapisu i bez postepu.")
    parser.add_argument("--cache-only", action="store_true", help="Tylko cache HTML, bez sieci.")
    parser.add_argument("--limit", type=int, default=0, help="Maks. liczba ksiazek w tym uruchomieniu (0 = wszystkie).")
    parser.add_argument("--workers", type=int, default=1, help="Watki pobierajace (zapisy zawsze 1-watkowe).")
    parser.add_argument("--rps", type=float, default=0.0, help="Globalne tempo zadan/s (np. 2).")
    parser.add_argument("--window", type=int, default=400, help="Rozmiar okna ksiazek trzymanych w pamieci.")
    parser.add_argument("--recheck", action="store_true", help="Ignoruj postep - zweryfikuj ponownie wszystkie.")
    parser.add_argument("--save-cache", action="store_true", help="Zapisuj pobrany HTML do cache.")
    parser.add_argument("--fix-publisher", action="store_true", help="Popraw takze wydawce.")
    parser.add_argument("--log-file", default="logs/repair.log", help="Plik logu (rotowany).")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.log_file) or ".", exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(args.log_file, rotation="20 MB", retention="14 days", level="INFO", encoding="utf-8")
    logger.info(f"Logi: {args.log_file}")

    repair(
        dry_run=args.dry_run,
        cache_only=args.cache_only,
        limit=args.limit if args.limit > 0 else None,
        workers=max(1, args.workers),
        recheck=args.recheck,
        save_cache=args.save_cache,
        fix_publisher=args.fix_publisher,
        rps=args.rps if args.rps > 0 else None,
        window=max(50, args.window),
    )


if __name__ == "__main__":
    main()
