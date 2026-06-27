import re
from bs4 import BeautifulSoup
from loguru import logger
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import config

# Bazowy adres portalu — używany do absolutyzacji linków względnych (zaczynających się od "/")
BASE_URL = "https://lubimyczytac.pl"

# --- Wzorce rozpoznawania typów linków ---
# "Wylęgarnie" linków: strony listujące książki (autorzy, cykle, kategorie,
# wydawnictwa, tagi oraz kategorie po ID). Pająk wchodzi na nie, by znaleźć
# kolejne książki i kolejne wylęgarnie.
#   - /autor/<id>/...           -> strona autora
#   - /cykl/<id>/...            -> strona cyklu
#   - /kategoria/<dział>/<kat>  -> kategoria (po nazwie)
#   - /ksiazki/k/<id>/...       -> kategoria (po ID) — występuje m.in. na stronach cykli
#   - /ksiazki/t/<tag>          -> strona tagu — portal NIE używa już /tag/, tylko /ksiazki/t/
#   - /wydawnictwo/<id>/...     -> strona wydawnictwa
LIST_PATTERNS = (
    "/autor/",
    "/cykl/",
    "/kategoria/",
    "/ksiazki/k/",
    "/ksiazki/t/",
    "/wydawnictwo/",
)

# Podstrony-śmieci, których NIE chcemy dodawać do kolejki (opinie, cytaty,
# dyskusje, formularze dodawania itp.). Sprawdzane jako fragment URL-a.
JUNK_PATTERNS = (
    "/opinie",
    "/dyskusje",
    "/cytat",      # pokrywa zarówno /cytat/<id> jak i /cytaty/...
    "/wiadomosci",
    "/oceny",
    "/dodaj",
    "/wydania",
    "/podobne",
    "/fani",       # lista obserwujących wydawnictwo/autora — bezużyteczna dla zbierania książek
)


