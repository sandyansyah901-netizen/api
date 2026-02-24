# File: app/services/bulk_upload_service.py
"""
Bulk Upload Service - MEMORY LEAK FIXED + AUTO-THUMBNAIL (COMPLETE VERSION)
============================================================================
Service untuk handle semua operasi bulk upload.

FIXES APPLIED:
✅ Shared ThreadPoolExecutor (FIXED MEMORY LEAK)
✅ Thread-safe executor management
✅ Proper cleanup on shutdown
✅ Reference counting for executor lifecycle
✅ Safe dict access dengan .get()
✅ Memory-efficient progress store cleanup
✅ ✨ AUTO-GENERATE THUMBNAIL 16:9 saat upload (BARU!)
✅ FIX #3: Import timezone dan ganti semua datetime.utcnow()
✅ FIX #12: Module-level helper functions (create_upload_id, create_resume_token)
✅ GROUP-AWARE: Upload ke group 1 atau group 2 berdasarkan active_upload_group
              Path di DB di-prefix '@' kalau group 2

✅ PERF FIX: _upload_single_chapter sekarang pakai rclone copy (folder batch)
             bukan copyto per-file. Jauh lebih cepat untuk banyak file kecil.
             --transfers 8 --checkers 8 --drive-chunk-size 64M
"""

import os
import uuid
import zipfile
import json
import re
import logging
import asyncio
import shutil
import threading
import atexit
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta, timezone  # ✅ FIX #3: Added timezone import

from app.core.base import settings
from app.services.rclone_service import RcloneService
from app.services.natural_sorter import NaturalSorter

# ✅ IMPORT THUMBNAIL SERVICE (BARU)
from app.services.thumbnail_service import ThumbnailService

logger = logging.getLogger(__name__)

# ==========================================
# ✅ THREAD-SAFE In-Memory Stores
# ==========================================

# Progress tracking: {upload_id: progress_dict}
upload_progress_store: Dict[str, dict] = {}
upload_progress_lock = threading.Lock()

# Resume tokens: {token: resume_data}
resume_token_store: Dict[str, dict] = {}
resume_token_lock = threading.Lock()

# ✅ Auto-cleanup old entries (prevent memory leak)
PROGRESS_EXPIRY_HOURS = 24
TOKEN_EXPIRY_HOURS = 48


# ==========================================
# ✅ FIX #12: Module-Level Helper Functions
# ==========================================

def create_upload_id() -> str:
    """Generate unique upload ID."""
    return str(uuid.uuid4())


def create_resume_token() -> str:
    """Generate resume token."""
    return str(uuid.uuid4())


def cleanup_expired_progress():
    """
    ✅ Cleanup expired progress entries to prevent memory leak.

    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    try:
        with upload_progress_lock:
            now = datetime.now(timezone.utc)  # ✅ FIX #3
            expired_ids = []

            for upload_id, progress in upload_progress_store.items():
                started_at_str = progress.get("started_at")
                if started_at_str:
                    try:
                        started_at = datetime.fromisoformat(started_at_str)
                        if now - started_at > timedelta(hours=PROGRESS_EXPIRY_HOURS):
                            expired_ids.append(upload_id)
                    except:
                        pass

            for upload_id in expired_ids:
                del upload_progress_store[upload_id]

            if expired_ids:
                logger.info(f"Cleaned up {len(expired_ids)} expired progress entries")
    except Exception as e:
        logger.error(f"Error cleaning up progress: {str(e)}")


def cleanup_expired_tokens():
    """
    ✅ Cleanup expired resume tokens.

    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    try:
        with resume_token_lock:
            now = datetime.now(timezone.utc)  # ✅ FIX #3
            expired_tokens = []

            for token, data in resume_token_store.items():
                created_at = data.get("created_at")
                if created_at:
                    try:
                        if now - created_at > timedelta(hours=TOKEN_EXPIRY_HOURS):
                            expired_tokens.append(token)
                    except:
                        pass

            for token in expired_tokens:
                del resume_token_store[token]

            if expired_tokens:
                logger.info(f"Cleaned up {len(expired_tokens)} expired resume tokens")
    except Exception as e:
        logger.error(f"Error cleaning up tokens: {str(e)}")


def auto_detect_chapter_info(
    folder_name: str,
    naming_pattern: str = r"[Cc]hapter[_\s]?(\d+(?:\.\d+)?)"
) -> Dict[str, Any]:
    """
    Auto-detect chapter info dari nama folder.

    Contoh:
    - "Chapter_61" → chapter_main=61, chapter_label="Chapter 61", slug="chapter-61"
    - "Chapter_01.5" → chapter_main=1, chapter_sub=5, chapter_label="Chapter 1.5"
    """
    match = re.search(naming_pattern, folder_name, re.IGNORECASE)

    if match:
        number_str = match.group(1)

        if "." in number_str:
            parts = number_str.split(".")
            chapter_main = int(parts[0])
            chapter_sub = int(parts[1]) if len(parts) > 1 else 0
        else:
            chapter_main = int(number_str)
            chapter_sub = 0

        if chapter_sub > 0:
            chapter_label = f"Chapter {chapter_main}.{chapter_sub}"
            slug_num = f"{chapter_main}-{chapter_sub}"
        else:
            chapter_label = f"Chapter {chapter_main}"
            slug_num = str(chapter_main)

        return {
            "chapter_main": chapter_main,
            "chapter_sub": chapter_sub,
            "chapter_label": chapter_label,
            "slug_suffix": slug_num,
            "chapter_folder_name": folder_name,
            "detected": True
        }

    return {
        "chapter_main": 0,
        "chapter_sub": 0,
        "chapter_label": folder_name,
        "slug_suffix": folder_name.lower().replace(" ", "-").replace("_", "-"),
        "chapter_folder_name": folder_name,
        "detected": False
    }


def generate_chapter_slug(manga_slug: str, chapter_main: int, chapter_sub: int = 0) -> str:
    """Generate unique slug untuk chapter."""
    if chapter_sub > 0:
        return f"{manga_slug}-chapter-{chapter_main}-{chapter_sub}"
    return f"{manga_slug}-chapter-{chapter_main}"


# ==========================================
# ✅ GROUP-AWARE HELPER
# ==========================================

def _get_active_upload_group_and_prefix() -> Tuple[int, str]:
    """
    Get active upload group dan path prefix yang sesuai.
    """
    try:
        from app.services.multi_remote_service import MultiRemoteService
        instance = MultiRemoteService._global_instance
        if instance and instance.is_initialized:
            group = instance.get_active_upload_group()
            prefix = settings.GROUP2_PATH_PREFIX if group == 2 else ""
            return group, prefix
    except Exception as e:
        logger.debug(f"Could not get active upload group from MultiRemoteService: {e}")

    try:
        from app.core.base import get_active_upload_group
        group = get_active_upload_group()
        prefix = settings.GROUP2_PATH_PREFIX if group == 2 else ""
        return group, prefix
    except Exception as e:
        logger.debug(f"Could not get active upload group from base: {e}")

    return 1, ""


