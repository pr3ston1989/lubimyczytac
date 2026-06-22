# LubimyCzytać Scraper — instrukcja i ściąga (cheat sheet)

Kompletny opis wszystkich funkcji, komend i parametrów. Komendy podane dla
**Linux/macOS** (`python3`) oraz **Windows** (`python`). Gdzie się różnią —
zaznaczono osobno.

---

## Spis treści
1. [Instalacja i środowisko](#1-instalacja-i-srodowisko)
2. [Konfiguracja (zmienne środowiskowe)](#2-konfiguracja-zmienne-srodowiskowe)
3. [main.py — scraper (tryby)](#3-mainpy--scraper-tryby)
4. [repair_authors.py — naprawa autorów w bazie](#4-repair_authorspy--naprawa-autorow-w-bazie)
5. [stress_test.py — test wielowątkowości](#5-stress_testpy--test-wielowatkowosci)
6. [pytest — testy jednostkowe/integracyjne](#6-pytest--testy)
7. [Praca na serwerze (SSH, tmux, nohup)](#7-praca-na-serwerze-ssh-tmux-nohup)
8. [Zatrzymywanie i wznawianie](#8-zatrzymywanie-i-wznawianie)
9. [Strojenie szybkości i HTTP 429](#9-strojenie-szybkosci-i-http-429)
10. [Zapytania SQL — diagnostyka i sprzątanie](#10-zapytania-sql--diagnostyka-i-sprzatanie)
11. [Rozwiązywanie problemów](#11-rozwiazywanie-problemow)

---

## 1. Instalacja i środowisko

```bash
# Linux/macOS
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pytest            # tylko do testów
```
```bat
REM Windows
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install pytest
```

Baza tworzy się sama przy pierwszym uruchomieniu w `data/database.db`.
**Zawsze rób kopię przed operacjami zapisu:**
```bash
cp data/database.db data/database.backup.db      # Linux/macOS
```
```bat
copy data\database.db data\database.backup.db    REM Windows
```

---

## 2. Konfiguracja (zmienne środowiskowe)

Ustawiane w pliku `.env` (w katalogu projektu) **lub** w środowisku powłoki.
Wszystkie mają sensowne wartości domyślne.

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/database.db` | Lokalizacja bazy |
| `REQUESTS_PER_SECOND` | `1.0` | **Globalny** limit żądań/s (wszystkie wątki razem). Chroni przed 429. |
| `MAX_HTTP_RETRIES` | `4` | Liczba ponowień przy 429/503/błędach sieci |
| `MIN_DELAY` / `MAX_DELAY` | `1.0` / `3.0` | (legacy) losowe opóźnienie; tempo i tak reguluje limiter |
| `LOG_LEVEL` | `INFO` | Poziom logów |

Przykład pliku `.env`:
```
REQUESTS_PER_SECOND=2
MAX_HTTP_RETRIES=4
```

> W `repair_authors.py` tempo ustawisz też wygodną flagą `--rps` (działa
> wszędzie, także na Windows, bez kombinowania ze zmiennymi środowiskowymi).

---

## 3. main.py — scraper (tryby)

Dwa sposoby użycia: **menu interaktywne** albo **komendy CLI**.

### Menu interaktywne
```bash
python3 main.py            # Linux/macOS
python main.py             # Windows
```
Opcje menu:
| Nr | Funkcja | Opis |
|---|---|---|
| 1 | 🚀 Pełny skan | Zasiewa katalog/nowości i scrapuje kolejkę |
| 2 | 🔄 Wznów pracę | Kontynuuje pobieranie z kolejki |
| 3 | ✨ Pobierz nowości | Skanuje tylko najnowsze dodania |
| 4 | 🤖 Tryb Daemon | Szuka nowych ID 24/7 (jednowątkowo) |
| 5 | 🕳️ Lataj dziury | Uzupełnia brakujące ID w bazie (pyta o liczbę wątków) |
| 6 | 🔗 Pobierz link | Scrapuje jeden konkretny adres |
| 7 | 📊 Statystyki bazy | Liczby książek/autorów/wydawnictw/opinii/kolejki |
| 8 | 🛠️ Reset bazy | Inicjalizacja (tworzy tabele) |
| 9 | 🎯 Skaner ID | Wielowątkowy skan zakresu ID w górę/dół |
| 10 | 🕷️ Tryb Pająk | Przechodzi po linkach portalu i dokłada nowe książki |
| 0 | ❌ Wyjście | |

### Komendy CLI (do skryptów / serwera)
```bash
python3 main.py init-db          # utwórz/zainicjuj bazę
python3 main.py full-scan        # zasiej starty i scrapuj kolejkę
python3 main.py resume           # wznów pobieranie z kolejki
python3 main.py update-new       # pobierz nowości
python3 main.py scrape-url --url "https://lubimyczytac.pl/ksiazka/245373/kasacja"
python3 main.py spider           # tryb pająka (crawl po linkach)
python3 main.py fill-gaps        # uzupełnij dziury w ID
python3 main.py id-range-scan    # skan zakresu ID (domyślnie od 1, w górę, 20000)
python3 main.py daemon-ids       # tryb 24/7 szukania nowych ID
python3 main.py stats            # statystyki bazy
```

**Liczba wątków** w trybach CLI to domyślnie 5 (z konstruktora `Scraper`).
Aby zmienić liczbę wątków, użyj **menu** (opcje 5/9/10 pytają o wątki) albo
ustaw tempo przez `REQUESTS_PER_SECOND` (to ono, nie liczba wątków, ogranicza
łączny ruch).

Logi scrapera: `logs/app.log` (INFO) i `logs/errors.log` (błędy).

### Najczęstsze scenariusze
```bash
# Uzupełnianie katalogu (zalecane zamiast brute-force skanu ID):
REQUESTS_PER_SECOND=2 python3 main.py spider

# Latanie dziur w tle z umiarkowanym tempem:
REQUESTS_PER_SECOND=2 python3 main.py fill-gaps
```
> Na **Windows** nie używaj `ZMIENNA=wartość komenda`. Ustaw wcześniej:
> `set REQUESTS_PER_SECOND=2` (cmd) lub `$env:REQUESTS_PER_SECOND=2` (PowerShell),
> a potem `python main.py fill-gaps`.

---

## 4. repair_authors.py — naprawa autorów w bazie

Koryguje rekordy, którym stary parser przypisał obcych autorów. Źródłem prawdy
jest JSON‑LD / `data-ga-book-authors` ze strony książki. **Strumieniowy**
(stała, niska pamięć), **wznawialny**, z globalnym limiterem 429.

### Wszystkie parametry
| Flaga | Domyślnie | Opis |
|---|---|---|
| `--dry-run` | — | Tylko raport + `repair_log.csv`, **bez zapisu** do bazy i bez postępu |
| `--cache-only` | — | Używa wyłącznie cache HTML (`data/html_cache/`), bez sieci |
| `--limit N` | 0 (wszystkie) | Maks. liczba książek w tym uruchomieniu |
| `--workers N` | 1 | Liczba wątków pobierających (zapisy zawsze 1‑wątkowe) |
| `--rps X` | (env/1.0) | Globalne tempo żądań/s (zastępuje `REQUESTS_PER_SECOND`) |
| `--window N` | 400 | Rozmiar „okna" książek trzymanych w pamięci |
| `--recheck` | — | Ignoruj postęp — sprawdź ponownie wszystkie |
| `--save-cache` | — | Zapisuj pobrany HTML do cache (kolejne przebiegi darmowe) |
| `--fix-publisher` | — | Popraw także wydawcę |
| `--log-file PATH` | `logs/repair.log` | Plik logu (rotowany) |

### Typowy przepływ
```bash
# 1. Kopia bazy (ZAWSZE przed zapisem)
cp data/database.db data/database.backup.db

# 2. Podgląd na próbce (bez zapisu) — sprawdź repair_log.csv
python3 repair_authors.py --dry-run --limit 200 --rps 2

# 3. Właściwa naprawa całej bazy (autorzy + wydawca)
python3 repair_authors.py --workers 8 --rps 2 --save-cache --fix-publisher

# 4. Po przerwaniu — wznowienie (ta sama komenda; pomija zrobione)
python3 repair_authors.py --workers 8 --rps 2 --save-cache --fix-publisher

# Wariant: partiami po 50 tys. dziennie
python3 repair_authors.py --workers 8 --rps 2 --save-cache --limit 50000

# Wariant: pełna ponowna weryfikacja od zera
python3 repair_authors.py --recheck --workers 8 --rps 2

# Wariant: bez sieci (tylko z zapisanego cache)
python3 repair_authors.py --cache-only --workers 8

# Mało RAM? zmniejsz okno:
python3 repair_authors.py --workers 8 --rps 2 --window 200
```

Wyniki:
- `repair_log.csv` — `timestamp, book_id, external_id, url, field, old, new, source`
- tabela `author_repair_progress` w bazie — postęp (`fixed`/`ok`/`skipped`)

Podgląd logu:
```bash
column -s, -t repair_log.csv | less -S      # Linux/macOS
```

---

## 5. stress_test.py — test wielowątkowości

W pełni **offline** (podmieniona warstwa HTTP + baza tymczasowa). Sprawdza, czy
przy wielu wątkach nie dochodzi do pomieszania autorów (race conditions).

| Flaga | Domyślnie | Opis |
|---|---|---|
| `--books N` | 300 | Liczba książek na przebieg |
| `--workers ...` | 8 16 32 | Lista liczby wątków do przetestowania |

```bash
python3 stress_test.py
python3 stress_test.py --books 500 --workers 8 16 32 64
```
Oczekiwane: tabela z `OK` dla każdej liczby wątków, „0 pomieszanych autorów",
kod wyjścia 0.

---

## 6. pytest — testy

```bash
pytest -v
```
Obejmuje: ekstrakcję autora (JSON‑LD → data‑ga → nagłówek), ignorowanie pułapek
(widgety/rekomendacje), idempotentny zapis (rescrape usuwa błędnych autorów).

---

## 7. Praca na serwerze (SSH, tmux, nohup)

### tmux (zalecane — przeżywa rozłączenie SSH)
```bash
tmux new -s repair                 # nowa sesja
source venv/bin/activate
python3 repair_authors.py --workers 8 --rps 2 --save-cache --fix-publisher
# odłącz: Ctrl+B, potem D
tmux attach -t repair              # powrót do sesji
tmux ls                            # lista sesji
```

### nohup (proces w tle)
```bash
nohup python3 repair_authors.py --workers 8 --rps 2 --save-cache --fix-publisher \
      >/dev/null 2>&1 &
echo $!                            # PID procesu
tail -f logs/repair.log            # podgląd postępu
```

### Monitoring
```bash
tail -f logs/repair.log            # naprawa
tail -f logs/app.log               # scraper / pająk / skaner
```

---

## 8. Zatrzymywanie i wznawianie

- **Stop:** `Ctrl+C` (w tmux/terminalu) lub `kill <PID>` (nohup). Zatrzymanie
  następuje w kilka sekund; pierwszy `Ctrl+C` = łagodne zatrzymanie (dokańcza
  bieżące, porzuca kolejkę), drugi `Ctrl+C` = twarde przerwanie.
- **Wznawianie:** uruchom **tę samą komendę** ponownie.
  - `repair_authors.py` — pomija książki już przetworzone (tabela
    `author_repair_progress`).
  - scraper/pająk/skaner — kolejka wraca z `processing` → `pending`, więc
    kontynuuje od miejsca przerwania.
- `--save-cache` (naprawa) sprawia, że nic nie pobierasz dwa razy.

---

## 9. Strojenie szybkości i HTTP 429

- **Łączny ruch ogranicza `--rps` / `REQUESTS_PER_SECOND`, NIE liczba wątków.**
  Limiter jest adaptacyjny (AIMD): po 429 mocno zwalnia, po sukcesach powoli
  przyspiesza — sam dostraja się tuż poniżej progu blokady.
- Start: `--rps 2`. Brak `HTTP 429` w logu przez kilka minut → podnieś `--rps 3`,
  potem `4`. Gdy 429 wracają — limiter sam zejdzie; możesz też ręcznie obniżyć.
- Wątki (`--workers 8`) pomagają **utrzymać** tempo blisko limitu mimo backoffów,
  ale nie podnoszą go powyżej `--rps`.
- Szacunki czasu dla ~342 tys. książek: `--rps 2` ≈ 48 h, `--rps 3` ≈ 32 h,
  `--rps 4` ≈ 24 h.
- **Uwaga prawno‑etyczna:** zachowaj umiarkowane tempo i sprawdź `robots.txt`/
  regulamin serwisu.

---

## 10. Zapytania SQL — diagnostyka i sprzątanie

(otwórz bazę: `sqlite3 data/database.db` albo DB Browser for SQLite)

```sql
-- Postęp naprawy
SELECT status, COUNT(*) FROM author_repair_progress GROUP BY status;

-- Rozkład: ile książek ma N autorów (po naprawie powinno spaść do 1–kilku)
SELECT n, COUNT(*) FROM (
  SELECT book_id, COUNT(*) n FROM book_authors GROUP BY book_id
) GROUP BY n ORDER BY n;

-- Autorzy podpięci do podejrzanie wielu książek (ślad starego błędu)
SELECT a.name, COUNT(*) AS books
FROM book_authors ba JOIN authors a ON a.id = ba.author_id
GROUP BY a.id ORDER BY books DESC LIMIT 30;

-- Liczby ogólne
SELECT COUNT(*) FROM books;
SELECT COUNT(*) FROM authors;

-- SPRZĄTANIE PO ZAKOŃCZENIU CAŁEJ NAPRAWY:
-- usuń osierocone „śmieci-autorów" (np. „Więcej", „Zobacz stronę autora"),
-- którzy nie są już powiązani z żadną książką.
-- (Uruchamiaj DOPIERO po ukończeniu repair_authors, nie w trakcie.)
DELETE FROM authors WHERE id NOT IN (SELECT DISTINCT author_id FROM book_authors);
```

---

## 11. Rozwiązywanie problemów

| Objaw | Przyczyna / rozwiązanie |
|---|---|
| `can't open file 'repair_authors.py'` | Stara wersja kodu — pobierz gałąź z poprawkami (`git pull` na właściwej gałęzi) |
| `pytest` pokazuje `skipped` | Brak zależności — `pip install -r requirements.txt` |
| Masowe `HTTP 429` | Za wysokie tempo — obniż `--rps`; limiter i tak sam zwalnia |
| Naprawa zjada RAM | Upewnij się, że używasz aktualnej (strumieniowej) wersji; zmniejsz `--window` |
| Ctrl+C nie zatrzymuje | Użyj aktualnej wersji; pierwszy Ctrl+C = łagodnie, drugi = twardo |
| Wszystko leci `skip` w naprawie | Problem z siecią/pobieraniem — sprawdź `logs/repair.log`; spróbuj `--rps 1` |
| `database is locked` | Zamknij inne procesy piszące do bazy; WAL + busy_timeout zwykle to obsługują |
| Na Windows `ZMIENNA=... komenda` nie działa | Użyj flagi `--rps` albo `set ZMIENNA=...` (cmd) / `$env:ZMIENNA=...` (PowerShell) |

---

## Skrót: „chcę po prostu naprawić autorów"
```bash
cp data/database.db data/database.backup.db
python3 repair_authors.py --dry-run --limit 200 --rps 2     # podgląd
python3 repair_authors.py --workers 8 --rps 2 --save-cache --fix-publisher
# przerwane? ta sama komenda wznawia.
```
