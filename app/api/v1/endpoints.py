# File: app/api/v1/endpoints.py
"""
API Endpoints - Public & User
==============================
Gabungan: auth.py, manga.py, chapter.py

REVISI:
- Tambah helper get_cover_url()
- Show cover di list manga
- Show cover di detail manga
- Show description
✅ FIX #3: Import timezone dan ganti datetime.utcnow()
✅ REVISI BARU: Tambah endpoint public /cover/{manga_slug} untuk akses cover langsung
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional
from datetime import timedelta, datetime, timezone  # ✅ FIX #3: Added timezone import
from pathlib import Path
import logging

from app.core.base import (
    get_db, get_current_user, require_role,
    verify_password, get_password_hash, create_access_token, settings
)
from app.models.models import (
    User, Role, Manga, MangaType, Genre, Chapter, Page
)
from app.schemas.schemas import UserLogin, UserRegister, Token, UserResponse

logger = logging.getLogger(__name__)


# ✅ Helper function untuk convert cover path ke URL
def get_cover_url(cover_path: Optional[str]) -> Optional[str]:
    """
    Helper: Convert cover path to full URL.
    
    Args:
        cover_path: Relative path (covers/manga-slug.jpg)
        
    Returns:
        Full URL (/static/covers/manga-slug.jpg)
    """
    if not cover_path:
        return None
    return f"/static/{cover_path}"


# ==========================================
# AUTH ROUTER
# ==========================================

auth_router = APIRouter()


@auth_router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_data: UserRegister, db: Session = Depends(get_db)):
    """
    Register user baru.
    
    - Username harus unique
    - Email harus unique
    - Password akan di-hash otomatis
    - Default role: "user"
    """
    # Check username exists
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    
    # Check email exists
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Create new user
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        is_active=True
    )
    
    # Add default role "user"
    default_role = db.query(Role).filter(Role.name == "user").first()
    if default_role:
        new_user.roles = [default_role]
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    logger.info(f"New user registered: {new_user.username}")
    
    return new_user


@auth_router.post("/login", response_model=Token)
def login(user_data: UserLogin, db: Session = Depends(get_db)):
    """
    Login dan dapatkan JWT access token.
    
    Token ini harus disertakan di header:
    Authorization: Bearer <token>
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    # Find user
    user = db.query(User).filter(User.username == user_data.username).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    
    # Verify password
    if not verify_password(user_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    
    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive"
        )
    
    # ✅ FIX #3: Update last login with timezone-aware datetime
    user.last_login = datetime.now(timezone.utc)
    db.commit()
    
    # Create access token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username},
        expires_delta=access_token_expires
    )
    
    logger.info(f"User logged in: {user.username}")
    
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


@auth_router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """
    Get informasi user yang sedang login.
    
    Membutuhkan authentication token.
    """
    return current_user


@auth_router.post("/logout")
def logout(current_user: User = Depends(get_current_user)):
    """
    Logout (di sisi client harus hapus token).
    
    Note: JWT token tetap valid sampai expired.
    Client harus hapus token dari storage.
    """
    logger.info(f"User logged out: {current_user.username}")
    
    return {
        "message": "Successfully logged out",
        "username": current_user.username
    }


# ==========================================
# MANGA ROUTER
# ==========================================

manga_router = APIRouter()


@manga_router.get("/types")
def list_manga_types(db: Session = Depends(get_db)):
    """
    [PUBLIC] List semua tipe manga yang tersedia.
    
    Contoh: manga, manhwa, manhua, novel, dll.
    """
    types = db.query(MangaType).all()
    return {
        "types": [
            {
                "id": t.id,
                "name": t.name,
                "slug": t.slug,
                "total_manga": len(t.manga_list)
            }
            for t in types
        ]
    }


@manga_router.get("/genres")
def list_genres(db: Session = Depends(get_db)):
    """
    [PUBLIC] List semua genre yang tersedia.
    
    Contoh: action, adventure, comedy, drama, dll.
    """
    genres = db.query(Genre).all()
    return {
        "genres": [
            {
                "id": g.id,
                "name": g.name,
                "slug": g.slug,
                "total_manga": len(g.manga_list)
            }
            for g in genres
        ]
    }


