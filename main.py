"""
Main Application Entry Point - WITH GLOBAL SINGLETON INIT + SERVE DAEMONS
==========================================================================
FastAPI application dengan semua fitur termasuk Reading Features & Analytics.

REVISI BESAR:
✅ ✨ ADDED: Serve daemon startup & shutdown (NEW!)
✅ ✨ ADDED: Health check logging untuk daemons
✅ ✨ ADDED: Graceful shutdown untuk cleanup daemons
✅ ✨ ADDED: Global MultiRemoteService singleton init at startup
✅ ✨ ADDED: Startup verification untuk multi-remote
✅ ✨ ADDED: Health check untuk multi-remote status

Previous fixes still active:
✅ Mount static files untuk serve covers
✅ Tambah endpoint /static/covers untuk public access
✅ Database connection verification dengan retry mechanism
✅ Auto table creation untuk development mode
✅ Upload router import dari file terpisah
✅ FIX #2: IP address di-hash sebelum disimpan (privacy)
✅ FIX #3: Database session leak di middleware (pakai try/finally)
✅ FIX #7: Hapus create_all() di module level (hanya di lifespan dev mode)
✅ FIX #12: CacheControlMiddleware UPDATED - Aggressive browser caching (7 days)

REVISI TERBARU:
✅ FIX DOUBLE DAEMON: MultiRemoteService._start_serve_daemons() skip jika
   daemon sudah running di RcloneService (cegah port conflict)
✅ FIX LIFESPAN SHUTDOWN: Tambah sync daemon status dari RcloneService ke
   MultiRemoteService.remote_status saat startup
✅ REMOVED: scheduler import dan pemanggilan (background_tasks & scheduler dihapus)

✅ ✨ NEW: _sync_daemon_status_to_multi_remote() sync GROUP 2 daemon status juga

✅ FIX COVER STATIC: Pastikan covers directory dibuat sebelum mount StaticFiles,
   dan mount dilakukan di dalam lifespan agar path selalu valid saat app ready.
   Juga tambah endpoint fallback GET /covers/{filename} untuk akses langsung.
"""

from fastapi import FastAPI, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError, OperationalError
from sqlalchemy import text
import logging
import time
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

from app.core.base import (
    settings, engine, get_db,
    RequestIDMiddleware, SecurityHeadersMiddleware,
    setup_logging
)
from app.models.models import Base

# ==========================================
# ✅ FIX #13: Import models di top level (bukan lazy import di middleware)
# ==========================================
from app.models.models import Manga, MangaView, Chapter, ChapterView, User
from app.core.base import SessionLocal, decode_access_token

# ==========================================
# ✅ IMPORT ROUTERS - FIXED UPLOAD IMPORT
# ==========================================
from app.api.v1.endpoints import (
    auth_router, manga_router, chapter_router
)
from app.api.v1.admin_endpoints import (
    admin_router, image_proxy_router
)

# ✅ UPLOAD ROUTER DARI FILE TERPISAH
from app.api.v1.upload_endpoints import upload_router

from app.api.v1.reading_endpoints import (
    reading_router, bookmarks_router, lists_router
)
from app.api.v1.analytics_endpoints import (
    analytics_router
)

# ✅ ✨ IMPORT MULTI-REMOTE SERVICE
from app.services.multi_remote_service import MultiRemoteService

# ✅ ✨ IMPORT RCLONE SERVICE (untuk sync daemon status)
from app.services.rclone_service import RcloneService

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)

# ==========================================
# ✅ ✨ GLOBAL SINGLETON INSTANCE
# ==========================================
multi_remote_service: MultiRemoteService = None


# ==========================================
# ✅ FIX #2: Helper untuk hash IP address (privacy)
# ==========================================
def hash_ip_address(ip: str) -> str:
    """
    Hash IP address sebelum disimpan ke database.
    """
    if not ip:
        return None
    salted = f"{ip}:{settings.SECRET_KEY}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()[:32]