def _get_rclone_for_group(group: int) -> RcloneService:
    """
    Get RcloneService instance yang sesuai dengan group.
    """
    if group == 2 and settings.is_next_group_configured:
        remote_name = settings.RCLONE_NEXT_PRIMARY_REMOTE
        logger.debug(f"Using Group 2 remote: {remote_name}")
        return RcloneService(remote_name=remote_name)
    else:
        return RcloneService()


# ==========================================
# ✅ PERF FIX: Batch folder upload helper
#
# SEBELUMNYA: upload per-file dengan rclone copyto (lambat!)
#   for file in files:
#       rclone copyto local_file remote:path/file
#
# SEKARANG: upload seluruh folder sekaligus dengan rclone copy (cepat!)
#   rclone copy local_dir remote:dest_dir --transfers 8 --checkers 8
#
# Keuntungan:
# - Batch request, pipeline optimized
# - Minimal TCP handshake
# - Bisa 5-10x lebih cepat untuk banyak file kecil
# ==========================================

def _batch_upload_folder(
    rclone: RcloneService,
    local_dir: Path,
    remote_folder_path: str,
    timeout: int = 600
) -> Tuple[bool, str]:
    """
    ✅ PERF FIX: Upload seluruh folder sekaligus pakai rclone copy.

    Jauh lebih cepat dari upload per-file untuk banyak gambar kecil.

    Args:
        rclone: RcloneService instance
        local_dir: Path folder lokal yang berisi gambar
        remote_folder_path: Path folder tujuan di remote (tanpa remote prefix)
        timeout: Timeout dalam detik (default 600 = 10 menit)

    Returns:
        (success: bool, error_message: str)
    """
    remote_dest = f"{rclone.remote_name}:{remote_folder_path}"

    # ✅ Flags untuk performa maksimal
    result = rclone._run_command([
        "copy",
        str(local_dir),
        remote_dest,
        "--transfers", "8",        # Upload 8 file paralel
        "--checkers", "8",         # Check 8 file paralel
        "--drive-chunk-size", "64M",  # Chunk besar untuk file besar
        "--fast-list",             # Kurangi API calls
        "--no-traverse",           # Skip traversal untuk folder baru
        "--progress",
    ], timeout=timeout)

    if result.returncode == 0:
        return True, ""
    else:
        error = result.stderr if isinstance(result.stderr, str) else result.stderr.decode('utf-8', errors='ignore')
        return False, error