def _normalize_url(href: str, keep_page: bool = False) -> Optional[str]:
    """
    Sprowadza surowy atrybut href do kanonicznej, porównywalnej postaci URL.

    Robi następujące rzeczy:
      1. Absolutyzuje linki względne ("/ksiazka/..." -> "https://lubimyczytac.pl/ksiazka/...").
      2. Odrzuca linki spoza domeny lubimyczytac.pl oraz nie-HTTP (mailto:, javascript: itp.).
      3. Usuwa fragment (#...) i — domyślnie — wszystkie parametry zapytania (?...).
      4. Gdy keep_page=True, zachowuje WYŁĄCZNIE numeryczny parametr ?page=N
         (potrzebny dla paginacji list), odrzucając resztę (np. ?sortBy=...).
      5. Ucina końcowy ukośnik ze ścieżki, by "/foo" i "/foo/" były tym samym adresem.

    Argumenty:
        href: surowa wartość atrybutu href z tagu <a>.
        keep_page: czy zachować parametr ?page=N (dla linków do list).

    Zwraca:
        Znormalizowany, absolutny URL (str) albo None, jeśli link należy odrzucić.
    """
    if not href:
        return None

    href = href.strip()

    # Odrzucamy kotwice i pseudo-linki od razu
    if href.startswith("#") or href.lower().startswith(("mailto:", "javascript:", "tel:")):
        return None

    # Absolutyzacja: "//host/..." oraz "/sciezka"
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = BASE_URL + href
    elif not href.startswith("http"):
        # Inny dziwny schemat lub link względny bez wiodącego "/" — pomijamy
        return None

    parsed = urlparse(href)

    # Tylko HTTP(S) i tylko nasza domena (łapie też subdomeny typu www.)
    if parsed.scheme not in ("http", "https"):
        return None
    if "lubimyczytac.pl" not in parsed.netloc:
        return None

    # Budowa parametru query — domyślnie pusty (czyścimy ?sortBy, ?phrase itd.)
    query = ""
    if keep_page:
        for key, value in parse_qsl(parsed.query):
            # Zachowujemy tylko sensowny, dodatni numer strony
            if key == "page" and value.isdigit() and int(value) > 0:
                query = urlencode({"page": value})
                break

    # Normalizacja ścieżki: ucięcie końcowego "/" (ale zostawiamy samotny "/")
    path = parsed.path.rstrip("/") or "/"

    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def extract_pagination_links(html: str, base_url: str, max_list_pages: Optional[int] = None) -> List[str]:
    """
    Na podstawie widgetu paginacji (atrybut data-maxpage) generuje pełen zestaw
    adresów kolejnych stron listy w formacie "<bazowy_url>?page=N".

    Dlaczego tak: portal renderuje paginację na dwa sposoby:
      * Wydawnictwa/kategorie/autorzy: prawdziwe linki <a href="...?page=2"> (GET działa).
      * Cykle/serie: przyciski JS (href="#") ładowane AJAX-em — brak gotowych linków.
    W obu przypadkach w HTML jest jednak <input ... data-maxpage="N">, więc
    odczytujemy maksymalną liczbę stron i sami budujemy adresy ?page=N. Dla list
    AJAX-owych serwer może zignorować parametr (zwróci stronę 1) — wtedy zadziała
    deduplikacja (po URL w kolejce i po external_id książek), więc to bezpieczne.

    Argumenty:
        html: pełny kod HTML analizowanej strony listy.
        base_url: adres analizowanej strony (źródło) — baza dla budowania ?page=N.
        max_list_pages: górny limit liczby generowanych stron (ochrona przed
            ogromnymi kategoriami). None => odczyt z config.MAX_LIST_PAGES.
            Wartość <= 0 oznacza brak limitu (generujemy wszystkie strony).

    Zwraca:
        Listę adresów URL (str) kolejnych stron (od strony 2 wzwyż). Pusta lista,
        jeśli paginacji nie ma lub jest tylko jedna strona.
    """
    if not base_url:
        return []

    soup = BeautifulSoup(html, "lxml")

    # Domyślny limit pobieramy z konfiguracji (0 = bez limitu)
    if max_list_pages is None:
        max_list_pages = getattr(config, "MAX_LIST_PAGES", 0)

    # Szukamy dowolnego elementu z atrybutem data-maxpage (input paginatora)
    maxpage = None
    pager = soup.select_one("[data-maxpage]")
    if pager and pager.get("data-maxpage"):
        try:
            maxpage = int(pager["data-maxpage"])
        except (ValueError, TypeError):
            maxpage = None

    # Brak paginacji albo tylko jedna strona — nie ma czego generować
    if not maxpage or maxpage < 2:
        return []

    # Ostatnia strona do wygenerowania (z uwzględnieniem limitu ochronnego)
    last_page = maxpage
    if max_list_pages and max_list_pages > 0:
        last_page = min(maxpage, max_list_pages)

    # Czysta baza adresu — bez query i fragmentu, z uciętym końcowym "/"
    if not base_url.startswith("http"):
        base_url = BASE_URL + base_url
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/") or "/"
    clean_base = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    # Generujemy adresy od strony 2 (strona 1 to bieżący, już przetwarzany URL)
    return [f"{clean_base}?page={n}" for n in range(2, last_page + 1)]

def clean_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    cleaned = re.sub(r'[^\d]', '', text)
    return int(cleaned) if cleaned else None

def clean_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    # Zamiana przecinka na kropke dla formatu float
    cleaned = re.sub(r'[^\d,.]', '', text).replace(',', '.')
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

