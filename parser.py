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

def extract_links(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, 'lxml')
    links = []
    seen = set()
    
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('/'):
            href = "https://lubimyczytac.pl" + href
            
        # Oczyszczanie linku z kotwic (#) oraz parametrów sortowania (?)
        url = href.split('#')[0]
        if "?" in url and "strona=" not in url:
            url = url.split('?')[0]
            
        if url in seen:
            continue

        # 1. KSIĄŻKI I AUDIOBOOKI (Cel główny)
        if any(x in url for x in ["/ksiazka/", "/audiobook/"]):
            # Ścisła blokada pobocznych podstron danej książki (np. edycja, cytaty)
            if any(x in url for x in ["/opinie/", "/wydania/", "/dodaj", "/cytaty", "/podobne"]):
                continue
                
            if re.search(r"/(ksiazka|audiobook)/(\d+)/", url):
                seen.add(url)
                # Typ 'book' - najwyższy priorytet (10), od razu scrapujemy
                links.append({"url": url, "type": "book", "priority": 10})
                
        # 2. WYLĘGARNIE LINKÓW (Katalogi, Autorzy, Wydawnictwa, Kategorie, Cykle, Tagi)
        elif any(x in url for x in ["/autor/", "/cykl/", "/kategoria/", "/wydawnictwo/", "/tag/"]):
            # Ścisła blokada śmieci społecznościowych i forów na profilach
            if any(x in url for x in ["/opinie", "/dyskusje", "/cytaty", "/wiadomosci", "/oceny"]):
                continue
                
            seen.add(url)
            # Typ 'list' - niższy priorytet (5). Pająk wejdzie tu w wolnej chwili, 
            # pobierze kod HTML i poszuka w nim nowych linków do książek.
            links.append({"url": url, "type": "list", "priority": 5})
            
    return links