@manga_router.get("/")
def list_manga(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(None, description="Search by title"),
    type_slug: Optional[str] = Query(None, description="Filter by manga type"),
    genre_slug: Optional[str] = Query(None, description="Filter by genre"),
    status: Optional[str] = Query(None, description="Filter by status (ongoing/completed)"),
    sort_by: str = Query("updated_at", description="Sort by: title | created_at | updated_at"),
    sort_order: str = Query("desc", description="Sort order: asc | desc"),
    db: Session = Depends(get_db)
):
    """
    [PUBLIC] List manga dengan filter dan pagination.
    
    Features:
    - Search by title
    - Filter by type, genre, status
    - Sorting by multiple fields
    - Pagination
    - ✅ Show cover images
    - ✅ Show description
    """
    query = db.query(Manga)
    
    # Search filter
    if search:
        query = query.filter(Manga.title.ilike(f"%{search}%"))
    
    # Type filter
    if type_slug:
        query = query.join(MangaType).filter(MangaType.slug == type_slug)
    
    # Genre filter
    if genre_slug:
        query = query.join(Manga.genres).filter(Genre.slug == genre_slug)
    
    # Status filter
    if status:
        query = query.filter(Manga.status == status)
    
    # Sorting
    sort_column = getattr(Manga, sort_by, Manga.updated_at)
    if sort_order == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())
    
    # Get total count before pagination
    total = query.count()
    
    # Apply pagination
    manga_list = query.offset((page - 1) * page_size).limit(page_size).all()
    
    # Format response
    items = []
    for m in manga_list:
        items.append({
            "id": m.id,
            "title": m.title,
            "slug": m.slug,
            "description": m.description,  # ✅ ADDED
            "cover_url": get_cover_url(m.cover_image_path),  # ✅ ADDED
            "status": m.status,
            "type": {
                "id": m.manga_type.id,
                "name": m.manga_type.name,
                "slug": m.manga_type.slug
            },
            "genres": [
                {"id": g.id, "name": g.name, "slug": g.slug}
                for g in m.genres
            ],
            "total_chapters": len(m.chapters),
            "latest_chapter": m.chapters[-1].chapter_label if m.chapters else None,
            "updated_at": m.updated_at
        })
    
    return {
        "items": items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    }