def clean_isbn(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # Zostawia tylko cyfry i litery X (poprawny format ISBN-10/13)
    cleaned = re.sub(r'[^0-9X]', '', text.upper())
    return cleaned if cleaned else None

def standardize_date(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # Konwertuje DD.MM.YYYY na YYYY-MM-DD
    match = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', text)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    return text.strip()

def extract_book_info(html: str, url: str) -> Optional[Dict[str, Any]]:
    soup = BeautifulSoup(html, 'lxml')
    
    # Wyciaganie ID i typu z URL
    match = re.search(r"/(ksiazka|audiobook)/(\d+)/([^/]+)", url)
    if not match:
        logger.warning(f"Nie udało się sparsować URL: {url}")
        return None
        
    book_type, external_id, slug = match.groups()
    
    data = {
        "url": url,
        "type": book_type,
        "external_id": int(external_id),
        "slug": slug,
        "title": None,
        "original_title": None,
        "description": None,
        "avg_rating": None,
        "cover_url": None,
        "authors": [],
        "categories": [],
        "publisher": None,
        "series": None,
        "pages": None,
        "duration_minutes": None,
        "release_date": None,
        "premiere_date": None,
        "isbn": None, 
        "translator": None,
        "format": None,
        "volume_number": None,
    }

    # Tytul
    h1 = soup.select_one('h1.book__title')
    if h1:
        data['title'] = h1.get_text(strip=True)

    # Opis
    desc = soup.select_one("div#book-description")
    if desc: 
        raw_desc = desc.get_text("\n", strip=True)
        # Czyszczenie nadmiarowych pustych linii
        data['description'] = re.sub(r'\n+', '\n', raw_desc).replace('\xa0', ' ')

    # Ocena
    rating = soup.select_one(".rating-value .big-number")
    if rating:
        data['avg_rating'] = clean_float(rating.get_text())

    # Autorzy
    for a in soup.select("a[href*='/autor/']"):
        name = a.get_text(strip=True)
        if name and {"name": name} not in data['authors']:
            data['authors'].append({"name": name})

    # Szczegoly ksiazki (tabela dt/dd)
    details_div = soup.select_one("#book-details")
    if details_div:
        dts = details_div.find_all('dt')
        dds = details_div.find_all('dd')
        
        for dt, dd in zip(dts, dds):
            label = dt.get_text(strip=True).lower()
            value = dd.get_text(strip=True)
            
            if 'data wydania' in label and 'pol' not in label:
                data['release_date'] = standardize_date(value)
            elif 'wyd. pol' in label:
                data['premiere_date'] = standardize_date(value)
            elif 'liczba stron' in label:
                data['pages'] = clean_int(value)
            elif 'isbn' in label:
                data['isbn'] = clean_isbn(value)
            elif 'tłumacz' in label:
                data['translator'] = value
            elif 'tytuł oryginału' in label:
                data['original_title'] = value
            elif 'format' in label:
                data['format'] = value
            elif 'czas trwania' in label:
                hours = re.search(r'(\d+)\s*godz', value)
                mins = re.search(r'(\d+)\s*min', value)
                h = int(hours.group(1)) if hours else 0
                m = int(mins.group(1)) if mins else 0
                data['duration_minutes'] = (h * 60) + m
            elif 'cykl' in label:
                a_tag = dd.find('a')
                if a_tag: 
                    raw_series = a_tag.get_text(strip=True)
                    # Rozdzielanie nazwy serii od tomu: "Nazwa (tom 1)"
                    s_match = re.search(r'^(.*?)\s*\(\s*tom\s*([^)]+)\s*\)$', raw_series, re.IGNORECASE)
                    if s_match:
                        data['series'] = {"name": s_match.group(1).strip()}
                        data['volume_number'] = s_match.group(2).strip()
                    else:
                        data['series'] = {"name": raw_series}

    # Wydawnictwo
    pub = soup.select_one("a[href*='/wydawnictwo/']")
    if pub:
        data['publisher'] = {"name": pub.get_text(strip=True)}

    # Kategorie
    for cat in soup.select("a.book__category"):
        items = [c.strip() for c in cat.get_text(strip=True).split(',')]
        for c in items:
            if c and {"name": c} not in data['categories']:
                data['categories'].append({"name": c})

    # Okladka
    cover = soup.select_one("a#js-lightboxCover")
    if cover:
        data['cover_url'] = cover.get("href")
    
    return data

def extract_reviews(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, 'lxml')
    reviews_data = []
    
    # 1. Oficjalna recenzja (jesli istnieje)
    official = soup.select_one(".official-review__collapse .collapse-content, #review .collapse-content")
    if official:
        text = official.get_text("\n", strip=True)
        if text:
            reviews_data.append({
                "username": "Oficjalna Recenzja", 
                "full_text": text,
                "is_featured": True,
                "rating": None 
            })
    
    # 2. Opinie uzytkownikow
    reviews = soup.select("div.comment")
    for rev in reviews:
        if 'official-review' in rev.get('class', []):
            continue

        # Proba pobrania pelnego tekstu
        text_element = rev.select_one("p.expandTextNoJS") or rev.select_one(".comment-cloud__body p")
        if not text_element:
            continue
            
        full_text = text_element.get_text("\n", strip=True)
        if not full_text:
            continue
        
        author_elem = rev.select_one(".reviewer-nick a")
        username = author_elem.get_text(strip=True) if author_elem else "Anonim"
        
        rating_elem = rev.select_one(".rating-value .big-number")
        rating = clean_int(rating_elem.get_text()) if rating_elem else None
        
        reviews_data.append({
            "username": username,
            "full_text": full_text,
            "is_featured": False,
            "rating": rating
        })
        
    return reviews_data

def extract_links(html: str, base_url: Optional[str] = None, max_list_pages: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Wyciąga z kodu HTML wszystkie wartościowe linki do dalszego crawlowania:
      * książki/audiobooki  -> typ "book", priorytet 10 (pobierane w pierwszej kolejności),
      * wylęgarnie list      -> typ "list", priorytet 5 (autorzy, cykle, kategorie,
                                wydawnictwa, tagi, kategorie po ID),
      * kolejne strony list  -> typ "list", priorytet 5 (paginacja ?page=N, generowana
                                z data-maxpage, jeśli podano base_url).

    Argumenty:
        html: pełny kod HTML analizowanej strony.
        base_url: adres analizowanej strony. Jeśli podany ORAZ nie jest to strona
            szczegółów książki, dogeneruje linki paginacji do kolejnych stron listy.
        max_list_pages: limit liczby stron paginacji do wygenerowania (None => z configu).

    Zwraca:
        Listę słowników {"url", "type", "priority"} — zdeduplikowaną po URL.
    """
    soup = BeautifulSoup(html, 'lxml')
    links: List[Dict[str, Any]] = []
    seen = set()

    def add(url: Optional[str], type_: str, priority: int) -> None:
        """Dodaje link do wyniku, pilnując deduplikacji po znormalizowanym URL."""
        if url and url not in seen:
            seen.add(url)
            links.append({"url": url, "type": type_, "priority": priority})

    for a in soup.find_all('a', href=True):
        raw = a['href']

        # 1. KSIĄŻKI I AUDIOBOOKI (cel główny) — najwyższy priorytet
        if "/ksiazka/" in raw or "/audiobook/" in raw:
            url = _normalize_url(raw)  # czyścimy też ?... -> kanoniczny adres książki
            if not url:
                continue
            # Blokada pobocznych podstron książki (opinie, cytaty, wydania, podobne...)
            if any(j in url for j in JUNK_PATTERNS):
                continue
            # Musi to być właściwy adres szczegółów: /ksiazka/<id>/ lub /audiobook/<id>/
            if re.search(r"/(ksiazka|audiobook)/\d+/", url):
                add(url, "book", 10)
            continue

        # 2. WYLĘGARNIE LINKÓW (listy) — autorzy, cykle, kategorie, wydawnictwa, tagi
        if any(p in raw for p in LIST_PATTERNS):
            # Blokada śmieci (opinie, dyskusje, cytaty, listy fanów itp.)
            if any(j in raw for j in JUNK_PATTERNS):
                continue
            # keep_page=True: zachowujemy ?page=N z gotowych linków paginacji
            url = _normalize_url(raw, keep_page=True)
            add(url, "list", 5)
            continue

    # 3. PAGINACJA — dogenerowanie pełnego zakresu stron listy.
    # Robimy to tylko dla stron-list (nie dla szczegółów książki), aby nie tworzyć
    # sztucznych adresów ?page=N na stronach pojedynczych książek.
    if base_url and not re.search(r"/(ksiazka|audiobook)/\d+/", base_url):
        # Nie regenerujemy paginacji ze stron, które same są już stroną N (?page=...),
        # żeby nie powielać tej samej roboty z każdej kolejnej strony.
        if "page=" not in urlparse(base_url).query:
            for page_url in extract_pagination_links(html, base_url, max_list_pages):
                add(page_url, "list", 5)

    return links