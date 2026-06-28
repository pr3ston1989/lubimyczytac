import argparse
import sys
from loguru import logger
from rich.console import Console
from rich.table import Table

import config
from database import init_db, get_session
from scraper import Scraper
from progress import add_to_queue

logger.remove()
logger.add(sys.stdout, level="INFO") 
logger.add("logs/app.log", rotation="10 MB", level="INFO")
logger.add("logs/errors.log", rotation="10 MB", level="ERROR")

def show_stats():
    from models import Book, Author, Publisher, Review, ScrapeQueue
    console = Console()
    with get_session() as session:
        books = session.query(Book).count()
        authors = session.query(Author).count()
        pubs = session.query(Publisher).count()
        reviews = session.query(Review).count()
        queue_pending = session.query(ScrapeQueue).filter_by(status='pending').count()
        queue_errors = session.query(ScrapeQueue).filter_by(status='archived_error').count()

    table = Table(title="📊 Statystyki Lokalnej Bazy Danych", header_style="bold magenta")
    table.add_column("Kategoria", style="cyan", no_wrap=True)
    table.add_column("Ilosc rekordow", style="green", justify="right")

    table.add_row("📚 Zapisane Ksiazki/Audiobooki", f"{books:,}".replace(",", " "))
    table.add_row("✍️ Unikalni Autorzy", f"{authors:,}".replace(",", " "))
    table.add_row("🏢 Wydawnictwa", f"{pubs:,}".replace(",", " "))
    table.add_row("💬 Publiczne Opinie", f"{reviews:,}".replace(",", " "))
    table.add_section()
    table.add_row("⏳ Linki oczekujace w kolejce", f"{queue_pending:,}".replace(",", " "))
    table.add_row("⛔ Odrzucone linki (404 / Bledy)", str(queue_errors), style="red")

    console.print("\n")
    console.print(table)
    console.print("\n")

def run_interactive_menu(scraper: Scraper):
    while True:
        print("\n" + "="*55)
        print(" 📖 LUBIMYCZYTAC.PL SCRAPER - PANEL GLOWNY")
        print("="*55)
        print(" 1. 🚀 Pelny skan       (Szukaj na stronach portalu)")
        print(" 2. 🔄 Wznow prace      (Kontynuuj pobieranie z kolejki)")
        print(" 3. ✨ Pobierz nowosci  (Skanuj tylko najnowsze dodania)")
        print(" 4. 🤖 Tryb Daemon      (Szukaj nowych ID w tle 24/7)")
        print(" 5. 🕳️ Lataj dziury      (Wypelniaj braki historyczne w gore)")
        print(" 6. 🔗 Pobierz link     (Zescrapuj jeden konkretny adres)")
        print(" 7. 📊 Statystyki bazy  (Wyswietl zebrane dane)")
        print(" 8. 🛠️ Reset bazy        (Inicjalizacja nowej bazy)")
        print(" 9. 🎯 Skaner ID (🚀)   (Wielowatkowy skan w GORE lub w DOL)")
        print(" 10.🕷️ Tryb Pajak (Crawl)(Przechodz po linkach stron portalu)")
        print(" 11.📦 Zbierz z cache   (Wyciagnij linki z zapisanych stron)")
        print(" 0. ❌ Wyjscie")
        print("="*55)

        choice = input("Wybierz opcje (0-11): ").strip()

        try:
            if choice == '1':
                scraper.mode = "full-scan"
                init_db()
                scraper.seed_start_urls()
                scraper.run_queue()
            elif choice == '2':
                delay_input = input(f"Delay min,max w sek (obecny: {config.MIN_DELAY},{config.MAX_DELAY}; Enter=bez zmian): ").strip()
                if delay_input:
                    parts = delay_input.split(',')
                    if len(parts) == 2:
                        config.MIN_DELAY = float(parts[0])
                        config.MAX_DELAY = float(parts[1])
                    elif len(parts) == 1:
                        config.MIN_DELAY = float(parts[0])
                        config.MAX_DELAY = float(parts[0])
                scraper.mode = "resume"
                scraper.run_queue()
            elif choice == '3':
                scraper.mode = "update-new"
                scraper.seed_start_urls()
                scraper.run_queue()
            elif choice == '4':
                scraper.mode = "daemon-ids"
                scraper.run_daemon()
            elif choice == '5':
                workers_input = input("Podaj liczbe watkow do latania dziur (domyslnie 5): ").strip()
                workers = int(workers_input) if workers_input.isdigit() and int(workers_input) > 0 else 5
                scraper.mode = "fill-gaps"
                scraper.max_workers = workers
                scraper.run_gap_filler()
            elif choice == '6':
                url = input("Wklej link do ksiazki: ").strip()
                if url:
                    scraper.mode = "scrape-url"
                    add_to_queue(url, "book", priority=100)
                    scraper.run_queue()
            elif choice == '7':
                show_stats()
            elif choice == '8':
                init_db()
                print("Baza danych gotowa do pracy.")
            elif choice == '9':
                workers_input = input("Podaj liczbe watkow (domyslnie 5): ").strip()
                workers = int(workers_input) if workers_input.isdigit() and int(workers_input) > 0 else 5
                
                start_id_input = input("Podaj ID startowe: ").strip()
                if not start_id_input.isdigit():
                    print("Bledne ID startowe!")
                    continue
                start_id = int(start_id_input)
                
                direction_input = input("Wybierz kierunek (1 = W GORE dla nowosci, 2 = W DOL dla brakow): ").strip()
                direction = "up" if direction_input == '1' else "down"
                
                count_input = input("Ile ID sprawdzić w tej serii? (0 = do końca, domyślnie 20000): ").strip()
                if count_input == '0':
                    count = 0
                else:
                    count = int(count_input) if count_input.isdigit() and int(count_input) > 0 else 20000
                
                scraper.mode = "id-range-scan"
                scraper.max_workers = workers
                scraper.run_custom_id_scanner(start_id=start_id, direction=direction, count=count)
            elif choice == '10':
                workers_input = input("Podaj liczbe watkow dla pajaka (domyslnie 5): ").strip()
                workers = int(workers_input) if workers_input.isdigit() and int(workers_input) > 0 else 5
                
                delay_input = input(f"Delay min,max w sek (obecny: {config.MIN_DELAY},{config.MAX_DELAY}; Enter=bez zmian): ").strip()
                if delay_input:
                    parts = delay_input.split(',')
                    if len(parts) == 2:
                        config.MIN_DELAY = float(parts[0])
                        config.MAX_DELAY = float(parts[1])
                    elif len(parts) == 1:
                        config.MIN_DELAY = float(parts[0])
                        config.MAX_DELAY = float(parts[0])
                
                print("\n[!] Tryb Pajaka analizuje linki i dopisuje nowe do kolejki, porownujac je po czystym ID.")
                use_current = input("Czy chcesz kontynuowac z obecna kolejka? (t/n): ").strip().lower()
                
                scraper.mode = "spider"
                scraper.max_workers = workers
                
                if use_current != 't':
                    init_db()
                    scraper.seed_start_urls()
                    
                scraper.run_queue()
            elif choice == '11':
                print("\n[📦] Tryb zbierania linkow z cache.")
                print("    Przeszukam zapisane pliki HTML i wyciagne z nich linki do kolejki.")
                
                scraper.mode = "spider"
                new_links_count = scraper.harvest_links_from_cache()
                
                if new_links_count > 0:
                    run_now = input(f"\nZnaleziono {new_links_count} nowych linkow. Uruchomic pajaka? (t/n): ").strip().lower()
                    if run_now == 't':
                        workers_input = input("Podaj liczbe watkow (domyslnie 5): ").strip()
                        workers = int(workers_input) if workers_input.isdigit() and int(workers_input) > 0 else 5
                        scraper.max_workers = workers
                        scraper.run_queue()
                else:
                    print("[i] Nie znaleziono nowych linkow w cache. Kolejka nie zostala zmieniona.")
            elif choice == '0':
                print("Do zobaczenia!")
                break
            else:
                print("Nieznana opcja. Sprobuj ponownie.")
                
        except KeyboardInterrupt:
            print("\n\n⛔ Przerwano dzialanie zadania (Ctrl+C).")
            print("💡 Postep zostal zachowany. Wracam do menu glownego...")
        except Exception as e:
            print(f"\n[BLAD] Wystapil nieoczekiwany problem: {e}")

