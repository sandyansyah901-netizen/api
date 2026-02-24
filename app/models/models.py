# File: app/models/models.py
"""
Database Models - All in One + Reading Features
================================================
Gabungan semua models + Reading History, Bookmarks, Reading Lists, Analytics

REVISI:
✅ cover_image_url → cover_image_path (path lokal)
✅ FIX #2: ip_address kolom tetap ada tapi akan menyimpan HASHED IP (bukan raw)
           Perubahan dilakukan di main.py middleware, kolom DB tetap String(45)
           Karena SHA-256 hash[:32] = 32 chars, masih muat di String(45)
✅ FIX #4: datetime.utcnow() → datetime.now(timezone.utc)
           Semua penggunaan datetime.utcnow diganti ke timezone-aware version
           Kompatibel dengan Python 3.12+ (utcnow deprecated sejak 3.12)
"""

from sqlalchemy import (
    Column, BigInteger, String, Integer, DateTime, ForeignKey,
    Boolean, Table, Enum as SQLEnum, Text, Index
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, timezone
import enum

Base = declarative_base()


# ==========================================
# ✅ FIX #4: Timezone-aware UTC helper
#
# Kenapa harus diganti:
# - datetime.utcnow() deprecated di Python 3.12+
# - datetime.utcnow() return naive datetime (tanpa timezone info)
# - datetime.now(timezone.utc) return aware datetime (dengan UTC timezone)
# - SQLAlchemy menyimpan ke DB dengan benar di kedua kasus,
#   tapi aware datetime lebih correct secara semantik
#
# Fungsi ini dipakai sebagai default= dan onupdate= di semua Column
# ==========================================
def utcnow() -> datetime:
    """
    Return current UTC time as timezone-aware datetime.

    Pengganti datetime.utcnow() yang deprecated di Python 3.12+.

    Returns:
        datetime with UTC timezone info
    """
    return datetime.now(timezone.utc)


# ==========================================
# ENUMS
# ==========================================

class MangaStatus(str, enum.Enum):
    ongoing = "ongoing"
    completed = "completed"


class StorageStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class ReadingListStatus(str, enum.Enum):
    plan_to_read = "plan_to_read"
    reading = "reading"
    completed = "completed"
    dropped = "dropped"
    on_hold = "on_hold"


class ChapterType(str, enum.Enum):
    regular = "regular"
    special = "special"
    extra = "extra"
    omake = "omake"
    side_story = "side_story"


# ==========================================
# ASSOCIATION TABLES
# ==========================================

user_role = Table(
    'user_role',
    Base.metadata,
    Column('user_id', BigInteger, ForeignKey('users.id'), primary_key=True),
    Column('role_id', BigInteger, ForeignKey('roles.id'), primary_key=True)
)

manga_genre = Table(
    'manga_genre',
    Base.metadata,
    Column('manga_id', BigInteger, ForeignKey('manga.id'), primary_key=True),
    Column('genre_id', BigInteger, ForeignKey('genres.id'), primary_key=True)
)


# ==========================================
# USER & ROLE MODELS
# ==========================================

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    avatar_url = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)           # ✅ FIX #4
    last_login = Column(DateTime, nullable=True)

    roles = relationship("Role", secondary=user_role, back_populates="users")
    chapters = relationship("Chapter", back_populates="uploader")
    reading_history = relationship("ReadingHistory", back_populates="user", cascade="all, delete-orphan")
    bookmarks = relationship("Bookmark", back_populates="user", cascade="all, delete-orphan")
    reading_lists = relationship("ReadingList", back_populates="user", cascade="all, delete-orphan")


class Role(Base):
    __tablename__ = "roles"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)

    users = relationship("User", secondary=user_role, back_populates="roles")


# ==========================================
# STORAGE MODEL
# ==========================================

class StorageSource(Base):
    __tablename__ = "storage_sources"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    source_name = Column(String(100), nullable=False)
    base_folder_id = Column(String(255), nullable=False)
    status = Column(SQLEnum(StorageStatus), default=StorageStatus.active, nullable=False)
    created_at = Column(DateTime, default=utcnow)            # ✅ FIX #4
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)  # ✅ FIX #4

    manga_list = relationship("Manga", back_populates="storage_source")


