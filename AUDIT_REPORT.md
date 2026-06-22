# Audyt, naprawa i optymalizacja scrapera (lubimyczytac.pl)

> Uwaga o środowisku audytu: analiza wykonana w sandboxie bez dostępu do
> internetu (`INTEGRATIONS_ONLY`) i bez możliwości instalacji `bs4`,
> `sqlalchemy`, `curl_cffi`. Dlatego diagnoza opiera się na statycznej analizie
> kodu (każda przyczyna wskazana z konkretną lokalizacją), a dostarczone testy
> i `stress_test.py` są w pełni uruchamialne w docelowym środowisku z
> zainstalowanymi zależnościami. Selektory autora oparto o strukturę kontraktu
> już obecną w kodzie (`h1.book__title`, `#book-details`, `a.book__category`)
> oraz typowy układ strony; lista selektorów jest jednym miejscem do korekty,
> gdyby portal zmienił HTML (patrz `parser.AUTHOR_CONTAINER_SELECTORS`).

---

## Etap 1 — Architektura i przepływ danych

### Komponenty
| Plik | Rola |
|------|------|
| `main.py` | CLI + menu, wybór trybu, wywołania scrapera |
| `config.py` | konfiguracja, losowe nagłówki/User-Agent, opóźnienia |
| `scraper.py` | pobieranie HTTP, orkiestracja kolejki, wątki, zapis książek |
| `parser.py` | parsowanie HTML → słownik danych książki, recenzje, linki |
| `downloader.py` | pobieranie i zapis okładek |
| `database.py` | silnik SQLAlchemy + sesje (SQLite, WAL) |
| `models.py` | modele ORM (`Book`, `Author`, `book_authors`, kolejka itd.) |
| `progress.py` | kolejka zadań (`ScrapeQueue`): pobieranie paczek, statusy, retry |

### Przepływ danych (książka)
```
URL (ScrapeQueue) 
  -> Scraper.process_single_item (wątek z puli)
       -> session.get(url)                      [HTTP -> HTML]
       -> process_book_page(url, html, db)
            -> parser.extract_book_info(html,url) -> dict `data`
                 (title, external_id, authors[], publisher, series, ...)
            -> Book (insert/lookup po external_id)
            -> Author lookup/insert + book.authors
            -> parser.extract_reviews(html) -> Review
            -> downloader.download_cover -> Cover
            -> db.commit()
  -> mark_status(completed)
```

### Gdzie mogło dochodzić do pomieszania autorów (wskazane etapy)
1. **Ekstrakcja autora** (`parser.extract_book_info`) — selektor zbierał autorów
   z całej strony, w tym z innych książek. **(główna przyczyna)**
2. **Pobieranie HTTP** (`scraper.process_single_item`) — współdzielona sesja
   `curl_cffi` między wątkami mogła mieszać odpowiedzi. **(druga przyczyna,
   zależna od liczby wątków)**
3. **Zapis/aktualizacja** (`process_book_page`) — autorzy tylko dopisywani,
   nigdy nie usuwani → błędne powiązania utrwalały się i kumulowały.

---

## Etap 2 — Źródło błędu (z konkretnymi lokalizacjami)

### PRZYCZYNA #1 (główna, deterministyczna): zbyt szeroki selektor autora
**Plik:** `parser.py`, `extract_book_info` (kod sprzed naprawy):
```python
for a in soup.select("a[href*='/autor/']"):
    name = a.get_text(strip=True)
    if name and {"name": name} not in data['authors']:
        data['authors'].append({"name": name})
```
`soup.select("a[href*='/autor/']")` pobiera **każdy** link do autora na stronie.
Strona książki na lubimyczytac zawiera dziesiątki takich linków poza nagłówkiem:
widgety „Inne książki autora”, „Podobne książki”, rekomendacje
(„Czytelnicy polecają”), recenzje odsyłające do innych tytułów, stopka.
Efekt: do książki trafia jej autor **oraz** autorzy zupełnie innych książek →
„losowi autorzy z innych książek”. Błąd występuje nawet przy 1 wątku.

