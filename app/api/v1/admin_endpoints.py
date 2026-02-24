"""
API Endpoints - Admin, Image Proxy + Multi-Remote Support + THUMBNAIL MANAGEMENT
================================================================================
Admin management + Image Proxy + Custom 16:9 Thumbnail Management

REVISI:
✅ ✨ REMOVED get_multi_remote_service() function (auto-create instance)
✅ ✨ CHANGED to use global multi_remote_service from main.py
✅ ✨ ALL endpoints now reuse single global instance (no re-init!)
✅ ✨ FIX PERFORMANCE: _get_daemon_url_for_file() pakai cached URL (no health check per request)
✅ ✨ FIX PERFORMANCE: _stream_from_serve_daemon() pakai singleton HTTPX client dari HttpxClientManager
✅ ✨ ROUND ROBIN: _get_daemon_url_for_file() → get_next_daemon_url() untuk load balancing antar remote

✅ ✨ GROUP AWARE (NEW):
    - _get_daemon_url_for_file() baca prefix '@' dari path untuk routing ke group 2
    - get_image_proxy() strip '@' prefix sebelum kirim ke rclone
    - Semua admin endpoint TIDAK BERUBAH

FIXES:
✅ FIX: clean_path = validated_path.lstrip(settings.GROUP2_PATH_PREFIX) diganti ke
        settings.clean_path(validated_path) untuk menghindari bug lstrip() yang strip
        karakter individual bukan string prefix.
✅ FIX: settings.get_next_secondary_remotes() → settings.get_next_backup_remotes()
        karena method get_next_secondary_remotes() tidak ada di Settings.

REVISI COVER:
✅ ✨ NEW: Public cover endpoint untuk frontend (GET /cover/{manga_slug})
✅ ✨ NEW: Admin cover info endpoint (GET /admin/manga/{manga_id}/cover/info)
✅ ✨ NEW: Admin list all covers endpoint (GET /admin/covers/list)
✅ ✨ FIX: save_cover_local() di CoverService sekarang preserve format asli
          Endpoint upload cover sekarang pass source_filename ke CoverService
"""

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query,
    File, UploadFile, Form, Request, BackgroundTasks
)
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional, AsyncIterator, Tuple
from pathlib import Path
import io
import logging
import httpx

from app.core.base import get_db, get_current_user, require_role, settings
from app.models.models import (
    User, Role, Manga, MangaType, Genre, Chapter, Page,
    StorageSource, ImageCache
)
from app.services.cache_manager import CacheManager
from app.services.cover_service import CoverService

# ✅ FIX: Import HttpxClientManager untuk singleton HTTPX client
from app.services.rclone_service import HttpxClientManager

import main

from app.services.thumbnail_service import ThumbnailService

from app.schemas.schemas import (
    MangaUpdateRequest, ChapterUpdateRequest,
    UserRoleUpdateRequest, UserStatusUpdateRequest
)

logger = logging.getLogger(__name__)


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def get_cover_url(cover_path: Optional[str]) -> Optional[str]:
    """Helper: Convert cover path to full URL."""
    if not cover_path:
        return None
    return f"/static/{cover_path}"


