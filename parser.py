import re
from bs4 import BeautifulSoup
from loguru import logger
from typing import Optional, List, Dict, Any

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

def _clean_list_url(href: str) -> str:
    """Oczyszcza URL typu 'list' zachowując parametr paginacji (page=N) i filtry katalogowe."""
    url = href.split('#')[0]
    if "?" in url:
        base, query = url.split('?', 1)
        # Dla stron katalogowych z filtrami (/katalog/ksiazki?...) - zachowaj caly query string
        if "/katalog/" in base:
            # Zachowaj pelny query string (zawiera filtry category[], authors[] itp.)
            url = f"{base}?{query}"
        else:
            # Dla pozostalych stron (autorzy, cykle, tagi) - zachowaj tylko page=
            page_match = re.search(r'(?:^|&)(page=\d+)', query)
            if page_match:
                url = f"{base}?{page_match.group(1)}"
            else:
                url = base
    return url


def extract_links(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, 'lxml')
    links = []
    seen = set()
    
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('/'):
            href = "https://lubimyczytac.pl" + href
        
        # Ignoruj linki spoza domeny
        if not href.startswith("https://lubimyczytac.pl"):
            continue

        # Usun kotwice
        url = href.split('#')[0]
        
        # Ignoruj puste lub javascript linki
        if not url or url.endswith('javascript:;'):
            continue

        # 1. KSIĄŻKI I AUDIOBOOKI (Cel główny)
        if any(x in url for x in ["/ksiazka/", "/audiobook/"]):
            # Oczysc z query params - ksiazki nie potrzebuja paginacji
            url = url.split('?')[0]
            
            # Ścisła blokada pobocznych podstron danej książki
            if any(x in url for x in ["/opinie/", "/wydania/", "/dodaj", "/cytaty", "/podobne"]):
                continue
                
            if re.search(r"/(ksiazka|audiobook)/(\d+)/", url):
                if url not in seen:
                    seen.add(url)
                    links.append({"url": url, "type": "book", "priority": 10})
                
        # 2. WYLĘGARNIE LINKÓW (Autorzy, Wydawnictwa, Cykle - stary format z ID)
        elif any(x in url for x in ["/autor/", "/cykl/", "/wydawnictwo/"]):
            url = _clean_list_url(url)
            
            # Ścisła blokada śmieci społecznościowych i forów
            if any(x in url for x in ["/opinie", "/dyskusje", "/cytaty", "/wiadomosci", "/oceny",
                                       "/forum/", "/profil/", "/dodaj"]):
                continue
            
            if url not in seen:
                seen.add(url)
                links.append({"url": url, "type": "list", "priority": 5})

        # 3. KATEGORIE (nowy format /kategoria/grupa/nazwa)
        elif "/kategoria/" in url:
            url = _clean_list_url(url)
            if url not in seen:
                seen.add(url)
                links.append({"url": url, "type": "list", "priority": 5})

        # 4. TAGI (format /ksiazki/t/nazwa-tagu lub /tag/)
        elif "/ksiazki/t/" in url or "/tag/" in url:
            url = _clean_list_url(url)
            if url not in seen:
                seen.add(url)
                links.append({"url": url, "type": "list", "priority": 4})

        # 5. STRONY KATALOGOWE Z PAGINACJĄ (nowy format /katalog/...)
        elif "/katalog/" in url or url.rstrip('/') == "https://lubimyczytac.pl/katalog":
            url = _clean_list_url(url)
            if any(x in url for x in ["/dodaj", "/edytuj"]):
                continue
            if url not in seen:
                seen.add(url)
                links.append({"url": url, "type": "list", "priority": 5})
        
        # 6. SEKCJE ZBIORCZE (nowości, zapowiedzi, popularne autorzy, cykle)
        elif any(x in url for x in ["/ksiazki/nowosci", "/ksiazki/zapowiedzi",
                                     "/ksiazki/nowosci-i-zapowiedzi",
                                     "/autorzy", "/cykle",
                                     "/ksiazki/tagi", "/ksiazki/kategorie",
                                     "/patronaty"]):
            url = _clean_list_url(url)
            if url not in seen:
                seen.add(url)
                links.append({"url": url, "type": "list", "priority": 3})

        # 7. STARY FORMAT KATEGORII (/ksiazki/k/ID/nazwa)
        elif re.search(r"/ksiazki/k/\d+/", url):
            url = _clean_list_url(url)
            if url not in seen:
                seen.add(url)
                links.append({"url": url, "type": "list", "priority": 5})
            
    return links


def generate_pagination_urls(base_url: str, max_pages: int = 50) -> List[Dict[str, Any]]:
    """Generuje URL-e paginacyjne dla danej strony listy.
    
    Przydatne gdy strona nie zawiera linków paginacyjnych w HTML
    (np. katalog główny z JS-ową paginacją), ale serwer obsługuje ?page=N.
    """
    links = []
    # Usun istniejacy parametr page jesli jest
    clean_url = re.sub(r'[?&]page=\d+', '', base_url).rstrip('?&')
    separator = '?' if '?' not in clean_url else '&'
    
    for page in range(2, max_pages + 1):
        url = f"{clean_url}{separator}page={page}"
        links.append({"url": url, "type": "list", "priority": 4})
    
    return links