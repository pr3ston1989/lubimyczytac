"""Testy integracyjne: zapis do bazy + idempotentna przebudowa autorow.

Sprawdza, ze ponowne scrapowanie USUWA bledne powiazania autorow zamiast je
akumulowac (kluczowe dla naprawy juz uszkodzonych danych przez rescrape).

Wymaga SQLAlchemy + bs4 + lxml. Gdy brak - testy pomijane.
"""

import os
import tempfile
import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("bs4")
pytest.importorskip("lxml")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
from models import Base, Book, Author


URL = "https://lubimyczytac.pl/ksiazka/555/test"


@pytest.fixture()
def temp_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="lc_test_")
    db_path = os.path.join(tmpdir, "t.db")
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", SessionLocal)
    yield SessionLocal


def _html(author_name: str, trap_name: str = "Obcy Autor") -> str:
    return f"""<html><body>
      <div class="title-container">
        <h1 class="book__title">Tytul 555</h1>
        <div class="book__author">
          <a class="link-name" href="/autor/555/a">{author_name}</a>
        </div>
      </div>
      <section class="book__similar"><a href="/autor/999/x">{trap_name}</a></section>
    </body></html>"""


def test_rescrape_replaces_wrong_author(temp_db, monkeypatch):
    import scraper
    # cache HTML do katalogu tymczasowego, by nie zasmiecac repo
    monkeypatch.setattr(scraper, "CACHE_DIR", tempfile.mkdtemp(prefix="lc_cache_"))

    s = scraper.Scraper()

    # 1. Pierwszy zapis - poprawny autor.
    with database.get_session() as db:
        s.process_book_page(URL, _html("Wlasciwy Autor"), db)

    with temp_db() as db:
        book = db.query(Book).filter_by(external_id=555).first()
        assert book is not None
        assert sorted(a.name for a in book.authors) == ["Wlasciwy Autor"]

    # 2. Symulujemy USZKODZENIE danych: recznie dopinamy obcego autora.
    with temp_db() as db:
        book = db.query(Book).filter_by(external_id=555).first()
        bad = Author(name="Przypadkowy Obcy")
        db.add(bad)
        db.flush()
        book.authors.append(bad)
        db.commit()

    with temp_db() as db:
        book = db.query(Book).filter_by(external_id=555).first()
        assert len(book.authors) == 2  # uszkodzone

    # 3. Ponowny scrape z poprawnym HTML musi przywrocic JEDNEGO autora.
    with database.get_session() as db:
        s.process_book_page(URL, _html("Wlasciwy Autor"), db)

    with temp_db() as db:
        book = db.query(Book).filter_by(external_id=555).first()
        assert sorted(a.name for a in book.authors) == ["Wlasciwy Autor"]


def test_trap_author_never_saved(temp_db, monkeypatch):
    import scraper
    monkeypatch.setattr(scraper, "CACHE_DIR", tempfile.mkdtemp(prefix="lc_cache_"))
    s = scraper.Scraper()

    with database.get_session() as db:
        s.process_book_page(URL, _html("Prawdziwy", trap_name="Z Rekomendacji"), db)

    with temp_db() as db:
        book = db.query(Book).filter_by(external_id=555).first()
        names = [a.name for a in book.authors]
        assert names == ["Prawdziwy"]
        # autor-pulapka nie zostal nawet utworzony jako rekord
        assert db.query(Author).filter_by(name="Z Rekomendacji").first() is None
