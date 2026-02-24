# File: app/services/upload_service.py
"""
Upload Service - MEMORY LEAK FIXED + GROUP-AWARE
===================================
Service untuk handle upload images ke Google Drive.

FIXES APPLIED:
✅ Reusable ThreadPoolExecutor (FIXED MEMORY LEAK)
✅ Proper executor shutdown dengan atexit
✅ Graceful shutdown on SIGTERM/SIGINT
✅ Thread-safe executor management
✅ Better error handling in background tasks
✅ Resource cleanup on class destruction
✅ FIX #5: Signal handler registration wrapped in try/except
           signal.signal() hanya bisa dipanggil dari MAIN THREAD.
           Jika UploadService di-instantiate dari background thread
           (misal dari BackgroundTasks atau ThreadPoolExecutor),
           signal.signal() akan raise ValueError.
           bulk_upload_service.py sudah handle ini, tapi file ini BELUM.

✅ GROUP-AWARE UPLOAD (NEW!):
   - Cek active upload group dari MultiRemoteService
   - Kalau group 1 → upload ke primary remote, simpan path normal
   - Kalau group 2 → upload ke RCLONE_NEXT_PRIMARY_REMOTE, simpan path dengan prefix '@'
   - Prefix '@' di DB path = marker bahwa file ada di group 2 remote
"""

import logging
import time
import asyncio
import atexit
import signal
import threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from app.core.base import settings
from app.services.rclone_service import RcloneService
from app.services.natural_sorter import NaturalSorter

logger = logging.getLogger(__name__)


