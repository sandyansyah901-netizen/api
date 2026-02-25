"""
API Endpoints - Analytics & Admin Dashboard
============================================
Analytics untuk admin: overview, manga views, user growth, popular genres

✅ FIX #3: Import timezone dan ganti semua datetime.utcnow()

Endpoints (GET):
- /api/v1/admin/analytics/overview          - Dashboard overview
- /api/v1/admin/analytics/manga-views       - Manga views statistics
- /api/v1/admin/analytics/user-growth       - User registration trend
- /api/v1/admin/analytics/popular-genres    - Most popular genres
- /api/v1/admin/analytics/top-manga         - Top manga by metrics
- /api/v1/admin/analytics/recent-activity   - Recent user activity

Endpoints (DELETE / Pruning):
- /api/v1/admin/analytics/manga-views                       - Delete by period
- /api/v1/admin/analytics/manga-views/manga/{manga_id}      - Delete by manga
- /api/v1/admin/analytics/manga-views/all                   - Delete all (confirm=true)
- /api/v1/admin/analytics/chapter-views                     - Delete by period
- /api/v1/admin/analytics/chapter-views/chapter/{chapter_id} - Delete by chapter
- /api/v1/admin/analytics/chapter-views/all                 - Delete all (confirm=true)
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_
from typing import Optional
from datetime import datetime, timedelta, timezone  # ✅ FIX #3: Added timezone import
import logging

from app.core.base import get_db, require_role
from app.models.models import (
    User, Manga, Chapter, MangaView, ChapterView, Genre, 
    ReadingHistory, Bookmark, ReadingList
)
from app.schemas.schemas import (
    AnalyticsOverviewResponse, MangaViewsResponse, UserGrowthResponse
)

logger = logging.getLogger(__name__)

analytics_router = APIRouter()


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def get_date_range(period: str = "today"):
    """
    Get date range for filtering.
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    now = datetime.now(timezone.utc)  # ✅ FIX #3
    
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    elif period == "year":
        start = now - timedelta(days=365)
    else:
        start = now - timedelta(days=30)
    
    return start, now


# ==========================================
# ANALYTICS ENDPOINTS
# ==========================================

@analytics_router.get("/overview")
def get_analytics_overview(
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Get analytics dashboard overview.
    
    Returns:
    - Total users, active users
    - Total manga, chapters
    - Views statistics
    - Popular genres
    - User growth trend
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    # Total counts
    total_users = db.query(User).count()
    total_manga = db.query(Manga).count()
    total_chapters = db.query(Chapter).count()
    
    # Active users (logged in last 7 days)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)  # ✅ FIX #3
    active_users_week = db.query(User).filter(
        User.last_login >= week_ago
    ).count()
    
    # Today's active users
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)  # ✅ FIX #3
    active_users_today = db.query(User).filter(
        User.last_login >= today_start
    ).count()
    
    # Views statistics
    total_manga_views = db.query(MangaView).count()
    total_chapter_views = db.query(ChapterView).count()
    
    # Views today
    views_today = db.query(MangaView).filter(
        MangaView.viewed_at >= today_start
    ).count()
    
    # Views this week
    views_week = db.query(MangaView).filter(
        MangaView.viewed_at >= week_ago
    ).count()
    
    # Views this month
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)  # ✅ FIX #3
    views_month = db.query(MangaView).filter(
        MangaView.viewed_at >= month_ago
    ).count()
    
    # Popular genres (by manga count)
    popular_genres = db.query(
        Genre.name,
        Genre.slug,
        func.count(Manga.id).label('manga_count')
    ).join(
        Manga.genres
    ).group_by(
        Genre.id, Genre.name, Genre.slug
    ).order_by(
        desc('manga_count')
    ).limit(10).all()
    
    genres_list = [
        {"name": g.name, "slug": g.slug, "manga_count": g.manga_count}
        for g in popular_genres
    ]
    
    # User growth (last 30 days)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)  # ✅ FIX #3
    
    user_growth_data = db.query(
        func.date(User.created_at).label('date'),
        func.count(User.id).label('new_users')
    ).filter(
        User.created_at >= thirty_days_ago
    ).group_by(
        func.date(User.created_at)
    ).order_by('date').all()
    
    user_growth = {
        "labels": [str(g.date) for g in user_growth_data],
        "data": [g.new_users for g in user_growth_data]
    }
    
    # Manga status breakdown
    manga_ongoing = db.query(Manga).filter(Manga.status == "ongoing").count()
    manga_completed = db.query(Manga).filter(Manga.status == "completed").count()
    
    # Reading stats
    total_bookmarks = db.query(Bookmark).count()
    total_reading_lists = db.query(ReadingList).count()
    
    return {
        "database": {
            "total_users": total_users,
            "active_users_today": active_users_today,
            "active_users_week": active_users_week,
            "total_manga": total_manga,
            "manga_ongoing": manga_ongoing,
            "manga_completed": manga_completed,
            "total_chapters": total_chapters
        },
        "views": {
            "total_manga_views": total_manga_views,
            "total_chapter_views": total_chapter_views,
            "views_today": views_today,
            "views_week": views_week,
            "views_month": views_month
        },
        "engagement": {
            "total_bookmarks": total_bookmarks,
            "total_reading_lists": total_reading_lists
        },
        "popular_genres": genres_list,
        "user_growth": user_growth,
        "timestamp": datetime.now(timezone.utc).isoformat()  # ✅ FIX #3
    }


