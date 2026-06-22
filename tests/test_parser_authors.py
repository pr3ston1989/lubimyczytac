"""Testy jednostkowe ekstrakcji autorow - sedno bledu "obcy autorzy".

Wymaga beautifulsoup4 + lxml. Gdy brak, testy sa pomijane (skip).
"""

import pytest

pytest.importorskip("bs4")
pytest.importorskip("lxml")

from parser import extract_book_info, extract_authors  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

URL = "https://lubimyczytac.pl/ksiazka/123/super-ksiazka"


def _page(author_block: str, extra: str = "") -> str:
    return f"""<html><body>
      <div class="title-container">
        <h1 class="book__title">Super Ksiazka</h1>
        {author_block}
      </div>
      {extra}
    </body></html>"""


def test_only_header_author_is_extracted():
    """Autorzy z widgetow 'Podobne'/rekomendacji NIE moga trafic do ksiazki."""
    trap = """
      <section class="book__similar">
        <a href="/autor/999/jan-obcy">Jan Obcy</a>
        <a href="/autor/888/anna-rekomendacja">Anna Rekomendacja</a>
      </section>
      <footer><a href="/autor/0/redakcja">Redakcja</a></footer>
    """
    html = _page(
        '<div class="book__author"><a class="link-name" href="/autor/1/adam-nowak">Adam Nowak</a></div>',
        trap,
    )
    data = extract_book_info(html, URL)
    assert data is not None
    assert data["authors"] == [{"name": "Adam Nowak"}]


def test_multiple_real_authors_in_header():
    html = _page(
        '<div class="book__author">'
        '<a class="link-name" href="/autor/1/a">Autor Jeden</a>'
        '<a class="link-name" href="/autor/2/b">Autor Dwa</a>'
        '</div>',
        '<section class="book__similar"><a href="/autor/999/x">Obcy</a></section>',
    )
    data = extract_book_info(html, URL)
    assert data["authors"] == [{"name": "Autor Jeden"}, {"name": "Autor Dwa"}]


def test_deduplicates_repeated_author():
    html = _page(
        '<div class="book__author">'
        '<a class="link-name" href="/autor/1/a">Adam Nowak</a>'
        '<a class="link-name" href="/autor/1/a">Adam Nowak</a>'
        '</div>'
    )
    data = extract_book_info(html, URL)
    assert data["authors"] == [{"name": "Adam Nowak"}]


def test_fallback_to_link_name_when_no_container():
    """Brak kontenera -> uzywamy tylko a.link-name (nie calej strony)."""
    soup = BeautifulSoup(
        '<html><body>'
        '<a class="link-name" href="/autor/1/a">Glowny Autor</a>'
        '<section class="book__similar"><a href="/autor/999/x">Obcy</a></section>'
        '</body></html>',
        "lxml",
    )
    authors = extract_authors(soup)
    assert authors == [{"name": "Glowny Autor"}]


def test_no_global_author_harvest():
    """Regresja: globalny select wszystkich /autor/ jest zakazany."""
    html = _page(
        '<div class="book__author"><a class="link-name" href="/autor/1/a">Wlasciwy</a></div>',
        '<div><a href="/autor/2/b">Obcy 2</a><a href="/autor/3/c">Obcy 3</a></div>',
    )
    data = extract_book_info(html, URL)
    names = [a["name"] for a in data["authors"]]
    assert "Obcy 2" not in names and "Obcy 3" not in names
    assert names == ["Wlasciwy"]



# --- Testy na realnym HTML lubimyczytac (fixture: book_kasacja.html) ---

import os  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "book_kasacja.html")


def _load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_real_html_author_from_ga_attribute():
    """Na realnym HTML autor pochodzi z data-ga-book-authors, nie z widgetow."""
    html = _load_fixture()
    data = extract_book_info(html, "https://lubimyczytac.pl/ksiazka/245373/kasacja")
    assert data["authors"] == [{"name": "Remigiusz Mroz"}]


def test_real_html_traps_ignored():
    """Autorzy z 'Inne wydania'/'Czytelnicy polecaja'/stopki nie trafiaja do ksiazki."""
    html = _load_fixture()
    data = extract_book_info(html, "https://lubimyczytac.pl/ksiazka/245373/kasacja")
    names = [a["name"] for a in data["authors"]]
    for trap in ["Anna Obca", "Jan Rekomendacja", "Piotr Polecany", "Maria Inna", "Redakcja"]:
        assert trap not in names


def test_real_html_core_fields():
    html = _load_fixture()
    data = extract_book_info(html, "https://lubimyczytac.pl/ksiazka/245373/kasacja")
    assert data["title"] == "Kasacja"
    assert data["publisher"] == {"name": "Czwarta Strona"}
    assert data["external_id"] == 245373
    assert data["isbn"] == "9788379762477"
    assert data["pages"] == 496


def test_ga_author_fallback_to_span_author():
    """Gdy brak atrybutu data-ga, autor pochodzi z naglowka span.author (nie z pulapek)."""
    html = _load_fixture().replace('data-ga-book-authors="Remigiusz Mroz"', "")
    data = extract_book_info(html, "https://lubimyczytac.pl/ksiazka/245373/kasacja")
    assert data["authors"] == [{"name": "Remigiusz Mroz"}]



# --- Testy JSON-LD (najodporniejsze zrodlo autora) ---

JSONLD_PAGE = """<html><body>
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"Book","name":"Tytul",
 "author":{"@type":"Person","name":"Slawek Gortych"},
 "isbn":"9788383878720","numberOfPages":"416"}
</script>
<section class="container book" data-ga-book-authors="ZLY Autor Z GA"
         data-ga-book-publishers="W.A.B.">
  <h1 class="book__title">Schronisko</h1>
  <span class="author"><a href="/autor/231269/x">Inny Naglowek</a></span>
  <section class="recommended-books"><a href="/autor/9/obcy">Obcy</a></section>
</section>
</body></html>"""


def test_jsonld_author_has_priority():
    """JSON-LD ma pierwszenstwo przed data-ga i naglowkiem."""
    data = extract_book_info(JSONLD_PAGE, "https://lubimyczytac.pl/ksiazka/5189210/schronisko")
    assert data["authors"] == [{"name": "Slawek Gortych"}]


def test_jsonld_multiple_authors():
    html = """<html><body>
    <script type="application/ld+json">
    {"@type":"Book","name":"X","author":[{"@type":"Person","name":"Autor A"},
     {"@type":"Person","name":"Autor B"}]}
    </script>
    <h1 class="book__title">X</h1></body></html>"""
    data = extract_book_info(html, "https://lubimyczytac.pl/ksiazka/1/x")
    assert data["authors"] == [{"name": "Autor A"}, {"name": "Autor B"}]


def test_jsonld_malformed_falls_back_to_ga():
    """Uszkodzony JSON-LD nie wysypuje parsera - schodzi do data-ga."""
    html = JSONLD_PAGE.replace('"author":{"@type":"Person","name":"Slawek Gortych"},', '"author": ,,,')
    data = extract_book_info(html, "https://lubimyczytac.pl/ksiazka/5189210/schronisko")
    # Niepoprawny JSON pomijany; data-ga przejmuje (ZLY Autor Z GA to tu jedyne wiarygodne zrodlo)
    assert data["authors"] == [{"name": "ZLY Autor Z GA"}]