Brak globalnego stanu w parserze (każde wywołanie tworzy własny `soup` i własny
`data`), więc to nie jest race condition — to błąd zakresu selektora.

**Naprawa:** nowa funkcja `parser.extract_authors(soup)` ogranicza wyszukiwanie
do kontenera nagłówka (`AUTHOR_CONTAINER_SELECTORS`), z bezpiecznym fallbackiem
do `a.link-name[href*='/autor/']` (a nie całej strony).

### PRZYCZYNA #2 (zależna od wątków): współdzielona sesja HTTP `curl_cffi`
**Plik:** `scraper.py`
```python
def __init__(...):
    self.session = cureq.Session(impersonate="chrome120")   # JEDNA sesja
...
def process_single_item(self, item_dict):
    res = self.session.get(url, timeout=15)                 # używana w N wątkach
```
`run_queue` uruchamia wiele `process_single_item` równolegle przez
`ThreadPoolExecutor`, wszystkie korzystają z **tej samej** sesji. `curl_cffi`
opakowuje pojedynczy uchwyt libcurl i **nie jest thread-safe**. Skutki przy
>2 wątkach: przeplot zapisów/odczytów, **otrzymanie przez wątek A odpowiedzi
zamówionej przez wątek B** (książka X dostaje HTML książki Y → autorzy Y),
oraz niestabilność/awarie. To drugie, niezależne źródło „pomieszania autorów”,
ujawniające się dokładnie przy zwiększaniu liczby wątków.

Dowód, że to było znane intuicyjnie: `_process_gap_id` **tworzy lokalną sesję**
(`with cureq.Session(...) as local_session`), czyli poprawny wzorzec — ale
ścieżka kolejki (`process_single_item`) go nie stosowała.

**Naprawa:** sesja **per-wątek** (`threading.local`) przez `get_http_session()`.

### PRZYCZYNA #3 (niestabilność wielowątkowa): SQLite `check_same_thread`
**Plik:** `database.py`
```python
engine = create_engine(DATABASE_URL, connect_args={"timeout": 60})
```
Dla pliku SQLite SQLAlchemy używa puli, która może podać połączenie utworzone w
wątku A do wątku B. Domyślne `check_same_thread=True` zgłasza wtedy
`ProgrammingError: SQLite objects created in a thread can only be used in that
same thread`. Przy 8/16/32 wątkach takie błędy narastają → „dodatkowe błędy i
niestabilność”. Dodatkowo brak `busy_timeout` powoduje natychmiastowe
`database is locked` przy równoległych zapisach.

**Naprawa:** `check_same_thread=False`, `PRAGMA busy_timeout=60000`, WAL,
sensowny `pool_size/max_overflow`, `pool_pre_ping=True`.

### PRZYCZYNA #4 (utrwalanie błędu): autorzy tylko dopisywani
**Plik:** `scraper.py`, `process_book_page` (przed naprawą):
```python
if author not in book.authors:
    book.authors.append(author)
```
Ponowne scrapowanie nigdy nie usuwało starych/błędnych powiązań — błąd raz
zapisany pozostawał, a lista autorów rosła.

**Naprawa:** idempotentna przebudowa — `book.authors = desired_authors`
(tylko gdy sparsowano co najmniej jednego autora, by nie wyczyścić danych przy
chwilowym błędzie parsowania). Dzięki temu sam rescrape naprawia rekord.

### Błędy poboczne wykryte przy okazji
- **`process_single_item` ustawiał status `"error"`**, którego nic nie wracało
  do `pending`; istniejący `mark_queue_failed` (retry + dead-letter) **nie był
  używany** w ścieżce kolejki → zadania ginęły. **Naprawione** (podpięto
  `mark_queue_failed` + `log_error`).
- **Zadania w stanie `processing`** po przerwaniu (Ctrl+C/crash) nie wracały do
  kolejki. **Naprawione** — `run_queue` na starcie resetuje `processing→pending`.

---

## Etap 3 — Naprawa kodu (zmienione pliki)