# ==========================================
# ✅ FIX #3: Helper untuk extract user_id dari auth header (DRY)
# ==========================================
def extract_user_id_from_request(request: Request, db: Session) -> int:
    """Extract user_id dari Authorization header jika ada."""
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    try:
        token = auth_header.split(" ")[1]
        payload = decode_access_token(token)
        if not payload:
            return None

        username = payload.get("sub")
        if not username:
            return None

        user = db.query(User).filter(User.username == username).first()
        if user:
            return user.id
    except Exception:
        pass

    return None


# ==========================================
# ✅ FIX DOUBLE DAEMON: Helper untuk sync daemon status
# ==========================================
def _sync_daemon_status_to_multi_remote(service: MultiRemoteService):
    """
    Sync status daemon yang sudah running di RcloneService
    ke MultiRemoteService.remote_status.
    """
    if not settings.RCLONE_SERVE_HTTP_ENABLED:
        return

    running_daemons = RcloneService.get_all_serve_daemon_status()

    groups_to_sync = [1]
    if settings.is_next_group_configured:
        groups_to_sync.append(2)

    for grp in groups_to_sync:
        g = service._groups.get(grp, {})
        g_status = g.get("status", {})

        for remote_name, daemon_status in running_daemons.items():
            if remote_name not in g_status:
                continue

            if daemon_status.get("running"):
                rs = g_status[remote_name]
                rs.serve_daemon_running = True
                rs.serve_daemon_port = daemon_status.get("port")
                rs.serve_daemon_url = daemon_status.get("url")

                with RcloneService._serve_lock:
                    if remote_name in RcloneService._serve_daemons:
                        rs.serve_daemon_process = RcloneService._serve_daemons[remote_name].get("process")

                logger.info(
                    f"  🔄 Synced daemon status for '{remote_name}' (Group {grp}): "
                    f"running at {daemon_status.get('url')}"
                )

    for remote_name, daemon_status in running_daemons.items():
        if remote_name not in service.remote_status:
            continue

        if daemon_status.get("running"):
            status_obj = service.remote_status[remote_name]
            status_obj.serve_daemon_running = True
            status_obj.serve_daemon_port = daemon_status.get("port")
            status_obj.serve_daemon_url = daemon_status.get("url")

            with RcloneService._serve_lock:
                if remote_name in RcloneService._serve_daemons:
                    status_obj.serve_daemon_process = RcloneService._serve_daemons[remote_name].get("process")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    # ==========================================
    # STARTUP
    # ==========================================
    logger.info("🚀 Starting Manga Reader API", extra={
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT
    })

    # Test database connection dengan retry mechanism
    max_retries = 3
    retry_delay = 2

    for attempt in range(max_retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("✅ Database connection verified")
            break
        except OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"⚠️ Database connection attempt {attempt + 1} failed, "
                    f"retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.error(
                    f"❌ Database connection failed after {max_retries} attempts: {str(e)}"
                )
                raise RuntimeError(
                    "Failed to connect to database. Please check DATABASE_URL in .env file."
                )
        except Exception as e:
            logger.error(f"❌ Unexpected database error: {str(e)}")
            raise

    # ✅ FIX #7: Auto-create tables HANYA di development mode
    if settings.ENVIRONMENT == "development" or settings.DEBUG:
        try:
            logger.info("🔧 Creating/verifying database tables (development mode)...")
            Base.metadata.create_all(bind=engine)
            logger.info("✅ Database tables ready")
        except Exception as e:
            logger.warning(f"⚠️ Table creation warning: {str(e)}")

    # ==========================================
    # ✅ ✨ INIT GLOBAL MULTI-REMOTE SERVICE
    # ==========================================
    global multi_remote_service

    try:
        logger.info("🚀 Initializing global MultiRemoteService...")
        multi_remote_service = MultiRemoteService.get_global_instance()

        if settings.RCLONE_SERVE_HTTP_ENABLED:
            logger.info("🔄 Syncing serve daemon status from RcloneService (all groups)...")
            _sync_daemon_status_to_multi_remote(multi_remote_service)

        health = multi_remote_service.get_health_status()
        logger.info(
            f"✅ MultiRemoteService ready: "
            f"{health['available_remotes']}/{health['total_remotes']} remotes available (Group 1)"
        )

        if settings.is_next_group_configured:
            g2_info = health.get("group2", {})
            logger.info(
                f"✅ Group 2 status: "
                f"{g2_info.get('available_remotes', 0)}/{g2_info.get('total_remotes', 0)} "
                f"remotes available"
            )
            logger.info(
                f"  📁 Group 2 path prefix: '{settings.GROUP2_PATH_PREFIX}'"
            )
            if settings.RCLONE_AUTO_SWITCH_GROUP:
                logger.info(
                    f"  🔄 Auto-switch enabled (threshold: {settings.RCLONE_GROUP1_QUOTA_GB} GB)"
                )
            else:
                logger.info("  ℹ️ Auto-switch disabled (manual switch)")
        else:
            logger.info("ℹ️ Group 2 not configured (RCLONE_NEXT_PRIMARY_REMOTE not set)")

        if settings.RCLONE_SERVE_HTTP_ENABLED:
            daemons_running = health.get('serve_daemons_running', 0)
            logger.info(
                f"✅ Serve daemons (Group 1): {daemons_running}/{health['total_remotes']} running"
            )

            if settings.is_next_group_configured:
                g2_info = health.get("group2", {})
                g2_daemons = g2_info.get("serve_daemons_running", 0)
                g2_total = g2_info.get("total_remotes", 0)
                logger.info(
                    f"✅ Serve daemons (Group 2): {g2_daemons}/{g2_total} running"
                )

        for remote_info in health['remotes']:
            status_icon = "✅" if remote_info['available'] else "❌"
            daemon_status = ""

            if settings.RCLONE_SERVE_HTTP_ENABLED and 'serve_daemon' in remote_info:
                daemon = remote_info['serve_daemon']
                if daemon['running']:
                    daemon_icon = "✅" if daemon['healthy'] else "⚠️"
                    daemon_status = f", daemon={daemon_icon} {daemon['url']}"
                else:
                    daemon_status = ", daemon=❌ not running"

            logger.info(
                f"  {status_icon} [G1] {remote_info['name']}: "
                f"healthy={remote_info['healthy']}, "
                f"success_rate={remote_info['success_rate']}%"
                f"{daemon_status}"
            )

        if settings.is_next_group_configured:
            g2_info = health.get("group2", {})
            for remote_info in g2_info.get("remotes", []):
                status_icon = "✅" if remote_info['available'] else "❌"
                daemon_status = ""

                if settings.RCLONE_SERVE_HTTP_ENABLED and 'serve_daemon' in remote_info:
                    daemon = remote_info['serve_daemon']
                    if daemon['running']:
                        daemon_icon = "✅" if daemon['healthy'] else "⚠️"
                        daemon_status = f", daemon={daemon_icon} {daemon['url']}"
                    else:
                        daemon_status = ", daemon=❌ not running"

                logger.info(
                    f"  {status_icon} [G2] {remote_info['name']}: "
                    f"healthy={remote_info['healthy']}, "
                    f"success_rate={remote_info['success_rate']}%"
                    f"{daemon_status}"
                )

    except Exception as e:
        logger.error(f"❌ Failed to initialize MultiRemoteService: {str(e)}")
        logger.warning("⚠️ App will run with degraded multi-remote functionality")

    # Verify Rclone (single instance test)
    try:
        from app.services.rclone_service import RcloneService
        rclone = RcloneService()
        if rclone.test_connection():
            logger.info("✅ Rclone connection verified")
        else:
            logger.warning("⚠️ Rclone connection failed")
    except Exception as e:
        logger.warning(f"⚠️ Rclone verification failed: {str(e)}")

    # ==========================================
    # ✅ FIX COVER STATIC: Ensure covers directory exists sebelum mount
    # ==========================================
    covers_dir = Path(settings.COVERS_DIR)
    covers_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"✅ Covers directory ready: {covers_dir.absolute()}")

    # ✅ FIX COVER STATIC: Mount covers directory di dalam lifespan
    # agar direktori pasti sudah ada saat mount dilakukan
    try:
        # Cek apakah sudah di-mount (hindari double mount saat reload)
        already_mounted = any(
            getattr(route, "name", None) == "static_covers"
            for route in app.routes
        )
        if not already_mounted:
            app.mount(
                "/static/covers",
                StaticFiles(directory=str(covers_dir)),
                name="static_covers"
            )
            logger.info(f"✅ Mounted static covers inside lifespan: {covers_dir.absolute()}")
        else:
            logger.info("ℹ️ Static covers already mounted, skipping")
    except Exception as e:
        logger.error(f"❌ Failed to mount static covers: {str(e)}")

    yield

    # ==========================================
    # SHUTDOWN
    # ==========================================
    logger.info("🛑 Shutting down Manga Reader API")

    try:
        if multi_remote_service:
            logger.info("🧹 Cleaning up MultiRemoteService...")
            multi_remote_service.shutdown()
            logger.info("✅ MultiRemoteService cleanup complete")
    except Exception as e:
        logger.error(f"❌ Error cleaning up MultiRemoteService: {str(e)}")

    try:
        logger.info("🧹 Cleaning up RcloneService serve daemons...")
        RcloneService._shutdown_all_daemons_sync()
        logger.info("✅ RcloneService daemon cleanup complete")
    except Exception as e:
        logger.error(f"❌ Error cleaning up RcloneService daemons: {str(e)}")


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    description="Backend API for Manga Reader Platform - Full Features Version",
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG or settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.DEBUG or settings.ENVIRONMENT != "production" else None,
)