def main():
    parser = argparse.ArgumentParser(description="LubimyCzytac CLI Scraper")
    parser.add_argument("command", nargs="?", choices=[
        "init-db", "full-scan", "scrape-url", "update-new", "resume", "stats", "daemon-ids", "fill-gaps", "id-range-scan", "spider", "harvest-cache"
    ], help="Komenda do wykonania (zostaw puste, by otworzyc menu)")
    parser.add_argument("--url", help="URL do pobrania dla trybu scrape-url")
    parser.add_argument("--workers", "-w", type=int, default=5, help="Liczba watkow (domyslnie 5)")
    parser.add_argument("--delay", "-d", type=str, default=None, help="Delay min,max w sekundach (np. '1.0,2.5')")
    
    args = parser.parse_args()
    
    # Ustawienie delay jesli podano
    if args.delay:
        parts = args.delay.split(',')
        if len(parts) == 2:
            config.MIN_DELAY = float(parts[0])
            config.MAX_DELAY = float(parts[1])
        elif len(parts) == 1:
            config.MIN_DELAY = float(parts[0])
            config.MAX_DELAY = float(parts[0])
    
    if not args.command:
        scraper = Scraper()
        run_interactive_menu(scraper)
        return

    scraper = Scraper(mode=args.command, max_workers=args.workers)
    try:
        if args.command == "init-db":
            init_db()
        elif args.command == "full-scan":
            init_db()
            scraper.seed_start_urls()
            scraper.run_queue()
        elif args.command == "resume":
            scraper.run_queue()
        elif args.command == "scrape-url":
            if not args.url:
                print("Musisz podac parametr --url")
                return
            add_to_queue(args.url, "book", priority=100)
            scraper.run_queue()
        elif args.command == "update-new":
            scraper.seed_start_urls()
            scraper.run_queue()
        elif args.command == "daemon-ids":
            scraper.run_daemon()
        elif args.command == "fill-gaps":
            scraper.run_gap_filler()
        elif args.command == "id-range-scan":
            scraper.run_custom_id_scanner(start_id=1, direction="up", count=20000)
        elif args.command == "spider":
            scraper.run_queue()
        elif args.command == "harvest-cache":
            scraper.mode = "spider"
            new_links = scraper.harvest_links_from_cache()
            if new_links > 0:
                print(f"Dodano {new_links} nowych linkow do kolejki. Uruchom 'resume' lub 'spider' by je przetworzyc.")
            else:
                print("Nie znaleziono nowych linkow w cache.")
        elif args.command == "stats":
            show_stats()

    except KeyboardInterrupt:
        print("\n\n⛔ Przerwano dzialanie programu (Ctrl+C).")
        sys.exit(0)

if __name__ == '__main__':
    main()