@analytics_router.get("/manga-views")
def get_manga_views(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    period: str = Query("month", description="today | week | month | year | all"),
    sort_by: str = Query("total_views", description="total_views | views_today | title"),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Get manga views statistics.
    
    Returns manga dengan view counts.
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    # Date ranges
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)  # ✅ FIX #3
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)  # ✅ FIX #3
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)  # ✅ FIX #3
    
    # Base query
    query = db.query(
        Manga.id,
        Manga.title,
        Manga.slug,
        func.count(MangaView.id).label('total_views'),
        func.count(func.distinct(MangaView.user_id)).label('unique_viewers')
    ).outerjoin(MangaView)
    
    # Filter by period for total_views
    if period == "today":
        query = query.filter(MangaView.viewed_at >= today_start)
    elif period == "week":
        query = query.filter(MangaView.viewed_at >= week_ago)
    elif period == "month":
        query = query.filter(MangaView.viewed_at >= month_ago)
    # "all" = no filter
    
    query = query.group_by(Manga.id, Manga.title, Manga.slug)
    
    # Sorting
    if sort_by == "title":
        query = query.order_by(Manga.title.asc())
    else:  # total_views
        query = query.order_by(desc('total_views'))
    
    total = query.count()
    results = query.offset((page - 1) * page_size).limit(page_size).all()
    
    items = []
    for result in results:
        # Get views breakdown
        views_today = db.query(MangaView).filter(
            and_(
                MangaView.manga_id == result.id,
                MangaView.viewed_at >= today_start
            )
        ).count()
        
        views_week = db.query(MangaView).filter(
            and_(
                MangaView.manga_id == result.id,
                MangaView.viewed_at >= week_ago
            )
        ).count()
        
        views_month = db.query(MangaView).filter(
            and_(
                MangaView.manga_id == result.id,
                MangaView.viewed_at >= month_ago
            )
        ).count()
        
        items.append({
            "manga_id": result.id,
            "manga_title": result.title,
            "manga_slug": result.slug,
            "total_views": result.total_views or 0,
            "views_today": views_today,
            "views_week": views_week,
            "views_month": views_month,
            "unique_viewers": result.unique_viewers or 0
        })
    
    return {
        "items": items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        },
        "period": period
    }