# ==========================================
# MANGA MODELS
# ==========================================

class MangaType(Base):
    __tablename__ = "manga_types"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    slug = Column(String(50), unique=True, nullable=False)

    manga_list = relationship("Manga", back_populates="manga_type")


class Genre(Base):
    __tablename__ = "genres"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    slug = Column(String(50), unique=True, nullable=False)

    manga_list = relationship("Manga", secondary=manga_genre, back_populates="genres")


class Manga(Base):
    __tablename__ = "manga"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    cover_image_path = Column(String(500), nullable=True)  # Path lokal (covers/manga-slug.jpg)
    storage_id = Column(BigInteger, ForeignKey("storage_sources.id"), nullable=False)
    type_id = Column(BigInteger, ForeignKey("manga_types.id"), nullable=False)
    status = Column(SQLEnum(MangaStatus), default=MangaStatus.ongoing)
    created_at = Column(DateTime, default=utcnow)            # ✅ FIX #4
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)  # ✅ FIX #4

    storage_source = relationship("StorageSource", back_populates="manga_list")
    manga_type = relationship("MangaType", back_populates="manga_list")
    genres = relationship("Genre", secondary=manga_genre, back_populates="manga_list")
    alt_titles = relationship("MangaAltTitle", back_populates="manga", cascade="all, delete-orphan")
    chapters = relationship("Chapter", back_populates="manga", cascade="all, delete-orphan")
    readers = relationship("ReadingHistory", back_populates="manga")
    bookmarked_by = relationship("Bookmark", back_populates="manga")
    reading_lists = relationship("ReadingList", back_populates="manga")
    views = relationship("MangaView", back_populates="manga", cascade="all, delete-orphan")


class MangaAltTitle(Base):
    __tablename__ = "manga_alt_titles"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    manga_id = Column(BigInteger, ForeignKey("manga.id"), nullable=False)
    title = Column(String(255), nullable=False)
    lang = Column(String(5), nullable=False)

    manga = relationship("Manga", back_populates="alt_titles")


# ==========================================
# CHAPTER & PAGE MODELS
# ==========================================

class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    manga_id = Column(BigInteger, ForeignKey("manga.id"), nullable=False)
    chapter_main = Column(Integer, nullable=False)
    chapter_sub = Column(Integer, default=0)
    chapter_label = Column(String(100), nullable=False)
    slug = Column(String(255), unique=True, index=True, nullable=False)
    chapter_folder_name = Column(String(100), nullable=False)
    volume_number = Column(Integer, nullable=True)
    chapter_type = Column(SQLEnum(ChapterType), default=ChapterType.regular)
    anchor_path = Column(String(500), nullable=True)
    preview_url = Column(String(500), nullable=True)
    uploaded_by = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)            # ✅ FIX #4
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)  # ✅ FIX #4

    manga = relationship("Manga", back_populates="chapters")
    uploader = relationship("User", back_populates="chapters")
    pages = relationship("Page", back_populates="chapter", cascade="all, delete-orphan")
    readers = relationship("ReadingHistory", back_populates="chapter")
    views = relationship("ChapterView", back_populates="chapter", cascade="all, delete-orphan")

    # ✅ FIX #19: Composite index untuk frequent queries
    __table_args__ = (
        Index('idx_manga_chapter', 'manga_id', 'chapter_main', 'chapter_sub'),
    )


class Page(Base):
    __tablename__ = "pages"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    chapter_id = Column(BigInteger, ForeignKey("chapters.id"), nullable=False)
    gdrive_file_id = Column(String(255), nullable=False)
    page_order = Column(Integer, nullable=False)
    is_anchor = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)            # ✅ FIX #4

    chapter = relationship("Chapter", back_populates="pages")


# ==========================================
# IMAGE CACHE MODEL
# ==========================================

class ImageCache(Base):
    __tablename__ = "image_cache"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    chapter_id = Column(BigInteger, ForeignKey('chapters.id'), nullable=False)
    gdrive_file_id = Column(String(255), unique=True, index=True, nullable=False)
    local_path = Column(String(500), nullable=False)
    page_order = Column(Integer, nullable=False)
    last_accessed = Column(DateTime, default=utcnow, nullable=False)  # ✅ FIX #4
    is_persistent = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow)            # ✅ FIX #4

    chapter = relationship("Chapter", backref="cached_images")