def _prepare_chapter_temp_dir(
    image_files: List[Path],
    preserve_filenames: bool = False
) -> Tuple[Path, Dict[str, int]]:
    """
    ✅ PERF FIX: Siapkan temp dir dengan file yang sudah di-rename.

    Karena rclone copy mengambil nama file dari lokal,
    kita harus rename file dulu (001.jpg, 002.jpg, ...) sebelum upload batch.

    Args:
        image_files: List file gambar yang sudah diurutkan
        preserve_filenames: Kalau True, pakai nama asli

    Returns:
        (temp_dir: Path, filename_to_order: Dict[str, int])
        - temp_dir: Path ke temp dir berisi file yang sudah dipersiapkan
        - filename_to_order: mapping filename → page_order
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="bulk_upload_"))
    filename_to_order: Dict[str, int] = {}

    for idx, img_file in enumerate(image_files, start=1):
        if preserve_filenames:
            target_name = img_file.name
        else:
            target_name = f"{idx:03d}{img_file.suffix.lower()}"

        dest = temp_dir / target_name

        # Symlink atau copy (copy lebih aman cross-device)
        try:
            dest.symlink_to(img_file.resolve())
        except (OSError, NotImplementedError):
            # Windows tidak support symlink tanpa privilege, fallback ke copy
            shutil.copy2(str(img_file), str(dest))

        filename_to_order[target_name] = idx

    return temp_dir, filename_to_order


# ==========================================
# Bulk Upload Service
# ==========================================

class BulkUploadService:
    """
    Service untuk semua operasi bulk upload.

    ✅ MEMORY-SAFE VERSION with shared executor
    ✅ AUTO-GENERATE THUMBNAIL 16:9 (BARU!)
    ✅ FIX #3: All datetime.utcnow() replaced
    ✅ GROUP-AWARE: Upload ke group 1 atau group 2, path di DB di-prefix '@' kalau group 2
    ✅ PERF FIX: Batch folder upload (rclone copy) bukan per-file (rclone copyto)
    """

    # ✅ Class-level shared executor (prevents memory leak)
    _executor: Optional[ThreadPoolExecutor] = None
    _executor_lock = threading.Lock()
    _executor_refcount = 0
    _shutdown_registered = False

    TEMP_DIR = Path(settings.RCLONE_CACHE_DIR) / "bulk_uploads"
    ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

    def __init__(self, db=None):
        """Initialize service with shared executor."""
        self.db = db
        self.rclone = RcloneService()
        self.TEMP_DIR.mkdir(parents=True, exist_ok=True)

        # ✅ Acquire shared executor
        self._acquire_executor()

        # ✅ Cleanup expired entries on init
        cleanup_expired_progress()
        cleanup_expired_tokens()

    @classmethod
    def _acquire_executor(cls):
        """
        ✅ Thread-safe executor acquisition with reference counting.
        """
        with cls._executor_lock:
            if cls._executor is None:
                logger.info("🚀 Creating shared ThreadPoolExecutor for bulk upload")
                cls._executor = ThreadPoolExecutor(
                    max_workers=4,
                    thread_name_prefix="bulk-upload-worker-"
                )

                if not cls._shutdown_registered:
                    atexit.register(cls._shutdown_executor)

                    try:
                        signal.signal(signal.SIGTERM, cls._signal_handler)
                        signal.signal(signal.SIGINT, cls._signal_handler)
                    except (ValueError, OSError):
                        logger.warning("Cannot register signal handlers (not main thread)")

                    cls._shutdown_registered = True
                    logger.info("✅ Registered shutdown handlers for bulk upload executor")

            cls._executor_refcount += 1
            logger.debug(f"Bulk executor acquired (refcount: {cls._executor_refcount})")

    @classmethod
    def _release_executor(cls):
        """
        ✅ Thread-safe executor release with reference counting.
        """
        with cls._executor_lock:
            cls._executor_refcount -= 1
            logger.debug(f"Bulk executor released (refcount: {cls._executor_refcount})")

    @classmethod
    def _shutdown_executor(cls):
        """
        ✅ Gracefully shutdown shared executor.
        """
        with cls._executor_lock:
            if cls._executor is not None:
                logger.info("🛑 Shutting down bulk upload executor...")

                try:
                    cls._executor.shutdown(wait=True, cancel_futures=False)
                    logger.info("✅ Bulk executor shutdown complete (graceful)")
                except Exception as e:
                    logger.error(f"Error during bulk executor shutdown: {str(e)}")
                    try:
                        cls._executor.shutdown(wait=False, cancel_futures=True)
                        logger.warning("⚠️ Bulk executor shutdown complete (forced)")
                    except:
                        pass
                finally:
                    cls._executor = None
                    cls._executor_refcount = 0

    @classmethod
    def _signal_handler(cls, signum, frame):
        """
        ✅ Handle termination signals gracefully.
        """
        logger.info(f"Received signal {signum}, shutting down bulk executor...")
        cls._shutdown_executor()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    def __del__(self):
        """✅ Cleanup on instance destruction."""
        try:
            self._release_executor()
        except Exception as e:
            logger.error(f"Error in __del__: {str(e)}")

    # ==========================================
    # ZIP Processing
    # ==========================================

    def extract_zip(self, zip_content: bytes, session_id: str) -> Optional[Path]:
        """Extract ZIP file ke temporary directory."""
        extract_dir = self.TEMP_DIR / session_id

        try:
            extract_dir.mkdir(parents=True, exist_ok=True)
            zip_path = extract_dir / "upload.zip"

            with open(zip_path, "wb") as f:
                f.write(zip_content)

            with zipfile.ZipFile(zip_path, "r") as zf:
                for zip_info in zf.infolist():
                    if ".." in zip_info.filename or zip_info.filename.startswith("/"):
                        raise ValueError(f"ZIP berisi path berbahaya: {zip_info.filename}")

                zf.extractall(extract_dir / "content")

            zip_path.unlink()

            logger.info(f"ZIP extracted to: {extract_dir / 'content'}")
            return extract_dir / "content"

        except Exception as e:
            logger.error(f"ZIP extraction failed: {str(e)}", exc_info=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            return None

    def cleanup_session(self, session_id: str):
        """Hapus temporary files untuk session."""
        session_dir = self.TEMP_DIR / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(f"Cleaned up session: {session_id}")

    # ==========================================
    # Chapter Detection
    # ==========================================

    def detect_chapters_from_directory(
        self,
        content_dir: Path,
        naming_pattern: str = r"[Cc]hapter[_\s]?(\d+(?:\.\d+)?)"
    ) -> List[Dict]:
        """Auto-detect semua chapter dari hasil extract ZIP."""
        chapters = []

        if not content_dir.exists():
            return chapters

        top_items = list(content_dir.iterdir())
        chapter_dirs = []

        for item in top_items:
            if item.is_dir():
                chapter_info = auto_detect_chapter_info(item.name, naming_pattern)
                if chapter_info["detected"]:
                    chapter_dirs.append(item)
                else:
                    sub_items = list(item.iterdir())
                    for sub_item in sub_items:
                        if sub_item.is_dir():
                            sub_info = auto_detect_chapter_info(sub_item.name, naming_pattern)
                            if sub_info["detected"]:
                                chapter_dirs.append(sub_item)

        chapter_dirs = sorted(
            chapter_dirs,
            key=lambda x: NaturalSorter.extract_numbers(x.name)
        )

        for chapter_dir in chapter_dirs:
            info = auto_detect_chapter_info(chapter_dir.name, naming_pattern)

            image_files = sorted([
                f for f in chapter_dir.iterdir()
                if f.is_file() and f.suffix.lower() in self.ALLOWED_IMAGE_EXTS
            ], key=lambda x: NaturalSorter.extract_numbers(x.name))

            if image_files:
                info["local_path"] = chapter_dir
                info["files"] = image_files
                info["file_count"] = len(image_files)
                info["total_size_bytes"] = sum(f.stat().st_size for f in image_files)
                chapters.append(info)

        return chapters

    # ==========================================
    # ✅ THREAD-SAFE Progress Tracking
    # ==========================================

    def init_progress(
        self,
        upload_id: str,
        total_chapters: int,
        total_files: int,
        manga_slug: str = ""
    ):
        """
        ✅ Initialize progress tracker (thread-safe).

        ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
        """
        with upload_progress_lock:
            upload_progress_store[upload_id] = {
                "upload_id": upload_id,
                "status": "processing",
                "progress": 0,
                "manga_slug": manga_slug,
                "total_chapters": total_chapters,
                "completed_chapters": 0,
                "failed_chapters": 0,
                "total_files": total_files,
                "uploaded_files": 0,
                "current_chapter": None,
                "current_file": None,
                "estimated_time_remaining_seconds": None,
                "started_at": datetime.now(timezone.utc).isoformat(),  # ✅ FIX #3
                "completed_at": None,
                "error": None,
                "results": []
            }

    def update_progress(self, upload_id: str, **kwargs):
        """✅ Update progress entry (thread-safe)."""
        with upload_progress_lock:
            if upload_id not in upload_progress_store:
                logger.warning(f"Upload ID {upload_id} not found in progress store")
                return

            upload_progress_store[upload_id].update(kwargs)

            store = upload_progress_store[upload_id]
            total_files = store.get("total_files", 0)
            uploaded_files = store.get("uploaded_files", 0)

            if total_files > 0:
                store["progress"] = int((uploaded_files / total_files) * 100)
            else:
                store["progress"] = 0

    def get_progress(self, upload_id: str) -> Optional[Dict]:
        """✅ Get progress info (thread-safe)."""
        with upload_progress_lock:
            return upload_progress_store.get(upload_id, None)

    def increment_uploaded_files(self, upload_id: str, count: int = 1):
        """✅ Safely increment uploaded_files counter."""
        with upload_progress_lock:
            if upload_id in upload_progress_store:
                store = upload_progress_store[upload_id]
                store["uploaded_files"] = store.get("uploaded_files", 0) + count

                total_files = store.get("total_files", 0)
                if total_files > 0:
                    store["progress"] = int((store["uploaded_files"] / total_files) * 100)

    def increment_completed_chapters(self, upload_id: str, count: int = 1):
        """✅ Safely increment completed_chapters counter."""
        with upload_progress_lock:
            if upload_id in upload_progress_store:
                store = upload_progress_store[upload_id]
                store["completed_chapters"] = store.get("completed_chapters", 0) + count

    def increment_failed_chapters(self, upload_id: str, count: int = 1):
        """✅ Safely increment failed_chapters counter."""
        with upload_progress_lock:
            if upload_id in upload_progress_store:
                store = upload_progress_store[upload_id]
                store["failed_chapters"] = store.get("failed_chapters", 0) + count

    # ==========================================
    # Conflict Resolution
    # ==========================================

    def check_manga_conflict(self, manga_slug: str) -> bool:
        """Check apakah manga sudah ada."""
        if not self.db:
            return False
        from app.models.models import Manga
        return self.db.query(Manga).filter(Manga.slug == manga_slug).first() is not None

    def check_chapter_conflict(self, manga_id: int, chapter_main: int, chapter_sub: int = 0) -> bool:
        """Check apakah chapter sudah ada."""
        if not self.db:
            return False
        from app.models.models import Chapter
        return self.db.query(Chapter).filter(
            Chapter.manga_id == manga_id,
            Chapter.chapter_main == chapter_main,
            Chapter.chapter_sub == chapter_sub
        ).first() is not None

    # ==========================================
    # ✅ PERF FIX: Single Chapter Upload (Internal) + BATCH UPLOAD + AUTO-THUMBNAIL + GROUP-AWARE
    # ==========================================

    def _upload_single_chapter(
        self,
        manga_slug: str,
        base_folder_id: str,
        chapter_info: Dict,
        manga_id: int,
        uploader_id: int,
        preserve_filenames: bool = False,
        upload_id: Optional[str] = None,
        active_group: int = 1,
        path_prefix: str = ""
    ) -> Dict:
        """
        Upload satu chapter ke GDrive dan simpan ke DB.

        ✅ PERF FIX: Sekarang pakai batch upload (rclone copy folder)
                     bukan per-file (rclone copyto). 5-10x lebih cepat!

        ✅ AUTO-GENERATE THUMBNAIL 16:9 (BARU!)
        ✅ GROUP-AWARE: pakai remote yang sesuai group, path di DB di-prefix @ kalau group 2

        Args:
            manga_slug: Slug manga
            base_folder_id: Base folder ID di GDrive
            chapter_info: Dict info chapter
            manga_id: ID manga di DB
            uploader_id: ID user uploader
            preserve_filenames: Keep original filename atau auto-rename
            upload_id: ID untuk progress tracking (optional)
            active_group: 1 atau 2 (untuk pilih remote yang tepat)
            path_prefix: "" untuk group 1, "@" untuk group 2 (prefix di DB path)
        """
        chapter_folder_name = chapter_info["chapter_folder_name"]
        chapter_main = chapter_info["chapter_main"]
        chapter_sub = chapter_info.get("chapter_sub", 0)
        chapter_label = chapter_info["chapter_label"]
        image_files: List[Path] = chapter_info["files"]

        # ✅ GROUP-AWARE: pilih rclone service yang sesuai group
        rclone = _get_rclone_for_group(active_group)

        temp_dir: Optional[Path] = None

        try:
            # 1. Tentukan path folder di GDrive
            manga_folder = f"{base_folder_id}/{manga_slug}"
            chapter_folder = f"{manga_folder}/{chapter_folder_name}"

            # Update progress: chapter sedang diproses
            if upload_id:
                with upload_progress_lock:
                    if upload_id in upload_progress_store:
                        upload_progress_store[upload_id]["current_chapter"] = chapter_folder_name

            # 2. Buat folder manga + chapter di GDrive (mkdir)
            rclone._run_command(["mkdir", f"{rclone.remote_name}:{manga_folder}"])
            rclone._run_command(["mkdir", f"{rclone.remote_name}:{chapter_folder}"])

            # ==========================================
            # ✅ PERF FIX: Siapkan temp dir dengan file renamed
            # ==========================================
            # Sort file secara natural dulu
            sorted_files = sorted(image_files, key=lambda x: NaturalSorter.extract_numbers(x.name))

            temp_dir, filename_to_order = _prepare_chapter_temp_dir(
                sorted_files, preserve_filenames
            )

            file_count = len(sorted_files)
            logger.info(
                f"📦 Batch uploading {file_count} files for '{chapter_label}' "
                f"to Group {active_group} remote '{rclone.remote_name}': {chapter_folder}"
            )

            # ==========================================
            # ✅ PERF FIX: Batch upload seluruh folder sekaligus
            # ==========================================
            success, error_msg = _batch_upload_folder(
                rclone,
                temp_dir,
                chapter_folder,
                timeout=max(300, file_count * 10)  # Minimal 5 menit, atau 10 detik/file
            )

            if not success:
                raise Exception(f"Batch upload gagal: {error_msg}")

            logger.info(f"✅ Batch upload selesai: {file_count} files untuk '{chapter_label}'")

            # ==========================================
            # Build uploaded_pages list dari filename_to_order
            # (setelah batch upload berhasil)
            # ==========================================
            uploaded_pages = []
            for filename, page_order in sorted(filename_to_order.items(), key=lambda x: x[1]):
                db_path = f"{path_prefix}{chapter_folder}/{filename}"
                clean_path = f"{chapter_folder}/{filename}"
                uploaded_pages.append({
                    "gdrive_path": db_path,
                    "gdrive_path_clean": clean_path,
                    "page_order": page_order,
                    "original_name": filename
                })

            # Update progress: semua file chapter ini terupload
            if upload_id:
                self.increment_uploaded_files(upload_id, file_count)

            if not uploaded_pages:
                return {"success": False, "error": "No files uploaded"}

            # 3. Create chapter record di DB
            if self.db:
                from app.models.models import Chapter, Page

                chapter_slug = generate_chapter_slug(manga_slug, chapter_main, chapter_sub)

                # Handle duplicate slug
                base_slug = chapter_slug
                counter = 1
                while self.db.query(Chapter).filter(Chapter.slug == chapter_slug).first():
                    chapter_slug = f"{base_slug}-v{counter}"
                    counter += 1

                new_chapter = Chapter(
                    manga_id=manga_id,
                    chapter_main=chapter_main,
                    chapter_sub=chapter_sub,
                    chapter_label=chapter_label,
                    slug=chapter_slug,
                    chapter_folder_name=chapter_folder_name,
                    uploaded_by=uploader_id
                )

                self.db.add(new_chapter)
                self.db.flush()

                # Create page records
                for page_info in uploaded_pages:
                    page = Page(
                        chapter_id=new_chapter.id,
                        gdrive_file_id=page_info["gdrive_path"],
                        page_order=page_info["page_order"],
                        is_anchor=(page_info["page_order"] == 1)
                    )
                    self.db.add(page)

                # ✅ 4. AUTO-GENERATE THUMBNAIL 16:9 (BARU!)
                thumbnail_generated = False
                thumbnail_path = None
                first_page = uploaded_pages[0]

                if uploaded_pages:
                    try:
                        thumbnail_service = ThumbnailService()

                        source_path_clean = first_page["gdrive_path_clean"]
                        thumbnail_clean = f"{chapter_folder}/thumbnail.jpg"
                        thumbnail_db_path = f"{path_prefix}{thumbnail_clean}"

                        logger.info(f"🎨 Auto-generating thumbnail for {chapter_label}...")

                        success_thumb = thumbnail_service.generate_16_9_thumbnail(
                            source_path_clean,
                            thumbnail_clean
                        )

                        if success_thumb:
                            new_chapter.anchor_path = thumbnail_db_path
                            new_chapter.preview_url = f"/api/v1/image-proxy/image/{thumbnail_db_path}"
                            thumbnail_generated = True
                            thumbnail_path = thumbnail_db_path
                            logger.info(f"✅ Thumbnail generated: {thumbnail_db_path}")
                        else:
                            new_chapter.anchor_path = first_page["gdrive_path"]
                            new_chapter.preview_url = f"/api/v1/image-proxy/image/{first_page['gdrive_path']}"
                            logger.warning(f"⚠️ Thumbnail generation failed, using page 1")

                    except Exception as e:
                        logger.error(f"❌ Error generating thumbnail: {str(e)}, using page 1")
                        new_chapter.anchor_path = first_page["gdrive_path"]
                        new_chapter.preview_url = f"/api/v1/image-proxy/image/{first_page['gdrive_path']}"

                self.db.commit()

                return {
                    "success": True,
                    "chapter_id": new_chapter.id,
                    "chapter_slug": chapter_slug,
                    "chapter_label": chapter_label,
                    "chapter_number": chapter_main,
                    "gdrive_path": f"{path_prefix}{chapter_folder}",
                    "gdrive_path_clean": chapter_folder,
                    "storage_group": active_group,
                    "path_prefix": path_prefix,
                    "total_pages": len(uploaded_pages),
                    "status": "success",
                    "thumbnail": {
                        "generated": thumbnail_generated,
                        "type": "custom_16_9" if thumbnail_generated else "page_1_original",
                        "path": thumbnail_path if thumbnail_generated else first_page["gdrive_path"]
                    }
                }

            return {
                "success": True,
                "chapter_label": chapter_label,
                "chapter_number": chapter_main,
                "gdrive_path": f"{path_prefix}{chapter_folder}",
                "gdrive_path_clean": chapter_folder,
                "storage_group": active_group,
                "path_prefix": path_prefix,
                "total_pages": len(uploaded_pages),
                "status": "success"
            }

        except Exception as e:
            logger.error(f"Failed to upload chapter {chapter_label}: {str(e)}", exc_info=True)
            if self.db:
                self.db.rollback()
            return {
                "success": False,
                "chapter_label": chapter_label,
                "chapter_number": chapter_main,
                "status": "failed",
                "error": str(e)
            }

        finally:
            # ✅ Selalu cleanup temp dir
            if temp_dir and temp_dir.exists():
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    logger.debug(f"Cleaned up temp dir: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp dir {temp_dir}: {e}")

    # ==========================================
    # Feature 1: Bulk Chapters Upload
    # ==========================================

    async def bulk_upload_chapters(
        self,
        manga_slug: str,
        zip_content: bytes,
        uploader_id: int,
        start_chapter: Optional[int] = None,
        end_chapter: Optional[int] = None,
        naming_pattern: str = r"[Cc]hapter[_\s]?(\d+(?:\.\d+)?)",
        overrides: Optional[Dict[str, Dict]] = None,
        conflict_strategy: str = "skip",
        dry_run: bool = False,
        parallel: bool = False,
        max_workers: int = 2,
        preserve_filenames: bool = False
    ) -> Dict:
        """
        Bulk upload chapters dari ZIP + AUTO-GENERATE THUMBNAILS.

        ✅ MEMORY-SAFE: Uses shared executor instead of creating new one
        ✅ AUTO-THUMBNAIL: Generate 16:9 thumbnail untuk setiap chapter
        ✅ FIX #3: All datetime.utcnow() replaced
        ✅ GROUP-AWARE: Detect active group, prefix path @ kalau group 2
        ✅ PERF FIX: Batch folder upload per chapter
        """
        upload_id = create_upload_id()
        session_id = create_upload_id()
        started_at = datetime.now(timezone.utc)  # ✅ FIX #3

        # ✅ GROUP-AWARE: deteksi group sebelum proses dimulai
        active_group, path_prefix = _get_active_upload_group_and_prefix()
        logger.info(f"📦 Bulk upload starting — Group {active_group}, prefix='{path_prefix}'")

        try:
            if not self.db:
                raise ValueError("Database session required")

            from app.models.models import Manga
            manga = self.db.query(Manga).filter(Manga.slug == manga_slug).first()
            if not manga:
                raise ValueError(f"Manga '{manga_slug}' tidak ditemukan")

            base_folder_id = manga.storage_source.base_folder_id

            content_dir = self.extract_zip(zip_content, session_id)
            if not content_dir:
                raise ValueError("Gagal extract ZIP file")

            chapters = self.detect_chapters_from_directory(content_dir, naming_pattern)

            if not chapters:
                raise ValueError("Tidak ada folder chapter valid ditemukan dalam ZIP")

            if overrides:
                for ch in chapters:
                    key = str(ch["chapter_main"])
                    if key in overrides:
                        ch.update(overrides[key])

            if start_chapter is not None:
                chapters = [ch for ch in chapters if ch["chapter_main"] >= start_chapter]
            if end_chapter is not None:
                chapters = [ch for ch in chapters if ch["chapter_main"] <= end_chapter]

            if not chapters:
                raise ValueError("Tidak ada chapter yang cocok dengan filter range")

            total_files = sum(ch["file_count"] for ch in chapters)

            if dry_run:
                conflicts = []
                for ch in chapters:
                    if self.check_chapter_conflict(manga.id, ch["chapter_main"], ch.get("chapter_sub", 0)):
                        conflicts.append({
                            "chapter_number": ch["chapter_main"],
                            "chapter_label": ch["chapter_label"],
                            "type": "chapter_exists"
                        })

                self.cleanup_session(session_id)

                return {
                    "dry_run": True,
                    "manga_slug": manga_slug,
                    "storage_group": active_group,
                    "path_prefix": path_prefix,
                    "would_upload": {
                        "total_chapters": len(chapters),
                        "total_files": total_files,
                        "total_size_mb": round(
                            sum(ch["total_size_bytes"] for ch in chapters) / (1024 * 1024), 2
                        ),
                        "chapters": [
                            {
                                "chapter_number": ch["chapter_main"],
                                "chapter_label": ch["chapter_label"],
                                "folder_name": ch["chapter_folder_name"],
                                "file_count": ch["file_count"]
                            }
                            for ch in chapters
                        ]
                    },
                    "conflicts": conflicts,
                    "warnings": [],
                    "can_proceed": len(conflicts) == 0 or conflict_strategy != "error",
                    "note": "Thumbnails 16:9 akan auto-generate untuk setiap chapter"
                }

            self.init_progress(upload_id, len(chapters), total_files, manga_slug)

            results = []
            failed_chapters = []

            if parallel and len(chapters) > 1:
                if self._executor is None:
                    logger.warning("Executor not available, falling back to sequential upload")
                    parallel = False

                if parallel:
                    futures = {}
                    for ch in chapters:
                        if self.check_chapter_conflict(manga.id, ch["chapter_main"], ch.get("chapter_sub", 0)):
                            if conflict_strategy == "skip":
                                results.append({
                                    "chapter_number": ch["chapter_main"],
                                    "chapter_label": ch["chapter_label"],
                                    "status": "skipped",
                                    "reason": "already_exists"
                                })
                                continue
                            elif conflict_strategy == "error":
                                raise ValueError(f"Chapter {ch['chapter_label']} sudah ada")

                        future = self._executor.submit(
                            self._upload_single_chapter,
                            manga_slug, base_folder_id, ch,
                            manga.id, uploader_id, preserve_filenames, upload_id,
                            active_group, path_prefix
                        )
                        futures[future] = ch

                    for future in as_completed(futures):
                        ch = futures[future]
                        result = future.result()
                        results.append(result)

                        if result.get("success"):
                            self.increment_completed_chapters(upload_id, 1)
                        else:
                            failed_chapters.append(ch["chapter_main"])
                            self.increment_failed_chapters(upload_id, 1)

            if not parallel:
                for ch in chapters:
                    if self.check_chapter_conflict(manga.id, ch["chapter_main"], ch.get("chapter_sub", 0)):
                        if conflict_strategy == "skip":
                            results.append({
                                "chapter_number": ch["chapter_main"],
                                "chapter_label": ch["chapter_label"],
                                "status": "skipped",
                                "reason": "already_exists"
                            })
                            continue
                        elif conflict_strategy == "error":
                            resume_token = create_resume_token()
                            with resume_token_lock:
                                resume_token_store[resume_token] = {
                                    "manga_slug": manga_slug,
                                    "remaining_chapters": [
                                        c for c in chapters
                                        if c["chapter_main"] >= ch["chapter_main"]
                                    ],
                                    "completed_results": results,
                                    "uploader_id": uploader_id,
                                    "preserve_filenames": preserve_filenames,
                                    "session_id": session_id,
                                    "active_group": active_group,
                                    "path_prefix": path_prefix,
                                    "created_at": datetime.now(timezone.utc)  # ✅ FIX #3
                                }
                            return {
                                "success": False,
                                "error": f"Chapter {ch['chapter_label']} sudah ada (conflict_strategy=error)",
                                "resume_token": resume_token,
                                "completed_chapters": [r for r in results if r.get("success")],
                                "failed_at": ch["chapter_main"]
                            }

                    result = self._upload_single_chapter(
                        manga_slug, base_folder_id, ch,
                        manga.id, uploader_id, preserve_filenames, upload_id,
                        active_group, path_prefix
                    )
                    results.append(result)

                    if result.get("success"):
                        self.increment_completed_chapters(upload_id, 1)
                    else:
                        failed_chapters.append(ch["chapter_main"])
                        self.increment_failed_chapters(upload_id, 1)

            duration = (datetime.now(timezone.utc) - started_at).total_seconds()  # ✅ FIX #3
            successful = [r for r in results if r.get("success")]

            thumbnails_generated = sum(
                1 for r in successful
                if r.get("thumbnail", {}).get("generated", False)
            )

            self.update_progress(
                upload_id,
                status="completed",
                progress=100,
                completed_at=datetime.now(timezone.utc).isoformat(),  # ✅ FIX #3
                results=results
            )

            return {
                "success": True,
                "upload_id": upload_id,
                "manga_slug": manga_slug,
                "storage_group": active_group,
                "path_prefix": path_prefix,
                "total_chapters_uploaded": len(successful),
                "total_chapters_skipped": len([r for r in results if r.get("status") == "skipped"]),
                "chapters": results,
                "stats": {
                    "total_files": sum(r.get("total_pages", 0) for r in successful),
                    "total_size_mb": round(
                        sum(ch["total_size_bytes"] for ch in chapters) / (1024 * 1024), 2
                    ),
                    "duration_seconds": round(duration, 2),
                    "thumbnails_generated": thumbnails_generated,
                    "thumbnails_fallback": len(successful) - thumbnails_generated
                },
                "failed_chapters": failed_chapters,
                "note": f"{thumbnails_generated}/{len(successful)} chapters with custom 16:9 thumbnails"
            }

        except Exception as e:
            logger.error(f"Bulk upload failed: {str(e)}", exc_info=True)

            try:
                self.update_progress(upload_id, status="failed", error=str(e))
            except:
                pass

            if "results" in locals() and results:
                resume_token = create_resume_token()
                with resume_token_lock:
                    resume_token_store[resume_token] = {
                        "manga_slug": manga_slug,
                        "error": str(e),
                        "partial_results": results,
                        "active_group": active_group,
                        "path_prefix": path_prefix,
                        "created_at": datetime.now(timezone.utc)  # ✅ FIX #3
                    }
                return {
                    "success": False,
                    "error": str(e),
                    "resume_token": resume_token
                }

            return {"success": False, "error": str(e)}

        finally:
            self.cleanup_session(session_id)

    # ==========================================
    # Feature 4: Validate JSON
    # ==========================================

    def validate_json_config(
        self,
        config: Dict,
        check_existing: bool = True
    ) -> Dict:
        """
        Validasi JSON config tanpa upload apapun.
        """
        errors = []
        warnings = []
        validation_results = []

        if "manga_list" in config:
            manga_list = config["manga_list"]
        elif "manga_slug" in config:
            manga_list = [config]
        elif "chapters" in config and "manga_slug" in config:
            manga_list = [config]
        else:
            return {
                "valid": False,
                "errors": ["Config tidak valid: harus memiliki 'manga_list' atau 'manga_slug'"],
                "can_proceed": False
            }

        total_chapters = 0

        for manga_config in manga_list:
            manga_result = {
                "manga_slug": manga_config.get("manga_slug", manga_config.get("slug", "unknown")),
                "status": "ok",
                "warnings": [],
                "chapters": []
            }

            manga_slug = manga_config.get("manga_slug") or manga_config.get("slug")
            if not manga_slug:
                manga_result["status"] = "error"
                errors.append(f"Missing manga_slug")
                validation_results.append(manga_result)
                continue

            if check_existing and self.db:
                from app.models.models import Manga
                existing_manga = self.db.query(Manga).filter(Manga.slug == manga_slug).first()
                if existing_manga:
                    manga_result["warnings"].append("Manga sudah ada, akan di-skip creation")
                    manga_result["manga_id"] = existing_manga.id
                else:
                    if "manga_list" in config:
                        required = ["title", "type_slug", "storage_id"]
                        for field in required:
                            if field not in manga_config:
                                errors.append(f"Manga '{manga_slug}': missing required field '{field}'")
                                manga_result["status"] = "error"

            chapters = manga_config.get("chapters", [])
            total_chapters += len(chapters)

            for ch_config in chapters:
                ch_result = {
                    "chapter_number": ch_config.get("chapter_main", "?"),
                    "status": "ok",
                    "conflicts": []
                }

                required_ch = ["chapter_main", "chapter_folder_name"]
                for field in required_ch:
                    if field not in ch_config:
                        ch_result["status"] = "error"
                        errors.append(f"Chapter missing required field: {field}")

                if check_existing and self.db and "manga_id" in manga_result:
                    from app.models.models import Chapter
                    existing_ch = self.db.query(Chapter).filter(
                        Chapter.manga_id == manga_result["manga_id"],
                        Chapter.chapter_main == ch_config.get("chapter_main"),
                        Chapter.chapter_sub == ch_config.get("chapter_sub", 0)
                    ).first()

                    if existing_ch:
                        ch_result["conflicts"].append({
                            "type": "chapter_exists",
                            "existing_slug": existing_ch.slug
                        })
                        ch_result["status"] = "conflict"

                manga_result["chapters"].append(ch_result)

            validation_results.append(manga_result)

        has_errors = len(errors) > 0
        has_conflicts = any(
            any(ch.get("status") == "conflict" for ch in mr.get("chapters", []))
            for mr in validation_results
        )

        return {
            "valid": not has_errors,
            "summary": {
                "total_manga": len(manga_list),
                "total_chapters": total_chapters,
            },
            "validation_results": validation_results,
            "errors": errors,
            "warnings": warnings,
            "conflicts_found": has_conflicts,
            "can_proceed": not has_errors,
            "note": "Auto-thumbnail 16:9 will be generated for all new chapters"
        }

    # ==========================================
    # Feature 2 & 3: Bulk from JSON
    # ==========================================

    async def bulk_upload_from_json(
        self,
        metadata: Dict,
        zip_content: bytes,
        uploader_id: int,
        conflict_strategy: Dict[str, str] = None,
        dry_run: bool = False
    ) -> Dict:
        """
        Feature 2: Bulk upload chapters dengan JSON metadata + ZIP images + AUTO-THUMBNAILS.

        ✅ FIX #3: All datetime.utcnow() replaced
        ✅ GROUP-AWARE: Detect active group, prefix path @ kalau group 2
        ✅ PERF FIX: Batch folder upload per chapter
        """
        if conflict_strategy is None:
            conflict_strategy = {"on_manga_exists": "skip", "on_chapter_exists": "skip"}

        upload_id = create_upload_id()
        session_id = create_upload_id()
        started_at = datetime.now(timezone.utc)  # ✅ FIX #3
        results = []

        active_group, path_prefix = _get_active_upload_group_and_prefix()
        logger.info(f"📦 Bulk JSON upload starting — Group {active_group}, prefix='{path_prefix}'")

        try:
            content_dir = self.extract_zip(zip_content, session_id)
            if not content_dir:
                raise ValueError("Gagal extract ZIP file")

            manga_slug = metadata.get("manga_slug")
            if not manga_slug:
                raise ValueError("metadata.manga_slug required")

            from app.models.models import Manga
            manga = self.db.query(Manga).filter(Manga.slug == manga_slug).first()
            if not manga:
                raise ValueError(f"Manga '{manga_slug}' tidak ditemukan")

            base_folder_id = manga.storage_source.base_folder_id
            chapters_config = metadata.get("chapters", [])

            if dry_run:
                self.cleanup_session(session_id)
                return {
                    "dry_run": True,
                    "manga_slug": manga_slug,
                    "storage_group": active_group,
                    "path_prefix": path_prefix,
                    "would_process": len(chapters_config),
                    "chapters": chapters_config,
                    "note": "Thumbnails 16:9 akan auto-generate untuk setiap chapter"
                }

            total_files = 0
            chapters_to_process = []

            for ch_config in chapters_config:
                folder_name = ch_config.get("chapter_folder_name")
                if not folder_name:
                    continue

                chapter_dir = content_dir / folder_name
                if not chapter_dir.exists():
                    chapter_dir = content_dir / manga_slug / folder_name

                if not chapter_dir.exists():
                    logger.warning(f"Folder not found: {folder_name}")
                    results.append({
                        "chapter_label": ch_config.get("chapter_label", folder_name),
                        "status": "failed",
                        "error": f"Folder '{folder_name}' tidak ditemukan dalam ZIP"
                    })
                    continue

                image_files = sorted([
                    f for f in chapter_dir.iterdir()
                    if f.is_file() and f.suffix.lower() in self.ALLOWED_IMAGE_EXTS
                ], key=lambda x: NaturalSorter.extract_numbers(x.name))

                if not image_files:
                    continue

                ch_info = {
                    "chapter_main": ch_config.get("chapter_main", 0),
                    "chapter_sub": ch_config.get("chapter_sub", 0),
                    "chapter_label": ch_config.get("chapter_label", f"Chapter {ch_config.get('chapter_main', '?')}"),
                    "chapter_folder_name": folder_name,
                    "files": image_files,
                    "file_count": len(image_files),
                    "total_size_bytes": sum(f.stat().st_size for f in image_files)
                }

                chapters_to_process.append(ch_info)
                total_files += len(image_files)

            self.init_progress(upload_id, len(chapters_to_process), total_files, manga_slug)

            thumbnails_generated = 0

            for ch in chapters_to_process:
                if self.check_chapter_conflict(manga.id, ch["chapter_main"], ch.get("chapter_sub", 0)):
                    strategy = conflict_strategy.get("on_chapter_exists", "skip")
                    if strategy == "skip":
                        results.append({
                            "chapter_label": ch["chapter_label"],
                            "status": "skipped",
                            "reason": "chapter_exists"
                        })
                        continue
                    elif strategy == "error":
                        raise ValueError(f"Chapter {ch['chapter_label']} sudah ada")

                result = self._upload_single_chapter(
                    manga_slug, base_folder_id, ch,
                    manga.id, uploader_id, False, upload_id,
                    active_group, path_prefix
                )
                results.append(result)

                if result.get("success") and result.get("thumbnail", {}).get("generated"):
                    thumbnails_generated += 1

            duration = (datetime.now(timezone.utc) - started_at).total_seconds()  # ✅ FIX #3
            successful = [r for r in results if r.get("success")]

            return {
                "success": True,
                "upload_id": upload_id,
                "manga_slug": manga_slug,
                "storage_group": active_group,
                "path_prefix": path_prefix,
                "total_chapters": len(results),
                "uploaded_chapters": successful,
                "stats": {
                    "total_files": sum(r.get("total_pages", 0) for r in successful),
                    "duration_seconds": round(duration, 2),
                    "thumbnails_generated": thumbnails_generated,
                    "thumbnails_fallback": len(successful) - thumbnails_generated
                },
                "errors": [r for r in results if not r.get("success") and r.get("status") != "skipped"],
                "note": f"{thumbnails_generated}/{len(successful)} chapters with custom 16:9 thumbnails"
            }

        except Exception as e:
            logger.error(f"Bulk JSON upload failed: {str(e)}", exc_info=True)
            if self.db:
                self.db.rollback()
            return {"success": False, "error": str(e), "partial_results": results}

        finally:
            self.cleanup_session(session_id)

    async def bulk_upload_multiple_manga(
        self,
        config: Dict,
        zip_content: bytes,
        uploader_id: int,
        conflict_strategy: Dict[str, str] = None,
        dry_run: bool = False
    ) -> Dict:
        """
        Feature 3: Upload multiple manga sekaligus dari JSON + ZIP + AUTO-THUMBNAILS.

        ✅ FIX #3: All datetime.utcnow() replaced
        ✅ GROUP-AWARE: Detect active group, prefix path @ kalau group 2
        ✅ PERF FIX: Batch folder upload per chapter
        """
        if conflict_strategy is None:
            conflict_strategy = {"on_manga_exists": "skip", "on_chapter_exists": "skip"}

        session_id = create_upload_id()
        started_at = datetime.now(timezone.utc)  # ✅ FIX #3
        manga_results = []

        active_group, path_prefix = _get_active_upload_group_and_prefix()
        logger.info(f"📦 Bulk multiple manga upload starting — Group {active_group}, prefix='{path_prefix}'")

        try:
            manga_list_config = config.get("manga_list", [])
            if not manga_list_config:
                raise ValueError("Config harus memiliki 'manga_list'")

            content_dir = self.extract_zip(zip_content, session_id)
            if not content_dir:
                raise ValueError("Gagal extract ZIP file")

            for manga_config in manga_list_config:
                manga_slug = manga_config.get("slug")
                manga_title = manga_config.get("title")

                manga_result = {
                    "manga_slug": manga_slug,
                    "manga_title": manga_title,
                    "status": "processing",
                    "manga_id": None,
                    "chapters_created": 0,
                    "thumbnails_generated": 0,
                    "storage_group": active_group,
                    "path_prefix": path_prefix,
                    "errors": []
                }

                try:
                    from app.models.models import Manga, MangaType, Genre, MangaAltTitle
                    from app.models.models import StorageSource

                    existing_manga = self.db.query(Manga).filter(
                        Manga.slug == manga_slug
                    ).first()

                    if existing_manga:
                        on_manga_exists = conflict_strategy.get("on_manga_exists", "skip")
                        if on_manga_exists == "skip":
                            manga = existing_manga
                            manga_result["status"] = "manga_existed"
                        elif on_manga_exists == "error":
                            manga_result["status"] = "failed"
                            manga_result["errors"].append(f"Manga '{manga_slug}' sudah ada")
                            manga_results.append(manga_result)
                            continue
                        else:
                            manga = existing_manga
                    else:
                        storage_id = manga_config.get("storage_id", 1)
                        storage = self.db.query(StorageSource).filter(
                            StorageSource.id == storage_id
                        ).first()

                        if not storage:
                            raise ValueError(f"Storage ID {storage_id} tidak ditemukan")

                        type_slug = manga_config.get("type_slug", "manga")
                        manga_type = self.db.query(MangaType).filter(
                            MangaType.slug == type_slug
                        ).first()

                        if not manga_type:
                            raise ValueError(f"Type '{type_slug}' tidak ditemukan")

                        manga = Manga(
                            title=manga_title,
                            slug=manga_slug,
                            storage_id=storage.id,
                            type_id=manga_type.id,
                            status=manga_config.get("status", "ongoing")
                        )

                        genre_slugs = manga_config.get("genre_slugs", [])
                        if genre_slugs:
                            genres = self.db.query(Genre).filter(
                                Genre.slug.in_(genre_slugs)
                            ).all()
                            manga.genres = genres

                        self.db.add(manga)
                        self.db.flush()
                        manga_result["status"] = "manga_created"

                    manga_result["manga_id"] = manga.id
                    base_folder_id = manga.storage_source.base_folder_id

                    chapters_config = manga_config.get("chapters", [])

                    if dry_run:
                        manga_result["would_create_chapters"] = len(chapters_config)
                        manga_result["status"] = "dry_run_ok"
                        manga_result["note"] = "Auto-thumbnail 16:9 akan generate untuk setiap chapter"
                        manga_results.append(manga_result)
                        self.db.rollback()
                        continue

                    for ch_config in chapters_config:
                        folder_name = ch_config.get("chapter_folder_name")

                        chapter_dir = content_dir / manga_slug / folder_name
                        if not chapter_dir.exists():
                            chapter_dir = content_dir / folder_name

                        if not chapter_dir.exists():
                            manga_result["errors"].append(f"Folder '{folder_name}' tidak ditemukan")
                            continue

                        image_files = sorted([
                            f for f in chapter_dir.iterdir()
                            if f.is_file() and f.suffix.lower() in self.ALLOWED_IMAGE_EXTS
                        ], key=lambda x: NaturalSorter.extract_numbers(x.name))

                        if not image_files:
                            continue

                        ch_main = ch_config.get("chapter_main", 1)
                        ch_sub = ch_config.get("chapter_sub", 0)
                        ch_label = ch_config.get(
                            "chapter_label",
                            f"Chapter {ch_main}" + (f".{ch_sub}" if ch_sub else "")
                        )

                        ch_info = {
                            "chapter_main": ch_main,
                            "chapter_sub": ch_sub,
                            "chapter_label": ch_label,
                            "chapter_folder_name": folder_name,
                            "files": image_files,
                            "file_count": len(image_files),
                            "total_size_bytes": sum(f.stat().st_size for f in image_files)
                        }

                        # ✅ GROUP-AWARE: pass active_group dan path_prefix
                        result = self._upload_single_chapter(
                            manga_slug, base_folder_id, ch_info,
                            manga.id, uploader_id,
                            upload_id=None,
                            active_group=active_group,
                            path_prefix=path_prefix
                        )

                        if result["success"]:
                            manga_result["chapters_created"] += 1
                            if result.get("thumbnail", {}).get("generated"):
                                manga_result["thumbnails_generated"] += 1
                        else:
                            manga_result["errors"].append(result.get("error", "Unknown error"))

                    if manga_result["status"] == "processing":
                        manga_result["status"] = "success"

                    self.db.commit()

                except Exception as e:
                    self.db.rollback()
                    manga_result["status"] = "failed"
                    manga_result["errors"].append(str(e))
                    logger.error(f"Failed to process manga {manga_slug}: {str(e)}")

                manga_results.append(manga_result)

            duration = (datetime.now(timezone.utc) - started_at).total_seconds()  # ✅ FIX #3
            successful = [m for m in manga_results if m["status"] in ["success", "manga_existed", "manga_created"]]
            total_thumbnails = sum(m.get("thumbnails_generated", 0) for m in successful)
            total_chapters = sum(m.get("chapters_created", 0) for m in successful)

            return {
                "success": True,
                "dry_run": dry_run,
                "total_manga": len(manga_list_config),
                "total_chapters": total_chapters,
                "total_thumbnails_generated": total_thumbnails,
                "results": manga_results,
                "stats": {
                    "duration_seconds": round(duration, 2),
                    "thumbnail_success_rate": f"{(total_thumbnails/total_chapters*100) if total_chapters > 0 else 0:.1f}%"
                },
                "failed": [m for m in manga_results if m["status"] == "failed"]
            }

        except Exception as e:
            logger.error(f"Bulk manga upload failed: {str(e)}", exc_info=True)
            if self.db:
                self.db.rollback()
            return {"success": False, "error": str(e), "partial_results": manga_results}

        finally:
            self.cleanup_session(session_id)

    # ==========================================
    # Feature 7: Resume
    # ==========================================

    async def resume_upload(self, resume_token: str, uploader_id: int) -> Dict:
        """
        Feature 7: Resume upload yang gagal.

        ✅ FIX #3: All datetime.utcnow() replaced
        """
        with resume_token_lock:
            token_data = resume_token_store.get(resume_token)

        if not token_data:
            raise ValueError(f"Resume token tidak valid atau sudah expired: {resume_token}")

        manga_slug = token_data.get("manga_slug")
        remaining_chapters = token_data.get("remaining_chapters", [])

        if not remaining_chapters:
            return {
                "success": False,
                "error": "Tidak ada chapter yang bisa di-resume"
            }

        with resume_token_lock:
            if resume_token in resume_token_store:
                del resume_token_store[resume_token]

        from app.models.models import Manga
        manga = self.db.query(Manga).filter(Manga.slug == manga_slug).first()
        if not manga:
            raise ValueError(f"Manga '{manga_slug}' tidak ditemukan")

        base_folder_id = manga.storage_source.base_folder_id
        upload_id = create_upload_id()
        results = list(token_data.get("completed_results", []))

        # ✅ GROUP-AWARE: restore group dari token
        active_group = token_data.get("active_group", 1)
        path_prefix = token_data.get("path_prefix", "")

        self.init_progress(
            upload_id,
            len(remaining_chapters),
            sum(ch.get("file_count", 0) for ch in remaining_chapters),
            manga_slug
        )

        thumbnails_generated = 0

        for ch in remaining_chapters:
            result = self._upload_single_chapter(
                manga_slug, base_folder_id, ch,
                manga.id, uploader_id,
                token_data.get("preserve_filenames", False),
                upload_id,
                active_group=active_group,
                path_prefix=path_prefix
            )
            results.append(result)

            if result.get("success") and result.get("thumbnail", {}).get("generated"):
                thumbnails_generated += 1

        successful = [r for r in results if r.get("success")]

        return {
            "success": True,
            "upload_id": upload_id,
            "resumed": True,
            "manga_slug": manga_slug,
            "total_chapters": len(results),
            "successful_chapters": len(successful),
            "thumbnails_generated": thumbnails_generated,
            "chapters": results,
            "note": f"{thumbnails_generated}/{len(successful)} chapters with custom 16:9 thumbnails"
        }