# ==========================================
# ✅ FIX COVER STATIC: Mount covers JUGA di module-level sebagai fallback
# Ini menangani kasus di mana covers_dir sudah ada sebelum app start
# (misalnya sudah pernah dijalankan sebelumnya)
# ==========================================
_covers_path_module = Path(settings.COVERS_DIR)
_covers_path_module.mkdir(parents=True, exist_ok=True)  # Pastikan selalu ada

try:
    app.mount(
        "/static/covers",
        StaticFiles(directory=str(_covers_path_module)),
        name="static_covers"
    )
    logger.info(f"✅ Mounted static covers (module-level): {_covers_path_module.absolute()}")
except Exception as e:
    logger.warning(f"⚠️ Could not mount static covers at module-level: {e}")


# ==========================================
# Middleware
# ==========================================

app.add_middleware(RequestIDMiddleware)

from starlette.middleware.base import BaseHTTPMiddleware

class CacheControlMiddleware(BaseHTTPMiddleware):
    """
    Add Cache-Control headers - OPTIMIZED FOR NO-CACHE MODE

    ✅ UPDATED: Aggressive browser caching untuk image proxy (7 days)
    ✅ FIX COVER STATIC: covers juga mendapat cache header yang benar
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path

        if "/image-proxy/image/" in path:
            # ✅ AGGRESSIVE BROWSER CACHING (7 days!)
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
            expires = datetime.now(timezone.utc) + timedelta(days=7)
            response.headers["Expires"] = expires.strftime("%a, %d %b %Y %H:%M:%S GMT")

        elif path.startswith("/static/"):
            # Static covers - cache forever (immutable)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

        elif path.startswith("/covers/"):
            # ✅ FIX COVER STATIC: endpoint fallback /covers/{filename}
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

        else:
            # API endpoints - no cache
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response

app.add_middleware(CacheControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)


# ==========================================
# Exception Handlers
# ==========================================

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, 'request_id', 'unknown')
    logger.warning("Validation error", extra={
        "request_id": request_id,
        "errors": exc.errors(),
        "path": request.url.path
    })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "request_id": request_id}
    )


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError):
    request_id = getattr(request.state, 'request_id', 'unknown')
    logger.error("Database error", extra={
        "request_id": request_id,
        "error": str(exc),
        "path": request.url.path
    }, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "A database error occurred",
            "request_id": request_id
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, 'request_id', 'unknown')
    logger.error("Unhandled exception", extra={
        "request_id": request_id,
        "error": str(exc),
        "path": request.url.path
    }, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "An unexpected error occurred",
            "request_id": request_id
        }
    )


# ==========================================
# Request Timing Middleware
# ==========================================

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(round(process_time * 1000, 2))
    return response


# ==========================================
# ✅ FIX #2 + #3 + #10: View Tracking Middleware (REWRITTEN)
# ✅ FIX CONCURRENCY: DB calls offloaded to thread pool via run_in_executor
# ==========================================

def _do_track_manga_view(manga_slug: str, auth_header: str, client_ip: str):
    """Sync helper: track manga view in DB. Runs in thread pool."""
    db = None
    try:
        db = SessionLocal()
        manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
        if manga:
            user_id = None
            if auth_header and auth_header.startswith("Bearer "):
                try:
                    token = auth_header.split(" ")[1]
                    payload = decode_access_token(token)
                    if payload:
                        username = payload.get("sub")
                        if username:
                            user = db.query(User).filter(User.username == username).first()
                            if user:
                                user_id = user.id
                except Exception:
                    pass

            hashed_ip = hash_ip_address(client_ip)
            view = MangaView(
                manga_id=manga.id,
                user_id=user_id,
                ip_address=hashed_ip
            )
            db.add(view)
            db.commit()
    except Exception as e:
        logger.error(f"Failed to track manga view: {str(e)}")
        if db:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db:
            db.close()


def _do_track_chapter_view(chapter_slug: str, auth_header: str, client_ip: str):
    """Sync helper: track chapter view in DB. Runs in thread pool."""
    db = None
    try:
        db = SessionLocal()
        chapter = db.query(Chapter).filter(Chapter.slug == chapter_slug).first()
        if chapter:
            user_id = None
            if auth_header and auth_header.startswith("Bearer "):
                try:
                    token = auth_header.split(" ")[1]
                    payload = decode_access_token(token)
                    if payload:
                        username = payload.get("sub")
                        if username:
                            user = db.query(User).filter(User.username == username).first()
                            if user:
                                user_id = user.id
                except Exception:
                    pass

            hashed_ip = hash_ip_address(client_ip)
            view = ChapterView(
                chapter_id=chapter.id,
                user_id=user_id,
                ip_address=hashed_ip
            )
            db.add(view)
            db.commit()
    except Exception as e:
        logger.error(f"Failed to track chapter view: {str(e)}")
        if db:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db:
            db.close()


@app.middleware("http")
async def track_views_middleware(request: Request, call_next):
    """Track manga and chapter views for analytics.
    
    ✅ FIX CONCURRENCY: DB work runs in thread pool via run_in_executor
    so it never blocks the async event loop.
    """
    response = await call_next(request)

    # Only track successful GET requests
    if request.method != "GET" or response.status_code != 200:
        return response

    path = request.url.path.rstrip("/")
    parts = path.split("/")

    # Extract request info (non-blocking)
    auth_header = request.headers.get("authorization", "")
    client_ip = request.client.host if request.client else None

    # Track MANGA views — offload to thread pool
    if (
        len(parts) == 5
        and parts[1] == "api"
        and parts[2] == "v1"
        and parts[3] == "manga"
        and parts[4] not in ("types", "genres", "")
    ):
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _do_track_manga_view, parts[4], auth_header, client_ip)

    # Track CHAPTER views — offload to thread pool
    elif (
        len(parts) == 5
        and parts[1] == "api"
        and parts[2] == "v1"
        and parts[3] == "chapter"
        and parts[4] not in ("manga", "")
    ):
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _do_track_chapter_view, parts[4], auth_header, client_ip)

    return response


# ==========================================
# ✅ Include Routers - WITH UPLOAD ROUTER
# ==========================================

app.include_router(auth_router,         prefix="/api/v1/auth",         tags=["Authentication"])
app.include_router(manga_router,        prefix="/api/v1/manga",        tags=["Manga"])
app.include_router(chapter_router,      prefix="/api/v1/chapter",      tags=["Chapter"])
app.include_router(image_proxy_router,  prefix="/api/v1/image-proxy",  tags=["Image Proxy"])
app.include_router(reading_router,      prefix="/api/v1/reading",      tags=["Reading History"])
app.include_router(bookmarks_router,    prefix="/api/v1/bookmarks",    tags=["Bookmarks"])
app.include_router(lists_router,        prefix="/api/v1/lists",        tags=["Reading Lists"])
app.include_router(upload_router,       prefix="/api/v1/upload",       tags=["Upload"])
app.include_router(admin_router,        prefix="/api/v1/admin",        tags=["Admin"])
app.include_router(analytics_router,    prefix="/api/v1/admin/analytics", tags=["Analytics"])


# ==========================================
# ✅ FIX COVER STATIC: Endpoint fallback untuk akses cover langsung
# Menangani kasus di mana /static/covers/{filename} tidak bisa diakses
# karena StaticFiles mount belum aktif atau ada masalah routing
# ==========================================

@app.get("/covers/{filename}", tags=["Static"])
async def serve_cover_file(filename: str):
    """
    Fallback endpoint untuk serve cover image langsung.

    Berguna jika /static/covers/{filename} tidak bisa diakses.
    URL: GET /covers/{filename}

    Note: Gunakan /static/covers/{filename} sebagai URL utama.
    Endpoint ini hanya sebagai fallback.
    """
    covers_dir = Path(settings.COVERS_DIR)
    file_path = covers_dir / filename

    # Security: pastikan tidak ada path traversal
    try:
        # Resolve path dan pastikan masih di dalam covers_dir
        resolved = file_path.resolve()
        covers_resolved = covers_dir.resolve()
        if not str(resolved).startswith(str(covers_resolved)):
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid filename"}
            )
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid filename"}
        )

    if not file_path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"Cover '{filename}' not found"}
        )

    # Determine content type
    ext = file_path.suffix.lower()
    content_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = content_type_map.get(ext, "image/jpeg")

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Cover-Filename": filename,
        }
    )


# ==========================================
# Root Endpoints
# ==========================================

@app.get("/", tags=["System"])
async def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "status": "running",
        "features": [
            "Manga Management",
            "Smart Caching",
            "Image Proxy",
            "Cover Images (Local + GDrive Backup)",
            "Upload to Google Drive",
            "Bulk Upload (ZIP + JSON)",
            "Admin CRUD",
            "Progress Tracking",
            "Resume Failed Upload",
            "Reading History",
            "Bookmarks & Favorites",
            "Reading Lists (Plan to Read, Reading, Completed, etc)",
            "Analytics Dashboard",
            "View Tracking",
            "✅ ASYNC Image Proxy (NO-CACHE MODE)",
            "✅ Browser Caching (7 days)",
            "✅ Unlimited Concurrent Users",
            "✅ ✨ Global Multi-Remote Singleton (NEW!)",
            "✅ ✨ rclone serve http daemons per remote (NEW!)",
            "✅ ✨ No double daemon start (FIXED!)",
            "✅ ✨ Multi-Group Storage (Group 1 + Group 2) with '@' prefix routing",
            "✅ ✨ Cover static files accessible via /static/covers/ & /covers/ (FIXED!)",
        ],
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "auth": "/api/v1/auth",
            "manga": "/api/v1/manga",
            "chapter": "/api/v1/chapter",
            "reading": "/api/v1/reading",
            "bookmarks": "/api/v1/bookmarks",
            "lists": "/api/v1/lists",
            "upload": "/api/v1/upload",
            "admin": "/api/v1/admin",
            "analytics": "/api/v1/admin/analytics",
            "image_proxy": "/api/v1/image-proxy",
            "static_covers": "/static/covers",
            "covers_fallback": "/covers"
        }
    }


@app.get("/health", tags=["System"])
async def health_check(db: Session = Depends(get_db)):
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "checks": {}
    }

    # Check database
    try:
        db.execute(text("SELECT 1"))
        health_status["checks"]["database"] = "healthy"
    except Exception as e:
        health_status["checks"]["database"] = "unhealthy"
        health_status["status"] = "degraded"
        logger.error(f"Database health check failed: {str(e)}")

    # ✅ ✨ Check MultiRemoteService (group 1 + group 2)
    try:
        global multi_remote_service
        if multi_remote_service and multi_remote_service.is_initialized:
            remote_health = multi_remote_service.get_health_status()

            if remote_health['available_remotes'] > 0:
                health_status["checks"]["multi_remote_group1"] = "healthy"
            else:
                health_status["checks"]["multi_remote_group1"] = "unhealthy"
                health_status["status"] = "degraded"

            health_status["multi_remote_info"] = {
                "total_remotes": remote_health['total_remotes'],
                "available_remotes": remote_health['available_remotes'],
                "healthy_remotes": remote_health['healthy_remotes'],
                "serve_enabled": remote_health['serve_enabled'],
                "serve_daemons_running": remote_health.get('serve_daemons_running', 0),
                "remotes": remote_health['remotes']
            }

            if settings.is_next_group_configured:
                g2_info = remote_health.get("group2", {})
                g2_available = g2_info.get("available_remotes", 0)
                g2_total = g2_info.get("total_remotes", 0)

                health_status["checks"]["multi_remote_group2"] = (
                    "healthy" if g2_available > 0 else "unhealthy"
                )
                health_status["multi_remote_group2_info"] = {
                    "configured": g2_info.get("configured", False),
                    "enabled": g2_info.get("enabled", False),
                    "total_remotes": g2_total,
                    "available_remotes": g2_available,
                    "healthy_remotes": g2_info.get("healthy_remotes", 0),
                    "serve_daemons_running": g2_info.get("serve_daemons_running", 0),
                    "path_prefix": g2_info.get("path_prefix", "@"),
                    "daemon_count": g2_info.get("daemon_count", 0),
                }
            else:
                health_status["checks"]["multi_remote_group2"] = "not_configured"

        else:
            health_status["checks"]["multi_remote_group1"] = "not_initialized"
            health_status["checks"]["multi_remote_group2"] = "not_initialized"
            health_status["status"] = "degraded"

    except Exception as e:
        health_status["checks"]["multi_remote_group1"] = "error"
        health_status["checks"]["multi_remote_group2"] = "error"
        health_status["status"] = "degraded"
        logger.error(f"Multi-remote health check failed: {str(e)}")

    # Check Rclone (fallback single instance)
    try:
        from app.services.rclone_service import RcloneService
        rclone = RcloneService()
        if rclone.test_connection():
            health_status["checks"]["rclone_fallback"] = "healthy"
        else:
            health_status["checks"]["rclone_fallback"] = "unhealthy"
            if health_status["checks"].get("multi_remote_group1") != "healthy":
                health_status["status"] = "degraded"
    except Exception as e:
        health_status["checks"]["rclone_fallback"] = "unhealthy"

    # Check cache directory
    try:
        cache_dir = Path(settings.RCLONE_CACHE_DIR)
        if cache_dir.exists() and cache_dir.is_dir():
            health_status["checks"]["cache_directory"] = "healthy"
        else:
            health_status["checks"]["cache_directory"] = "unhealthy"
            health_status["status"] = "degraded"
    except Exception:
        health_status["checks"]["cache_directory"] = "unhealthy"
        health_status["status"] = "degraded"

    # Check covers directory
    try:
        covers_dir = Path(settings.COVERS_DIR)
        if covers_dir.exists() and covers_dir.is_dir():
            health_status["checks"]["covers_directory"] = "healthy"
            # ✅ FIX COVER STATIC: Tambah info cover files
            cover_files = list(covers_dir.glob("*.*"))
            health_status["covers_info"] = {
                "directory": str(covers_dir.absolute()),
                "total_files": len(cover_files),
                "static_url": "/static/covers/",
                "fallback_url": "/covers/"
            }
        else:
            health_status["checks"]["covers_directory"] = "unhealthy"
            health_status["status"] = "degraded"
    except Exception:
        health_status["checks"]["covers_directory"] = "unhealthy"
        health_status["status"] = "degraded"

    # Check executor stats
    try:
        from app.services.rclone_service import get_executor_stats
        executor_stats = get_executor_stats()
        health_status["checks"]["executor"] = executor_stats.get("status", "unknown")
        health_status["executor_stats"] = executor_stats
    except Exception as e:
        health_status["checks"]["executor"] = "error"
        logger.error(f"Executor stats check failed: {str(e)}")

    return health_status


@app.get("/routes", tags=["System"])
async def list_routes():
    """Debug endpoint: List semua routes yang terdaftar"""
    routes = []
    for route in app.routes:
        if hasattr(route, "methods"):
            routes.append({
                "path": route.path,
                "methods": list(route.methods),
                "name": route.name
            })
    return {"total_routes": len(routes), "routes": routes}


@app.get("/features", tags=["System"])
async def list_features():
    """List all available features and their status"""
    return {
        "core_features": {
            "manga_management": True,
            "chapter_management": True,
            "image_proxy": True,
            "smart_caching": False,
            "cover_images": True,
            "async_image_proxy": True,
            "browser_caching": True,
            "global_multi_remote_singleton": True,
            "rclone_serve_daemons": settings.RCLONE_SERVE_HTTP_ENABLED,
            "no_double_daemon_start": True,
            "multi_group_storage": settings.is_next_group_configured,
            "group2_path_prefix": settings.GROUP2_PATH_PREFIX if settings.is_next_group_configured else None,
            "group2_auto_switch": settings.RCLONE_AUTO_SWITCH_GROUP,
            # ✅ FIX COVER STATIC
            "cover_static_files": True,
            "cover_static_url": "/static/covers/",
            "cover_fallback_url": "/covers/",
        },
        "upload_features": {
            "single_chapter_upload": True,
            "bulk_chapter_upload": True,
            "json_metadata_upload": True,
            "multiple_manga_upload": True,
            "progress_tracking": True,
            "resume_upload": True,
            "cover_upload": True,
            "group_aware_upload": settings.is_next_group_configured,
            "active_upload_group": settings.get_active_upload_group() if settings.is_next_group_configured else 1,
        },
        "reading_features": {
            "reading_history": True,
            "save_progress": True,
            "bookmarks": True,
            "reading_lists": True,
            "custom_lists_statuses": ["plan_to_read", "reading", "completed", "dropped", "on_hold"],
            "rating_system": True,
        },
        "admin_features": {
            "user_management": True,
            "manga_crud": True,
            "chapter_crud": True,
            "storage_management": True,
            "cache_management": True,
            "analytics_dashboard": True,
            "cover_management": True,
        },
        "analytics_features": {
            "overview_dashboard": True,
            "manga_views_tracking": True,
            "chapter_views_tracking": True,
            "user_growth_analytics": True,
            "popular_genres": True,
            "top_manga_rankings": True,
            "recent_activity": True,
        },
        "privacy_features": {
            "ip_address_hashing": True,
            "note": "IP addresses are SHA-256 hashed before storage for privacy compliance"
        },
        "performance_features": {
            "async_image_proxy": True,
            "unlimited_concurrent_users": True,
            "no_server_cache": True,
            "browser_cache_7_days": True,
            "non_blocking_downloads": True,
            "auto_scaling_workers": True,
            "global_singleton_remotes": True,
            "rclone_serve_http_daemons": settings.RCLONE_SERVE_HTTP_ENABLED,
            "no_double_daemon_start": True,
            "group_aware_image_proxy": True,
            "note": "NO-CACHE MODE - Direct download from GDrive (or via serve daemon if enabled), browser caching only"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )