"""Test obciazeniowy / wykrywanie race conditions przy przypisywaniu autorow.

Test jest CALKOWICIE OFFLINE i deterministyczny:
  * podmienia warstwe HTTP (fake session) - kazdy URL ksiazki zwraca HTML,
    w ktorym PRAWIDLOWY autor to "Autor {id}", a dodatkowo na stronie znajduja
    sie "pulapki": linki do autorow INNYCH ksiazek (jak w realnych widgetach
    "Podobne ksiazki"/rekomendacje). Poprawny parser MUSI je zignorowac.
  * uzywa tymczasowej bazy SQLite.
  * uruchamia prawdziwa sciezke scrapera (kolejka + ThreadPoolExecutor)
    dla 8, 16 i 32 watkow.

Po kazdym przebiegu sprawdza, czy KAZDA ksiazka ma dokladnie swojego autora.
Jakiekolwiek pomieszanie autorow = wykryty race condition / blad wspoldzielenia.

Uzycie:
    python stress_test.py
    python stress_test.py --books 500 --workers 8 16 32 64
"""

import argparse
import os
import sys
import tempfile

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from rich.console import Console
from rich.table import Table

import config
import database
from models import Base, Book

console = Console()

# Przyspieszamy test - zerujemy opoznienia "grzecznosciowe" i limit szybkosci
# (test jest offline, wiec nie ma sensu pacing).
config.MIN_DELAY = 0.0
config.MAX_DELAY = 0.0
config.REQUESTS_PER_SECOND = 1_000_000.0
config.MAX_HTTP_RETRIES = 1


BASE = "https://lubimyczytac.pl"


def make_html(book_id: int, trap_id: int) -> str:
    """HTML strony ksiazki: poprawny autor + pulapka (autor innej ksiazki)."""
    return f"""<!DOCTYPE html><html><head><title>Ksiazka {book_id}</title></head>
<body>
  <div class="title-container">
    <h1 class="book__title">Tytul ksiazki {book_id}</h1>
    <div class="book__author">
      <a class="link-name" href="/autor/{book_id}/autor-{book_id}">Autor {book_id}</a>
    </div>
  </div>

  <!-- PULAPKA: widget z innymi ksiazkami / rekomendacje.
       Poprawny parser NIE moze tu zagladac. -->
  <section class="book__similar">
    <a href="/autor/{trap_id}/autor-{trap_id}">Autor {trap_id}</a>
    <a href="/autor/{trap_id + 1}/autor-{trap_id + 1}">Autor {trap_id + 1}</a>
  </section>
  <footer>
    <a href="/autor/0/redakcja">Redakcja</a>
  </footer>
</body></html>"""


class FakeResponse:
    def __init__(self, url, text, status_code=200):
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTPSession:
    """Bezstanowa, read-only sesja HTTP - bezpieczna do wspoldzielenia w tescie.

    Mapuje URL ksiazki na wygenerowany HTML. Stanowy bug parsera lub bazy
    ujawni sie jako pomieszanie autorow w wyniku koncowym.
    """

    def __init__(self, n_books: int):
        self.n_books = n_books

    def get(self, url, **kwargs):
        import re
        m = re.search(r"/ksiazka/(\d+)/", url)
        if not m:
            return FakeResponse(url, "<html></html>", 404)
        book_id = int(m.group(1))
        trap_id = (book_id % self.n_books) + 1  # inny autor jako pulapka
        return FakeResponse(url, make_html(book_id, trap_id))


def setup_temp_db(db_path: str):
    """Przekierowuje database.SessionLocal/engine na tymczasowa baze."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 60, "check_same_thread": False},
        pool_size=20,
        max_overflow=40,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=60000")
        cur.close()

    Base.metadata.create_all(engine)
    database.engine = engine
    database.SessionLocal = sessionmaker(bind=engine)


def run_once(n_books: int, workers: int) -> dict:
    # Swieza baza na kazdy przebieg.
    tmpdir = tempfile.mkdtemp(prefix="lc_stress_")
    db_path = os.path.join(tmpdir, "stress.db")
    setup_temp_db(db_path)

    # Import PO ustawieniu bazy - scraper/progress czytaja database.SessionLocal
    # dynamicznie w get_session(), wiec podmiana dziala.
    from progress import add_many_to_queue
    from scraper import Scraper

    # Kolejkujemy n_books ksiazek.
    links = [
        {"url": f"{BASE}/ksiazka/{i}/ksiazka-{i}", "type": "book", "priority": 10}
        for i in range(1, n_books + 1)
    ]
    add_many_to_queue(links)

    scraper = Scraper(mode="full-scan", max_workers=workers)
    # Podmiana warstwy HTTP na fake (bez sieci).
    fake = FakeHTTPSession(n_books)
    scraper.get_http_session = lambda: fake

    scraper.run_queue()

    # Weryfikacja.
    mismatches = []
    missing = []
    with database.get_session() as s:
        books = s.query(Book).all()
        found_ids = set()
        for b in books:
            found_ids.add(b.external_id)
            names = sorted(a.name for a in b.authors)
            expected = [f"Autor {b.external_id}"]
            if names != expected:
                mismatches.append({
                    "external_id": b.external_id,
                    "expected": expected,
                    "got": names,
                })
        for i in range(1, n_books + 1):
            if i not in found_ids:
                missing.append(i)

    return {
        "workers": workers,
        "books_expected": n_books,
        "books_saved": len(found_ids),
        "missing": missing,
        "mismatches": mismatches,
    }


def main():
    parser = argparse.ArgumentParser(description="Test obciazeniowy poprawnosci autorow.")
    parser.add_argument("--books", type=int, default=300, help="Liczba ksiazek na przebieg.")
    parser.add_argument("--workers", type=int, nargs="+", default=[8, 16, 32],
                        help="Lista liczby watkow do przetestowania.")
    args = parser.parse_args()

    results = []
    for w in args.workers:
        console.print(f"\n[bold cyan]=== Przebieg: {w} watkow, {args.books} ksiazek ===[/bold cyan]")
        results.append(run_once(args.books, w))

    table = Table(title="Wynik testu obciazeniowego", header_style="bold magenta")
    table.add_column("Watki", justify="right")
    table.add_column("Oczekiwane", justify="right")
    table.add_column("Zapisane", justify="right")
    table.add_column("Braki", justify="right")
    table.add_column("Pomieszani autorzy", justify="right", style="red")
    table.add_column("Wynik")

    all_ok = True
    for r in results:
        ok = not r["mismatches"] and not r["missing"]
        all_ok = all_ok and ok
        table.add_row(
            str(r["workers"]),
            str(r["books_expected"]),
            str(r["books_saved"]),
            str(len(r["missing"])),
            str(len(r["mismatches"])),
            "[green]OK[/green]" if ok else "[red]FAIL[/red]",
        )
    console.print("\n")
    console.print(table)

    # Szczegoly bledow (max 10 na przebieg).
    for r in results:
        if r["mismatches"]:
            console.print(f"\n[red]Pomieszani autorzy przy {r['workers']} watkach (max 10):[/red]")
            for mm in r["mismatches"][:10]:
                console.print(f"  ksiazka ext_id={mm['external_id']}: "
                              f"oczekiwano {mm['expected']}, otrzymano {mm['got']}")

    if all_ok:
        console.print("\n[bold green]SUKCES: brak race conditions, autorzy poprawni przy kazdej liczbie watkow.[/bold green]\n")
        sys.exit(0)
    else:
        console.print("\n[bold red]PORAZKA: wykryto pomieszanie autorow lub braki rekordow.[/bold red]\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