@manga_router.get("/{manga_slug}")
def get_manga_detail(manga_slug: str, db: Session = Depends(get_db)):
    """
    [PUBLIC] Get detail manga by slug.
    
    Returns:
    - Basic info
    - Type & genres
    - Alternative titles
    - List of chapters (summary)
    - ✅ Cover image URL
    - ✅ Description
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    
    if not manga:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manga '{manga_slug}' not found"
        )
    
    return {
        "id": manga.id,
        "title": manga.title,
        "slug": manga.slug,
        "description": manga.description,  # ✅ ADDED
        "cover_url": get_cover_url(manga.cover_image_path),  # ✅ ADDED
        "status": manga.status,
        "type": {
            "id": manga.manga_type.id,
            "name": manga.manga_type.name,
            "slug": manga.manga_type.slug
        },
        "genres": [
            {"id": g.id, "name": g.name, "slug": g.slug}
            for g in manga.genres
        ],
        "alt_titles": [
            {"title": a.title, "lang": a.lang}
            for a in manga.alt_titles
        ],
        "total_chapters": len(manga.chapters),
        "created_at": manga.created_at,
        "updated_at": manga.updated_at
    }


# ==========================================
# ✅ REVISI BARU: Public Cover Endpoint
# Endpoint untuk akses cover manga langsung via slug
# Mengembalikan file image langsung (bukan JSON)
# ==========================================

@manga_router.get("/cover/{manga_slug}")
def get_manga_cover(manga_slug: str, db: Session = Depends(get_db)):
    """
    [PUBLIC] Get cover image untuk manga by slug.
    
    Mengembalikan file image langsung (bukan JSON redirect).
    Support semua format: .jpg, .jpeg, .png, .webp
    
    Response:
    - 200: File image (image/jpeg, image/png, image/webp)
    - 404: Manga tidak ditemukan atau tidak punya cover
    
    Contoh:
        GET /api/v1/manga/cover/one-piece
        → Returns: image file langsung
    
    Gunakan ini untuk:
        <img src="/api/v1/manga/cover/one-piece" />
    
    Atau gunakan cover_url dari endpoint detail:
        cover_url: "/static/covers/one-piece.jpg"
    """
    # 1. Cari manga berdasarkan slug
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    
    if not manga:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manga '{manga_slug}' not found"
        )
    
    # 2. Cek apakah manga punya cover
    if not manga.cover_image_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manga '{manga_slug}' does not have a cover image"
        )
    
    # 3. Resolve path cover di local server
    #    cover_image_path format: "covers/manga-slug.jpg" (relative)
    covers_base_dir = Path(settings.COVERS_DIR)
    
    # Ambil hanya nama file dari cover_image_path
    # cover_image_path bisa berupa "covers/one-piece.jpg" → ambil "one-piece.jpg"
    cover_filename = Path(manga.cover_image_path).name
    cover_full_path = covers_base_dir / cover_filename
    
    # 4. Cek apakah file fisik ada di disk
    if not cover_full_path.exists():
        logger.warning(
            f"Cover file not found on disk for manga '{manga_slug}': {cover_full_path}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cover image file not found for '{manga_slug}'"
        )
    
    # 5. Tentukan content type berdasarkan ekstensi file
    ext = cover_full_path.suffix.lower()
    content_type_map = {
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
        ".webp": "image/webp",
    }
    media_type = content_type_map.get(ext, "image/jpeg")
    
    logger.info(f"Serving cover for '{manga_slug}': {cover_full_path} ({media_type})")
    
    # 6. Return file langsung dengan caching header
    return FileResponse(
        path=str(cover_full_path),
        media_type=media_type,
        headers={
            # Cache cover di browser selama 7 hari (immutable karena slug tidak berubah)
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Manga-Slug": manga_slug,
            "X-Cover-Format": ext.lstrip("."),
        }
    )


# ==========================================
# CHAPTER ROUTER
# ==========================================

chapter_router = APIRouter()


@chapter_router.get("/manga/{manga_slug}")
def list_chapters_by_manga(
    manga_slug: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort_order: str = Query("asc", description="asc | desc"),
    db: Session = Depends(get_db)
):
    """
    [PUBLIC] List chapters untuk manga tertentu.
    
    Default sort: ascending (chapter 1, 2, 3...)
    Bisa diubah ke descending untuk latest first.
    """
    # Find manga
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    
    if not manga:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manga '{manga_slug}' not found"
        )
    
    # Query chapters
    query = db.query(Chapter).filter(Chapter.manga_id == manga.id)
    
    # Sorting
    if sort_order == "asc":
        query = query.order_by(Chapter.chapter_main.asc(), Chapter.chapter_sub.asc())
    else:
        query = query.order_by(Chapter.chapter_main.desc(), Chapter.chapter_sub.desc())
    
    # Count total
    total = query.count()
    
    # Pagination
    chapters = query.offset((page - 1) * page_size).limit(page_size).all()
    
    # Format response
    chapter_items = []
    for ch in chapters:
        chapter_items.append({
            "id": ch.id,
            "chapter_main": ch.chapter_main,
            "chapter_sub": ch.chapter_sub,
            "chapter_label": ch.chapter_label,
            "slug": ch.slug,
            "total_pages": len(ch.pages),
            "preview_url": ch.preview_url,  # ✅ TAMBAHKAN INI
            "anchor_path": ch.anchor_path,  # ✅ TAMBAHKAN INI (optional)
            "uploaded_by": ch.uploader.username if ch.uploader else None,
            "created_at": ch.created_at
        })
    
    return {
        "manga_slug": manga_slug,
        "manga_title": manga.title,
        "total_chapters": total,
        "chapters": chapter_items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    }


@chapter_router.get("/{chapter_slug}")
def get_chapter_detail(chapter_slug: str, db: Session = Depends(get_db)):
    """
    [PUBLIC] Get detail chapter by slug.
    
    Returns:
    - Chapter info
    - Manga info
    - List of pages dengan proxy URLs
    """
    chapter = db.query(Chapter).filter(Chapter.slug == chapter_slug).first()
    
    if not chapter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chapter '{chapter_slug}' not found"
        )
    
    manga = chapter.manga
    
    # Sort pages by page_order
    sorted_pages = sorted(chapter.pages, key=lambda p: p.page_order)
    
    return {
        "id": chapter.id,
        "chapter_main": chapter.chapter_main,
        "chapter_sub": chapter.chapter_sub,
        "chapter_label": chapter.chapter_label,
        "slug": chapter.slug,
        "manga": {
            "id": manga.id,
            "title": manga.title,
            "slug": manga.slug
        },
        "pages": [
            {
                "id": page.id,
                "page_order": page.page_order,
                "gdrive_file_id": page.gdrive_file_id,
                "is_anchor": page.is_anchor,
                "proxy_url": f"/api/v1/image-proxy/image/{page.gdrive_file_id}"
            }
            for page in sorted_pages
        ],
        "total_pages": len(chapter.pages),
        "uploaded_by": chapter.uploader.username if chapter.uploader else None,
        "created_at": chapter.created_at
    }