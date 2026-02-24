"""
API Endpoints - Reading Features
=================================
Reading History, Bookmarks, Reading Lists

FIXED:
✅ Tambah helper get_cover_url()
✅ Fix manga.cover_image_url → manga.cover_image_path
✅ Konsisten dengan model database
✅ FIX #3: Import timezone dan ganti semua datetime.utcnow()

Endpoints:
- Reading History: Save progress, get last read, get history
- Bookmarks: Add/remove favorites
- Reading Lists: Manage custom lists (Plan to Read, Reading, Completed, etc)
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_
from typing import Optional
from datetime import datetime, timezone  # ✅ FIX #3: Added timezone import
import logging

from app.core.base import get_db, get_current_user
from app.models.models import (
    User, Manga, Chapter, ReadingHistory, Bookmark, ReadingList, MangaView
)
from app.schemas.schemas import (
    SaveProgressRequest, ReadingHistoryResponse, LastReadResponse,
    BookmarkResponse, ReadingListRequest, ReadingListResponse
)

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
# READING HISTORY ROUTER
# ==========================================

reading_router = APIRouter()


@reading_router.post("/save", status_code=status.HTTP_200_OK)
def save_reading_progress(
    progress: SaveProgressRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Save reading progress.
    
    Auto-create atau update entry untuk user + manga + chapter.
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    try:
        # Get manga
        manga = db.query(Manga).filter(Manga.slug == progress.manga_slug).first()
        if not manga:
            raise HTTPException(status_code=404, detail=f"Manga '{progress.manga_slug}' tidak ditemukan")
        
        # Get chapter
        chapter = db.query(Chapter).filter(Chapter.slug == progress.chapter_slug).first()
        if not chapter:
            raise HTTPException(status_code=404, detail=f"Chapter '{progress.chapter_slug}' tidak ditemukan")
        
        # Verify chapter belongs to manga
        if chapter.manga_id != manga.id:
            raise HTTPException(status_code=400, detail="Chapter tidak termasuk manga ini")
        
        # Check existing history
        history = db.query(ReadingHistory).filter(
            and_(
                ReadingHistory.user_id == current_user.id,
                ReadingHistory.manga_id == manga.id,
                ReadingHistory.chapter_id == chapter.id
            )
        ).first()
        
        if history:
            # Update existing
            history.page_number = progress.page_number
            history.last_read_at = datetime.now(timezone.utc)  # ✅ FIX #3
        else:
            # Create new
            history = ReadingHistory(
                user_id=current_user.id,
                manga_id=manga.id,
                chapter_id=chapter.id,
                page_number=progress.page_number,
                last_read_at=datetime.now(timezone.utc)  # ✅ FIX #3
            )
            db.add(history)
        
        db.commit()
        
        logger.info(
            f"User {current_user.username} saved progress: {manga.title} - {chapter.chapter_label}, page {progress.page_number}"
        )
        
        return {
            "success": True,
            "message": "Progress saved",
            "manga_slug": manga.slug,
            "chapter_slug": chapter.slug,
            "page_number": progress.page_number
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to save progress: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save progress: {str(e)}")


@reading_router.get("/manga/{manga_slug}/last-read", response_model=LastReadResponse)
def get_last_read_chapter(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Get last read chapter untuk manga tertentu.
    
    Returns last chapter yang dibaca user + next chapter suggestion.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    # Get latest history untuk manga ini
    history = db.query(ReadingHistory).filter(
        and_(
            ReadingHistory.user_id == current_user.id,
            ReadingHistory.manga_id == manga.id
        )
    ).order_by(desc(ReadingHistory.last_read_at)).first()
    
    if not history:
        raise HTTPException(
            status_code=404,
            detail=f"No reading history found for manga '{manga_slug}'"
        )
    
    chapter = history.chapter
    total_pages = len(chapter.pages)
    
    # Find next chapter
    next_chapter = db.query(Chapter).filter(
        and_(
            Chapter.manga_id == manga.id,
            Chapter.chapter_main > chapter.chapter_main
        )
    ).order_by(Chapter.chapter_main.asc(), Chapter.chapter_sub.asc()).first()
    
    # If no next by chapter_main, try chapter_sub
    if not next_chapter:
        next_chapter = db.query(Chapter).filter(
            and_(
                Chapter.manga_id == manga.id,
                Chapter.chapter_main == chapter.chapter_main,
                Chapter.chapter_sub > chapter.chapter_sub
            )
        ).order_by(Chapter.chapter_sub.asc()).first()
    
    return {
        "manga_slug": manga.slug,
        "chapter_id": chapter.id,
        "chapter_slug": chapter.slug,
        "chapter_label": chapter.chapter_label,
        "page_number": history.page_number,
        "total_pages": total_pages,
        "last_read_at": history.last_read_at,
        "next_chapter": {
            "id": next_chapter.id,
            "chapter_label": next_chapter.chapter_label,
            "slug": next_chapter.slug,
            "chapter_folder_name": next_chapter.chapter_folder_name,
            "volume_number": next_chapter.volume_number,
            "chapter_type": next_chapter.chapter_type.value if hasattr(next_chapter.chapter_type, 'value') else next_chapter.chapter_type,
            "preview_url": next_chapter.preview_url,
            "created_at": next_chapter.created_at
        } if next_chapter else None
    }


@reading_router.get("/history", response_model=dict)
def get_reading_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Get user's complete reading history.
    
    Returns list of manga yang pernah dibaca, ordered by last_read_at.
    """
    # Query dengan distinct manga_id, ambil yang terakhir dibaca
    subquery = db.query(
        ReadingHistory.manga_id,
        func.max(ReadingHistory.last_read_at).label('max_read')
    ).filter(
        ReadingHistory.user_id == current_user.id
    ).group_by(ReadingHistory.manga_id).subquery()
    
    query = db.query(ReadingHistory).join(
        subquery,
        and_(
            ReadingHistory.manga_id == subquery.c.manga_id,
            ReadingHistory.last_read_at == subquery.c.max_read
        )
    ).order_by(desc(ReadingHistory.last_read_at))
    
    total = query.count()
    histories = query.offset((page - 1) * page_size).limit(page_size).all()
    
    items = []
    for history in histories:
        manga = history.manga
        chapter = history.chapter
        total_pages = len(chapter.pages)
        
        items.append({
            "manga_id": manga.id,
            "manga_title": manga.title,
            "manga_slug": manga.slug,
            "manga_cover": get_cover_url(manga.cover_image_path),  # ✅ FIXED
            "chapter_id": chapter.id,
            "chapter_label": chapter.chapter_label,
            "chapter_slug": chapter.slug,
            "page_number": history.page_number,
            "total_pages": total_pages,
            "last_read_at": history.last_read_at
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


@reading_router.delete("/history/manga/{manga_slug}")
def delete_reading_history(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Delete reading history untuk manga tertentu.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    deleted = db.query(ReadingHistory).filter(
        and_(
            ReadingHistory.user_id == current_user.id,
            ReadingHistory.manga_id == manga.id
        )
    ).delete()
    
    db.commit()
    
    return {
        "success": True,
        "message": f"Deleted {deleted} history entries",
        "manga_slug": manga_slug
    }


# ==========================================
# BOOKMARKS ROUTER
# ==========================================

bookmarks_router = APIRouter()


@bookmarks_router.post("/manga/{manga_slug}", status_code=status.HTTP_201_CREATED)
def add_bookmark(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Add manga to bookmarks (favorites).
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    # Check if already bookmarked
    existing = db.query(Bookmark).filter(
        and_(
            Bookmark.user_id == current_user.id,
            Bookmark.manga_id == manga.id
        )
    ).first()
    
    if existing:
        return {
            "success": True,
            "message": "Already bookmarked",
            "manga_slug": manga_slug,
            "created_at": existing.created_at
        }
    
    bookmark = Bookmark(
        user_id=current_user.id,
        manga_id=manga.id,
        created_at=datetime.now(timezone.utc)  # ✅ FIX #3
    )
    
    db.add(bookmark)
    db.commit()
    db.refresh(bookmark)
    
    logger.info(f"User {current_user.username} bookmarked manga: {manga.title}")
    
    return {
        "success": True,
        "message": "Bookmark added",
        "manga_slug": manga_slug,
        "created_at": bookmark.created_at
    }


@bookmarks_router.delete("/manga/{manga_slug}")
def remove_bookmark(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Remove manga from bookmarks.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    deleted = db.query(Bookmark).filter(
        and_(
            Bookmark.user_id == current_user.id,
            Bookmark.manga_id == manga.id
        )
    ).delete()
    
    db.commit()
    
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    
    logger.info(f"User {current_user.username} removed bookmark: {manga.title}")
    
    return {
        "success": True,
        "message": "Bookmark removed",
        "manga_slug": manga_slug
    }


@bookmarks_router.get("/", response_model=dict)
def get_bookmarks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("created_at", description="created_at | title | updated_at"),
    sort_order: str = Query("desc", description="asc | desc"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Get user's bookmarked manga.
    """
    query = db.query(Bookmark).filter(Bookmark.user_id == current_user.id)
    
    # Sorting
    if sort_by == "title":
        query = query.join(Manga).order_by(
            Manga.title.asc() if sort_order == "asc" else Manga.title.desc()
        )
    elif sort_by == "updated_at":
        query = query.join(Manga).order_by(
            Manga.updated_at.asc() if sort_order == "asc" else Manga.updated_at.desc()
        )
    else:  # created_at
        query = query.order_by(
            Bookmark.created_at.asc() if sort_order == "asc" else Bookmark.created_at.desc()
        )
    
    total = query.count()
    bookmarks = query.offset((page - 1) * page_size).limit(page_size).all()
    
    items = []
    for bookmark in bookmarks:
        manga = bookmark.manga
        total_chapters = len(manga.chapters)
        
        latest_chapter = None
        if manga.chapters:
            latest = sorted(
                manga.chapters,
                key=lambda ch: (ch.chapter_main, ch.chapter_sub),
                reverse=True
            )[0]
            latest_chapter = latest.chapter_label
        
        items.append({
            "manga_id": manga.id,
            "manga_title": manga.title,
            "manga_slug": manga.slug,
            "manga_cover": get_cover_url(manga.cover_image_path),  # ✅ FIXED
            "total_chapters": total_chapters,
            "latest_chapter": latest_chapter,
            "created_at": bookmark.created_at
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


@bookmarks_router.get("/check/{manga_slug}")
def check_bookmark(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Check if manga is bookmarked by user.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    bookmark = db.query(Bookmark).filter(
        and_(
            Bookmark.user_id == current_user.id,
            Bookmark.manga_id == manga.id
        )
    ).first()
    
    return {
        "manga_slug": manga_slug,
        "is_bookmarked": bookmark is not None,
        "created_at": bookmark.created_at if bookmark else None
    }


# ==========================================
# READING LISTS ROUTER
# ==========================================

lists_router = APIRouter()


@lists_router.post("/", status_code=status.HTTP_201_CREATED)
def add_to_reading_list(
    data: ReadingListRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Add manga to reading list atau update status.
    
    Status: plan_to_read | reading | completed | dropped | on_hold
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    manga = db.query(Manga).filter(Manga.slug == data.manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{data.manga_slug}' tidak ditemukan")
    
    # Check existing
    existing = db.query(ReadingList).filter(
        and_(
            ReadingList.user_id == current_user.id,
            ReadingList.manga_id == manga.id
        )
    ).first()
    
    if existing:
        # Update
        existing.status = data.status
        existing.rating = data.rating
        existing.notes = data.notes
        existing.updated_at = datetime.now(timezone.utc)  # ✅ FIX #3
        
        db.commit()
        db.refresh(existing)
        
        return {
            "success": True,
            "message": "Reading list updated",
            "manga_slug": data.manga_slug,
            "status": data.status,
            "rating": data.rating
        }
    else:
        # Create new
        reading_list = ReadingList(
            user_id=current_user.id,
            manga_id=manga.id,
            status=data.status,
            rating=data.rating,
            notes=data.notes,
            added_at=datetime.now(timezone.utc),  # ✅ FIX #3
            updated_at=datetime.now(timezone.utc)  # ✅ FIX #3
        )
        
        db.add(reading_list)
        db.commit()
        db.refresh(reading_list)
        
        logger.info(f"User {current_user.username} added to list: {manga.title} ({data.status})")
        
        return {
            "success": True,
            "message": "Added to reading list",
            "manga_slug": data.manga_slug,
            "status": data.status,
            "rating": data.rating
        }


@lists_router.delete("/manga/{manga_slug}")
def remove_from_reading_list(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Remove manga from reading list.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    deleted = db.query(ReadingList).filter(
        and_(
            ReadingList.user_id == current_user.id,
            ReadingList.manga_id == manga.id
        )
    ).delete()
    
    db.commit()
    
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Entry not found in reading list")
    
    return {
        "success": True,
        "message": "Removed from reading list",
        "manga_slug": manga_slug
    }


@lists_router.get("/", response_model=dict)
def get_reading_lists(
    status: Optional[str] = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("updated_at", description="updated_at | added_at | title | rating"),
    sort_order: str = Query("desc", description="asc | desc"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Get user's reading lists.
    
    Optional filter by status.
    """
    query = db.query(ReadingList).filter(ReadingList.user_id == current_user.id)
    
    # Filter by status
    if status:
        query = query.filter(ReadingList.status == status)
    
    # Sorting
    if sort_by == "title":
        query = query.join(Manga).order_by(
            Manga.title.asc() if sort_order == "asc" else Manga.title.desc()
        )
    elif sort_by == "rating":
        query = query.order_by(
            ReadingList.rating.asc() if sort_order == "asc" else ReadingList.rating.desc()
        )
    elif sort_by == "added_at":
        query = query.order_by(
            ReadingList.added_at.asc() if sort_order == "asc" else ReadingList.added_at.desc()
        )
    else:  # updated_at
        query = query.order_by(
            ReadingList.updated_at.asc() if sort_order == "asc" else ReadingList.updated_at.desc()
        )
    
    total = query.count()
    lists = query.offset((page - 1) * page_size).limit(page_size).all()
    
    items = []
    for entry in lists:
        manga = entry.manga
        total_chapters = len(manga.chapters)
        
        # Count read chapters
        read_chapters = db.query(ReadingHistory).filter(
            and_(
                ReadingHistory.user_id == current_user.id,
                ReadingHistory.manga_id == manga.id
            )
        ).count()
        
        items.append({
            "manga_id": manga.id,
            "manga_title": manga.title,
            "manga_slug": manga.slug,
            "manga_cover": get_cover_url(manga.cover_image_path),  # ✅ FIXED
            "status": entry.status.value if hasattr(entry.status, 'value') else entry.status,
            "rating": entry.rating,
            "notes": entry.notes,
            "total_chapters": total_chapters,
            "read_chapters": read_chapters,
            "added_at": entry.added_at,
            "updated_at": entry.updated_at
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


@lists_router.get("/status/{manga_slug}")
def get_manga_list_status(
    manga_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Get reading list status for specific manga.
    """
    manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga '{manga_slug}' tidak ditemukan")
    
    entry = db.query(ReadingList).filter(
        and_(
            ReadingList.user_id == current_user.id,
            ReadingList.manga_id == manga.id
        )
    ).first()
    
    if not entry:
        return {
            "manga_slug": manga_slug,
            "in_list": False,
            "status": None,
            "rating": None,
            "notes": None
        }
    
    return {
        "manga_slug": manga_slug,
        "in_list": True,
        "status": entry.status.value if hasattr(entry.status, 'value') else entry.status,
        "rating": entry.rating,
        "notes": entry.notes,
        "added_at": entry.added_at,
        "updated_at": entry.updated_at
    }


@lists_router.get("/stats")
def get_reading_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    [AUTH] Get user's reading statistics.
    """
    # Count by status
    stats_by_status = db.query(
        ReadingList.status,
        func.count(ReadingList.id).label('count')
    ).filter(
        ReadingList.user_id == current_user.id
    ).group_by(ReadingList.status).all()
    
    status_counts = {
        "plan_to_read": 0,
        "reading": 0,
        "completed": 0,
        "dropped": 0,
        "on_hold": 0
    }
    
    for stat in stats_by_status:
        status_val = stat.status.value if hasattr(stat.status, 'value') else stat.status
        status_counts[status_val] = stat.count
    
    # Total bookmarks
    total_bookmarks = db.query(Bookmark).filter(
        Bookmark.user_id == current_user.id
    ).count()
    
    # Total reading history
    total_history = db.query(
        func.count(func.distinct(ReadingHistory.manga_id))
    ).filter(
        ReadingHistory.user_id == current_user.id
    ).scalar()
    
    return {
        "reading_list": status_counts,
        "total_in_list": sum(status_counts.values()),
        "total_bookmarks": total_bookmarks,
        "total_history": total_history
    }