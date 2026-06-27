import re
from bs4 import BeautifulSoup
from loguru import logger
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import config

# Bazowy adres portalu
BASE_URL = "https://lubimyczytac.pl"

# "Wylegarnie" linkow: strony listujace ksiazki
LIST_PATTERNS = (
    "/autor/",
    "/cykl/",
    "/kategoria/",
    "/ksiazki/k/",
    "/ksiazki/t/",
    "/wydawnictwo/",
    "/katalog/",
    "/patronaty",
    "/autorzy",
    "/cykle",
    "/ksiazki/nowosci",
    "/ksiazki/zapowiedzi",
    "/ksiazki/nowosci-i-zapowiedzi",
    "/ksiazki/tagi",
    "/ksiazki/kategorie",
)

# Podstrony-smieci, ktorych NIE chcemy dodawac do kolejki
JUNK_PATTERNS = (
    "/opinie",
    "/dyskusje",
    "/cytat",
    "/wiadomosci",
    "/oceny",
    "/dodaj",
    "/wydania",
    "/podobne",
    "/fani",
    "/edytuj",
    "/forum/",
    "/profil/",
)

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

    # Szczegoly ksiazki (tabela dt/dd)
    details_div = soup.select_one("#book-details")
    
    # Autorzy - BEZPIECZNA EKSTRAKCJA
    # Strategia: Na lubimyczytac.pl autor(zy) ksiazki sa wymienieni tuz pod tytulem h1,
    # PRZED jakakolwiek sekcja z naglowkiem h2. Sekcje "Inne ksiazki autora",
    # "Czytelnicy przeczytali" itp. zaczynaja sie od h2.
    # Dlatego: zbierz linki /autor/ ktore pojawiaja sie PRZED pierwszym wystapienie
    # naglowka h2 na stronie (po h1 z tytulem).
    
    if h1:
        # Zbierz elementy nastepujace po h1 az do pierwszego h2
        for sibling in h1.find_all_next():
            if sibling.name == 'h2':
                break  # Dotarlismy do sekcji - koniec szukania autorow
            if sibling.name == 'a' and sibling.get('href', ''):
                href = sibling['href']
                if '/autor/' in href:
                    name = sibling.get_text(strip=True)
                    if name and {"name": name} not in data['authors']:
                        data['authors'].append({"name": name})
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

def _normalize_url(href: str, keep_page: bool = False) -> Optional[str]:
    """Normalizuje URL do kanonicznej postaci. Zachowuje ?page=N jesli keep_page=True."""
    if not href:
        return None
    href = href.strip()
    if href.startswith("#") or href.lower().startswith(("mailto:", "javascript:", "tel:")):
        return None
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = BASE_URL + href
    elif not href.startswith("http"):
        return None

    parsed = urlparse(href)
    if parsed.scheme not in ("http", "https"):
        return None
    if "lubimyczytac.pl" not in parsed.netloc:
        return None

    query = ""
    if keep_page:
        for key, value in parse_qsl(parsed.query):
            if key == "page" and value.isdigit() and int(value) > 0:
                query = urlencode({"page": value})
                break
        # Dla stron katalogowych zachowaj pelny query string (filtry)
        if "/katalog/" in parsed.path and parsed.query:
            query = parsed.query

    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def extract_pagination_links(html: str, base_url: str, max_list_pages: Optional[int] = None) -> List[str]:
    """
    Generuje adresy kolejnych stron listy na podstawie atrybutu data-maxpage.
    Fallback: jesli data-maxpage nie istnieje, generuje do 50 stron.
    """
    if not base_url:
        return []

    soup = BeautifulSoup(html, "lxml")

    if max_list_pages is None:
        max_list_pages = getattr(config, "MAX_LIST_PAGES", 0)

    # Szukamy elementu z atrybutem data-maxpage (widget paginatora)
    maxpage = None
    pager = soup.select_one("[data-maxpage]")
    if pager and pager.get("data-maxpage"):
        try:
            maxpage = int(pager["data-maxpage"])
        except (ValueError, TypeError):
            maxpage = None

    # Fallback: jesli brak data-maxpage, ale strona ma linki do ksiazek, generuj 50 stron
    if not maxpage or maxpage < 2:
        # Sprawdz czy strona w ogole ma ksiazki (jesli nie - nie generuj paginacji)
        has_books = bool(re.search(r'/(ksiazka|audiobook)/\d+/', html))
        if has_books:
            maxpage = 50
        else:
            return []

    last_page = maxpage
    if max_list_pages and max_list_pages > 0:
        last_page = min(maxpage, max_list_pages)

    # Czysta baza adresu
    if not base_url.startswith("http"):
        base_url = BASE_URL + base_url
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/") or "/"
    clean_base = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    return [f"{clean_base}?page={n}" for n in range(2, last_page + 1)]


def extract_links(html: str, base_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Wyciaga z HTML wszystkie wartosciowe linki:
      * ksiazki/audiobooki -> typ "book", priorytet 10
      * wylegarnie list    -> typ "list", priorytet 5
      * paginacja          -> typ "list", priorytet 4
    """
    soup = BeautifulSoup(html, 'lxml')
    links: List[Dict[str, Any]] = []
    seen = set()

    def add(url: Optional[str], type_: str, priority: int) -> None:
        if url and url not in seen:
            seen.add(url)
            links.append({"url": url, "type": type_, "priority": priority})

    for a in soup.find_all('a', href=True):
        raw = a['href']

        # 1. KSIAZKI I AUDIOBOOKI (cel glowny)
        if "/ksiazka/" in raw or "/audiobook/" in raw:
            url = _normalize_url(raw)
            if not url:
                continue
            if any(j in url for j in JUNK_PATTERNS):
                continue
            if re.search(r"/(ksiazka|audiobook)/\d+/", url):
                add(url, "book", 10)
            continue

        # 2. WYLEGARNIE LINKOW (listy)
        if any(p in raw for p in LIST_PATTERNS):
            if any(j in raw for j in JUNK_PATTERNS):
                continue
            url = _normalize_url(raw, keep_page=True)
            add(url, "list", 5)
            continue

    # 3. PAGINACJA — generowanie pelnego zakresu stron
    if base_url and not re.search(r"/(ksiazka|audiobook)/\d+/", base_url):
        if "page=" not in (urlparse(base_url).query or ""):
            for page_url in extract_pagination_links(html, base_url):
                add(page_url, "list", 4)

    return links