def validate_file_path(file_path: str) -> str:
    """Validate file path to prevent path traversal attacks.
    
    ✅ GROUP AWARE: path boleh dimulai dengan '@' untuk group 2.
    '@' bukan path traversal, jadi kita strip dulu sebelum validasi.
    """
    # ✅ NEW: Strip '@' prefix untuk validasi (bukan path traversal)
    check_path = file_path.lstrip("@")

    if '..' in check_path or check_path.startswith('/') or '\\' in check_path:
        logger.warning(f"Path traversal attempt detected: {file_path}")
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not check_path or len(check_path) < 5:
        raise HTTPException(status_code=400, detail="File path too short")

    valid_extensions = ['.jpg', '.jpeg', '.png', '.webp', '.gif']
    if not any(check_path.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(status_code=400, detail="Invalid file type. Only image files are allowed")

    return file_path


def get_image_content_type(filename: str) -> str:
    """Get content type based on file extension"""
    # Strip '@' prefix sebelum ambil extension
    clean = filename.lstrip("@")
    extension = clean.lower().split('.')[-1]

    content_types = {
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'webp': 'image/webp',
        'gif': 'image/gif'
    }

    return content_types.get(extension, 'image/jpeg')


def get_multi_remote_service():
    """
    Get global MultiRemoteService instance.

    ✅ Returns singleton initialized at app startup.
    ✅ No re-initialization overhead!
    """
    if main.multi_remote_service is None:
        logger.error("MultiRemoteService not initialized at startup!")
        raise HTTPException(
            status_code=503,
            detail="Storage service not available"
        )

    if not main.multi_remote_service.is_initialized:
        logger.error("MultiRemoteService exists but not initialized!")
        raise HTTPException(
            status_code=503,
            detail="Storage service not ready"
        )

    return main.multi_remote_service


# ==========================================
# ✅ FIX PERFORMANCE: _stream_from_serve_daemon
# Pakai singleton HttpxClientManager bukan buat client baru tiap request
# TIDAK BERUBAH
# ==========================================

async def _stream_from_serve_daemon(
    daemon_url: str,
    file_path: str,
    chunk_size: int = 65536
) -> AsyncIterator[bytes]:
    """
    ✅ FIX: True async streaming pakai singleton HTTPX AsyncClient.

    SEBELUMNYA: Buat httpx.AsyncClient baru tiap request
    → TLS handshake ulang tiap gambar = +100-200ms overhead

    SEKARANG: Pakai HttpxClientManager singleton dari rclone_service
    → Connection pool di-reuse, keepalive = 0ms overhead koneksi

    Args:
        daemon_url: Base URL daemon (e.g., http://127.0.0.1:8180)
        file_path: File path di remote (SUDAH clean, tanpa '@')
        chunk_size: Ukuran chunk per yield (default 64KB)

    Yields:
        bytes: Chunk data dari response stream
    """
    # ✅ Pakai singleton client (connection pool di-reuse, bukan buat client baru)
    client = HttpxClientManager.get_client(daemon_url)

    async with client.stream("GET", f"/{file_path}") as response:
        if response.status_code == 404:
            raise FileNotFoundError(f"File not found via daemon: {file_path}")
        if response.status_code != 200:
            raise RuntimeError(
                f"Daemon returned HTTP {response.status_code} for {file_path}"
            )
        async for chunk in response.aiter_bytes(chunk_size):
            yield chunk


# ==========================================
# ✅ GROUP AWARE: _get_daemon_url_for_file (DIREVISI)
# TIDAK BERUBAH dari versi sebelumnya
# ==========================================

async def _get_daemon_url_for_file(
    multi_remote,
    file_path: str = "",
    strategy: str = "round_robin"
) -> Tuple[Optional[str], int]:
    """
    ✅ UPDATED: Get daemon URL via Round Robin + GROUP AWARE.

    Baca prefix path untuk tentukan group:
        "manga_library/xxx/001.jpg"  → group 1 (gdrive..gdrive10)
        "@manga_library/xxx/001.jpg" → group 2 (gdrive11..gdrive20)

    Args:
        multi_remote: MultiRemoteService instance
        file_path: Path dari DB (mungkin ada prefix '@')
        strategy: kept for backward compat

    Returns:
        (daemon_url, group) - daemon_url bisa None jika tidak ada daemon running
                              group adalah 1 atau 2
    """
    # Determine group dari path prefix
    group = multi_remote.get_group_for_path(file_path)

    try:
        url = await multi_remote.get_next_daemon_url(group=group)
        return url, group
    except Exception as e:
        logger.warning(f"Failed to get daemon URL (G{group}): {str(e)}")
        return None, group


# ==========================================
# ADMIN ROUTER - MANGA MANAGEMENT
# ==========================================

admin_router = APIRouter()


@admin_router.get("/manga", response_model=dict)
def admin_list_manga(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    storage_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] List semua manga dengan detail lengkap termasuk storage info dan cover"""
    query = db.query(Manga)

    if search:
        query = query.filter(Manga.title.ilike(f"%{search}%"))

    if storage_id:
        query = query.filter(Manga.storage_id == storage_id)

    total = query.count()
    manga_list = query.offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for manga in manga_list:
        items.append({
            "id": manga.id,
            "title": manga.title,
            "slug": manga.slug,
            "description": manga.description,
            "cover_url": get_cover_url(manga.cover_image_path),
            "status": manga.status,
            "type": {
                "id": manga.manga_type.id,
                "name": manga.manga_type.name,
                "slug": manga.manga_type.slug
            },
            "storage": {
                "id": manga.storage_source.id,
                "name": manga.storage_source.source_name,
                "base_folder_id": manga.storage_source.base_folder_id,
                "status": manga.storage_source.status
            },
            "genres": [{"id": g.id, "name": g.name, "slug": g.slug} for g in manga.genres],
            "total_chapters": len(manga.chapters),
            "created_at": manga.created_at,
            "updated_at": manga.updated_at
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


@admin_router.post("/manga", status_code=status.HTTP_201_CREATED)
def admin_create_manga(
    title: str = Form(..., min_length=1, max_length=255),
    slug: str = Form(..., min_length=1, max_length=255),
    description: Optional[str] = Form(None),
    type_slug: str = Form(...),
    storage_id: int = Form(..., ge=1),
    status_manga: str = Form("ongoing"),
    genre_slugs: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Create manga baru"""
    try:
        existing = db.query(Manga).filter(Manga.slug == slug).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Manga dengan slug '{slug}' sudah ada"
            )

        manga_type = db.query(MangaType).filter(MangaType.slug == type_slug).first()
        if not manga_type:
            raise HTTPException(
                status_code=404,
                detail=f"Manga type '{type_slug}' tidak ditemukan"
            )

        storage = db.query(StorageSource).filter(StorageSource.id == storage_id).first()
        if not storage:
            raise HTTPException(
                status_code=404,
                detail=f"Storage ID {storage_id} tidak ditemukan"
            )

        if status_manga not in ["ongoing", "completed"]:
            raise HTTPException(
                status_code=400,
                detail="Status harus: ongoing atau completed"
            )

        new_manga = Manga(
            title=title,
            slug=slug,
            description=description,
            type_id=manga_type.id,
            storage_id=storage.id,
            status=status_manga
        )

        if genre_slugs:
            slug_list = [s.strip() for s in genre_slugs.split(",") if s.strip()]
            if slug_list:
                genres = db.query(Genre).filter(Genre.slug.in_(slug_list)).all()
                if len(genres) != len(slug_list):
                    found_slugs = {g.slug for g in genres}
                    missing = set(slug_list) - found_slugs
                    raise HTTPException(
                        status_code=404,
                        detail=f"Genre tidak ditemukan: {', '.join(missing)}"
                    )
                new_manga.genres = genres

        db.add(new_manga)
        db.commit()
        db.refresh(new_manga)

        logger.info(f"Admin {current_user.username} created manga: {title}")

        return {
            "success": True,
            "message": f"Manga '{title}' berhasil dibuat",
            "manga": {
                "id": new_manga.id,
                "title": new_manga.title,
                "slug": new_manga.slug,
                "description": new_manga.description,
                "cover_url": get_cover_url(new_manga.cover_image_path),
                "status": new_manga.status,
                "type": {
                    "id": manga_type.id,
                    "name": manga_type.name,
                    "slug": manga_type.slug
                },
                "storage_id": storage.id,
                "genres": [
                    {"id": g.id, "name": g.name, "slug": g.slug}
                    for g in new_manga.genres
                ],
                "created_at": new_manga.created_at,
                "updated_at": new_manga.updated_at
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create manga: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Gagal membuat manga: {str(e)}"
        )


@admin_router.get("/manga/{manga_id}")
def admin_get_manga(
    manga_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get detail manga by ID termasuk cover"""
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    return {
        "id": manga.id,
        "title": manga.title,
        "slug": manga.slug,
        "description": manga.description,
        "cover_url": get_cover_url(manga.cover_image_path),
        "status": manga.status,
        "type": {"id": manga.manga_type.id, "name": manga.manga_type.name},
        "storage": {
            "id": manga.storage_source.id,
            "name": manga.storage_source.source_name,
            "base_folder_id": manga.storage_source.base_folder_id
        },
        "genres": [{"id": g.id, "name": g.name, "slug": g.slug} for g in manga.genres],
        "alt_titles": [{"title": a.title, "lang": a.lang} for a in manga.alt_titles],
        "chapters": [
            {
                "id": ch.id,
                "chapter_label": ch.chapter_label,
                "slug": ch.slug,
                "chapter_folder_name": ch.chapter_folder_name,
                "total_pages": len(ch.pages),
                "created_at": ch.created_at
            }
            for ch in manga.chapters
        ],
        "total_chapters": len(manga.chapters),
        "created_at": manga.created_at,
        "updated_at": manga.updated_at
    }


@admin_router.put("/manga/{manga_id}")
def admin_update_manga(
    manga_id: int,
    update_data: MangaUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Update data manga"""
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    if update_data.title is not None:
        manga.title = update_data.title

    if update_data.description is not None:
        manga.description = update_data.description

    if update_data.cover_image_path is not None:
        manga.cover_image_path = update_data.cover_image_path

    if update_data.slug is not None and update_data.slug != manga.slug:
        existing = db.query(Manga).filter(
            Manga.slug == update_data.slug,
            Manga.id != manga_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Slug '{update_data.slug}' sudah digunakan")
        manga.slug = update_data.slug

    if update_data.status is not None:
        if update_data.status not in ["ongoing", "completed"]:
            raise HTTPException(status_code=400, detail="Status harus: ongoing | completed")
        manga.status = update_data.status

    if update_data.type_slug is not None:
        manga_type = db.query(MangaType).filter(MangaType.slug == update_data.type_slug).first()
        if not manga_type:
            raise HTTPException(status_code=404, detail=f"Type '{update_data.type_slug}' tidak ditemukan")
        manga.type_id = manga_type.id

    if update_data.storage_id is not None:
        storage = db.query(StorageSource).filter(StorageSource.id == update_data.storage_id).first()
        if not storage:
            raise HTTPException(status_code=404, detail=f"Storage ID {update_data.storage_id} tidak ditemukan")
        manga.storage_id = update_data.storage_id

    if update_data.genre_slugs is not None:
        genres = db.query(Genre).filter(Genre.slug.in_(update_data.genre_slugs)).all()
        manga.genres = genres

    db.commit()
    db.refresh(manga)

    logger.info(f"Admin {current_user.username} updated manga ID {manga_id}: {manga.title}")

    return {
        "success": True,
        "message": f"Manga '{manga.title}' berhasil diupdate",
        "manga_id": manga.id,
        "manga_slug": manga.slug,
        "cover_url": get_cover_url(manga.cover_image_path)
    }


@admin_router.delete("/manga/{manga_id}")
def admin_delete_manga(
    manga_id: int,
    delete_gdrive: bool = Query(False, description="Hapus juga files di Google Drive"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus manga beserta cover, semua chapters dan pages"""
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    manga_title = manga.title
    manga_slug = manga.slug

    cache_manager = CacheManager(db)
    for chapter in manga.chapters:
        cache_manager.cleanup_chapter_cache(chapter.id)

    if manga.cover_image_path:
        cover_service = CoverService()
        cover_service.delete_cover(manga.cover_image_path, delete_gdrive)

    gdrive_deleted = False
    if delete_gdrive:
        try:
            multi_remote = get_multi_remote_service()
            remote_name, rclone = multi_remote.get_next_remote(strategy="least_used")

            folder_path = f"{manga.storage_source.base_folder_id}/{manga_slug}"
            if rclone.delete_path(folder_path, is_directory=True):
                gdrive_deleted = True
                logger.info(f"Deleted GDrive folder via remote '{remote_name}': {folder_path}")
        except Exception as e:
            logger.error(f"Error deleting GDrive folder: {str(e)}")

    db.delete(manga)
    db.commit()

    logger.info(f"Admin {current_user.username} deleted manga: {manga_title} (ID: {manga_id})")

    return {
        "success": True,
        "message": f"Manga '{manga_title}' berhasil dihapus (termasuk cover)",
        "deleted_manga_id": manga_id,
        "gdrive_folder_deleted": gdrive_deleted
    }


@admin_router.put("/manga/{manga_id}/status")
def admin_toggle_manga_status(
    manga_id: int,
    new_status: str = Query(..., description="ongoing | completed"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Update status manga dengan cepat"""
    if new_status not in ["ongoing", "completed"]:
        raise HTTPException(status_code=400, detail="Status harus: ongoing | completed")

    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    old_status = manga.status
    manga.status = new_status
    db.commit()

    return {
        "success": True,
        "manga_id": manga_id,
        "manga_slug": manga.slug,
        "old_status": old_status,
        "new_status": new_status
    }


# ==========================================
# COVER MANAGEMENT
# ==========================================

@admin_router.post("/manga/{manga_id}/cover")
async def admin_upload_cover(
    manga_id: int,
    cover_file: UploadFile = File(...),
    backup_to_gdrive: bool = Query(True, description="Backup ke Google Drive"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Upload cover image untuk manga.

    ✅ FIX: Sekarang preserve format asli (jpg/png/webp) saat menyimpan.
    Sebelumnya selalu disimpan sebagai .jpg meskipun upload .webp atau .png.
    """
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    file_content = await cover_file.read()

    cover_service = CoverService()
    is_valid, error_msg = cover_service.validate_cover_image(
        cover_file.filename,
        len(file_content),
        cover_file.content_type
    )

    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # ✅ FIX: Pass source_filename agar CoverService preserve format asli
    local_path = cover_service.save_cover_local(
        file_content,
        manga.slug,
        optimize=True,
        source_filename=cover_file.filename  # ✅ NEW: preserve jpg/png/webp
    )

    if not local_path:
        raise HTTPException(status_code=500, detail="Failed to save cover locally")

    backup_success = False
    if backup_to_gdrive:
        backup_success = cover_service.backup_cover_to_gdrive(local_path, manga.slug)

    manga.cover_image_path = local_path
    db.commit()
    db.refresh(manga)

    logger.info(f"Admin {current_user.username} uploaded cover for manga: {manga.title}")

    return {
        "success": True,
        "message": f"Cover uploaded for '{manga.title}'",
        "manga_id": manga.id,
        "cover_path": local_path,
        "cover_url": get_cover_url(local_path),
        "backed_up_to_gdrive": backup_success
    }


@admin_router.delete("/manga/{manga_id}/cover")
def admin_delete_cover(
    manga_id: int,
    delete_gdrive: bool = Query(True, description="Hapus juga dari GDrive backup"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus cover manga dari local dan GDrive"""
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    if not manga.cover_image_path:
        raise HTTPException(status_code=404, detail="Manga tidak memiliki cover")

    cover_service = CoverService()
    success = cover_service.delete_cover(manga.cover_image_path, delete_gdrive)

    if success:
        manga.cover_image_path = None
        db.commit()

        return {
            "success": True,
            "message": f"Cover deleted for '{manga.title}'",
            "gdrive_deleted": delete_gdrive
        }

    raise HTTPException(status_code=500, detail="Failed to delete cover")


# ==========================================
# ✅ NEW: COVER INFO & LIST ENDPOINTS
# ==========================================

@admin_router.get("/manga/{manga_id}/cover/info")
def admin_get_cover_info(
    manga_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Get info detail tentang cover manga.
    
    Returns:
    - cover_url: URL publik untuk akses cover di frontend
    - cover_path: Relative path di server
    - file_exists: Apakah file cover ada di local server
    - file_size_kb: Ukuran file cover
    - format: Format gambar (jpg/png/webp)
    - access_url: URL lengkap untuk akses langsung
    """
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    if not manga.cover_image_path:
        return {
            "manga_id": manga_id,
            "manga_title": manga.title,
            "manga_slug": manga.slug,
            "has_cover": False,
            "cover_path": None,
            "cover_url": None,
            "file_exists": False,
            "file_size_kb": None,
            "format": None,
            "note": "Manga belum memiliki cover. Upload via POST /admin/manga/{id}/cover"
        }

    # Cek file lokal
    cover_service = CoverService()
    local_file = cover_service.COVERS_DIR / Path(manga.cover_image_path).name
    file_exists = local_file.exists()
    file_size_kb = None
    if file_exists:
        file_size_kb = round(local_file.stat().st_size / 1024, 2)

    # Detect format dari extension
    ext = Path(manga.cover_image_path).suffix.lower().lstrip(".")
    format_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WebP"}
    file_format = format_map.get(ext, ext.upper())

    return {
        "manga_id": manga_id,
        "manga_title": manga.title,
        "manga_slug": manga.slug,
        "has_cover": True,
        "cover_path": manga.cover_image_path,
        "cover_url": get_cover_url(manga.cover_image_path),
        "file_exists_local": file_exists,
        "file_size_kb": file_size_kb,
        "format": file_format,
        "note": "Gunakan cover_url untuk tampilkan cover di frontend"
    }


@admin_router.get("/covers/list")
def admin_list_covers(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    has_cover: Optional[bool] = Query(None, description="Filter: True=hanya yg punya cover, False=yg belum"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] List semua manga beserta status cover-nya.

    Berguna untuk:
    - Cek manga mana yang belum punya cover
    - Audit cover files di local server
    - Batch upload planning
    """
    query = db.query(Manga)

    if has_cover is True:
        query = query.filter(Manga.cover_image_path.isnot(None))
    elif has_cover is False:
        query = query.filter(Manga.cover_image_path.is_(None))

    total = query.count()
    manga_list = query.offset((page - 1) * page_size).limit(page_size).all()

    cover_service = CoverService()
    items = []

    for manga in manga_list:
        cover_info = {
            "manga_id": manga.id,
            "manga_title": manga.title,
            "manga_slug": manga.slug,
            "has_cover": manga.cover_image_path is not None,
            "cover_path": manga.cover_image_path,
            "cover_url": get_cover_url(manga.cover_image_path),
        }

        # Cek apakah file lokal ada (jika ada cover_path)
        if manga.cover_image_path:
            local_file = cover_service.COVERS_DIR / Path(manga.cover_image_path).name
            cover_info["file_exists_local"] = local_file.exists()
            if local_file.exists():
                cover_info["file_size_kb"] = round(local_file.stat().st_size / 1024, 2)
                ext = local_file.suffix.lower().lstrip(".")
                format_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WebP"}
                cover_info["format"] = format_map.get(ext, ext.upper())
            else:
                cover_info["file_size_kb"] = None
                cover_info["format"] = None
        else:
            cover_info["file_exists_local"] = False
            cover_info["file_size_kb"] = None
            cover_info["format"] = None

        items.append(cover_info)

    # Summary stats
    total_with_cover = db.query(Manga).filter(Manga.cover_image_path.isnot(None)).count()
    total_without_cover = db.query(Manga).count() - total_with_cover

    return {
        "items": items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        },
        "summary": {
            "total_manga": db.query(Manga).count(),
            "with_cover": total_with_cover,
            "without_cover": total_without_cover,
            "coverage_percent": round(total_with_cover / db.query(Manga).count() * 100, 1) if db.query(Manga).count() > 0 else 0
        }
    }


@admin_router.post("/covers/sync-from-gdrive")
def admin_sync_covers_from_gdrive(
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Download semua cover dari GDrive ke local server"""
    cover_service = CoverService()
    result = cover_service.sync_all_covers_from_gdrive()

    logger.info(f"Admin {current_user.username} triggered cover sync from GDrive")

    return result


@admin_router.get("/covers/stats")
def admin_get_cover_stats(
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get statistik covers di local server"""
    cover_service = CoverService()
    return cover_service.get_cover_stats()


# ==========================================
# CHAPTER MANAGEMENT
# ==========================================

@admin_router.get("/chapters")
def admin_list_chapters(
    manga_id: Optional[int] = Query(None),
    manga_slug: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] List chapters dengan filter"""
    query = db.query(Chapter)

    if manga_id:
        query = query.filter(Chapter.manga_id == manga_id)
    elif manga_slug:
        manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
        if manga:
            query = query.filter(Chapter.manga_id == manga.id)

    total = query.count()
    chapters = query.offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for ch in chapters:
        items.append({
            "id": ch.id,
            "manga_id": ch.manga_id,
            "manga_title": ch.manga.title if ch.manga else None,
            "manga_slug": ch.manga.slug if ch.manga else None,
            "chapter_main": ch.chapter_main,
            "chapter_sub": ch.chapter_sub,
            "chapter_label": ch.chapter_label,
            "slug": ch.slug,
            "chapter_folder_name": ch.chapter_folder_name,
            "total_pages": len(ch.pages),
            "uploaded_by": ch.uploader.username if ch.uploader else None,
            "created_at": ch.created_at
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


@admin_router.put("/chapter/{chapter_id}")
def admin_update_chapter(
    chapter_id: int,
    update_data: ChapterUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Update data chapter"""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

    if update_data.chapter_label is not None:
        chapter.chapter_label = update_data.chapter_label

    if update_data.slug is not None and update_data.slug != chapter.slug:
        existing = db.query(Chapter).filter(
            Chapter.slug == update_data.slug,
            Chapter.id != chapter_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Slug '{update_data.slug}' sudah digunakan")
        chapter.slug = update_data.slug

    if update_data.chapter_folder_name is not None:
        chapter.chapter_folder_name = update_data.chapter_folder_name

    if update_data.chapter_main is not None:
        chapter.chapter_main = update_data.chapter_main

    if update_data.chapter_sub is not None:
        chapter.chapter_sub = update_data.chapter_sub

    db.commit()
    db.refresh(chapter)

    logger.info(f"Admin {current_user.username} updated chapter ID {chapter_id}")

    return {
        "success": True,
        "message": f"Chapter '{chapter.chapter_label}' berhasil diupdate",
        "chapter_id": chapter.id,
        "chapter_slug": chapter.slug
    }


@admin_router.delete("/chapter/{chapter_id}")
def admin_delete_chapter(
    chapter_id: int,
    delete_gdrive: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus chapter beserta semua pages"""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

    chapter_label = chapter.chapter_label
    manga = chapter.manga

    cache_manager = CacheManager(db)
    cleared_cache = cache_manager.cleanup_chapter_cache(chapter_id)

    gdrive_deleted = False
    if delete_gdrive and manga:
        try:
            multi_remote = get_multi_remote_service()
            remote_name, rclone = multi_remote.get_next_remote(strategy="least_used")

            folder_path = f"{manga.storage_source.base_folder_id}/{manga.slug}/{chapter.chapter_folder_name}"
            gdrive_deleted = rclone.delete_path(folder_path, is_directory=True)

            if gdrive_deleted:
                logger.info(f"Deleted chapter folder via remote '{remote_name}': {folder_path}")
        except Exception as e:
            logger.error(f"Error deleting GDrive chapter folder: {str(e)}")

    db.delete(chapter)
    db.commit()

    logger.info(f"Admin {current_user.username} deleted chapter: {chapter_label} (ID: {chapter_id})")

    return {
        "success": True,
        "message": f"Chapter '{chapter_label}' berhasil dihapus",
        "deleted_chapter_id": chapter_id,
        "cache_cleared": cleared_cache,
        "gdrive_folder_deleted": gdrive_deleted
    }


# ==========================================
# ✨ THUMBNAIL MANAGEMENT
# ==========================================

@admin_router.post("/chapter/{chapter_id}/thumbnail/upload")
async def upload_custom_thumbnail(
    chapter_id: int,
    thumbnail: UploadFile = File(..., description="Custom thumbnail image (16:9 recommended)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Upload custom thumbnail untuk chapter.

    - Aspect ratio: 16:9 recommended (akan di-crop otomatis)
    - Max size: 5MB
    - Formats: JPG, PNG, WEBP
    - Will be optimized to 1280x720
    """
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if not chapter:
            raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

        manga = chapter.manga

        content = await thumbnail.read()

        thumbnail_service = ThumbnailService()
        is_valid, error_msg = thumbnail_service.validate_thumbnail_image(
            thumbnail.filename,
            len(content),
            thumbnail.content_type
        )

        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)

        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        chapter_folder = f"{manga.storage_source.base_folder_id}/{manga.slug}/{chapter.chapter_folder_name}"
        thumbnail_gdrive_path = f"{chapter_folder}/thumbnail.jpg"

        result = thumbnail_service.rclone._run_command([
            "copyto",
            tmp_path,
            f"{thumbnail_service.rclone.remote_name}:{thumbnail_gdrive_path}",
            "--progress"
        ], timeout=60)

        import os
        os.unlink(tmp_path)

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload thumbnail to GDrive: {result.stderr}"
            )

        chapter.anchor_path = thumbnail_gdrive_path
        chapter.preview_url = f"/api/v1/image-proxy/image/{thumbnail_gdrive_path}"

        db.commit()
        db.refresh(chapter)

        logger.info(
            f"Admin {current_user.username} uploaded custom thumbnail for chapter "
            f"{chapter.chapter_label} (ID: {chapter_id})"
        )

        return {
            "success": True,
            "message": "Custom thumbnail uploaded successfully",
            "chapter_id": chapter.id,
            "chapter_label": chapter.chapter_label,
            "thumbnail_path": thumbnail_gdrive_path,
            "preview_url": chapter.preview_url
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to upload custom thumbnail: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@admin_router.post("/chapter/{chapter_id}/thumbnail/generate")
def generate_chapter_thumbnail(
    chapter_id: int,
    source_page: int = Query(1, ge=1, description="Page number to use as source"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Auto-generate 16:9 thumbnail dari page tertentu.
    """
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if not chapter:
            raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

        page = db.query(Page).filter(
            Page.chapter_id == chapter_id,
            Page.page_order == source_page
        ).first()

        if not page:
            raise HTTPException(
                status_code=404,
                detail=f"Page {source_page} tidak ditemukan dalam chapter ini"
            )

        manga = chapter.manga

        source_path = page.gdrive_file_id
        chapter_folder = f"{manga.storage_source.base_folder_id}/{manga.slug}/{chapter.chapter_folder_name}"
        thumbnail_path = f"{chapter_folder}/thumbnail.jpg"

        logger.info(
            f"Generating thumbnail for chapter {chapter.chapter_label}: "
            f"source={source_path}, output={thumbnail_path}"
        )

        thumbnail_service = ThumbnailService()
        success = thumbnail_service.generate_16_9_thumbnail(source_path, thumbnail_path)

        if not success:
            raise HTTPException(status_code=500, detail="Failed to generate thumbnail")

        chapter.anchor_path = thumbnail_path
        chapter.preview_url = f"/api/v1/image-proxy/image/{thumbnail_path}"

        db.commit()
        db.refresh(chapter)

        logger.info(
            f"Admin {current_user.username} generated thumbnail for chapter "
            f"{chapter.chapter_label} from page {source_page}"
        )

        return {
            "success": True,
            "message": f"Thumbnail generated successfully from page {source_page}",
            "chapter_id": chapter.id,
            "chapter_label": chapter.chapter_label,
            "source_page": source_page,
            "thumbnail_path": thumbnail_path,
            "preview_url": chapter.preview_url
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to generate thumbnail: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


@admin_router.post("/manga/{manga_slug}/thumbnails/generate-all")
async def bulk_generate_thumbnails(
    manga_slug: str,
    source_page: int = Query(1, ge=1, description="Page number to use for all chapters"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
    background_tasks: BackgroundTasks = None
):
    """
    [ADMIN] Generate thumbnails untuk SEMUA chapter dalam manga.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")

    chapters = db.query(Chapter).filter(Chapter.manga_id == manga.id).all()

    if not chapters:
        raise HTTPException(status_code=404, detail="Tidak ada chapter ditemukan")

    if background_tasks:
        background_tasks.add_task(
            _bulk_generate_thumbnails_task,
            manga.id,
            [ch.id for ch in chapters],
            source_page,
            current_user.username
        )

    logger.info(
        f"Admin {current_user.username} started bulk thumbnail generation for "
        f"{manga.title} ({len(chapters)} chapters)"
    )

    return {
        "success": True,
        "message": f"Generating thumbnails for {len(chapters)} chapters in background",
        "manga_slug": manga_slug,
        "manga_title": manga.title,
        "total_chapters": len(chapters),
        "source_page": source_page,
        "note": "Check server logs for progress"
    }


def _bulk_generate_thumbnails_task(
    manga_id: int,
    chapter_ids: List[int],
    source_page: int,
    admin_username: str
):
    """Background task untuk generate thumbnails."""
    from app.core.base import SessionLocal

    db = SessionLocal()

    try:
        manga = db.query(Manga).filter(Manga.id == manga_id).first()
        if not manga:
            logger.error(f"Manga ID {manga_id} not found in background task")
            return

        thumbnail_service = ThumbnailService()
        success_count = 0
        failed_count = 0
        skipped_count = 0

        logger.info(
            f"🚀 Starting bulk thumbnail generation for '{manga.title}': "
            f"{len(chapter_ids)} chapters, source_page={source_page}"
        )

        for idx, chapter_id in enumerate(chapter_ids, 1):
            try:
                chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
                if not chapter:
                    skipped_count += 1
                    logger.warning(f"⚠️ Chapter ID {chapter_id} not found, skipping")
                    continue

                page = db.query(Page).filter(
                    Page.chapter_id == chapter_id,
                    Page.page_order == source_page
                ).first()

                if not page:
                    skipped_count += 1
                    logger.warning(
                        f"⚠️ Page {source_page} not found in chapter {chapter.chapter_label}, skipping"
                    )
                    continue

                chapter_folder = f"{manga.storage_source.base_folder_id}/{manga.slug}/{chapter.chapter_folder_name}"
                thumbnail_path = f"{chapter_folder}/thumbnail.jpg"

                logger.info(
                    f"[{idx}/{len(chapter_ids)}] Generating thumbnail for "
                    f"'{chapter.chapter_label}' from page {source_page}..."
                )

                success = thumbnail_service.generate_16_9_thumbnail(
                    page.gdrive_file_id,
                    thumbnail_path
                )

                if success:
                    chapter.anchor_path = thumbnail_path
                    chapter.preview_url = f"/api/v1/image-proxy/image/{thumbnail_path}"
                    db.commit()

                    success_count += 1
                    logger.info(f"✅ [{idx}/{len(chapter_ids)}] Success: {chapter.chapter_label}")
                else:
                    failed_count += 1
                    logger.error(f"❌ [{idx}/{len(chapter_ids)}] Failed: {chapter.chapter_label}")

            except Exception as e:
                failed_count += 1
                logger.error(
                    f"❌ Error generating thumbnail for chapter {chapter_id}: {str(e)}",
                    exc_info=True
                )

        logger.info(
            f"🎉 Bulk thumbnail generation complete for '{manga.title}': "
            f"✅ {success_count} success, ❌ {failed_count} failed, ⚠️ {skipped_count} skipped"
        )

    except Exception as e:
        logger.error(f"❌ Bulk thumbnail task failed: {str(e)}", exc_info=True)
    finally:
        db.close()


@admin_router.delete("/chapter/{chapter_id}/thumbnail")
def delete_chapter_thumbnail(
    chapter_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Delete custom thumbnail dan revert ke page 1.
    """
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if not chapter:
            raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

        if not chapter.anchor_path or "thumbnail.jpg" not in chapter.anchor_path:
            raise HTTPException(
                status_code=400,
                detail="Chapter tidak memiliki custom thumbnail"
            )

        thumbnail_service = ThumbnailService()

        result = thumbnail_service.rclone._run_command([
            "deletefile",
            f"{thumbnail_service.rclone.remote_name}:{chapter.anchor_path}"
        ], timeout=30)

        if result.returncode == 0:
            logger.info(f"Deleted thumbnail from GDrive: {chapter.anchor_path}")
        else:
            logger.warning(f"Failed to delete thumbnail from GDrive: {result.stderr}")

        first_page = db.query(Page).filter(
            Page.chapter_id == chapter_id
        ).order_by(Page.page_order.asc()).first()

        if first_page:
            chapter.anchor_path = first_page.gdrive_file_id
            chapter.preview_url = f"/api/v1/image-proxy/image/{first_page.gdrive_file_id}"
        else:
            chapter.anchor_path = None
            chapter.preview_url = None

        db.commit()
        db.refresh(chapter)

        logger.info(
            f"Admin {current_user.username} deleted thumbnail for chapter "
            f"{chapter.chapter_label}, reverted to page 1"
        )

        return {
            "success": True,
            "message": "Thumbnail deleted, reverted to page 1",
            "chapter_id": chapter.id,
            "new_anchor_path": chapter.anchor_path,
            "new_preview_url": chapter.preview_url
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete thumbnail: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")


@admin_router.get("/chapter/{chapter_id}/thumbnail/info")
def get_thumbnail_info(
    chapter_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Get info tentang thumbnail chapter.
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

    is_custom = chapter.anchor_path and "thumbnail.jpg" in chapter.anchor_path

    return {
        "chapter_id": chapter.id,
        "chapter_label": chapter.chapter_label,
        "has_custom_thumbnail": is_custom,
        "anchor_path": chapter.anchor_path,
        "preview_url": chapter.preview_url,
        "thumbnail_type": "custom_16_9" if is_custom else "page_1_original"
    }


# ==========================================
# MULTI-REMOTE MANAGEMENT
# ==========================================

@admin_router.get("/remotes/health")
def get_remotes_health(
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get health status of all rclone remotes (semua group)"""
    try:
        multi_remote = get_multi_remote_service()
        health_status = multi_remote.get_health_status()

        health_status["configuration"] = {
            "multi_remote_enabled": settings.is_multi_remote_enabled,
            "load_balancing_strategy": settings.RCLONE_LOAD_BALANCING_STRATEGY,
            "auto_recovery_enabled": settings.RCLONE_AUTO_RECOVERY_ENABLED,
            "quota_reset_hours": settings.RCLONE_QUOTA_RESET_HOURS,
            # ✅ NEW: group 2 config info
            "group2_configured": settings.is_next_group_configured,
            "group2_enabled": settings.is_group2_enabled,
            "group2_path_prefix": settings.GROUP2_PATH_PREFIX,
            "group1_quota_gb": settings.RCLONE_GROUP1_QUOTA_GB,
            "auto_switch_group": settings.RCLONE_AUTO_SWITCH_GROUP,
        }

        return health_status

    except Exception as e:
        logger.error(f"Failed to get remotes health: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.post("/remotes/{remote_name}/reset")
def reset_remote_health(
    remote_name: str,
    group: int = Query(1, ge=1, le=2, description="Remote group (1 atau 2)"),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Manual reset health status untuk remote tertentu"""
    try:
        multi_remote = get_multi_remote_service()
        # ✅ NEW: support group param
        success = multi_remote.reset_remote_health(remote_name, group=group)

        if success:
            logger.info(
                f"Admin {current_user.username} reset health for remote "
                f"'{remote_name}' (G{group})"
            )
            return {
                "success": True,
                "message": f"Remote '{remote_name}' (Group {group}) health reset successfully"
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Remote '{remote_name}' not found in group {group}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reset remote health: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/remotes/best")
def get_best_remote(
    group: int = Query(1, ge=1, le=2, description="Remote group (1 atau 2)"),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get remote dengan success rate tertinggi"""
    try:
        multi_remote = get_multi_remote_service()
        # ✅ NEW: support group param
        remote_name, _ = multi_remote.get_best_remote(group=group)

        status = multi_remote._groups[group]["status"][remote_name]

        return {
            "best_remote": remote_name,
            "group": group,
            "success_rate": round(status.success_rate, 2),
            "total_requests": status.total_requests,
            "successful_requests": status.successful_requests,
            "failed_requests": status.failed_requests,
            "last_used": status.last_used.isoformat() if status.last_used else None
        }

    except Exception as e:
        logger.error(f"Failed to get best remote: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/remotes/stats")
def get_remotes_statistics(
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get detailed statistics for all remotes (semua group)"""
    try:
        multi_remote = get_multi_remote_service()
        health_status = multi_remote.get_health_status()

        # Group 1 stats
        g1_remotes = [r for r in health_status["remotes"] if r.get("group", 1) == 1]
        total_requests_g1 = sum(r["total_requests"] for r in g1_remotes)
        total_successful_g1 = sum(r["successful_requests"] for r in g1_remotes)
        total_failed_g1 = sum(r["failed_requests"] for r in g1_remotes)
        avg_success_g1 = (
            sum(r["success_rate"] for r in g1_remotes) / len(g1_remotes)
            if g1_remotes else 0
        )

        # Group 2 stats
        g2_remotes = health_status.get("group2", {}).get("remotes", [])
        total_requests_g2 = sum(r["total_requests"] for r in g2_remotes)
        total_successful_g2 = sum(r["successful_requests"] for r in g2_remotes)
        total_failed_g2 = sum(r["failed_requests"] for r in g2_remotes)
        avg_success_g2 = (
            sum(r["success_rate"] for r in g2_remotes) / len(g2_remotes)
            if g2_remotes else 0
        )

        return {
            "summary": {
                "group1": {
                    "total_remotes": health_status["total_remotes"],
                    "healthy_remotes": health_status["healthy_remotes"],
                    "available_remotes": health_status["available_remotes"],
                    "total_requests": total_requests_g1,
                    "total_successful": total_successful_g1,
                    "total_failed": total_failed_g1,
                    "average_success_rate": round(avg_success_g1, 2),
                },
                "group2": {
                    "configured": settings.is_next_group_configured,
                    "total_remotes": health_status.get("group2", {}).get("total_remotes", 0),
                    "healthy_remotes": health_status.get("group2", {}).get("healthy_remotes", 0),
                    "available_remotes": health_status.get("group2", {}).get("available_remotes", 0),
                    "total_requests": total_requests_g2,
                    "total_successful": total_successful_g2,
                    "total_failed": total_failed_g2,
                    "average_success_rate": round(avg_success_g2, 2),
                },
            },
            "group1_remotes": g1_remotes,
            "group2_remotes": g2_remotes,
            "configuration": {
                "multi_remote_enabled": settings.is_multi_remote_enabled,
                "load_balancing_strategy": settings.RCLONE_LOAD_BALANCING_STRATEGY,
                "configured_remotes_g1": settings.get_rclone_remotes(),
                "configured_remotes_g2": settings.get_next_group_remotes(),
                "group2_path_prefix": settings.GROUP2_PATH_PREFIX,
                "group2_enabled": settings.is_group2_enabled,
            }
        }

    except Exception as e:
        logger.error(f"Failed to get remotes statistics: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# USER MANAGEMENT
# ==========================================

@admin_router.get("/users")
def admin_list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] List semua user"""
    query = db.query(User)

    if search:
        query = query.filter(
            (User.username.ilike(f"%{search}%")) | (User.email.ilike(f"%{search}%"))
        )

    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    total = query.count()
    users = query.offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for user in users:
        items.append({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_active": user.is_active,
            "roles": [r.name for r in user.roles],
            "total_uploads": len(user.chapters),
            "created_at": user.created_at,
            "last_login": user.last_login
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


@admin_router.put("/users/{user_id}/role")
def admin_update_user_role(
    user_id: int,
    update_data: UserRoleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Ubah role user"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Tidak bisa ubah role diri sendiri")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User ID {user_id} tidak ditemukan")

    roles = []
    for role_name in update_data.roles:
        role = db.query(Role).filter(Role.name == role_name).first()
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_name}' tidak ditemukan")
        roles.append(role)

    user.roles = roles
    db.commit()

    logger.info(f"Admin {current_user.username} updated roles for user {user.username}: {update_data.roles}")

    return {
        "success": True,
        "message": f"Role user '{user.username}' berhasil diupdate",
        "user_id": user_id,
        "new_roles": update_data.roles
    }


@admin_router.put("/users/{user_id}/status")
def admin_toggle_user_status(
    user_id: int,
    update_data: UserStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Aktifkan atau nonaktifkan user"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Tidak bisa ubah status diri sendiri")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User ID {user_id} tidak ditemukan")

    old_status = user.is_active
    user.is_active = update_data.is_active
    db.commit()

    action = "diaktifkan" if update_data.is_active else "dinonaktifkan"
    logger.info(f"Admin {current_user.username} {action} user: {user.username}")

    return {
        "success": True,
        "message": f"User '{user.username}' berhasil {action}",
        "user_id": user_id,
        "is_active": update_data.is_active
    }


@admin_router.delete("/users/{user_id}")
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus user dari sistem"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Tidak bisa hapus akun sendiri")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User ID {user_id} tidak ditemukan")

    username = user.username
    db.delete(user)
    db.commit()

    logger.info(f"Admin {current_user.username} deleted user: {username} (ID: {user_id})")

    return {
        "success": True,
        "message": f"User '{username}' berhasil dihapus",
        "deleted_user_id": user_id
    }


# ==========================================
# STORAGE MANAGEMENT
# ==========================================

@admin_router.get("/storage")
def admin_list_storage(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] List semua storage sources dengan statistik"""
    storages = db.query(StorageSource).all()

    items = []
    for storage in storages:
        total_manga = db.query(Manga).filter(Manga.storage_id == storage.id).count()
        total_chapters = db.query(Chapter).join(Manga).filter(
            Manga.storage_id == storage.id
        ).count()

        items.append({
            "id": storage.id,
            "source_name": storage.source_name,
            "base_folder_id": storage.base_folder_id,
            "status": storage.status,
            "total_manga": total_manga,
            "total_chapters": total_chapters,
            "created_at": storage.created_at
        })

    return {"items": items, "total": len(items)}


@admin_router.post("/storage/{storage_id}/test")
def admin_test_storage(
    storage_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Test koneksi ke storage source"""
    storage = db.query(StorageSource).filter(StorageSource.id == storage_id).first()
    if not storage:
        raise HTTPException(status_code=404, detail=f"Storage ID {storage_id} tidak ditemukan")

    try:
        multi_remote = get_multi_remote_service()
        health = multi_remote.get_health_status()

        return {
            "success": True,
            "storage_id": storage_id,
            "source_name": storage.source_name,
            "multi_remote_enabled": settings.is_multi_remote_enabled,
            "remotes_status": health["remotes"],
            "total_remotes": health["total_remotes"],
            "healthy_remotes": health["healthy_remotes"],
            "available_remotes": health["available_remotes"],
            # Group 2 info
            "group2_configured": settings.is_next_group_configured,
            "group2_enabled": settings.is_group2_enabled,
        }

    except Exception as e:
        logger.error(f"Storage test failed: {str(e)}")
        return {
            "success": False,
            "storage_id": storage_id,
            "source_name": storage.source_name,
            "status": "error",
            "error": str(e)
        }


@admin_router.put("/storage/{storage_id}/status")
def admin_toggle_storage_status(
    storage_id: int,
    new_status: str = Query(..., description="active | suspended"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Toggle status storage source"""
    if new_status not in ["active", "suspended"]:
        raise HTTPException(status_code=400, detail="Status harus: active | suspended")

    storage = db.query(StorageSource).filter(StorageSource.id == storage_id).first()
    if not storage:
        raise HTTPException(status_code=404, detail=f"Storage ID {storage_id} tidak ditemukan")

    storage.status = new_status
    db.commit()

    return {
        "success": True,
        "storage_id": storage_id,
        "new_status": new_status
    }


# ==========================================
# CACHE MANAGEMENT
# ==========================================

@admin_router.get("/cache/stats")
def admin_cache_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get statistik cache"""
    cache_manager = CacheManager(db)
    return cache_manager.get_cache_stats()


@admin_router.post("/cache/cleanup")
def admin_cache_cleanup(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Manual cleanup cache yang expired"""
    cache_manager = CacheManager(db)
    result = cache_manager.cleanup_expired()
    return result


@admin_router.delete("/cache/chapter/{chapter_id}")
def admin_clear_chapter_cache(
    chapter_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus cache untuk chapter tertentu"""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

    cache_manager = CacheManager(db)
    deleted = cache_manager.cleanup_chapter_cache(chapter_id)

    return {
        "success": True,
        "chapter_id": chapter_id,
        "chapter_label": chapter.chapter_label,
        "files_deleted": deleted
    }


@admin_router.delete("/cache/manga/{manga_id}")
def admin_clear_manga_cache(
    manga_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus semua cache untuk manga tertentu"""
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    cache_manager = CacheManager(db)
    total_deleted = 0
    for chapter in manga.chapters:
        total_deleted += cache_manager.cleanup_chapter_cache(chapter.id)

    return {
        "success": True,
        "manga_id": manga_id,
        "manga_title": manga.title,
        "total_files_deleted": total_deleted
    }


# ==========================================
# SYSTEM STATS
# ==========================================

@admin_router.get("/stats")
def admin_get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Get statistik sistem lengkap"""
    cache_manager = CacheManager(db)

    try:
        multi_remote = get_multi_remote_service()
        remote_health = multi_remote.get_health_status()
    except Exception as e:
        logger.error(f"Failed to get remote health: {str(e)}")
        remote_health = {"error": str(e)}

    return {
        "database": {
            "total_users": db.query(User).count(),
            "active_users": db.query(User).filter(User.is_active == True).count(),
            "total_manga": db.query(Manga).count(),
            "manga_ongoing": db.query(Manga).filter(Manga.status == "ongoing").count(),
            "manga_completed": db.query(Manga).filter(Manga.status == "completed").count(),
            "total_chapters": db.query(Chapter).count(),
            "total_pages": db.query(Page).count(),
            "total_storage_sources": db.query(StorageSource).count(),
            "active_storage": db.query(StorageSource).filter(
                StorageSource.status == "active"
            ).count()
        },
        "cache": cache_manager.get_cache_stats(),
        "remotes": remote_health,
        "roles": {
            r.name: db.query(User).filter(
                User.roles.any(Role.name == r.name)
            ).count()
            for r in db.query(Role).all()
        }
    }


# ==========================================
# GENRE & TYPE MANAGEMENT
# ==========================================

@admin_router.post("/genres")
def admin_create_genre(
    name: str = Query(..., min_length=1, max_length=50),
    slug: str = Query(..., min_length=1, max_length=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Tambah genre baru"""
    if db.query(Genre).filter(Genre.slug == slug).first():
        raise HTTPException(status_code=400, detail=f"Genre slug '{slug}' sudah ada")

    genre = Genre(name=name, slug=slug)
    db.add(genre)
    db.commit()
    db.refresh(genre)

    return {"success": True, "genre": {"id": genre.id, "name": genre.name, "slug": genre.slug}}


@admin_router.delete("/genres/{genre_id}")
def admin_delete_genre(
    genre_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Hapus genre"""
    genre = db.query(Genre).filter(Genre.id == genre_id).first()
    if not genre:
        raise HTTPException(status_code=404, detail=f"Genre ID {genre_id} tidak ditemukan")

    if genre.manga_list:
        raise HTTPException(
            status_code=400,
            detail=f"Genre '{genre.name}' masih digunakan oleh {len(genre.manga_list)} manga"
        )

    db.delete(genre)
    db.commit()

    return {"success": True, "message": f"Genre '{genre.name}' berhasil dihapus"}


@admin_router.post("/manga-types")
def admin_create_manga_type(
    name: str = Query(..., min_length=1, max_length=50),
    slug: str = Query(..., min_length=1, max_length=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Tambah tipe manga baru"""
    if db.query(MangaType).filter(MangaType.slug == slug).first():
        raise HTTPException(status_code=400, detail=f"Type slug '{slug}' sudah ada")

    manga_type = MangaType(name=name, slug=slug)
    db.add(manga_type)
    db.commit()
    db.refresh(manga_type)

    return {"success": True, "type": {"id": manga_type.id, "name": manga_type.name, "slug": manga_type.slug}}


@admin_router.get("/roles")
def admin_list_roles(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] List semua roles yang tersedia"""
    roles = db.query(Role).all()
    return {
        "roles": [
            {
                "id": r.id,
                "name": r.name,
                "user_count": len(r.users)
            }
            for r in roles
        ]
    }


@admin_router.post("/roles")
def admin_create_role(
    name: str = Query(..., min_length=1, max_length=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))
):
    """[ADMIN] Buat role baru"""
    if db.query(Role).filter(Role.name == name).first():
        raise HTTPException(status_code=400, detail=f"Role '{name}' sudah ada")

    role = Role(name=name)
    db.add(role)
    db.commit()
    db.refresh(role)

    return {"success": True, "role": {"id": role.id, "name": role.name}}


# ==========================================
# ✅ GROUP MANAGEMENT ENDPOINTS
# ==========================================

@admin_router.get("/groups/status")
def get_groups_status(
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Get status semua storage group.

    Returns info tentang:
    - Group 1: primary + backup remotes (gdrive..gdrive10)
    - Group 2: next primary + backup remotes (gdrive11..gdrive20)
    - Active group untuk upload baru
    - Daemon status per group
    - Quota info
    """
    try:
        multi_remote = get_multi_remote_service()

        # get_health_status() sudah return semua info termasuk group2 nested di dalamnya
        health = multi_remote.get_health_status()

        # Ambil group 1 info dari health response
        g1_remotes = health.get("remotes", [])
        g1_total = health.get("total_remotes", 0)
        g1_healthy = health.get("healthy_remotes", 0)
        g1_available = health.get("available_remotes", 0)
        g1_daemons = health.get("serve_daemons_running", 0)
        g1_daemon_urls = health.get("active_daemon_urls", [])

        # Ambil group 2 info dari nested key "group2"
        g2_info = health.get("group2", {})
        g2_remotes = g2_info.get("remotes", [])
        g2_total = g2_info.get("total_remotes", 0)
        g2_healthy = g2_info.get("healthy_remotes", 0)
        g2_available = g2_info.get("available_remotes", 0)
        g2_daemons = g2_info.get("serve_daemons_running", 0)
        g2_daemon_urls = g2_info.get("active_daemon_urls", [])

        active_group = multi_remote.get_active_upload_group()

        return {
            "active_upload_group": active_group,
            "group2_path_prefix": settings.GROUP2_PATH_PREFIX,
            "auto_switch_group": settings.RCLONE_AUTO_SWITCH_GROUP,
            "group1": {
                "primary": settings.RCLONE_PRIMARY_REMOTE,
                "backups": settings.get_secondary_remotes(),
                "all_remotes": settings.get_rclone_remotes(),
                "quota_limit_gb": settings.RCLONE_GROUP1_QUOTA_GB,
                "total_remotes": g1_total,
                "healthy_remotes": g1_healthy,
                "available_remotes": g1_available,
                "serve_daemons_running": g1_daemons,
                "active_daemon_urls": g1_daemon_urls,
                "remotes": g1_remotes,
            },
            "group2": {
                "configured": settings.is_next_group_configured,
                "enabled": settings.is_group2_enabled,
                "primary": settings.RCLONE_NEXT_PRIMARY_REMOTE or None,
                "backups": settings.get_next_backup_remotes(),
                "all_remotes": settings.get_next_group_remotes(),
                "total_remotes": g2_total,
                "healthy_remotes": g2_healthy,
                "available_remotes": g2_available,
                "serve_daemons_running": g2_daemons,
                "active_daemon_urls": g2_daemon_urls,
                "remotes": g2_remotes,
                "path_prefix": settings.GROUP2_PATH_PREFIX,
            },
        }

    except Exception as e:
        logger.error(f"Failed to get groups status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.post("/groups/switch")
def manual_switch_group(
    target_group: int = Query(..., ge=1, le=2, description="Target group (1 atau 2)"),
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Manual switch active upload group.

    - target_group=1 → Upload ke Group 1 (gdrive, gdrive1..gdrive10), path tanpa prefix
    - target_group=2 → Upload ke Group 2 (gdrive11..gdrive20), path dengan prefix '@'

    Berguna untuk:
    - Force switch ke group 2 sebelum group 1 penuh
    - Fallback ke group 1 jika group 2 bermasalah
    """
    try:
        if target_group == 2 and not settings.is_next_group_configured:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Group 2 belum dikonfigurasi. "
                    "Set RCLONE_NEXT_PRIMARY_REMOTE di .env terlebih dahulu."
                )
            )

        multi_remote = get_multi_remote_service()

        # ✅ set_active_upload_group() ada di MultiRemoteService
        # (validates group 2 tersedia sebelum switch)
        multi_remote.set_active_upload_group(target_group)

        logger.info(
            f"Admin {current_user.username} manually switched active upload group to G{target_group}"
        )

        path_info = (
            f"Path baru akan disimpan dengan prefix '{settings.GROUP2_PATH_PREFIX}' di database"
            if target_group == 2
            else "Path baru akan disimpan tanpa prefix (group 1 normal)"
        )

        primary_remote = (
            settings.RCLONE_NEXT_PRIMARY_REMOTE
            if target_group == 2
            else settings.RCLONE_PRIMARY_REMOTE
        )

        return {
            "success": True,
            "message": f"Active upload group berhasil dipindah ke Group {target_group}",
            "active_group": target_group,
            "primary_remote": primary_remote,
            "path_prefix": settings.GROUP2_PATH_PREFIX if target_group == 2 else "",
            "note": path_info,
        }

    except HTTPException:
        raise
    except RuntimeError as e:
        # RuntimeError dari set_active_upload_group jika group 2 tidak ready
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to switch group: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/groups/quota")
def get_groups_quota_info(
    current_user: User = Depends(require_role("admin"))
):
    """
    [ADMIN] Get quota info untuk semua group.

    Returns:
    - Group 1 quota usage & limit
    - Group 2 konfigurasi
    - Active upload group
    - Auto-switch status
    """
    multi_remote = get_multi_remote_service()
    active_group = multi_remote.get_active_upload_group()

    # Coba ambil dari StorageGroupService jika tersedia
    quota_stats = None
    try:
        from app.services.storage_group_service import GroupQuotaTracker
        quota_stats = GroupQuotaTracker.get_instance().get_stats()
    except Exception as e:
        logger.warning(f"Could not get quota stats from StorageGroupService: {str(e)}")

    return {
        "active_upload_group": active_group,
        "auto_switch_enabled": settings.RCLONE_AUTO_SWITCH_GROUP,
        "group1_quota_limit_gb": settings.RCLONE_GROUP1_QUOTA_GB,
        "group1_quota_note": (
            "0 = unlimited / manual switch only"
            if settings.RCLONE_GROUP1_QUOTA_GB == 0
            else f"Auto-switch to group 2 when usage >= {settings.RCLONE_GROUP1_QUOTA_GB} GB"
        ),
        "group2_configured": settings.is_next_group_configured,
        "group2_enabled": settings.is_group2_enabled,
        "group2_path_prefix": settings.GROUP2_PATH_PREFIX,
        "quota_tracker": quota_stats,
    }


# ==========================================
# ✅ IMAGE PROXY ROUTER (GROUP AWARE)
# ==========================================

image_proxy_router = APIRouter()


def validate_file_path(file_path: str) -> str:
    """
    Validate file path to prevent path traversal attacks.

    ✅ GROUP AWARE: path boleh dimulai dengan GROUP2_PATH_PREFIX ('@')
    karena itu adalah group marker, bukan path traversal.
    Strip prefix dulu sebelum validasi.
    """
    # ✅ Strip group prefix sebelum validasi keamanan
    # Gunakan settings.clean_path() bukan lstrip() karena lstrip strip per-karakter
    check_path = settings.clean_path(file_path)

    if ".." in check_path or check_path.startswith("/") or "\\" in check_path:
        logger.warning(f"Path traversal attempt detected: {file_path}")
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not check_path or len(check_path) < 5:
        raise HTTPException(status_code=400, detail="File path too short")

    valid_extensions = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    if not any(check_path.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only image files are allowed"
        )

    return file_path


def get_image_content_type(filename: str) -> str:
    """Get content type based on file extension"""
    # Strip group prefix sebelum ambil extension
    clean = settings.clean_path(filename)
    extension = clean.lower().split(".")[-1]

    content_types = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }

    return content_types.get(extension, "image/jpeg")


async def _get_daemon_url_for_file(
    multi_remote,
    file_path: str = "",
    strategy: str = "round_robin"
) -> Tuple[Optional[str], int]:
    """
    ✅ GROUP AWARE: Get daemon URL via Round Robin sesuai group path.

    Baca prefix path untuk tentukan group:
        "manga_library/xxx/001.jpg"  → group 1 (gdrive..gdrive10)
        "@manga_library/xxx/001.jpg" → group 2 (gdrive11..gdrive20)

    Args:
        multi_remote: MultiRemoteService instance
        file_path: Path dari DB (mungkin ada prefix '@')
        strategy: kept for backward compat (pakai round robin internal)

    Returns:
        (daemon_url, group) - daemon_url bisa None jika tidak ada daemon running
    """
    # Determine group dari path prefix
    group = multi_remote.get_group_for_path(file_path)

    try:
        url = await multi_remote.get_next_daemon_url(group=group)
        return url, group
    except Exception as e:
        logger.warning(f"Failed to get daemon URL (G{group}): {str(e)}")
        return None, group


async def _stream_from_serve_daemon(
    daemon_url: str,
    file_path: str,
    chunk_size: int = 65536
) -> AsyncIterator[bytes]:
    """
    ✅ True async streaming pakai singleton HTTPX AsyncClient.

    Menggunakan HttpxClientManager singleton agar connection pool di-reuse
    (tidak buka TCP baru + TLS handshake tiap request).

    Args:
        daemon_url: Base URL daemon (e.g., http://127.0.0.1:8180)
        file_path: File path di remote - SUDAH CLEAN (tanpa '@' prefix)
        chunk_size: Ukuran chunk per yield (default 64KB)

    Yields:
        bytes: Chunk data dari response stream
    """
    # ✅ Pakai singleton client (connection pool di-reuse)
    client = HttpxClientManager.get_client(daemon_url)

    async with client.stream("GET", f"/{file_path}") as response:
        if response.status_code == 404:
            raise FileNotFoundError(f"File not found via daemon: {file_path}")
        if response.status_code != 200:
            raise RuntimeError(
                f"Daemon returned HTTP {response.status_code} for {file_path}"
            )
        async for chunk in response.aiter_bytes(chunk_size):
            yield chunk


@image_proxy_router.get("/image/{gdrive_file_path:path}")
async def get_image_proxy(
    gdrive_file_path: str,
    request: Request
):
    """
    ✅ [PUBLIC] ULTRA-FAST ASYNC Image Proxy dengan Round Robin + GROUP AWARE

    ✅ GROUP AWARE ROUTING:
    - Path tanpa '@' → Group 1 (gdrive..gdrive10)
    - Path dengan '@' prefix → Group 2 (gdrive11..gdrive20)
    - '@' prefix di-strip menggunakan settings.clean_path() sebelum dikirim ke rclone/daemon

    ✅ ROUND ROBIN LOAD BALANCING per group:
    - Request tersebar merata ke semua daemon dalam group yang aktif
    - Quota tidak menumpuk di 1 remote

    Priority 1: HTTPX true streaming via daemon sesuai group
    Priority 2: Fallback ke rclone cat via multi_remote_service sesuai group
    """
    request_id = getattr(request.state, "request_id", "unknown")

    try:
        # ✅ Validate path (boleh ada '@' prefix untuk group 2)
        validated_path = validate_file_path(gdrive_file_path)
        content_type = get_image_content_type(validated_path)

        # ✅ Determine group dari prefix
        is_group2 = settings.is_group2_path(validated_path)
        active_group = 2 if is_group2 else 1

        # ✅ Strip prefix '@' untuk actual file path ke rclone/daemon
        # PENTING: gunakan settings.clean_path(), BUKAN lstrip()
        # lstrip("@") strip setiap '@' karakter dari kiri,
        # sedangkan clean_path() strip string prefix "@" dengan benar.
        clean_path = settings.clean_path(validated_path)

        logger.info(
            "Image proxy request",
            extra={
                "request_id": request_id,
                "file_path": clean_path,
                "group": active_group,
                "client_ip": request.client.host if request.client else "unknown",
            },
        )

        # ==========================================
        # Priority 1: True streaming via HTTPX daemon (GROUP AWARE)
        # ==========================================
        try:
            multi_remote = get_multi_remote_service()

            # ✅ Get daemon URL sesuai group dari path prefix
            daemon_url, resolved_group = await _get_daemon_url_for_file(
                multi_remote,
                file_path=validated_path,  # pass validated_path agar group bisa dibaca
                strategy=settings.RCLONE_LOAD_BALANCING_STRATEGY,
            )

            if daemon_url:
                logger.debug(
                    f"Streaming via serve daemon G{resolved_group} Round Robin: {daemon_url}",
                    extra={"request_id": request_id},
                )

                return StreamingResponse(
                    # ✅ Kirim clean_path (tanpa '@') ke daemon
                    _stream_from_serve_daemon(daemon_url, clean_path),
                    media_type=content_type,
                    headers={
                        "Cache-Control": "public, max-age=604800, immutable",
                        "X-Cache-Status": "DIRECT-STREAM",
                        "X-Request-ID": request_id,
                        "X-Async": "true",
                        "X-Storage-Mode": f"serve-daemon-httpx-round-robin-stream-g{resolved_group}",
                        "X-Serve-Daemon": daemon_url,
                        "X-Storage-Group": str(resolved_group),
                    },
                )

        except FileNotFoundError:
            logger.warning(f"File not found via daemon: {clean_path}")
            raise HTTPException(status_code=404, detail="Image not found")
        except Exception as daemon_err:
            logger.warning(
                f"Serve daemon streaming failed ({daemon_err}), "
                f"falling back to rclone cat...",
                extra={"request_id": request_id},
            )

        # ==========================================
        # Priority 2: Fallback rclone cat (GROUP AWARE)
        # ==========================================
        logger.info(
            "Falling back to rclone cat download",
            extra={"request_id": request_id},
        )

        try:
            multi_remote = get_multi_remote_service()

            # ✅ download dengan group yang sesuai, path tanpa '@'
            file_content = await multi_remote.download_file_to_memory_async(
                clean_path,
                max_retries=2,
                strategy=settings.RCLONE_LOAD_BALANCING_STRATEGY,
                group=active_group,
            )
        except Exception as e:
            logger.error(f"Multi-remote download failed: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=502,
                detail="Failed to download image from storage",
            )

        if not file_content:
            logger.warning(f"Image not found: {clean_path}")
            raise HTTPException(status_code=404, detail="Image not found")

        logger.info(
            f"✅ Image downloaded (fallback rclone cat G{active_group}): "
            f"{len(file_content)} bytes",
            extra={"request_id": request_id},
        )

        return StreamingResponse(
            io.BytesIO(file_content),
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=604800, immutable",
                "X-Cache-Status": "DIRECT-FALLBACK",
                "X-Request-ID": request_id,
                "X-Async": "true",
                "X-Content-Length": str(len(file_content)),
                "X-Storage-Mode": f"rclone-cat-fallback-g{active_group}",
                "X-Storage-Group": str(active_group),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in image proxy",
            extra={"request_id": request_id, "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="An unexpected error occurred")


@image_proxy_router.get("/health")
async def health_check():
    """
    [PUBLIC] Health check endpoint untuk image proxy service.
    ✅ Enhanced dengan Round Robin daemon status + Group info.
    """
    try:
        multi_remote = get_multi_remote_service()
        health = multi_remote.get_health_status()

        rclone_status = "healthy" if health["available_remotes"] > 0 else "unhealthy"

        # Group 1 daemon via round robin
        g1_daemon_url = await multi_remote.get_next_daemon_url(group=1)
        g1_daemon_available = g1_daemon_url is not None
        g1_daemons_running = health.get("serve_daemons_running", 0)

        # Group 2 daemon (jika configured)
        g2_daemon_url = None
        g2_daemons_running = 0
        if settings.is_next_group_configured:
            try:
                g2_daemon_url = await multi_remote.get_next_daemon_url(group=2)
                g2_info = health.get("group2", {})
                g2_daemons_running = g2_info.get("serve_daemons_running", 0)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Multi-remote health check failed: {str(e)}")
        rclone_status = "unhealthy"
        health = {"error": str(e)}
        g1_daemon_available = False
        g1_daemons_running = 0
        g1_daemon_url = None
        g2_daemon_url = None
        g2_daemons_running = 0

    try:
        from app.services.rclone_service import get_executor_stats
        executor_stats = get_executor_stats()
        executor_status = (
            "healthy" if executor_stats.get("active_threads", 0) < 1000 else "warning"
        )
    except Exception as e:
        executor_stats = {"error": str(e)}
        executor_status = "unknown"

    overall_status = (
        "healthy"
        if rclone_status == "healthy" and executor_status == "healthy"
        else "degraded"
    )

    return {
        "status": overall_status,
        "components": {
            "rclone": rclone_status,
            "executor": executor_status,
            "multi_remote": health,
            "executor_stats": executor_stats,
        },
        "features": {
            "async_enabled": True,
            "unlimited_workers": True,
            "cache_mode": "disabled (direct download)",
            "browser_cache": "enabled (7 days)",
            "multi_remote_enabled": settings.is_multi_remote_enabled,
            "load_balancing_strategy": settings.RCLONE_LOAD_BALANCING_STRATEGY,
            # Group 1
            "serve_daemon_streaming_g1": g1_daemon_available,
            "serve_daemon_url_g1": g1_daemon_url,
            "serve_daemons_running_g1": g1_daemons_running,
            # Group 2
            "group2_configured": settings.is_next_group_configured,
            "group2_enabled": settings.is_group2_enabled,
            "serve_daemon_streaming_g2": g2_daemon_url is not None,
            "serve_daemon_url_g2": g2_daemon_url,
            "serve_daemons_running_g2": g2_daemons_running,
            # General
            "group2_path_prefix": settings.GROUP2_PATH_PREFIX,
            "round_robin_load_balancing": True,
            "httpx_singleton_client": True,
            "no_health_check_per_request": True,
            "group_aware_routing": True,
            "clean_path_via_settings": True,
        },
        "version": settings.VERSION,
    }


@image_proxy_router.get("/stats")
async def get_proxy_stats():
    """
    [PUBLIC] Proxy stats - NO CACHE MODE.
    ✅ Enhanced dengan Round Robin daemon info + Group info.
    """
    try:
        multi_remote = get_multi_remote_service()
        remote_stats = multi_remote.get_health_status()

        g1_daemon_url = await multi_remote.get_next_daemon_url(group=1)
        g1_daemons_running = remote_stats.get("serve_daemons_running", 0)
        total_remotes_g1 = remote_stats.get("total_remotes", 0)

        g2_daemon_url = None
        g2_daemons_running = 0
        total_remotes_g2 = 0
        if settings.is_next_group_configured:
            try:
                g2_daemon_url = await multi_remote.get_next_daemon_url(group=2)
                g2_info = remote_stats.get("group2", {})
                g2_daemons_running = g2_info.get("serve_daemons_running", 0)
                total_remotes_g2 = g2_info.get("total_remotes", 0)
            except Exception:
                pass

    except Exception as e:
        remote_stats = {"error": str(e)}
        g1_daemon_url = None
        g1_daemons_running = 0
        total_remotes_g1 = 0
        g2_daemon_url = None
        g2_daemons_running = 0
        total_remotes_g2 = 0

    try:
        from app.services.rclone_service import get_executor_stats
        executor_stats = get_executor_stats()
    except Exception as e:
        executor_stats = {"error": str(e)}

    any_daemon_available = g1_daemon_url is not None or g2_daemon_url is not None

    return {
        "remotes": remote_stats,
        "executor": executor_stats,
        "configuration": {
            "background_tasks_enabled": settings.BACKGROUND_TASK_ENABLED,
            "multi_remote_enabled": settings.is_multi_remote_enabled,
            "load_balancing_strategy": settings.RCLONE_LOAD_BALANCING_STRATEGY,
            "async_enabled": True,
            "unlimited_workers": True,
            "cache_mode": "disabled",
            "browser_cache": "enabled (7 days)",
            "group2_configured": settings.is_next_group_configured,
            "group2_enabled": settings.is_group2_enabled,
            "group2_path_prefix": settings.GROUP2_PATH_PREFIX,
            "auto_switch_group": settings.RCLONE_AUTO_SWITCH_GROUP,
        },
        "streaming": {
            "mode": (
                "httpx_serve_daemon_round_robin"
                if any_daemon_available
                else "rclone_cat_fallback"
            ),
            "group_aware_routing": True,
            "path_prefix_detection": f"prefix='{settings.GROUP2_PATH_PREFIX}' → group 2",
            "clean_path_method": "settings.clean_path() (NOT lstrip, strip string prefix correctly)",
            "group1": {
                "daemon_available": g1_daemon_url is not None,
                "daemon_current_url": g1_daemon_url,
                "daemons_running": g1_daemons_running,
                "total_remotes": total_remotes_g1,
            },
            "group2": {
                "configured": settings.is_next_group_configured,
                "daemon_available": g2_daemon_url is not None,
                "daemon_current_url": g2_daemon_url,
                "daemons_running": g2_daemons_running,
                "total_remotes": total_remotes_g2,
            },
            "httpx_chunk_size": "64KB",
            "true_streaming": any_daemon_available,
            "httpx_client": "singleton per daemon URL (connection pool reused)",
            "load_balancing": "round_robin across all running daemons per group",
        },
        "performance": {
            "async_mode": "enabled",
            "concurrent_capacity": "unlimited (auto-scale)",
            "blocking_behavior": "non-blocking",
            "queue_behavior": "no queue (instant dispatch)",
            "cache_strategy": "browser-only (server-side disabled)",
            "disk_usage": "0 bytes (no server cache)",
            "httpx_overhead": "~0ms (singleton client per daemon, connection reused)",
            "round_robin_overhead": "~0ms (in-memory counter, no HTTP ping)",
            "quota_distribution": (
                f"spread across {g1_daemons_running} G1 daemons + "
                f"{g2_daemons_running} G2 daemons"
            ),
            "avg_response_time": (
                "~1-3s streaming (daemon round robin), ~2-5s fallback (cat)"
            ),
        },
    }