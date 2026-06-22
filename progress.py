from typing import List, Dict, Any, Optional
from database import get_session
from models import ScrapeQueue, ScrapeError
from loguru import logger
from sqlalchemy.dialects.sqlite import insert

def add_many_to_queue(links_data: List[Dict[str, Any]]):
    """Masowo dodaje linki do kolejki wykonujac tylko JEDEN zapis na dysk."""
    if not links_data:
        return

    with get_session() as session:
        # Przygotowanie slownikow do zapisu
        values = [{"url": d["url"], "type": d["type"], "priority": d["priority"]} for d in links_data]
        
        # Specjalne polecenie SQLite: INSERT ... ON CONFLICT DO NOTHING
        stmt = insert(ScrapeQueue).values(values)
        stmt = stmt.on_conflict_do_nothing(index_elements=['url'])
        
        session.execute(stmt)
        session.commit()

def add_to_queue(url: str, type_str: str, priority: int = 0):
    """Zachowujemy stara funkcje dla pojedynczych linkow z main.py"""
    add_many_to_queue([{"url": url, "type": type_str, "priority": priority}])

def get_next_in_queue() -> Optional[ScrapeQueue]:
    with get_session() as session:
        return session.query(ScrapeQueue).filter(ScrapeQueue.status == 'pending').order_by(ScrapeQueue.priority.desc(), ScrapeQueue.id.asc()).first()

def mark_queue_status(queue_id: int, status: str):
    with get_session() as session:
        item = session.query(ScrapeQueue).get(queue_id)
        if item:
            item.status = status
            session.commit()

def log_error(url: str, error_msg: str):
    with get_session() as session:
        error = ScrapeError(url=url, error_msg=error_msg)
        session.add(error)
        session.commit()
    logger.error(f"Blad dla {url}: {error_msg}")

def mark_queue_failed(queue_id: int):
    with get_session() as session:
        item = session.query(ScrapeQueue).get(queue_id)
        if item:
            item.retry_count += 1
            if item.retry_count >= 3:
                item.status = "archived_error"
                logger.error(f"URL: {item.url} oznaczony jako archived_error (3 bledy).")
            else:
                item.status = "pending" # Wraca do kolejki do ponownej proby
            session.commit()


def get_batch_queue(limit=5):
    """Pobiera paczke zadan i od razu oznacza je jako processing, by zapobiec dublowaniu."""
    from database import get_session
    from models import ScrapeQueue
    
    with get_session() as session:
        items = session.query(ScrapeQueue).filter_by(status='pending').order_by(ScrapeQueue.priority.desc(), ScrapeQueue.id.asc()).limit(limit).all()
        if not items:
            return []
        
        batch = [{"id": i.id, "url": i.url, "type": i.type} for i in items]
        for item in items:
            item.status = "processing"
        session.commit()
        return batch
    
def mark_status(queue_id, status):
    """Zmienia status zadania w kolejce na podstawie jego ID."""
    from database import get_session
    from models import ScrapeQueue
    
    with get_session() as session:
        item = session.query(ScrapeQueue).get(queue_id)
        if item:
            item.status = status
            session.commit()