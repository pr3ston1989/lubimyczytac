from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from models import Base

DATABASE_URL = "sqlite:///data/database.db"

# Konfiguracja silnika bezpieczna dla wielu watkow.
#
# check_same_thread=False:
#   Domyslnie sterownik sqlite3 zabrania uzycia polaczenia w innym watku niz ten,
#   ktory je utworzyl. QueuePool SQLAlchemy moze jednak wspoldzielic polaczenia
#   miedzy watkami -> przy >2 watkach pojawia sie blad
#   "SQLite objects created in a thread can only be used in that same thread".
#   Wylaczamy te kontrole, a spojnosc zapewniamy przez sesje-na-operacje
#   (get_session()) oraz tryb WAL + busy_timeout.
#
# timeout=60: ile sekund polaczenie czeka na zwolnienie blokady zapisu.
engine = create_engine(
    DATABASE_URL,
    connect_args={"timeout": 60, "check_same_thread": False},
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # WAL - rownoczesne odczyty nie blokuja zapisu (lepsza wspolbieznosc).
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # busy_timeout - watek probujacy pisac czeka (do 60s) zamiast od razu
    # zglaszac "database is locked". Kluczowe przy 8/16/32 watkach.
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine)


def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
