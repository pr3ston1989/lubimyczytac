# LubimyCzytac Scraper

Solidny, oparty na CLI scraper do pobierania metadanych książek i okładek z serwisu lubimyczytac.pl.

## Instalacja (Windows)
1. Uruchom plik `install.bat` klikając w niego dwukrotnie. Skrypt sprawdzi Pythona, pobierze zależności i utworzy bazę danych.
2. Otwórz terminal (CMD / PowerShell).
3. Aktywuj środowisko: `venv\Scripts\activate`

## Uruchamianie

* **Pełny skan (od zera):**
    `python main.py full-scan`
* **Wznowienie pracy (po zamknięciu/błędzie):**
    `python main.py resume`
* **Aktualizacja (tylko nowości):**
    `python main.py update-new`
* **Scrapowanie konkretnego linku:**
    `python main.py scrape-url --url "https://lubimyczytac.pl/ksiazka/123/tytul"`
* **Statystyki bazy:**
    `python main.py stats`

## Baza Danych
Dane zapisywane są w `data/database.db` (SQLite). Okładki znajdują się w `media/covers/`.