class UploadService:
    """
    Service untuk handle upload images ke Google Drive.

    ✅ MEMORY-SAFE VERSION with reusable executor and proper cleanup
    ✅ GROUP-AWARE: Upload ke group 1 atau group 2 sesuai active group
    """

    # Class-level executor (shared across instances)
    _executor: Optional[ThreadPoolExecutor] = None
    _executor_lock = threading.Lock()
    _executor_refcount = 0
    _shutdown_registered = False

    # Temporary upload directory
    TEMP_UPLOAD_DIR = Path(settings.RCLONE_CACHE_DIR) / "uploads"

    # Allowed image types
    ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
    ALLOWED_MIMETYPES = {'image/jpeg', 'image/png', 'image/webp'}

    # Max file size (MB)
    MAX_FILE_SIZE_MB = 10

    def __init__(self):
        """Initialize upload service with shared executor."""
        self._ensure_upload_dir()

        # ✅ PRIMARY remote untuk upload (group 1 default)
        self.primary_rclone = RcloneService()
        self.primary_remote_name = settings.get_primary_remote()
        self.primary_rclone.remote_name = self.primary_remote_name

        # ✅ SECONDARY remotes untuk mirror (group 1)
        self.secondary_remotes: Dict[str, RcloneService] = {}

        if settings.is_multi_remote_enabled:
            for remote_name in settings.get_secondary_remotes():
                try:
                    rclone = RcloneService()
                    rclone.remote_name = remote_name

                    if rclone.test_connection():
                        self.secondary_remotes[remote_name] = rclone
                        logger.info(f"✅ Secondary remote '{remote_name}' initialized for mirroring")
                    else:
                        logger.warning(f"⚠️ Secondary remote '{remote_name}' failed connection test")
                except Exception as e:
                    logger.error(f"❌ Failed to initialize secondary remote '{remote_name}': {str(e)}")

        # ✅ GROUP-AWARE: Setup group 2 remote jika dikonfigurasi
        # Group 2 rclone instance, hanya dibuat jika RCLONE_NEXT_PRIMARY_REMOTE ada
        self._group2_rclone: Optional[RcloneService] = None
        self._group2_remote_name: Optional[str] = None

        if settings.is_next_group_configured:
            try:
                next_primary = settings.get_next_primary_remote()
                if next_primary:
                    self._group2_rclone = RcloneService(remote_name=next_primary)
                    self._group2_remote_name = next_primary
                    logger.info(f"✅ Group 2 primary remote '{next_primary}' initialized")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize group 2 remote: {str(e)}")

        # ✅ FIX: Get or create shared executor (prevents memory leak)
        self._acquire_executor()

        logger.info(
            f"UploadService initialized - Primary: {self.primary_remote_name}, "
            f"Secondaries: {list(self.secondary_remotes.keys())}, "
            f"Mirror enabled: {settings.is_mirror_upload_enabled}, "
            f"Group 2 remote: {self._group2_remote_name or 'not configured'}"
        )

    # ==========================================
    # ✅ GROUP-AWARE HELPERS (NEW!)
    # ==========================================

    def _get_active_group(self) -> int:
        """
        Get active upload group dari MultiRemoteService (atau settings fallback).

        Returns:
            1 = upload ke group 1 (path normal)
            2 = upload ke group 2 (path dengan prefix '@')
        """
        try:
            # Import di sini untuk avoid circular import
            from app.services.multi_remote_service import MultiRemoteService
            instance = MultiRemoteService._global_instance
            if instance and instance.is_initialized:
                return instance.get_active_upload_group()
        except Exception as e:
            logger.debug(f"Cannot get active group from MultiRemoteService: {e}")

        # Fallback ke settings
        return settings.get_active_upload_group()

    def _get_active_group_rclone(self) -> Tuple[int, str, RcloneService]:
        """
        Get (group, remote_name, rclone_instance) untuk active upload group.

        Returns:
            Tuple of (group_number, remote_name, rclone_instance)
            - group 1 → primary remote
            - group 2 → next primary remote (jika dikonfigurasi), fallback group 1
        """
        active_group = self._get_active_group()

        if active_group == 2 and self._group2_rclone is not None:
            logger.debug(f"Using Group 2 remote: {self._group2_remote_name}")
            return 2, self._group2_remote_name, self._group2_rclone
        else:
            if active_group == 2:
                logger.warning(
                    "Active group is 2 but no group 2 remote configured, "
                    "falling back to group 1"
                )
            return 1, self.primary_remote_name, self.primary_rclone

    def _make_db_path(self, clean_path: str, group: int) -> str:
        """
        Buat path untuk disimpan ke DB, tambah prefix '@' jika group 2.

        Args:
            clean_path: Path bersih tanpa prefix (actual rclone path)
            group: 1 atau 2

        Returns:
            Path untuk DB: clean jika group 1, '@clean' jika group 2
        """
        if group == 2:
            return settings.make_group2_path(clean_path)
        return clean_path

    # ==========================================
    # ✅ FIX #5: Safe signal handler registration
    #
    # SEBELUMNYA (CRASH jika bukan main thread):
    #   signal.signal(signal.SIGTERM, cls._signal_handler)
    #   signal.signal(signal.SIGINT, cls._signal_handler)
    #
    # MASALAH:
    #   signal.signal() HANYA bisa dipanggil dari MAIN THREAD.
    #   Jika UploadService di-instantiate dari:
    #   - FastAPI BackgroundTasks
    #   - ThreadPoolExecutor worker thread
    #   - Uvicorn worker process
    #   Maka signal.signal() akan raise ValueError:
    #     "signal only works in main thread of the main interpreter"
    #   Dan ini menyebabkan CRASH tanpa ada fallback.
    #
    # SOLUSI:
    #   Wrap signal.signal() dalam try/except (ValueError, OSError)
    #   Sama seperti yang sudah dilakukan di bulk_upload_service.py.
    #   Jika gagal register signal handler, cleanup tetap dijamin
    #   oleh atexit.register() yang bekerja di semua thread.
    # ==========================================
    @classmethod
    def _acquire_executor(cls):
        """
        ✅ Thread-safe executor acquisition with reference counting.

        Creates executor once and reuses across all instances.
        """
        with cls._executor_lock:
            if cls._executor is None:
                logger.info("🚀 Creating shared ThreadPoolExecutor for mirror tasks")
                cls._executor = ThreadPoolExecutor(
                    max_workers=3,
                    thread_name_prefix="mirror-worker-"
                )

                # ✅ Register cleanup handlers (only once)
                if not cls._shutdown_registered:
                    atexit.register(cls._shutdown_executor)

                    # ✅ FIX #5: Safe signal registration with try/except
                    # signal.signal() raises ValueError jika dipanggil dari
                    # non-main thread. atexit tetap menjamin cleanup.
                    try:
                        signal.signal(signal.SIGTERM, cls._signal_handler)
                        signal.signal(signal.SIGINT, cls._signal_handler)
                    except (ValueError, OSError):
                        # ValueError: "signal only works in main thread"
                        # OSError: signal handling not supported (rare edge case)
                        logger.warning(
                            "Cannot register signal handlers for UploadService executor "
                            "(not main thread). Cleanup will still happen via atexit."
                        )

                    cls._shutdown_registered = True
                    logger.info("✅ Registered shutdown handlers for executor")

            cls._executor_refcount += 1
            logger.debug(f"Executor acquired (refcount: {cls._executor_refcount})")

    @classmethod
    def _release_executor(cls):
        """
        ✅ Thread-safe executor release with reference counting.

        Only shutdowns executor when no instances remain.
        """
        with cls._executor_lock:
            cls._executor_refcount -= 1
            logger.debug(f"Executor released (refcount: {cls._executor_refcount})")

            # Don't shutdown here - let atexit handle it
            # This prevents premature shutdown

    @classmethod
    def _shutdown_executor(cls):
        """
        ✅ Gracefully shutdown shared executor.

        Called by atexit or signal handlers.
        """
        with cls._executor_lock:
            if cls._executor is not None:
                logger.info("🛑 Shutting down shared ThreadPoolExecutor...")

                try:
                    # Give running tasks 30 seconds to complete
                    cls._executor.shutdown(wait=True, cancel_futures=False)
                    logger.info("✅ Executor shutdown complete (graceful)")
                except Exception as e:
                    logger.error(f"Error during executor shutdown: {str(e)}")
                    # Force shutdown if graceful fails
                    try:
                        cls._executor.shutdown(wait=False, cancel_futures=True)
                        logger.warning("⚠️ Executor shutdown complete (forced)")
                    except Exception:
                        pass
                finally:
                    cls._executor = None
                    cls._executor_refcount = 0

    @classmethod
    def _signal_handler(cls, signum, frame):
        """
        ✅ Handle termination signals gracefully.
        """
        logger.info(f"Received signal {signum}, shutting down executor...")
        cls._shutdown_executor()
        # Re-raise signal for proper termination
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    def __del__(self):
        """✅ Cleanup on instance destruction."""
        try:
            self._release_executor()
        except Exception as e:
            logger.error(f"Error in __del__: {str(e)}")

    def _ensure_upload_dir(self):
        """Ensure temporary upload directory exists."""
        self.TEMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Upload directory: {self.TEMP_UPLOAD_DIR.absolute()}")

    def validate_image(self, filename: str, file_size: int) -> Tuple[bool, Optional[str]]:
        """Validate uploaded image file."""
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.ALLOWED_EXTENSIONS:
            return False, f"Invalid extension. Allowed: {', '.join(self.ALLOWED_EXTENSIONS)}"

        file_size_mb = file_size / (1024 * 1024)
        if file_size_mb > self.MAX_FILE_SIZE_MB:
            return False, f"File terlalu besar. Max: {self.MAX_FILE_SIZE_MB}MB"

        return True, None

    def validate_mimetype(self, content_type: str) -> bool:
        return content_type in self.ALLOWED_MIMETYPES

    async def save_temp_file(self, file_content: bytes, filename: str) -> Path:
        """Save uploaded file ke temporary directory."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_filename = f"{timestamp}_{filename}"
        temp_path = self.TEMP_UPLOAD_DIR / safe_filename

        with open(temp_path, 'wb') as f:
            f.write(file_content)

        logger.info(f"Saved temporary file: {temp_path}")
        return temp_path

    def create_gdrive_folder_structure(
        self,
        base_folder_id: str,
        manga_slug: str,
        chapter_folder_name: str
    ) -> Optional[str]:
        """
        Create folder structure di active group remote.

        ✅ GROUP-AWARE: Buat folder di group 1 atau group 2 sesuai active group.
        Return clean path (tanpa prefix @) — prefix ditambah nanti saat simpan ke DB.
        """
        try:
            # ✅ NEW: Get active group remote
            active_group, remote_name, rclone = self._get_active_group_rclone()

            manga_folder_path = f"{base_folder_id}/{manga_slug}"
            chapter_folder_path = f"{manga_folder_path}/{chapter_folder_name}"

            logger.info(
                f"Creating folder structure in Group {active_group} remote '{remote_name}': "
                f"{chapter_folder_path}"
            )

            # Buat manga folder
            manga_result = rclone._run_command([
                "mkdir",
                f"{remote_name}:{manga_folder_path}"
            ])
            if manga_result.returncode == 0:
                logger.info(f"Manga folder ready in G{active_group}: {manga_folder_path}")
            else:
                logger.warning(
                    f"Manga folder mkdir warning (mungkin sudah ada): {manga_result.stderr}"
                )

            # Buat chapter folder
            chapter_result = rclone._run_command([
                "mkdir",
                f"{remote_name}:{chapter_folder_path}"
            ])

            if chapter_result.returncode == 0:
                logger.info(
                    f"✅ Chapter folder created in G{active_group}: {chapter_folder_path}"
                )
                return chapter_folder_path

            # Cek apakah sudah ada
            verified_path = rclone.construct_chapter_folder_path(
                base_folder_id, manga_slug, chapter_folder_name
            )

            if verified_path:
                logger.info(
                    f"Chapter folder already exists in G{active_group}: {chapter_folder_path}"
                )
                return chapter_folder_path

            logger.error(
                f"Failed to create/verify chapter folder in G{active_group}: "
                f"{chapter_folder_path}"
            )
            return None

        except Exception as e:
            logger.error(f"Error creating folder structure: {str(e)}", exc_info=True)
            return None

    def upload_images_to_gdrive(
        self,
        temp_files: List[Path],
        gdrive_folder_path: str,
        preserve_names: bool = False
    ) -> List[Dict[str, str]]:
        """
        Upload multiple images ke active group remote.

        ✅ GROUP-AWARE:
        - Upload ke group 1 atau group 2 sesuai active group
        - gdrive_path dalam return value adalah CLEAN path (tanpa prefix @)
        - Caller (process_chapter_upload) yang akan tambah prefix @ ke DB path
          via _make_db_path()

        Note: gdrive_folder_path harus CLEAN path (tanpa @) karena
        rclone tidak mengenal prefix @.
        """
        uploaded_files = []

        # ✅ NEW: Get active group remote
        active_group, remote_name, rclone = self._get_active_group_rclone()

        # Sort files naturally
        sorted_files = sorted(
            temp_files,
            key=lambda x: NaturalSorter.extract_numbers(x.name)
        )

        for index, temp_file in enumerate(sorted_files, start=1):
            try:
                if preserve_names:
                    target_filename = temp_file.name
                else:
                    extension = temp_file.suffix.lower()
                    target_filename = f"{index:03d}{extension}"

                # clean_gdrive_path = path yang actual di rclone (tanpa prefix @)
                clean_gdrive_path = f"{gdrive_folder_path}/{target_filename}"
                remote_path = f"{remote_name}:{clean_gdrive_path}"

                logger.info(
                    f"Uploading {temp_file.name} → {clean_gdrive_path} "
                    f"(Group {active_group}: {remote_name})"
                )

                result = rclone._run_command([
                    "copyto",
                    str(temp_file),
                    remote_path,
                    "--progress"
                ], timeout=120)

                if result.returncode == 0:
                    # ✅ Simpan clean_gdrive_path dulu
                    # DB path (dengan/tanpa @) ditentukan oleh caller
                    uploaded_files.append({
                        "original_name": temp_file.name,
                        "gdrive_path": clean_gdrive_path,  # CLEAN path tanpa @
                        "page_order": index,
                        "size": temp_file.stat().st_size,
                        "_active_group": active_group  # Info group untuk caller
                    })
                    logger.info(f"✅ Uploaded to G{active_group} '{remote_name}': {target_filename}")
                else:
                    logger.error(
                        f"❌ Failed to upload to G{active_group} '{remote_name}': "
                        f"{temp_file.name} - {result.stderr}"
                    )

            except Exception as e:
                logger.error(f"Error uploading {temp_file.name}: {str(e)}", exc_info=True)

        return uploaded_files

    def mirror_folder_to_secondaries(
        self,
        gdrive_folder_path: str,
        background: bool = True
    ) -> Dict[str, bool]:
        """
        Mirror folder dari PRIMARY ke SECONDARY remotes.

        ✅ GROUP-AWARE: Mirror sesuai active group.
        - Group 1 → mirror ke RCLONE_BACKUP_REMOTES
        - Group 2 → mirror ke RCLONE_NEXT_BACKUP_REMOTES

        Note: gdrive_folder_path harus CLEAN path (tanpa @).
        """
        if not settings.is_mirror_upload_enabled:
            logger.info("Mirror upload disabled, skipping")
            return {}

        # ✅ NEW: Tentukan secondary remotes berdasarkan active group
        active_group, primary_remote_name, primary_rclone = self._get_active_group_rclone()

        if active_group == 2:
            backup_remotes = settings.get_next_backup_remotes()
        else:
            backup_remotes = settings.get_secondary_remotes()

        if not backup_remotes:
            logger.info(f"No secondary remotes for Group {active_group}, skipping mirror")
            return {}

        logger.info(
            f"🔄 Mirroring folder '{gdrive_folder_path}' from Group {active_group} "
            f"primary '{primary_remote_name}' to {len(backup_remotes)} backup remotes"
        )

        # Delay untuk beri waktu upload selesai & flush
        if background and settings.RCLONE_AUTO_COPY_DELAY > 0:
            logger.info(f"Waiting {settings.RCLONE_AUTO_COPY_DELAY}s before mirroring...")
            time.sleep(settings.RCLONE_AUTO_COPY_DELAY)

        mirror_results = {}

        # Source path (dari primary group yang aktif)
        source_path = f"{primary_remote_name}:{gdrive_folder_path}"

        # Mirror ke semua backup remotes group yang aktif
        for backup_remote_name in backup_remotes:
            try:
                dest_path = f"{backup_remote_name}:{gdrive_folder_path}"

                logger.info(f"Mirroring: {source_path} → {dest_path}")

                result = primary_rclone._run_command([
                    "copy",
                    source_path,
                    dest_path,
                    "--create-empty-src-dirs",
                    "--progress",
                    "--transfers=4",
                    "--checkers=8"
                ], timeout=300)

                if result.returncode == 0:
                    mirror_results[backup_remote_name] = True
                    logger.info(f"✅ Mirrored to '{backup_remote_name}' successfully")
                else:
                    mirror_results[backup_remote_name] = False
                    logger.error(
                        f"❌ Failed to mirror to '{backup_remote_name}': {result.stderr}"
                    )

            except Exception as e:
                mirror_results[backup_remote_name] = False
                logger.error(
                    f"❌ Exception mirroring to '{backup_remote_name}': {str(e)}",
                    exc_info=True
                )

        success_count = sum(1 for v in mirror_results.values() if v)
        logger.info(
            f"🎉 Mirror completed: {success_count}/{len(backup_remotes)} "
            f"Group {active_group} backups successful"
        )

        return mirror_results

    async def mirror_folder_async(self, gdrive_folder_path: str):
        """
        ✅ FIX: Async wrapper dengan shared reusable executor.

        Prevents memory leak by reusing class-level executor.
        Note: gdrive_folder_path harus CLEAN path (tanpa @).
        """
        if self._executor is None:
            logger.warning("Executor not available, skipping mirror")
            return

        loop = asyncio.get_event_loop()

        try:
            # ✅ Use shared reusable executor (MEMORY LEAK FIXED!)
            await loop.run_in_executor(
                self._executor,
                self.mirror_folder_to_secondaries,
                gdrive_folder_path,
                False  # background=False karena sudah di-async
            )
        except Exception as e:
            logger.error(f"❌ Background mirror failed: {str(e)}", exc_info=True)

    def cleanup_temp_files(self, temp_files: List[Path]):
        """Clean up temporary files setelah upload."""
        for temp_file in temp_files:
            try:
                if temp_file.exists():
                    temp_file.unlink()
                    logger.info(f"Deleted temp file: {temp_file}")
            except Exception as e:
                logger.error(f"Error deleting temp file {temp_file}: {str(e)}")

    def get_upload_stats(self, uploaded_files: List[Dict]) -> Dict:
        """Get statistics of uploaded files."""
        total_size = sum(f['size'] for f in uploaded_files)
        return {
            "total_files": len(uploaded_files),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "files": [
                {
                    "page_order": f['page_order'],
                    "filename": Path(f['gdrive_path']).name,
                    "size_kb": round(f['size'] / 1024, 2)
                }
                for f in uploaded_files
            ]
        }

    async def process_chapter_upload(
        self,
        manga_slug: str,
        chapter_folder_name: str,
        base_folder_id: str,
        files: List[Tuple[bytes, str]],
        preserve_filenames: bool = False,
        enable_mirror: bool = True
    ) -> Dict:
        """
        Process complete chapter upload workflow WITH AUTO-MIRROR.

        ✅ GROUP-AWARE:
        - Detect active group
        - Upload ke group yang aktif
        - Return uploaded_files dengan gdrive_path yang sudah di-prefix @
          jika group 2 (untuk disimpan ke DB)
        - gdrive_folder_path di return value juga include prefix @ untuk group 2
        """
        temp_files = []
        uploaded_files = []
        mirror_results = {}

        try:
            logger.info(f"Processing {len(files)} files for upload")

            # ✅ Detect active group di awal proses
            active_group, remote_name, _ = self._get_active_group_rclone()
            logger.info(
                f"Active upload group: {active_group} (remote: {remote_name})"
            )

            # Step 1: Validate dan save temporary files
            for file_content, filename in files:
                is_valid, error_msg = self.validate_image(filename, len(file_content))
                if not is_valid:
                    raise ValueError(f"Validation failed for {filename}: {error_msg}")

                temp_path = await self.save_temp_file(file_content, filename)
                temp_files.append(temp_path)

            logger.info(f"Saved {len(temp_files)} files to temporary directory")

            # Step 2: Create folder structure di active group remote
            # clean_gdrive_folder_path = path tanpa prefix @
            clean_gdrive_folder_path = self.create_gdrive_folder_structure(
                base_folder_id, manga_slug, chapter_folder_name
            )

            if not clean_gdrive_folder_path:
                raise Exception(
                    f"Gagal membuat folder di Group {active_group} remote '{remote_name}'"
                )

            # Step 3: Upload ke active group remote
            # uploaded_files berisi clean_gdrive_path (tanpa @)
            raw_uploaded_files = self.upload_images_to_gdrive(
                temp_files, clean_gdrive_folder_path, preserve_filenames
            )

            if not raw_uploaded_files:
                raise Exception(
                    f"Tidak ada file yang berhasil diupload ke Group {active_group}"
                )

            # ✅ NEW: Konversi clean_gdrive_path → db_gdrive_path
            # Tambah prefix @ jika group 2 sebelum return ke caller
            for file_info in raw_uploaded_files:
                clean_path = file_info["gdrive_path"]
                db_path = self._make_db_path(clean_path, active_group)

                uploaded_files.append({
                    "original_name": file_info["original_name"],
                    "gdrive_path": db_path,  # DB path: '@...' jika group 2
                    "page_order": file_info["page_order"],
                    "size": file_info["size"],
                })

            # Step 4: Get stats
            stats = self.get_upload_stats(uploaded_files)

            logger.info(
                f"✅ Upload to Group {active_group} complete: "
                f"{stats['total_files']} files, {stats['total_size_mb']}MB"
            )

            # ✅ Step 5: AUTO-MIRROR ke secondaries (BACKGROUND TASK)
            # Mirror pakai clean path (tanpa @) karena rclone tidak kenal @
            if enable_mirror and settings.is_mirror_upload_enabled:
                logger.info(
                    f"🔄 Starting background mirror task for folder: "
                    f"{clean_gdrive_folder_path}"
                )

                # ✅ Run mirror di background dengan shared executor
                asyncio.create_task(self.mirror_folder_async(clean_gdrive_folder_path))

                if active_group == 2:
                    backup_remotes = settings.get_next_backup_remotes()
                else:
                    backup_remotes = settings.get_secondary_remotes()

                mirror_results = {
                    "mirror_enabled": True,
                    "mirror_status": "running_in_background",
                    "target_remotes": backup_remotes,
                    "active_group": active_group,
                }
            else:
                mirror_results = {
                    "mirror_enabled": False,
                    "reason": "disabled" if not settings.is_mirror_upload_enabled else "no_secondaries",
                }

            # ✅ gdrive_folder_path yang dikembalikan ke caller
            # Gunakan DB path (dengan @ jika group 2) agar konsisten
            db_folder_path = self._make_db_path(clean_gdrive_folder_path, active_group)

            return {
                "success": True,
                "gdrive_folder_path": db_folder_path,  # Sudah include @ jika group 2
                "uploaded_files": uploaded_files,       # Path sudah include @ jika group 2
                "stats": stats,
                "mirror": mirror_results,
                "primary_remote": remote_name,
                "active_group": active_group,
            }

        except Exception as e:
            logger.error(f"Upload process failed: {str(e)}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "uploaded_files": uploaded_files,
                "primary_remote": self.primary_remote_name
            }

        finally:
            if temp_files:
                self.cleanup_temp_files(temp_files)


class ImageOptimizer:
    """Optional: Optimasi image sebelum upload."""

    @staticmethod
    def optimize_image(
        input_path: Path,
        output_path: Path,
        max_width: int = 2000,
        quality: int = 85
    ) -> bool:
        try:
            from PIL import Image

            img = Image.open(input_path)

            if img.width > max_width:
                ratio = max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((max_width, new_height), Image.LANCZOS)
                logger.info(f"Resized image to {max_width}x{new_height}")

            img.save(output_path, quality=quality, optimize=True)

            original_size = input_path.stat().st_size
            optimized_size = output_path.stat().st_size
            reduction = ((original_size - optimized_size) / original_size) * 100

            logger.info(f"Optimized: {reduction:.1f}% size reduction")
            return True

        except Exception as e:
            logger.error(f"Image optimization failed: {str(e)}")
            return False