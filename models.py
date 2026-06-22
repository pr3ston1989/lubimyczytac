from datetime import datetime
from typing import List, Optional
from sqlalchemy import String, Integer, Float, Text, Boolean, DateTime, ForeignKey, Table, Column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

book_authors = Table(
    "book_authors",
    Base.metadata,
    Column("book_id", ForeignKey("books.id"), primary_key=True),
    Column("author_id", ForeignKey("authors.id"), primary_key=True),
    Column("role", String(50), default="author")
)

book_categories = Table(
    "book_categories",
    Base.metadata,
    Column("book_id", ForeignKey("books.id"), primary_key=True),
    Column("category_id", ForeignKey("categories.id"), primary_key=True)
)

class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[int] = mapped_column(unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(500), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    
    title: Mapped[str] = mapped_column(String(500))
    original_title: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    
    isbn: Mapped[Optional[str]] = mapped_column(String(20))
    language: Mapped[Optional[str]] = mapped_column(String(50))
    pages: Mapped[Optional[int]]
    duration_minutes: Mapped[Optional[int]]  # <--- DODAJ TO
    
    release_date: Mapped[Optional[str]] = mapped_column(String(50))
    premiere_date: Mapped[Optional[str]] = mapped_column(String(50))
    format: Mapped[Optional[str]] = mapped_column(String(100))
    translator: Mapped[Optional[str]] = mapped_column(String(255))
    volume_number: Mapped[Optional[str]] = mapped_column(String(20)) # <--- DODAJ TĘ LINIJKĘ
    
    avg_rating: Mapped[Optional[float]]
    
    publisher_id: Mapped[Optional[int]] = mapped_column(ForeignKey("publishers.id"))
    series_id: Mapped[Optional[int]] = mapped_column(ForeignKey("series.id"))
    
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    # Relacje
    authors: Mapped[List["Author"]] = relationship(secondary=book_authors, back_populates="books")
    categories: Mapped[List["Category"]] = relationship(secondary=book_categories, back_populates="books")
    publisher: Mapped[Optional["Publisher"]] = relationship(back_populates="books")
    series: Mapped[Optional["Series"]] = relationship(back_populates="books")
    reviews: Mapped[List["Review"]] = relationship(back_populates="book")
    cover: Mapped[Optional["Cover"]] = relationship(back_populates="book", uselist=False)

class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    books: Mapped[List[Book]] = relationship(secondary=book_authors, back_populates="authors")

class Publisher(Base):
    __tablename__ = "publishers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    books: Mapped[List[Book]] = relationship(back_populates="publisher")

class Series(Base):
    __tablename__ = "series"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    books: Mapped[List[Book]] = relationship(back_populates="series")

class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    books: Mapped[List[Book]] = relationship(secondary=book_categories, back_populates="categories")

class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    username: Mapped[Optional[str]] = mapped_column(String(255))
    rating: Mapped[Optional[int]]
    full_text: Mapped[Text] = mapped_column(Text)
    is_featured: Mapped[bool] = mapped_column(default=False)
    
    book: Mapped[Book] = relationship(back_populates="reviews")


class Cover(Base):
    __tablename__ = "covers"
    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), unique=True)
    source_url: Mapped[str] = mapped_column(String(500))
    local_path: Mapped[str] = mapped_column(String(500))
    
    # --- PRZYWRÓCONE KOLUMNY ---
    width: Mapped[Optional[int]]
    height: Mapped[Optional[int]]
    size_bytes: Mapped[Optional[int]]
    # ---------------------------
    
    sha256: Mapped[str] = mapped_column(String(64))
    
    book: Mapped[Book] = relationship(back_populates="cover")

class ScrapeQueue(Base):
    __tablename__ = "scrape_queue"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    priority: Mapped[int] = mapped_column(default=0)
    retry_count: Mapped[int] = mapped_column(default=0) # <--- NOWA KOLUMNA (Dead-Letter)

class ScrapeError(Base):
    __tablename__ = "scrape_errors"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(500))
    error_msg: Mapped[Text] = mapped_column(Text)