@analytics_router.get("/user-growth")
def get_user_growth(
    days: int = Query(30, ge=1, le=365, description="Number of days to show"),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Get user registration growth trend.
    
    Returns daily new user registrations.
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    start_date = datetime.now(timezone.utc) - timedelta(days=days)  # ✅ FIX #3
    
    growth_data = db.query(
        func.date(User.created_at).label('date'),
        func.count(User.id).label('new_users')
    ).filter(
        User.created_at >= start_date
    ).group_by(
        func.date(User.created_at)
    ).order_by('date').all()
    
    # Calculate cumulative
    cumulative = 0
    items = []
    
    for entry in growth_data:
        cumulative += entry.new_users
        items.append({
            "date": str(entry.date),
            "new_users": entry.new_users,
            "total_users": cumulative
        })
    
    return {
        "period_days": days,
        "total_new_users": sum(e.new_users for e in growth_data),
        "data": items
    }


@analytics_router.get("/popular-genres")
def get_popular_genres(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Get most popular genres by manga count and views.
    """
    # By manga count
    by_manga = db.query(
        Genre.id,
        Genre.name,
        Genre.slug,
        func.count(Manga.id).label('manga_count')
    ).join(
        Manga.genres
    ).group_by(
        Genre.id, Genre.name, Genre.slug
    ).order_by(
        desc('manga_count')
    ).limit(limit).all()
    
    items = []
    for genre in by_manga:
        # Get total views for this genre's manga
        total_views = db.query(
            func.count(MangaView.id)
        ).join(
            Manga, MangaView.manga_id == Manga.id
        ).join(
            Manga.genres
        ).filter(
            Genre.id == genre.id
        ).scalar() or 0
        
        # Get bookmarks count
        bookmarks = db.query(
            func.count(Bookmark.id)
        ).join(
            Manga, Bookmark.manga_id == Manga.id
        ).join(
            Manga.genres
        ).filter(
            Genre.id == genre.id
        ).scalar() or 0
        
        items.append({
            "id": genre.id,
            "name": genre.name,
            "slug": genre.slug,
            "manga_count": genre.manga_count,
            "total_views": total_views,
            "bookmarks": bookmarks
        })
    
    return {
        "genres": items,
        "total_genres": db.query(Genre).count()
    }


@analytics_router.get("/top-manga")
def get_top_manga(
    metric: str = Query("views", description="views | bookmarks | reading_lists"),
    period: str = Query("month", description="today | week | month | all"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Get top manga by various metrics.
    """
    start_date, _ = get_date_range(period)
    
    if metric == "views":
        # Top by views
        query = db.query(
            Manga.id,
            Manga.title,
            Manga.slug,
            func.count(MangaView.id).label('count')
        ).join(MangaView)
        
        if period != "all":
            query = query.filter(MangaView.viewed_at >= start_date)
        
        results = query.group_by(
            Manga.id, Manga.title, Manga.slug
        ).order_by(desc('count')).limit(limit).all()
        
        metric_name = "views"
        
    elif metric == "bookmarks":
        # Top by bookmarks
        results = db.query(
            Manga.id,
            Manga.title,
            Manga.slug,
            func.count(Bookmark.id).label('count')
        ).join(Bookmark).group_by(
            Manga.id, Manga.title, Manga.slug
        ).order_by(desc('count')).limit(limit).all()
        
        metric_name = "bookmarks"
        
    else:  # reading_lists
        # Top by reading lists
        results = db.query(
            Manga.id,
            Manga.title,
            Manga.slug,
            func.count(ReadingList.id).label('count')
        ).join(ReadingList).group_by(
            Manga.id, Manga.title, Manga.slug
        ).order_by(desc('count')).limit(limit).all()
        
        metric_name = "in_reading_lists"
    
    items = [
        {
            "rank": idx + 1,
            "manga_id": r.id,
            "manga_title": r.title,
            "manga_slug": r.slug,
            metric_name: r.count
        }
        for idx, r in enumerate(results)
    ]
    
    return {
        "metric": metric,
        "period": period,
        "items": items
    }


@analytics_router.get("/recent-activity")
def get_recent_activity(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Get recent user activity (views, bookmarks, lists).
    """
    # Recent views
    recent_views = db.query(
        MangaView.viewed_at,
        User.username,
        Manga.title.label('manga_title')
    ).join(
        User, MangaView.user_id == User.id, isouter=True
    ).join(
        Manga, MangaView.manga_id == Manga.id
    ).order_by(
        desc(MangaView.viewed_at)
    ).limit(limit).all()
    
    views_list = [
        {
            "type": "view",
            "username": v.username or "Anonymous",
            "manga_title": v.manga_title,
            "timestamp": v.viewed_at
        }
        for v in recent_views
    ]
    
    # Recent bookmarks
    recent_bookmarks = db.query(
        Bookmark.created_at,
        User.username,
        Manga.title.label('manga_title')
    ).join(User).join(Manga).order_by(
        desc(Bookmark.created_at)
    ).limit(20).all()
    
    bookmarks_list = [
        {
            "type": "bookmark",
            "username": b.username,
            "manga_title": b.manga_title,
            "timestamp": b.created_at
        }
        for b in recent_bookmarks
    ]
    
    # Combine and sort
    all_activity = views_list + bookmarks_list
    all_activity.sort(key=lambda x: x['timestamp'], reverse=True)
    
    return {
        "recent_activity": all_activity[:limit]
    }


# ==========================================
# VIEWS CLEANUP / PRUNING ENDPOINTS
# ==========================================

@analytics_router.delete("/manga-views")
def delete_manga_views_by_period(
    older_than_days: int = Query(30, ge=1, le=3650, description="Hapus views lebih tua dari N hari"),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Hapus manga views yang lebih tua dari N hari.

    Berguna untuk pruning/cleanup tabel manga_views agar tidak terlalu besar.
    Default: hapus views yang lebih tua dari 30 hari.

    Contoh:
        DELETE /api/v1/admin/analytics/manga-views?older_than_days=90
        → Hapus semua manga views yang berumur > 90 hari
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    deleted_count = db.query(MangaView).filter(
        MangaView.viewed_at < cutoff_date
    ).delete(synchronize_session=False)

    db.commit()

    logger.info(
        f"Admin {current_user.username} deleted {deleted_count} manga views "
        f"older than {older_than_days} days (cutoff: {cutoff_date.date()})"
    )

    return {
        "success": True,
        "deleted_count": deleted_count,
        "message": f"Deleted {deleted_count} manga views older than {older_than_days} days",
        "cutoff_date": cutoff_date.isoformat()
    }


@analytics_router.delete("/manga-views/manga/{manga_id}")
def delete_manga_views_by_manga(
    manga_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Hapus semua views untuk satu manga tertentu.

    Berguna untuk reset view count suatu manga (misalnya setelah migrasi data
    atau saat ada view spam pada manga tertentu).

    Args:
        manga_id: ID manga yang views-nya ingin dihapus
    """
    # Validasi manga ada
    manga = db.query(Manga).filter(Manga.id == manga_id).first()
    if not manga:
        raise HTTPException(status_code=404, detail=f"Manga ID {manga_id} tidak ditemukan")

    deleted_count = db.query(MangaView).filter(
        MangaView.manga_id == manga_id
    ).delete(synchronize_session=False)

    db.commit()

    logger.info(
        f"Admin {current_user.username} deleted {deleted_count} views "
        f"for manga '{manga.title}' (ID: {manga_id})"
    )

    return {
        "success": True,
        "deleted_count": deleted_count,
        "manga_id": manga_id,
        "manga_title": manga.title,
        "manga_slug": manga.slug,
        "message": f"Deleted {deleted_count} views for manga '{manga.title}'"
    }


@analytics_router.delete("/manga-views/all")
def delete_all_manga_views(
    confirm: bool = Query(False, description="Harus True untuk konfirmasi hapus semua data"),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Hapus SEMUA data manga views.

    ⚠️ BERBAHAYA: Aksi ini tidak bisa dibatalkan!
    Wajib sertakan query param `confirm=true` sebagai konfirmasi.

    Contoh:
        DELETE /api/v1/admin/analytics/manga-views/all?confirm=true
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Tambahkan query param '?confirm=true' untuk konfirmasi. "
                   "Aksi ini akan menghapus SEMUA data manga views dan tidak bisa dibatalkan."
        )

    deleted_count = db.query(MangaView).delete(synchronize_session=False)
    db.commit()

    logger.warning(
        f"Admin {current_user.username} DELETED ALL manga views: {deleted_count} rows removed"
    )

    return {
        "success": True,
        "deleted_count": deleted_count,
        "message": f"Deleted ALL {deleted_count} manga views from database"
    }


@analytics_router.delete("/chapter-views")
def delete_chapter_views_by_period(
    older_than_days: int = Query(30, ge=1, le=3650, description="Hapus views lebih tua dari N hari"),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Hapus chapter views yang lebih tua dari N hari.

    Berguna untuk pruning/cleanup tabel chapter_views agar tidak terlalu besar.
    Default: hapus views yang lebih tua dari 30 hari.

    Contoh:
        DELETE /api/v1/admin/analytics/chapter-views?older_than_days=90
        → Hapus semua chapter views yang berumur > 90 hari
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    deleted_count = db.query(ChapterView).filter(
        ChapterView.viewed_at < cutoff_date
    ).delete(synchronize_session=False)

    db.commit()

    logger.info(
        f"Admin {current_user.username} deleted {deleted_count} chapter views "
        f"older than {older_than_days} days (cutoff: {cutoff_date.date()})"
    )

    return {
        "success": True,
        "deleted_count": deleted_count,
        "message": f"Deleted {deleted_count} chapter views older than {older_than_days} days",
        "cutoff_date": cutoff_date.isoformat()
    }


@analytics_router.delete("/chapter-views/chapter/{chapter_id}")
def delete_chapter_views_by_chapter(
    chapter_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Hapus semua views untuk satu chapter tertentu.

    Berguna untuk reset view count suatu chapter.

    Args:
        chapter_id: ID chapter yang views-nya ingin dihapus
    """
    # Validasi chapter ada
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail=f"Chapter ID {chapter_id} tidak ditemukan")

    deleted_count = db.query(ChapterView).filter(
        ChapterView.chapter_id == chapter_id
    ).delete(synchronize_session=False)

    db.commit()

    logger.info(
        f"Admin {current_user.username} deleted {deleted_count} views "
        f"for chapter '{chapter.chapter_label}' (ID: {chapter_id})"
    )

    return {
        "success": True,
        "deleted_count": deleted_count,
        "chapter_id": chapter_id,
        "chapter_label": chapter.chapter_label,
        "chapter_slug": chapter.slug,
        "manga_id": chapter.manga_id,
        "message": f"Deleted {deleted_count} views for chapter '{chapter.chapter_label}'"
    }


@analytics_router.delete("/chapter-views/all")
def delete_all_chapter_views(
    confirm: bool = Query(False, description="Harus True untuk konfirmasi hapus semua data"),
    db: Session = Depends(get_db),
    current_user = Depends(require_role("admin"))
):
    """
    [ADMIN] Hapus SEMUA data chapter views.

    ⚠️ BERBAHAYA: Aksi ini tidak bisa dibatalkan!
    Wajib sertakan query param `confirm=true` sebagai konfirmasi.

    Contoh:
        DELETE /api/v1/admin/analytics/chapter-views/all?confirm=true
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Tambahkan query param '?confirm=true' untuk konfirmasi. "
                   "Aksi ini akan menghapus SEMUA data chapter views dan tidak bisa dibatalkan."
        )

    deleted_count = db.query(ChapterView).delete(synchronize_session=False)
    db.commit()

    logger.warning(
        f"Admin {current_user.username} DELETED ALL chapter views: {deleted_count} rows removed"
    )

    return {
        "success": True,
        "deleted_count": deleted_count,
        "message": f"Deleted ALL {deleted_count} chapter views from database"
    }