# ==========================================
# READING FEATURES MODELS
# ==========================================

class ReadingHistory(Base):
    """Track user reading progress per chapter."""
    __tablename__ = "reading_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    manga_id = Column(BigInteger, ForeignKey('manga.id'), nullable=False)
    chapter_id = Column(BigInteger, ForeignKey('chapters.id'), nullable=False)
    page_number = Column(Integer, default=1)
    last_read_at = Column(DateTime, default=utcnow, onupdate=utcnow)  # ✅ FIX #4

    user = relationship("User", back_populates="reading_history")
    manga = relationship("Manga", back_populates="readers")
    chapter = relationship("Chapter", back_populates="readers")

    __table_args__ = (
        Index('idx_user_manga', 'user_id', 'manga_id'),
        Index('idx_user_chapter', 'user_id', 'chapter_id'),
    )


class Bookmark(Base):
    """User favorite manga bookmarks."""
    __tablename__ = "bookmarks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    manga_id = Column(BigInteger, ForeignKey('manga.id'), nullable=False)
    created_at = Column(DateTime, default=utcnow)            # ✅ FIX #4

    user = relationship("User", back_populates="bookmarks")
    manga = relationship("Manga", back_populates="bookmarked_by")

    __table_args__ = (
        Index('idx_user_bookmark', 'user_id', 'manga_id', unique=True),
    )


class ReadingList(Base):
    """User custom reading lists (Plan to Read, Reading, Completed, etc)."""
    __tablename__ = "reading_lists"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    manga_id = Column(BigInteger, ForeignKey('manga.id'), nullable=False)
    status = Column(SQLEnum(ReadingListStatus), default=ReadingListStatus.reading, nullable=False)
    rating = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    added_at = Column(DateTime, default=utcnow)              # ✅ FIX #4
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)  # ✅ FIX #4

    user = relationship("User", back_populates="reading_lists")
    manga = relationship("Manga", back_populates="reading_lists")

    __table_args__ = (
        Index('idx_user_manga_list', 'user_id', 'manga_id', unique=True),
        Index('idx_user_status', 'user_id', 'status'),
    )


# ==========================================
# ANALYTICS MODELS
# ==========================================

class MangaView(Base):
    """
    Track manga page views for analytics.

    ✅ FIX #2: Kolom ip_address tetap String(45) tapi akan menyimpan
    HASHED IP (bukan raw IP). Hash dilakukan di main.py middleware
    menggunakan SHA-256[:32] sehingga panjang = 32 chars, muat di String(45).

    Ini memungkinkan:
    - Menghitung unique visitors (hash yang sama = visitor yang sama)
    - TANPA menyimpan IP asli (privacy compliance)
    """
    __tablename__ = "manga_views"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    manga_id = Column(BigInteger, ForeignKey('manga.id'), nullable=False)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    ip_address = Column(String(45), nullable=True)  # Stores HASHED IP, not raw IP
    viewed_at = Column(DateTime, default=utcnow)             # ✅ FIX #4

    manga = relationship("Manga", back_populates="views")

    __table_args__ = (
        Index('idx_manga_views', 'manga_id', 'viewed_at'),
        Index('idx_manga_views_user', 'manga_id', 'user_id'),  # ✅ FIX #19: extra index
    )


class ChapterView(Base):
    """
    Track chapter views for analytics.

    ✅ FIX #2: Sama seperti MangaView, ip_address menyimpan HASHED IP.
    """
    __tablename__ = "chapter_views"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    chapter_id = Column(BigInteger, ForeignKey('chapters.id'), nullable=False)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    ip_address = Column(String(45), nullable=True)  # Stores HASHED IP, not raw IP
    viewed_at = Column(DateTime, default=utcnow)             # ✅ FIX #4

    chapter = relationship("Chapter", back_populates="views")

    __table_args__ = (
        Index('idx_chapter_views', 'chapter_id', 'viewed_at'),
        Index('idx_chapter_views_user', 'chapter_id', 'user_id'),  # ✅ FIX #19: extra index
    )