| Plik | Zmiana | Dlaczego naprawia problem |
|------|--------|---------------------------|
| `parser.py` | `extract_authors()` z zakresem do nagłówka (`AUTHOR_CONTAINER_SELECTORS`) + fallback `a.link-name`; podpięte w `extract_book_info` | eliminuje pobieranie autorów z widgetów innych książek (PRZYCZYNA #1) |
| `scraper.py` | `get_http_session()` (thread-local) zamiast `self.session`; użyte w `fetch`, `process_single_item`, `run_daemon` | usuwa współdzielenie niethreadsafe sesji (PRZYCZYNA #2) |
| `scraper.py` | idempotentny `book.authors = desired_authors` | usuwa stare/błędne powiązania przy rescrape (PRZYCZYNA #4) |
| `scraper.py` | `mark_queue_failed` + `log_error` w obsłudze błędu; reset `processing→pending`; jeden `ThreadPoolExecutor` na cały bieg; COUNT raz na paczkę | poprawność retry + wydajność |
| `database.py` | `check_same_thread=False`, `busy_timeout`, pula, `pool_pre_ping` | stabilność przy 8/16/32 wątkach (PRZYCZYNA #3) |

Testy: `tests/test_parser_authors.py` (jednostkowe — zakres selektora,
ignorowanie pułapek, deduplikacja, fallback) oraz
`tests/test_integration_authors.py` (integracyjne — rescrape usuwa błędnego
autora, autor-pułapka nigdy nie zapisany).

---

## Etap 4 — Naprawa istniejących danych: `repair_authors.py`

Skrypt **nie kasuje bazy** i **nie scrapuje całego katalogu**. Dla każdej
książki:
1. bierze źródło HTML **najpierw z lokalnego cache** `data/html_cache/{type}_{external_id}.html`
   (bez sieci), a dopiero gdy brak — pobiera pojedynczą stronę po `book.url`;
2. parsuje **naprawionym** parserem → prawidłowi autorzy;
3. porównuje z obecnymi powiązaniami;
4. gdy różnica — nadpisuje `book.authors` (usuwa błędne);
5. zapisuje log `repair_log.csv`: `timestamp, book_id, external_id, url,
   old_authors, new_authors, source` (źródło: `cache:...` lub `network`).

Wykorzystuje zapisane w rekordzie `external_id`, `slug`, `url`, `type` — zgodnie
z wymaganiem naprawy bez pełnego rescrape.

```bash
python repair_authors.py --dry-run         # tylko raport, bez zapisu
python repair_authors.py --cache-only      # bez sieci (tylko cache HTML)
python repair_authors.py --only-suspicious # tylko rekordy z >1 autorem
python repair_authors.py --limit 1000      # ogranicz zakres
python repair_authors.py                   # właściwa naprawa (cache + sieć)
```
Rekomendacja: najpierw `--dry-run --only-suspicious`, przejrzeć `repair_log.csv`,
potem uruchomić właściwą naprawę.

---

## Etap 5 — Optymalizacja wydajności

| Poziom | Optymalizacja | Zysk | Ryzyko | Status |
|--------|---------------|------|--------|--------|
| Łatwy | Jeden `ThreadPoolExecutor` na cały bieg (zamiast nowej puli na każdą paczkę) | mały–średni (mniej narzutu) | niskie | **zrobione** |
| Łatwy | `COUNT(pending)` raz na paczkę zamiast po każdym zadaniu | średni przy dużej kolejce (eliminuje O(n) zapytań) | niskie | **zrobione** |
| Łatwy | Większe paczki (`max_workers*4`) — pula stale zasilona | mały | niskie | **zrobione** |
| Łatwy | Indeks na `ScrapeQueue.status` + `priority` dla pobierania paczek | średni przy >100k kolejce | niskie | rekomendacja |
| Średni | Pobieranie okładki **poza** transakcją DB (teraz `download_cover` trzyma otwartą transakcję na czas I/O sieci → długie blokady zapisu) | średni–duży przy wielu wątkach | średnie | rekomendacja |
| Średni | Wspólny `soup` dla `extract_book_info` + `extract_links` w trybie spider (teraz HTML parsowany 2×) | średni (CPU) | niskie | rekomendacja |
| Średni | Batchowanie zapisów recenzji / `bulk_save_objects` | średni | średnie | rekomendacja |
| Średni | Cache słownikowy autorów/wydawnictw/kategorii w obrębie biegu (mniej `SELECT` N+1) | średni | niskie | rekomendacja |
| Duży | Przepisanie I/O na `asyncio` + `aiohttp`/`httpx` + producer/consumer | duży (I/O-bound) | wysokie | rekomendacja |
| Duży | Migracja na PostgreSQL przy dużej skali i wysokiej współbieżności zapisu | duży | wysokie | opcjonalnie |

Marnotrawstwo do usunięcia: podwójne parsowanie HTML (spider), powtórne
`COUNT`, transakcja trzymana przez pobieranie okładki, brak indeksu pod zapytanie
kolejki.

---

## Etap 6 — Stabilna wielowątkowość (8 / 16 / 32)

Przyczyny niestabilności przy >2 wątkach i ich rozwiązania:

| Zasób | Problem | Rozwiązanie |
|-------|---------|-------------|
| Sesja HTTP `curl_cffi` | współdzielona, nie thread-safe → mieszanie odpowiedzi, awarie | **sesja per-wątek** (`threading.local`) |
| Połączenia SQLite | `check_same_thread=True` + pula → błąd „can only be used in that same thread” | `check_same_thread=False`, pula, `pool_pre_ping` |
| Blokady zapisu SQLite | „database is locked” przy równoległych zapisach | WAL + `busy_timeout=60000` |
| Sesja ORM | wcześniej OK — `get_session()` daje sesję na operację (session-per-thread) | utrzymane |
| Parser | brak globalnego stanu (lokalny `soup`/`data`) | bez zmian, potwierdzone |
| Kolejka | `get_batch_queue` atomowo oznacza `processing` (brak dublowania) | utrzymane + reset osieroconych `processing` |

Po zmianach 8/16/32 wątki korzystają z izolowanych sesji HTTP i bezpiecznego
dostępu do SQLite. Zapisy nadal serializują się na pojedynczym writerze SQLite
(naturalne ograniczenie) — przy bardzo wysokiej współbieżności zapisu rozważyć
PostgreSQL.

---

## Etap 7 — Test obciążeniowy: `stress_test.py`

Offline i deterministyczny (podmieniona warstwa HTTP + tymczasowa baza).
Każda książka ma w HTML prawidłowego autora `Autor {id}` oraz „pułapki” (linki do
autorów innych książek). Test uruchamia pełną ścieżkę kolejki dla 8/16/32 wątków
i weryfikuje, że **każda** książka ma dokładnie swojego autora. Wykrywa
race conditions i pomieszanie autorów; kończy się kodem wyjścia 0/1 i raportem.

```bash
python stress_test.py
python stress_test.py --books 500 --workers 8 16 32 64
```

---

## Uruchamianie testów

```bash
# zależności
pip install -r requirements.txt

# testy jednostkowe + integracyjne
pytest -v

# test obciążeniowy / race detection
python stress_test.py
```

---

## Dalsze rekomendacje rozwoju

1. **Stałe selektory w jednym miejscu** — wszystkie CSS-selektory wynieść do
   `config`/stałych z testami snapshot na realnym HTML (odporność na zmiany
   portalu).
2. **Asyncio** dla części I/O (pobieranie) z ograniczeniem współbieżności na
   domenę i `connection pooling`.
3. **Okładki poza transakcją** + osobna kolejka pobierania mediów.
4. **PostgreSQL** przy dużej skali i równoległych zapisach.
5. **Walidacja danych** (np. `pydantic`) na granicy parser→zapis: odrzucać
   rekordy bez tytułu/autora, zliczać anomalie (np. >N autorów = podejrzane).
6. **Metryki i alerty** (liczba 404, błędów, średni czas pobrania, % rekordów
   bez autora) — wczesne wykrywanie regresji parsera.
7. **CI** uruchamiające `pytest` + `stress_test.py` na każdym PR.
8. **Idempotentne UPSERT-y** dla recenzji/kategorii analogicznie do autorów.
9. **Respektowanie `robots.txt`** i limitów — poza zakresem audytu, ale istotne
   prawnie/etycznie.



---

# Aktualizacja po dostarczeniu realnego HTML i logów z uruchomienia (6 wątków)

Dwie istotne korekty/uzupełnienia diagnozy na podstawie faktów od użytkownika.

## A. Najpewniejsze źródło autora: atrybut `data-ga-book-authors`
Realny HTML strony książki zawiera na kontenerze:
```html
<section class="container book" id="container-book"
   data-ga-book-authors="Remigiusz Mróz"
   data-ga-book-publishers="Czwarta Strona"
   data-ga-book-category="kryminał, sensacja, thriller"> ...
<span class="author"><a class="dashBoardActivity__singleInfoBookAuthor"
   href="https://lubimyczytac.pl/autor/82094/remigiusz-mroz">Remigiusz Mróz</a></span>
```
Wnioski:
- Stary selektor `soup.select("a[href*='/autor/']")` zbierał m.in. autorów z
  sekcji **„Inne wydania”**, rekomendacji („Czytelnicy polecają”) i stopki →
  potwierdzona przyczyna obcych autorów.
- Realny link autora ma klasę `dashBoardActivity__singleInfoBookAuthor`
  (a nie `link-name`) i leży w `span.author` — mój pierwotny fallback został
  poprawiony.
- **Najlepsze, jednoznaczne źródło to atrybut `data-ga-book-authors`** ustawiany
  przez sam serwis. `parser.extract_authors` używa go teraz jako źródła #1
  (z fallbackiem do `span.author`/`.book__author`). Analogicznie publisher
  korzysta z `data-ga-book-publishers`.
- Nowy test `tests/test_parser_authors.py` weryfikuje to na fixturze
  `tests/fixtures/book_kasacja.html` zbudowanej z realnego HTML (z pułapkami).

## B. Realna przyczyna „błędów przy >2 wątkach”: HTTP 429 (rate-limiting)
Log z uruchomienia (tryb „Skaner ID”, 6 wątków):
```
ERROR | scraper:_process_gap_id:289 - Blad przy lataniu ID 5188584: HTTP 429
... (masowe HTTP 429) ...
```
To **nie crash ani błąd wątków**, lecz **odrzucanie żądań przez serwer**
(Too Many Requests). Przy 6 wątkach łączny ruch przekraczał tolerancję serwisu.
Dodatkowe problemy wykryte w `_process_gap_id`:
- **brak nagłówków** w żądaniu (`local_session.get(url, timeout=15)` bez
  `get_random_headers()`) → łatwiejsze do zablokowania;
- tworzenie **nowej sesji `curl_cffi` na każde ID** (kosztowny handshake TLS,
  brak reużycia połączeń);
- 429 było logowane jako „Blad sieci” i **ID było gubione** (brak ponowienia).

### Wprowadzone poprawki (rate-limiting)
1. **Globalny, współdzielony limiter** `ratelimit.RateLimiter` — ogranicza
   **łączną** liczbę żądań/s wszystkich wątków (domyślnie
   `REQUESTS_PER_SECOND=2`, konfigurowalne env). To liczba żądań/s, a nie liczba
   wątków, decyduje o 429. Każdy wątek rezerwuje slot pod lockiem, a usypia poza
   lockiem (równoległe I/O, globalne tempo).
2. **`Scraper.request()`** — jedna ścieżka HTTP z: rate-limitem, **backoffem dla
   429/503**, respektowaniem **`Retry-After`**, ponawianiem błędów sieciowych
   (`MAX_HTTP_RETRIES`), nagłówkami i sesją per-wątek. Używana w
   `process_single_item`, `run_daemon`, `_process_gap_id`, `fetch`.
3. **Adaptacyjność**: po 429/503 limiter zwiększa odstęp (`penalize`), po sukcesach
   wraca do tempa bazowego (`recover`) — scraper sam się dostraja do limitów.
4. `_process_gap_id` korzysta teraz z sesji per-wątek (reużycie połączeń) i
   wysyła prawidłowe nagłówki.

### Praktyczne zalecenie strojenia
Przy ~19 mln ID skanowanie jest z natury agresywne. Zacznij od
`REQUESTS_PER_SECOND=1.5–2` i `max_workers=8`; jeśli 429 znika — zwiększaj RPS
ostrożnie. Liczba wątków powyżej tego, co przepuszcza limiter, nie zwiększy
przepustowości (wątki i tak czekają na slot), ale nie powoduje już 429.

> Uwaga prawno-etyczna: wysoka częstotliwość odpytywania i skan całego katalogu
> mogą naruszać regulamin/`robots.txt` serwisu. Ustaw konserwatywny RPS i rozważ
> kontakt z właścicielem serwisu.

## Zmienione/nowe pliki (uzupełnienie)
| Plik | Zmiana |
|------|--------|
| `parser.py` | autor z `data-ga-book-authors` (+ poprawny fallback `span.author`); publisher z `data-ga-book-publishers` |
| `ratelimit.py` | **nowy** — globalny adaptacyjny limiter szybkości |
| `config.py` | `REQUESTS_PER_SECOND`, `MAX_HTTP_RETRIES` |
| `scraper.py` | `request()` z rate-limitem + backoff 429/503 (`Retry-After`); użyte we wszystkich ścieżkach HTTP; nagłówki + sesja per-wątek w gap fillerze |
| `tests/fixtures/book_kasacja.html` | **nowy** — fixture z realnego HTML |
| `tests/test_parser_authors.py` | testy na realnym HTML (atrybut GA + ignorowanie pułapek + fallback) |



---

# Aktualizacja: praca na serwerze (SSH) i stabilizacja 429

## Limiter AIMD (stabilne tempo zamiast oscylacji wokol 429)
Wczesniejszy `recover()` mnozyl odstep przez 0.9 po kazdym sukcesie -> przy wielu
watkach kilkanascie sukcesow kasowalo kare po 429 i serwer znow blokowal
(oscylacja widoczna w logach). Limiter dziala teraz w schemacie **AIMD**:
- **429/503 -> mnozenie odstepu** (gwaltowne zwolnienie),
- **sukces -> odjecie malego, stalego kroku** (powolny powrot).
Tempo samo ustala sie tuz ponizej progu blokady. Domyslne
`REQUESTS_PER_SECOND` obnizone do **1.0**; reguluj flaga `--rps` (repair) lub
zmienna `REQUESTS_PER_SECOND` (scraper).

## Logi, postep, przerywanie, wznawianie (SSH-friendly)
- `repair_authors.py`: pasek `rich` tylko w terminalu (TTY); przez SSH/`nohup`
  wypisuje **cykliczny log postepu** (`Postep: X/Y (%) | fix/ok/skip | tempo`).
  Logi do pliku: `--log-file` (domyslnie `logs/repair.log`, rotowany).
- Tryby skanera ID / latania dziur (`scraper.py`): co 200 ID logują
  `zapisane / 404 / bledy / tempo` do `logs/app.log` (widoczne bez TTY).
- **Przerywanie**: `Ctrl+C` bezpieczne - postep zapisywany po kazdej paczce
  (repair) / po kazdym ID (skaner). **Wznawianie**: ponowne uruchomienie pomija
  zrobione (tabela `author_repair_progress` / istniejace rekordy + `archived_error`).

## Przyklady uruchomienia na serwerze
```bash
# Naprawa bazy w tle, log do pliku, bezpieczne tempo:
nohup python repair_authors.py --workers 6 --rps 1 --save-cache --fix-publisher \
      --log-file logs/repair.log >/dev/null 2>&1 &
tail -f logs/repair.log          # podglad postepu

# Latanie dziur w tle:
REQUESTS_PER_SECOND=1 nohup python main.py fill-gaps >/dev/null 2>&1 &
tail -f logs/app.log

# Wznowienie po przerwaniu - ta sama komenda; pomija juz zrobione.
```
Wskazowka: przy niskim `--rps` duzo watkow nie przyspieszy (czekaja na slot
limitera) - 4-8 watkow w zupelnosci wystarcza.
