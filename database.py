from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from models import Base

DATABASE_URL = "sqlite:///data/database.db"

# Timeout 30s dla wątków oczekujących w kolejce do zapisu
engine = create_engine(DATABASE_URL, connect_args={"timeout": 60})

# Tryb WAL - ekstremalna wydajność współbieżna
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

SessionLocal = sessionmaker(bind=engine)

def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)

def get_session():
    return SessionLocal()