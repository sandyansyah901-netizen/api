"""
Multi-Remote Rclone Service - TRUE GLOBAL SINGLETON + HTTPX + GROUP-AWARE
==========================================================================
Service untuk manage multiple rclone remotes untuk bypass Google Drive quota limit.

REVISI:
✅ ✨ REMOVED: _start_serve_daemons() dari initialize() - daemon dihandle RcloneService
✅ ✨ REMOVED: _stop_serve_daemons() dari shutdown() - daemon dihandle RcloneService
✅ ✨ CHANGED: _check_serve_daemon_health() pakai httpx.Client bukan requests
✅ ✨ CHANGED: _download_via_serve_daemon() pakai httpx.Client bukan requests
✅ ✨ CHANGED: get_health_status() baca daemon status dari RcloneService._serve_daemons
✅ ✨ ADDED: stream_file_async() untuk true streaming di FastAPI endpoint
✅ ✨ CHANGED: download_file_to_memory_async() delegate ke RcloneService.download_file_async
✅ ✨ ADDED: get_active_daemon_url() - cached daemon URL tanpa health check per request
✅ ✨ ADDED: get_next_daemon_url() - TRUE ROUND ROBIN across ALL active daemons (ASYNC)
✅ ✨ ADDED: _get_all_active_daemon_urls() - cached list semua daemon aktif
✅ ✨ DEPRECATED: get_active_daemon_url() masih ada untuk backward compat

✅ ✨ NEW: Group-Aware Multi-Group Support (1 instance, bukan 2 service)
        - MultiRemoteService sekarang tahu group 1 dan group 2
        - self._groups[1] = group 1 remotes (gdrive, gdrive1..gdrive10)
        - self._groups[2] = group 2 remotes (gdrive11, gdrive12..gdrive20)
        - get_next_remote(group=1|2) untuk routing upload
        - get_next_daemon_url(group=1|2) untuk round robin image proxy
        - Path prefix '@' dibaca di image proxy → routing ke group yang tepat
        - TIDAK ada singleton kedua
        - TIDAK ada duplikasi daemon/health/httpx pool
        - Semua backward compat property/method tetap ada dan menunjuk ke group 1

✅ ✨ NEW: Active Upload Group Management (thread-safe)
        - set_active_upload_group(group) → switch upload ke group 1 atau 2
        - get_active_upload_group() → cek group aktif saat ini
        - Dipanggil oleh upload_service, bulk_upload_service, smart_bulk_import_service
        - Thread-safe via Lock

✅ REVISI TERBARU:
        - get_health_status(group=1) → tambah parameter group opsional
          Saat group=1: return format lama (backward compat)
          Saat group=2: return format group 2
        - get_active_upload_group() sync dengan base.py global state
        - set_active_upload_group(group) sync dengan base.py global state
"""

import logging
import random
import time
import asyncio
import httpx
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from threading import Lock

from app.core.base import settings
from app.services.rclone_service import RcloneService, RcloneError

logger = logging.getLogger(__name__)


class RemoteStatus:
    """Track status per remote. TIDAK BERUBAH."""
    def __init__(self, remote_name: str):
        self.remote_name = remote_name
        self.is_healthy = True
        self.error_count = 0
        self.last_error_time: Optional[datetime] = None
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.last_used: Optional[datetime] = None
        self.quota_exceeded = False
        self.quota_reset_time: Optional[datetime] = None
        # ✅ Fields untuk sync daemon status dari main.py
        self.serve_daemon_running: bool = False
        self.serve_daemon_port: Optional[int] = None
        self.serve_daemon_url: Optional[str] = None
        self.serve_daemon_process = None

    @property
    def success_rate(self) -> float:
        """Calculate success rate (0-100). TIDAK BERUBAH."""
        if self.total_requests == 0:
            return 100.0
        return (self.successful_requests / self.total_requests) * 100

    @property
    def is_available(self) -> bool:
        """Check if remote is available for use. TIDAK BERUBAH."""
        if not self.is_healthy:
            return False

        if self.quota_exceeded:
            if self.quota_reset_time and datetime.utcnow() < self.quota_reset_time:
                return False
            else:
                self.quota_exceeded = False
                self.quota_reset_time = None

        return True

    def mark_success(self):
        """Mark request as successful. TIDAK BERUBAH."""
        self.total_requests += 1
        self.successful_requests += 1
        self.last_used = datetime.utcnow()
        self.error_count = 0

    def mark_failure(self, is_quota_error: bool = False):
        """Mark request as failed. TIDAK BERUBAH."""
        self.total_requests += 1
        self.failed_requests += 1
        self.last_error_time = datetime.utcnow()
        self.error_count += 1

        if is_quota_error:
            self.quota_exceeded = True
            self.quota_reset_time = datetime.utcnow() + timedelta(hours=24)
            logger.warning(
                f"Remote {self.remote_name} quota exceeded, "
                f"disabled until {self.quota_reset_time}"
            )

        if self.error_count >= 5:
            self.is_healthy = False
            logger.error(
                f"Remote {self.remote_name} marked as unhealthy "
                f"after {self.error_count} errors"
            )

    def reset_health(self):
        """Reset health status. TIDAK BERUBAH."""
        self.is_healthy = True
        self.error_count = 0
        logger.info(f"Remote {self.remote_name} health reset")


class MultiRemoteService:
    """
    Service untuk manage multiple rclone remotes dengan load balancing.

    ✅ ✨ CHANGED: Tidak lagi start/stop daemon (dihandle RcloneService)
    ✅ ✨ CHANGED: HTTP requests via httpx bukan requests
    ✅ ✨ ADDED: stream_file_async() untuk StreamingResponse
    ✅ ✨ ADDED: get_next_daemon_url() - TRUE ROUND ROBIN ASYNC (NEW!)
    ✅ ✨ KEPT: get_active_daemon_url() untuk backward compat
    ✅ Semua load balancing logic TETAP SAMA

    ✅ ✨ NEW: Group-Aware
    - self._groups[1] = group 1 (gdrive..gdrive10)
    - self._groups[2] = group 2 (gdrive11..gdrive20)
    - Semua backward compat property tetap menunjuk ke group 1

    ✅ ✨ NEW: Active Upload Group Management (thread-safe)
    - set_active_upload_group(group) → switch upload ke group 1 atau 2
    - get_active_upload_group() → cek group aktif saat ini

    ✅ REVISI TERBARU:
    - get_health_status(group=1) → parameter group opsional
    - get_active_upload_group() sync dengan base.py global state
    - set_active_upload_group(group) sync dengan base.py global state
    """

    # ✅ Global singleton instance - hanya 1
    _global_instance: Optional['MultiRemoteService'] = None
    _global_lock = Lock()

    # ==========================================
    # ✅ LAMA: Cached single daemon URL (backward compat, group 1)
    # ==========================================
    _cached_daemon_url: Optional[str] = None
    _cached_daemon_url_time: float = 0.0
    _DAEMON_CACHE_TTL: float = 30.0
    _daemon_cache_lock = Lock()

    # ==========================================
    # ✅ LAMA: Round Robin state + cached URL list (group 1, backward compat)
    # ==========================================
    _rr_index: int = 0
    _rr_lock = Lock()
    _cached_daemon_urls: Optional[List[str]] = None
    _cached_daemon_urls_time: float = 0.0
    _daemon_urls_lock = Lock()

    def __init__(self, remote_names: Optional[List[str]] = None):
        """
        Initialize multi-remote service.

        remote_names: Jika None, baca dari settings (group 1).
        Group 2 selalu dibaca dari settings secara otomatis.

        TIDAK BERUBAH dari versi original untuk group 1.
        Group 2 ditambah secara internal tanpa mengubah apapun yang lama.
        """
        # ─── Group 1 remote names ─────────────────────────────────────────
        if remote_names is None:
            all_remotes = settings.get_rclone_remotes()
            remote_names_g1 = [
                name.strip()
                for name in all_remotes
                if name and name.strip()
            ]
        else:
            remote_names_g1 = [
                name.strip()
                for name in remote_names
                if name and name.strip()
            ]

        if not remote_names_g1:
            error_msg = (
                "❌ No valid remote names configured!\n\n"
                "Please check your .env file:\n"
                "- RCLONE_PRIMARY_REMOTE must be set (e.g., 'gdrive')\n"
                "- RCLONE_BACKUP_REMOTES is optional (e.g., 'gdrive1,gdrive2')\n\n"
                f"Current config:\n"
                f"- RCLONE_PRIMARY_REMOTE: '{settings.RCLONE_PRIMARY_REMOTE}'\n"
                f"- RCLONE_BACKUP_REMOTES: '{settings.RCLONE_BACKUP_REMOTES}'\n"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)

        # ─── Group 2 remote names (dari settings, bisa kosong) ────────────
        remote_names_g2 = settings.get_next_group_remotes()  # [] jika tidak dikonfigurasi

        # ─── Backward compat: self.remote_names, self.remotes, self.remote_status
        #     tetap ada dan menunjuk ke group 1
        self.remote_names = remote_names_g1
        self.remotes: Dict[str, RcloneService] = {}       # akan diisi saat initialize()
        self.remote_status: Dict[str, RemoteStatus] = {}  # akan diisi saat initialize()
        self.current_index = 0
        self.lock = Lock()
        self._is_initialized = False

        # ─── NEW: Internal group structure ────────────────────────────────
        # Setiap group punya state sendiri: remotes, status, rr_index, daemon cache
        self._groups: Dict[int, Dict] = {
            1: {
                "remote_names": remote_names_g1,
                "remotes": {},
                "status": {},
                "rr_index": 0,
                "rr_lock": Lock(),
                "daemon_urls_cache": None,
                "daemon_urls_time": 0.0,
                "daemon_urls_lock": Lock(),
            },
            2: {
                "remote_names": remote_names_g2,
                "remotes": {},
                "status": {},
                "rr_index": 0,
                "rr_lock": Lock(),
                "daemon_urls_cache": None,
                "daemon_urls_time": 0.0,
                "daemon_urls_lock": Lock(),
            },
        }

        # ─── ✅ NEW: Active upload group state (thread-safe) ──────────────
        # Default = 1 (group 1 aktif)
        # Bisa di-switch via set_active_upload_group()
        self._active_upload_group: int = 1
        self._active_upload_group_lock = Lock()

        logger.info(
            f"MultiRemoteService constructed (NOT initialized yet) with remote names: "
            f"Group 1: {', '.join(remote_names_g1)} | "
            f"Group 2: {', '.join(remote_names_g2) if remote_names_g2 else 'not configured'}"
        )

    @property
    def is_initialized(self) -> bool:
        """Check if remotes have been initialized."""
        return self._is_initialized

    def initialize(self):
        """
        ✅ ✨ CHANGED: Tidak lagi start serve daemons di sini.
        Daemon dihandle oleh RcloneService masing-masing (sekali saja).

        ✅ ✨ NEW: Initialize group 1 + group 2 (jika dikonfigurasi).
        """
        if self._is_initialized:
            logger.warning("MultiRemoteService already initialized, skipping")
            return

        logger.info("🚀 Initializing MultiRemoteService (Group 1 + Group 2)...")

        # Initialize group 1 (wajib)
        self._initialize_remotes_for_group(group=1)

        # Backward compat: self.remotes dan self.remote_status menunjuk ke group 1
        self.remotes = self._groups[1]["remotes"]
        self.remote_status = self._groups[1]["status"]

        # Initialize group 2 (opsional, hanya jika dikonfigurasi)
        if self._groups[2]["remote_names"]:
            logger.info(
                f"🚀 Initializing Group 2 remotes: "
                f"{', '.join(self._groups[2]['remote_names'])}"
            )
            self._initialize_remotes_for_group(group=2)
        else:
            logger.info("ℹ️ Group 2 remotes not configured, skipping group 2 init")

        # Log serve daemon status per group
        if settings.RCLONE_SERVE_HTTP_ENABLED:
            for grp in [1, 2]:
                g = self._groups[grp]
                if not g["remote_names"]:
                    continue
                running_count = sum(
                    1 for rclone in g["remotes"].values()
                    if rclone.is_serve_running()
                )
                logger.info(
                    f"ℹ️ Serve daemons Group {grp} (managed by RcloneService): "
                    f"{running_count}/{len(g['remotes'])} running"
                )
                for name, rclone in g["remotes"].items():
                    url = rclone.get_serve_url()
                    icon = "✅" if url else "❌"
                    logger.info(f"  {icon} {name} (G{grp}): {url or 'not running'}")
        else:
            logger.info("ℹ️ Serve daemons disabled (direct cat mode)")

        self._is_initialized = True

        g1_count = len(self._groups[1]["remotes"])
        g2_count = len(self._groups[2]["remotes"])
        logger.info(
            f"✅ MultiRemoteService initialized: "
            f"Group 1 = {g1_count} remote(s), "
            f"Group 2 = {g2_count} remote(s)"
        )

    def _initialize_remotes_for_group(self, group: int):
        """
        ✅ NEW: Initialize semua rclone remote connections untuk group tertentu.
        Logic sama persis dengan _initialize_remotes() lama, cuma per-group.

        Args:
            group: 1 atau 2
        """
        g = self._groups[group]
        remote_names = g["remote_names"]

        for remote_name in remote_names:
            if not remote_name or not remote_name.strip():
                logger.warning(f"⚠️ Skipping empty remote name (Group {group})")
                continue

            try:
                rclone = RcloneService(remote_name=remote_name)
                g["remotes"][remote_name] = rclone
                g["status"][remote_name] = RemoteStatus(remote_name)
                logger.info(f"✅ Remote '{remote_name}' ready (Group {group}, singleton)")

            except Exception as e:
                logger.error(
                    f"❌ Failed to initialize remote '{remote_name}' "
                    f"(Group {group}): {str(e)}"
                )

        if not g["remotes"]:
            if group == 1:
                # Group 1 wajib ada
                error_msg = (
                    f"❌ No healthy remotes available for Group 1!\n\n"
                    f"Tried to initialize: {', '.join(remote_names)}\n"
                    f"All remotes failed connection test.\n\n"
                    f"Please check:\n"
                    f"1. rclone is installed and accessible\n"
                    f"2. Remote names in .env match 'rclone config' output\n"
                    f"3. Run 'rclone listremotes' to verify configuration\n"
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            else:
                # Group 2 opsional, hanya warning
                logger.warning(
                    f"⚠️ No healthy remotes available for Group 2. "
                    f"Group 2 will be disabled."
                )

    # LAMA: _initialize_remotes() tetap ada untuk backward compat internal
    def _initialize_remotes(self):
        """LAMA: backward compat. Delegate ke _initialize_remotes_for_group(1)."""
        self._initialize_remotes_for_group(group=1)

    # ✅ Global singleton accessor (TIDAK BERUBAH)
    @classmethod
    def get_global_instance(cls) -> 'MultiRemoteService':
        """Get or create global singleton instance."""
        with cls._global_lock:
            if cls._global_instance is None:
                logger.info("🆕 Creating global MultiRemoteService instance...")
                cls._global_instance = cls()
                cls._global_instance.initialize()
            elif not cls._global_instance.is_initialized:
                logger.info("🔄 Initializing existing MultiRemoteService instance...")
                cls._global_instance.initialize()

            return cls._global_instance

    @classmethod
    def reset_global_instance(cls):
        """Reset global singleton (untuk testing atau restart). TIDAK BERUBAH."""
        with cls._global_lock:
            if cls._global_instance:
                logger.info("🗑️ Resetting global MultiRemoteService instance")
                cls._global_instance = None

    def shutdown(self):
        """
        Graceful shutdown of MultiRemoteService.
        ✅ ✨ CHANGED: Tidak lagi stop serve daemons di sini.
        TIDAK BERUBAH dari versi sebelumnya.
        """
        logger.info("🛑 Shutting down MultiRemoteService...")
        logger.info("ℹ️ Serve daemon cleanup handled by RcloneService (via atexit/lifespan)")
        # Clear semua group
        for grp in [1, 2]:
            self._groups[grp]["remotes"].clear()
            self._groups[grp]["status"].clear()
        self.remotes.clear()
        self.remote_status.clear()
        self._is_initialized = False
        logger.info("✅ MultiRemoteService shutdown complete")

    # ==========================================
    # ✅ NEW: Active Upload Group Management (thread-safe)
    #
    # ✅ REVISI: get_active_upload_group() dan set_active_upload_group()
    # sekarang sync dua arah dengan base.py global state.
    #
    # Kenapa perlu sync?
    # - admin_endpoints.py pakai multi_remote.get_active_upload_group()
    # - upload_service.py pakai settings.get_active_upload_group()
    # - Keduanya harus return nilai yang sama
    #
    # Solusi: saat set, update KEDUA state (internal + base.py)
    # Saat get, baca dari base.py sebagai source of truth
    # ==========================================

    def get_active_upload_group(self) -> int:
        """
        ✅ REVISI: Get active upload group saat ini.

        Sekarang baca dari base.py global state sebagai source of truth,
        bukan hanya dari internal state.

        Thread-safe.

        Returns:
            1 jika upload ke group 1 (gdrive..gdrive10), path tanpa prefix
            2 jika upload ke group 2 (gdrive11..gdrive20), path dengan prefix '@'

        Usage:
            group = multi_remote.get_active_upload_group()
            if group == 2:
                db_path = f"@{clean_path}"
            else:
                db_path = clean_path
        """
        # ✅ REVISI: Import dan pakai base.py get_active_upload_group()
        # sebagai source of truth, bukan hanya internal state
        try:
            from app.core.base import get_active_upload_group as base_get_active_group
            base_group = base_get_active_group()
            # Sync internal state jika berbeda
            with self._active_upload_group_lock:
                if self._active_upload_group != base_group:
                    self._active_upload_group = base_group
            return base_group
        except ImportError:
            # Fallback ke internal state jika import gagal
            with self._active_upload_group_lock:
                return self._active_upload_group

    def set_active_upload_group(self, group: int) -> None:
        """
        ✅ REVISI: Set active upload group.

        Sekarang update KEDUA state:
        1. Internal self._active_upload_group (untuk method lain di class ini)
        2. base.py global state (untuk settings.get_active_upload_group())

        Thread-safe. Dipanggil oleh:
        - Admin endpoint POST /admin/groups/switch
        - Auto-switch logic saat group 1 penuh (quota exceeded)

        Args:
            group: 1 atau 2

        Raises:
            ValueError: jika group bukan 1 atau 2
            RuntimeError: jika group 2 dipilih tapi tidak dikonfigurasi

        Usage:
            # Manual switch via admin:
            multi_remote.set_active_upload_group(2)

            # Auto-switch saat quota exceeded:
            if quota_exceeded:
                multi_remote.set_active_upload_group(2)
        """
        if group not in (1, 2):
            raise ValueError(f"Invalid upload group: {group}. Must be 1 or 2.")

        if group == 2:
            # Validasi group 2 tersedia sebelum switch
            if not settings.is_next_group_configured:
                raise RuntimeError(
                    "Cannot switch to Group 2: RCLONE_NEXT_PRIMARY_REMOTE not configured in .env"
                )
            if not self._groups[2]["remotes"]:
                raise RuntimeError(
                    "Cannot switch to Group 2: No remotes initialized for Group 2. "
                    "Check if Group 2 remotes are properly configured."
                )

        with self._active_upload_group_lock:
            old_group = self._active_upload_group
            self._active_upload_group = group

        # ✅ REVISI: Sync ke base.py global state juga
        try:
            from app.core.base import set_active_upload_group as base_set_active_group
            base_set_active_group(group)
        except ImportError:
            logger.warning("Could not sync active upload group to base.py global state")

        if old_group != group:
            logger.info(
                f"✅ Active upload group switched: Group {old_group} → Group {group}"
            )
            if group == 2:
                logger.info(
                    f"  📁 New uploads will use path prefix '{settings.GROUP2_PATH_PREFIX}'"
                )
                logger.info(
                    f"  ���� Primary remote: {settings.RCLONE_NEXT_PRIMARY_REMOTE}"
                )
                backups = settings.get_next_backup_remotes()
                if backups:
                    logger.info(f"  💾 Backup remotes: {', '.join(backups)}")
            else:
                logger.info("  📁 New uploads will use normal path (no prefix)")
                logger.info(f"  📤 Primary remote: {settings.RCLONE_PRIMARY_REMOTE}")
        else:
            logger.debug(f"Active upload group unchanged: Group {group}")

    def get_upload_remotes(self) -> Tuple[str, List[str], str]:
        """
        ✅ NEW: Get remotes dan path prefix untuk upload berdasarkan active group.

        Convenience method untuk upload_service, bulk_upload_service, dll.

        Returns:
            (primary_remote, backup_remotes, path_prefix)
            - primary_remote: nama remote primary untuk upload
            - backup_remotes: list nama remote backup untuk mirror
            - path_prefix: '' untuk group 1, '@' untuk group 2

        Usage:
            primary, backups, prefix = multi_remote.get_upload_remotes()
            db_path = f"{prefix}{clean_gdrive_path}"
            upload_to(primary, clean_gdrive_path)
            for backup in backups:
                mirror_to(backup, clean_gdrive_path)
        """
        group = self.get_active_upload_group()

        if group == 2:
            primary = settings.RCLONE_NEXT_PRIMARY_REMOTE
            backups = settings.get_next_backup_remotes()
            prefix = settings.GROUP2_PATH_PREFIX
        else:
            primary = settings.RCLONE_PRIMARY_REMOTE
            backups = settings.get_secondary_remotes()
            prefix = ""

        return primary, backups, prefix

    # ==========================================
    # ✅ LAMA: get_active_daemon_url() - TETAP ADA untuk backward compat
    # Hanya return SATU URL dari group 1 (remote pertama yang aktif)
    # ==========================================

    def get_active_daemon_url(self) -> Optional[str]:
        """
        Get daemon URL dari cache (single URL, backward compat, group 1 only).

        ⚠️ DEPRECATED: Gunakan get_next_daemon_url() untuk round robin.
        Masih ada untuk backward compatibility dengan code yang sudah ada.

        Returns:
            Daemon URL string (pertama yang aktif di group 1) atau None
        """
        now = time.monotonic()

        with self._daemon_cache_lock:
            if (
                self._cached_daemon_url is not None
                and (now - self._cached_daemon_url_time) < self._DAEMON_CACHE_TTL
            ):
                return self._cached_daemon_url

            new_url = self._find_active_daemon_url()

            self._cached_daemon_url = new_url
            self._cached_daemon_url_time = now

            if new_url:
                logger.debug(f"Daemon URL cache refreshed (G1): {new_url}")
            else:
                logger.debug("No active daemon found (G1), cache set to None")

            return self._cached_daemon_url

    def _find_active_daemon_url(self) -> Optional[str]:
        """
        Cari daemon URL dari RcloneService group 1 yang sudah running.
        Hanya return SATU URL (yang pertama ditemukan).
        TIDAK BERUBAH - dipakai oleh get_active_daemon_url().
        """
        if not settings.RCLONE_SERVE_HTTP_ENABLED:
            return None

        for remote_name in self._groups[1]["remote_names"]:
            rclone = self._groups[1]["remotes"].get(remote_name)
            if rclone and rclone.is_serve_running():
                url = rclone.get_serve_url()
                if url:
                    logger.debug(f"Active daemon found (G1): {remote_name} at {url}")
                    return url

        return None

    def invalidate_daemon_cache(self):
        """
        Force invalidate daemon URL cache (single URL cache, group 1).
        TIDAK BERUBAH.
        """
        with self._daemon_cache_lock:
            self._cached_daemon_url_time = 0.0
            logger.info("Daemon URL cache invalidated (G1)")

    # ==========================================
    # ✅ LAMA: get_next_daemon_url() - TRUE ROUND ROBIN group 1 (ASYNC)
    # ✅ NEW: parameter group untuk pilih group 1 atau 2
    # ==========================================

    def _get_all_active_daemon_urls(self, group: int = 1) -> List[str]:
        """
        Get list SEMUA active daemon URLs untuk group tertentu, dengan cache 30 detik.

        LAMA (group=1): Tidak berubah, backward compat.
        NEW (group=2): Sama tapi dari group 2 remotes.

        Args:
            group: 1 atau 2

        Returns:
            List of active daemon URLs untuk group tersebut.
        """
        now = time.monotonic()
        g = self._groups[group]

        with g["daemon_urls_lock"]:
            if (
                g["daemon_urls_cache"] is not None
                and (now - g["daemon_urls_time"]) < self._DAEMON_CACHE_TTL
            ):
                return g["daemon_urls_cache"]

            urls = []

            if settings.RCLONE_SERVE_HTTP_ENABLED:
                for remote_name in g["remote_names"]:
                    rclone = g["remotes"].get(remote_name)
                    if rclone and rclone.is_serve_running():
                        url = rclone.get_serve_url()
                        if url:
                            urls.append(url)

            g["daemon_urls_cache"] = urls
            g["daemon_urls_time"] = now

            if urls:
                logger.debug(f"Daemon URLs cache refreshed (G{group}): {urls}")
            else:
                logger.debug(f"No active daemons found (G{group}), cache set to []")

            return g["daemon_urls_cache"]

    async def get_next_daemon_url(self, group: int = 1) -> Optional[str]:
        """
        TRUE ROUND ROBIN - Get next daemon URL bergiliran untuk group tertentu.

        LAMA: get_next_daemon_url() → group 1 (backward compat, default group=1)
        NEW: get_next_daemon_url(group=2) → group 2

        Distributes image proxy requests evenly across ALL active daemon remotes
        dalam group yang dipilih.

        Args:
            group: 1 atau 2 (default 1 untuk backward compat)

        Returns:
            Daemon URL string atau None jika tidak ada daemon running di group tersebut.
        """
        urls = self._get_all_active_daemon_urls(group=group)

        if not urls:
            return None

        g = self._groups[group]
        with g["rr_lock"]:
            idx = g["rr_index"] % len(urls)
            g["rr_index"] += 1

        selected = urls[idx]
        logger.debug(
            f"Round robin selected (G{group}): {selected} "
            f"(idx={idx}/{len(urls)}, total_rr={g['rr_index']})"
        )
        return selected

    def get_daemon_count(self, group: int = 1) -> int:
        """
        Get jumlah daemon yang sedang aktif untuk group tertentu.

        LAMA: get_daemon_count() → group 1
        NEW: get_daemon_count(group=2) → group 2

        Args:
            group: 1 atau 2 (default 1 untuk backward compat)

        Returns:
            Jumlah daemon running (int)
        """
        return len(self._get_all_active_daemon_urls(group=group))

    def invalidate_all_daemon_caches(self):
        """
        Force invalidate SEMUA daemon caches (single + list) untuk SEMUA group.
        TIDAK BERUBAH untuk group 1, ditambah group 2.
        """
        with self._daemon_cache_lock:
            self._cached_daemon_url_time = 0.0

        for grp in [1, 2]:
            with self._groups[grp]["daemon_urls_lock"]:
                self._groups[grp]["daemon_urls_time"] = 0.0

        logger.info("All daemon URL caches invalidated (all groups)")

    # ==========================================
    # ✅ LOAD BALANCING - TIDAK BERUBAH untuk group 1
    # ✅ NEW: parameter group untuk pilih group 1 atau 2
    # ==========================================

    def get_next_remote(self, strategy: str = "round_robin", group: int = 1) -> Tuple[str, RcloneService]:
        """
        Get next available remote based on strategy.

        LAMA: get_next_remote(strategy) → group 1 (backward compat, default group=1)
        NEW: get_next_remote(strategy, group=2) → group 2

        Args:
            strategy: round_robin | weighted | random | least_used
            group: 1 atau 2 (default 1 untuk backward compat)

        TIDAK BERUBAH logicnya, hanya ditambah parameter group.
        """
        g = self._groups[group]

        with self.lock:
            available = [
                (name, remote)
                for name, remote in g["remotes"].items()
                if g["status"][name].is_available
            ]

            if not available:
                self._auto_recover_remotes(group=group)

                available = [
                    (name, remote)
                    for name, remote in g["remotes"].items()
                    if g["status"][name].is_available
                ]

                if not available:
                    error_msg = (
                        f"❌ No healthy remotes available (Group {group})!\n\n"
                        f"Total configured remotes: {len(g['remotes'])}\n"
                        f"Remotes status:\n"
                    )
                    for name, status in g["status"].items():
                        error_msg += (
                            f"  - {name}: "
                            f"healthy={status.is_healthy}, "
                            f"quota_exceeded={status.quota_exceeded}, "
                            f"errors={status.error_count}\n"
                        )

                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

            if strategy == "round_robin":
                return self._round_robin_select(available, group=group)
            elif strategy == "weighted":
                return self._weighted_select(available, group=group)
            elif strategy == "random":
                return random.choice(available)
            elif strategy == "least_used":
                return self._least_used_select(available, group=group)
            else:
                return self._round_robin_select(available, group=group)

    def _round_robin_select(self, available, group: int = 1):
        """Round-robin selection per group. TIDAK BERUBAH logic, ditambah group param."""
        g = self._groups[group]
        selected = available[g["rr_index"] % len(available)]
        g["rr_index"] = (g["rr_index"] + 1) % len(available)
        return selected

    def _weighted_select(self, available, group: int = 1):
        """Weighted selection based on success rate. TIDAK BERUBAH."""
        g = self._groups[group]
        weights = [g["status"][name].success_rate for name, _ in available]
        total = sum(weights)
        if total == 0:
            return random.choice(available)
        normalized = [w / total for w in weights]
        selected_idx = random.choices(range(len(available)), weights=normalized)[0]
        return available[selected_idx]

    def _least_used_select(self, available, group: int = 1):
        """Select remote with least total requests. TIDAK BERUBAH."""
        g = self._groups[group]
        sorted_remotes = sorted(
            available,
            key=lambda x: g["status"][x[0]].total_requests
        )
        return sorted_remotes[0]

    def _auto_recover_remotes(self, group: int = 1):
        """Auto-recover remotes yang haven't errored in last 10 minutes. TIDAK BERUBAH."""
        recovery_threshold = datetime.utcnow() - timedelta(minutes=10)
        g = self._groups[group]

        for name, status in g["status"].items():
            if not status.is_healthy:
                if status.last_error_time and status.last_error_time < recovery_threshold:
                    status.reset_health()
                    logger.info(f"🔄 Auto-recovered remote: {name} (Group {group})")

    def get_remote_status(self, remote_name: str, group: int = 1) -> Optional[RemoteStatus]:
        """
        ✅ NEW: Get RemoteStatus untuk remote tertentu di group tertentu.
        Dipakai oleh image proxy untuk mark_success/mark_failure setelah streaming.

        Args:
            remote_name: Nama remote
            group: 1 atau 2

        Returns:
            RemoteStatus atau None jika tidak ditemukan.
        """
        return self._groups[group]["status"].get(remote_name)

    # ==========================================
    # ✅ DOWNLOAD METHODS - TIDAK BERUBAH untuk backward compat
    # ✅ NEW: tambah parameter group
    # ==========================================

    def download_file_to_memory(
        self,
        file_path: str,
        max_retries: int = 3,
        strategy: str = "round_robin",
        group: int = 1
    ) -> Optional[bytes]:
        """
        Download file ke memory dengan auto-failover.

        LAMA: download_file_to_memory(file_path, max_retries, strategy) → group 1
        NEW: tambah parameter group untuk pilih group 1 atau 2.
        Logic TIDAK BERUBAH.
        """
        attempts = 0
        max_total_attempts = len(self._groups[group]["remotes"]) * max_retries

        while attempts < max_total_attempts:
            try:
                remote_name, rclone = self.get_next_remote(strategy, group=group)
                status = self._groups[group]["status"][remote_name]

                logger.info(
                    f"Attempt {attempts + 1}: Using remote '{remote_name}' "
                    f"(G{group}) for {file_path}"
                )

                content = rclone.download_file_to_memory(file_path, max_retries=1)

                if content:
                    status.mark_success()
                    logger.info(
                        f"✅ Downloaded {file_path} via remote '{remote_name}' (G{group})"
                    )
                    return content
                else:
                    status.mark_failure()

            except RuntimeError as e:
                logger.error(f"No healthy remotes available (G{group}): {str(e)}")
                break
            except Exception as e:
                error_msg = str(e).lower()
                is_quota_error = any(keyword in error_msg for keyword in [
                    'quota', 'rate limit', 'too many requests', '403', 'forbidden'
                ])

                if 'remote_name' in locals():
                    self._groups[group]["status"][remote_name].mark_failure(is_quota_error)
                    logger.warning(
                        f"❌ Remote '{remote_name}' (G{group}) failed: {str(e)}"
                    )

            attempts += 1

        logger.error(
            f"Failed to download {file_path} after {attempts} attempts "
            f"across all remotes (G{group})"
        )
        return None

    async def download_file_to_memory_async(
        self,
        file_path: str,
        max_retries: int = 3,
        strategy: str = "round_robin",
        group: int = 1
    ) -> Optional[bytes]:
        """
        ASYNC VERSION - Download file ke memory.

        LAMA: download_file_to_memory_async(file_path, max_retries, strategy) → group 1
        NEW: tambah parameter group.
        Logic TIDAK BERUBAH.
        """
        total_attempts = len(self._groups[group]["remotes"]) * max_retries

        for attempt in range(total_attempts):
            try:
                remote_name, rclone = self.get_next_remote(strategy, group=group)
                status = self._groups[group]["status"][remote_name]

                logger.debug(
                    f"Async attempt {attempt + 1}: Using remote '{remote_name}' "
                    f"(G{group}) for {file_path}"
                )

                content = await rclone.download_file_async(file_path)

                if content:
                    status.mark_success()
                    logger.info(
                        f"✅ Async downloaded {file_path} via remote '{remote_name}' "
                        f"(G{group}) ({len(content)} bytes)"
                    )
                    return content
                else:
                    status.mark_failure()

            except RuntimeError as e:
                logger.error(f"No healthy remotes available (G{group}): {str(e)}")
                break
            except Exception as e:
                error_msg = str(e).lower()
                is_quota_error = any(keyword in error_msg for keyword in [
                    'quota', 'rate limit', 'too many requests', '403', 'forbidden'
                ])
                if 'remote_name' in locals():
                    self._groups[group]["status"][remote_name].mark_failure(is_quota_error)
                    logger.warning(
                        f"❌ Async remote '{remote_name}' (G{group}) failed: {str(e)}"
                    )

        logger.error(f"All async attempts failed for: {file_path} (G{group})")
        return None

    async def stream_file_async(
        self,
        file_path: str,
        strategy: str = "round_robin",
        group: int = 1
    ):
        """
        Stream file via HTTPX untuk StreamingResponse.

        LAMA: stream_file_async(file_path, strategy) → group 1
        NEW: tambah parameter group.
        Logic TIDAK BERUBAH.
        """
        remote_name, rclone = self.get_next_remote(strategy, group=group)
        status = self._groups[group]["status"][remote_name]

        try:
            has_content = False
            async for chunk in rclone.stream_file_async(file_path):
                has_content = True
                yield chunk

            if has_content:
                status.mark_success()
            else:
                status.mark_failure()

        except Exception as e:
            status.mark_failure()
            logger.error(f"Stream failed from '{remote_name}' (G{group}): {str(e)}")

            try:
                other_remote_name, other_rclone = self.get_next_remote(strategy, group=group)
                content = await other_rclone.download_file_async(file_path)
                if content:
                    self._groups[group]["status"][other_remote_name].mark_success()
                    chunk_size = 65536
                    for i in range(0, len(content), chunk_size):
                        yield content[i:i + chunk_size]
            except Exception as e2:
                logger.error(f"Stream fallback also failed (G{group}): {str(e2)}")

    # ==========================================
    # ✅ LIST FILES (TIDAK BERUBAH + tambah group param)
    # ==========================================

    def list_files_in_folder(
        self,
        folder_id: str,
        mime_type_filter: Optional[str] = None,
        sort: bool = True,
        strategy: str = "round_robin",
        group: int = 1
    ) -> List[Dict]:
        """
        List files dengan auto-failover.

        LAMA: list_files_in_folder(...) → group 1
        NEW: tambah parameter group.
        Logic TIDAK BERUBAH.
        """
        max_attempts = len(self._groups[group]["remotes"]) * 2

        for attempt in range(max_attempts):
            try:
                remote_name, rclone = self.get_next_remote(strategy, group=group)
                logger.info(
                    f"Listing files in {folder_id} via remote '{remote_name}' (G{group})"
                )
                files = rclone.list_files_in_folder(folder_id, mime_type_filter, sort)
                self._groups[group]["status"][remote_name].mark_success()
                return files

            except RuntimeError:
                break
            except Exception as e:
                if 'remote_name' in locals():
                    error_msg = str(e).lower()
                    is_quota_error = 'quota' in error_msg or 'rate limit' in error_msg
                    self._groups[group]["status"][remote_name].mark_failure(is_quota_error)

        return []

    # ==========================================
    # ✅ HEALTH STATUS
    # ✅ REVISI: Tambah parameter group opsional
    #
    # SEBELUMNYA: get_health_status() → return info semua group sekaligus
    # SEKARANG:
    #   - get_health_status()         → backward compat, return semua group (TIDAK BERUBAH)
    #   - get_health_status(group=1)  → hanya info group 1 (format lama)
    #   - get_health_status(group=2)  → hanya info group 2 (format baru)
    #
    # Diperlukan karena admin_endpoints.py dan main.py memanggil:
    #   health = multi_remote.get_health_status(group=1)
    #   health = multi_remote.get_health_status(group=2)
    # ==========================================

    def _check_serve_daemon_health(self, remote_name: str, group: int = 1) -> bool:
        """
        Check if serve daemon is healthy.
        TIDAK BERUBAH, ditambah group param.
        """
        rclone = self._groups[group]["remotes"].get(remote_name)
        if not rclone or not rclone.is_serve_running():
            return False

        url = rclone.get_serve_url()
        if not url:
            return False

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)
                return resp.status_code < 400
        except Exception:
            return False

    def get_health_status(self, group: Optional[int] = None) -> Dict:
        """
        Get health status of all remotes.

        ✅ REVISI: Tambah parameter group opsional.

        Args:
            group: None (default) → return semua group (backward compat, format lama)
                   1 → return hanya info group 1 dalam format yang sama dengan lama
                   2 → return hanya info group 2

        Backward compat:
            - get_health_status() tanpa argumen → TETAP return format lama (semua group)
            - Semua key lama masih ada: total_remotes, healthy_remotes, dll

        Group-specific:
            - get_health_status(group=1) → return format sama tapi hanya group 1
            - get_health_status(group=2) → return info group 2

        TIDAK BERUBAH: Jika group=None, behavior identik dengan versi sebelumnya.
        """
        # ==========================================
        # ✅ REVISI: Jika group spesifik diminta, return format per-group
        # ==========================================
        if group == 2:
            return self._get_health_status_for_group(group=2)

        if group == 1:
            # Return format sama dengan get_health_status() lama tapi hanya group 1
            # agar kode yang pakai group=1 tidak perlu ubah parsing response
            return self._get_health_status_for_group(group=1)

        # ==========================================
        # ✅ ORIGINAL: group=None → return semua group (TIDAK BERUBAH)
        # Kode di bawah ini identik dengan implementasi original
        # ==========================================

        # ─── Group 1 stats (backward compat, TIDAK BERUBAH) ──────────────
        g1 = self._groups[1]
        total_remotes_g1 = len(g1["remotes"])
        healthy_g1 = sum(1 for s in g1["status"].values() if s.is_healthy)
        available_g1 = sum(1 for s in g1["status"].values() if s.is_available)
        daemons_running_g1 = sum(
            1 for r in g1["remotes"].values() if r.is_serve_running()
        )

        status_info = {
            # ─── backward compat keys (group 1) ───────��──────────────────
            "total_remotes": total_remotes_g1,
            "healthy_remotes": healthy_g1,
            "available_remotes": available_g1,
            "initialized": self._is_initialized,
            "cached_instances": RcloneService.get_cached_instances(),
            "serve_enabled": settings.RCLONE_SERVE_HTTP_ENABLED,
            "serve_daemons_running": daemons_running_g1,
            "active_daemon_urls": self._get_all_active_daemon_urls(group=1),
            "daemon_count": self.get_daemon_count(group=1),
            "remotes": [],

            # ─── NEW: group 2 info ────────────────────────────────────────
            "group2": {
                "configured": settings.is_next_group_configured,
                "enabled": settings.is_group2_enabled,
                "total_remotes": len(self._groups[2]["remotes"]),
                "healthy_remotes": sum(
                    1 for s in self._groups[2]["status"].values() if s.is_healthy
                ),
                "available_remotes": sum(
                    1 for s in self._groups[2]["status"].values() if s.is_available
                ),
                "serve_daemons_running": sum(
                    1 for r in self._groups[2]["remotes"].values() if r.is_serve_running()
                ),
                "active_daemon_urls": self._get_all_active_daemon_urls(group=2),
                "daemon_count": self.get_daemon_count(group=2),
                "path_prefix": settings.GROUP2_PATH_PREFIX,
                "remotes": [],
            },

            # ─── NEW: active upload group ─────────────────────────────────
            "active_upload_group": self.get_active_upload_group(),
        }

        # ─── Group 1 remote details (TIDAK BERUBAH) ───────────────────────
        for name, status in g1["status"].items():
            rclone = g1["remotes"].get(name)
            serve_running = rclone.is_serve_running() if rclone else False
            serve_url = rclone.get_serve_url() if rclone else None

            remote_info = {
                "name": name,
                "group": 1,
                "healthy": status.is_healthy,
                "available": status.is_available,
                "success_rate": round(status.success_rate, 2),
                "total_requests": status.total_requests,
                "successful_requests": status.successful_requests,
                "failed_requests": status.failed_requests,
                "error_count": status.error_count,
                "quota_exceeded": status.quota_exceeded,
                "quota_reset_time": (
                    status.quota_reset_time.isoformat()
                    if status.quota_reset_time else None
                ),
                "last_used": status.last_used.isoformat() if status.last_used else None,
            }

            if settings.RCLONE_SERVE_HTTP_ENABLED:
                remote_info["serve_daemon"] = {
                    "running": serve_running,
                    "url": serve_url,
                    "healthy": self._check_serve_daemon_health(name, group=1) if serve_running else False
                }

            status_info["remotes"].append(remote_info)

        # ─── Group 2 remote details (NEW) ─────────────────────────────────
        g2 = self._groups[2]
        for name, status in g2["status"].items():
            rclone = g2["remotes"].get(name)
            serve_running = rclone.is_serve_running() if rclone else False
            serve_url = rclone.get_serve_url() if rclone else None

            remote_info = {
                "name": name,
                "group": 2,
                "healthy": status.is_healthy,
                "available": status.is_available,
                "success_rate": round(status.success_rate, 2),
                "total_requests": status.total_requests,
                "successful_requests": status.successful_requests,
                "failed_requests": status.failed_requests,
                "error_count": status.error_count,
                "quota_exceeded": status.quota_exceeded,
                "quota_reset_time": (
                    status.quota_reset_time.isoformat()
                    if status.quota_reset_time else None
                ),
                "last_used": status.last_used.isoformat() if status.last_used else None,
            }

            if settings.RCLONE_SERVE_HTTP_ENABLED:
                remote_info["serve_daemon"] = {
                    "running": serve_running,
                    "url": serve_url,
                    "healthy": self._check_serve_daemon_health(name, group=2) if serve_running else False
                }

            status_info["group2"]["remotes"].append(remote_info)

        return status_info

    def _get_health_status_for_group(self, group: int) -> Dict:
        """
        ✅ REVISI: Helper untuk get_health_status(group=1|2).

        Return health status untuk group tertentu saja, dalam format
        yang mirip dengan get_health_status() lama agar parsing di
        admin_endpoints.py dan main.py tidak perlu banyak berubah.

        Args:
            group: 1 atau 2

        Returns:
            Dict dengan keys: total_remotes, healthy_remotes, available_remotes,
            serve_daemons_running, remotes, active_daemon_urls, daemon_count,
            initialized, serve_enabled, cached_instances
        """
        g = self._groups[group]
        total_remotes = len(g["remotes"])
        healthy = sum(1 for s in g["status"].values() if s.is_healthy)
        available = sum(1 for s in g["status"].values() if s.is_available)
        daemons_running = sum(
            1 for r in g["remotes"].values() if r.is_serve_running()
        )

        result = {
            "total_remotes": total_remotes,
            "healthy_remotes": healthy,
            "available_remotes": available,
            "initialized": self._is_initialized,
            "cached_instances": RcloneService.get_cached_instances(),
            "serve_enabled": settings.RCLONE_SERVE_HTTP_ENABLED,
            "serve_daemons_running": daemons_running,
            "active_daemon_urls": self._get_all_active_daemon_urls(group=group),
            "daemon_count": self.get_daemon_count(group=group),
            "active_upload_group": self.get_active_upload_group(),
            "group": group,
            "remotes": [],
        }

        for name, status in g["status"].items():
            rclone = g["remotes"].get(name)
            serve_running = rclone.is_serve_running() if rclone else False
            serve_url = rclone.get_serve_url() if rclone else None

            remote_info = {
                "name": name,
                "group": group,
                "healthy": status.is_healthy,
                "available": status.is_available,
                "success_rate": round(status.success_rate, 2),
                "total_requests": status.total_requests,
                "successful_requests": status.successful_requests,
                "failed_requests": status.failed_requests,
                "error_count": status.error_count,
                "quota_exceeded": status.quota_exceeded,
                "quota_reset_time": (
                    status.quota_reset_time.isoformat()
                    if status.quota_reset_time else None
                ),
                "last_used": status.last_used.isoformat() if status.last_used else None,
            }

            if settings.RCLONE_SERVE_HTTP_ENABLED:
                remote_info["serve_daemon"] = {
                    "running": serve_running,
                    "url": serve_url,
                    "healthy": self._check_serve_daemon_health(name, group=group) if serve_running else False
                }

            result["remotes"].append(remote_info)

        return result

    # ==========================================
    # ✅ UTILITY METHODS (TIDAK BERUBAH + tambah group param)
    # ==========================================

    def reset_remote_health(self, remote_name: str, group: int = 1) -> bool:
        """
        Manual reset health status untuk remote tertentu.

        LAMA: reset_remote_health(remote_name) → group 1
        NEW: tambah parameter group.
        Logic TIDAK BERUBAH.
        """
        if remote_name in self._groups[group]["status"]:
            self._groups[group]["status"][remote_name].reset_health()
            return True
        return False

    def get_best_remote(self, group: int = 1) -> Tuple[str, RcloneService]:
        """
        Get remote dengan success rate tertinggi di group tertentu.

        LAMA: get_best_remote() → group 1
        NEW: tambah parameter group.
        Logic TIDAK BERUBAH.
        """
        g = self._groups[group]
        available = [
            (name, remote, g["status"][name].success_rate)
            for name, remote in g["remotes"].items()
            if g["status"][name].is_available
        ]

        if not available:
            raise RuntimeError(f"No healthy remotes available (Group {group})!")

        best = max(available, key=lambda x: x[2])
        return (best[0], best[1])

    # ==========================================
    # ✅ NEW: Helper untuk determine group dari path
    # Dipakai oleh image proxy di admin_endpoints.py
    # ==========================================

    def get_group_for_path(self, path: str) -> int:
        """
        Determine group untuk path tertentu berdasarkan prefix.

        Args:
            path: Database path (mungkin ada prefix '@')

        Returns:
            1 jika group 1, 2 jika group 2.

        Examples:
            >>> service.get_group_for_path("manga_library/xxx/001.jpg")
            1
            >>> service.get_group_for_path("@manga_library/xxx/001.jpg")
            2
        """
        return settings.get_group_for_path(path)

    def get_clean_path(self, path: str) -> str:
        """
        Strip group prefix dari path agar bisa dikirim ke rclone.

        Args:
            path: Database path (mungkin ada prefix '@')

        Returns:
            Clean path tanpa prefix.

        Examples:
            >>> service.get_clean_path("@manga_library/xxx/001.jpg")
            "manga_library/xxx/001.jpg"
            >>> service.get_clean_path("manga_library/xxx/001.jpg")
            "manga_library/xxx/001.jpg"
        """
        return settings.clean_path(path)

    def make_group2_path(self, clean_path: str) -> str:
        """
        Tambah GROUP2_PATH_PREFIX ke clean path untuk disimpan ke database.

        Dipakai oleh upload_service, bulk_upload_service, smart_bulk_import_service
        saat upload ke group 2, agar path di DB punya marker '@'.

        Args:
            clean_path: Path tanpa prefix (yang dikirim ke rclone)

        Returns:
            Path dengan prefix GROUP2_PATH_PREFIX untuk disimpan ke DB.

        Examples:
            >>> service.make_group2_path("manga_library/xxx/001.jpg")
            "@manga_library/xxx/001.jpg"
            >>> service.make_group2_path("@manga_library/xxx/001.jpg")
            "@manga_library/xxx/001.jpg"  # no double prefix
        """
        return settings.make_group2_path(clean_path)

    def is_group2_available(self) -> bool:
        """
        Check apakah group 2 tersedia untuk dipakai.

        Returns:
            True jika group 2 dikonfigurasi DAN minimal ada 1 remote yang available.
        """
        if not settings.is_next_group_configured:
            return False

        g2 = self._groups[2]
        return any(s.is_available for s in g2["status"].values())

    # ==========================================
    # ✅ NEW: Upload Path Helper
    # Dipakai oleh upload_service, bulk_upload_service, smart_bulk_import_service
    # untuk menentukan path DB yang benar berdasarkan active upload group.
    # ==========================================

    def build_db_path(self, clean_gdrive_path: str) -> str:
        """
        Build path yang akan disimpan ke database berdasarkan active upload group.

        Jika upload ke group 1 → path normal (tanpa prefix)
        Jika upload ke group 2 → path dengan prefix '@'

        Ini adalah satu-satunya method yang harus dipakai oleh upload services
        saat mau simpan gdrive_file_id ke tabel Page atau anchor_path ke Chapter.

        Args:
            clean_gdrive_path: Path file di GDrive (tanpa remote prefix, tanpa '@')
                               Contoh: "manga_library/one-piece/Chapter_01/001.jpg"

        Returns:
            Path untuk disimpan ke DB:
            - Group 1: "manga_library/one-piece/Chapter_01/001.jpg"
            - Group 2: "@manga_library/one-piece/Chapter_01/001.jpg"

        Examples:
            # Active group = 1
            >>> service.build_db_path("manga_library/xxx/001.jpg")
            "manga_library/xxx/001.jpg"

            # Active group = 2
            >>> service.build_db_path("manga_library/xxx/001.jpg")
            "@manga_library/xxx/001.jpg"
        """
        group = self.get_active_upload_group()
        if group == 2:
            return self.make_group2_path(clean_gdrive_path)
        return clean_gdrive_path

    def get_rclone_service_for_upload(self) -> tuple:
        """
        Get RcloneService instance dan remote name untuk upload berdasarkan active group.

        Returns:
            (remote_name, RcloneService) tuple dari group yang sedang aktif.

        Raises:
            RuntimeError: jika tidak ada remote yang available di group aktif.
        """
        group = self.get_active_upload_group()
        remote_name, rclone = self.get_next_remote(
            strategy=settings.RCLONE_LOAD_BALANCING_STRATEGY,
            group=group
        )
        return remote_name, rclone

    def get_backup_remotes_for_current_group(self) -> list:
        """
        Get list backup remote names untuk group yang sedang aktif.

        Returns:
            List of backup remote names (tanpa primary).
        """
        group = self.get_active_upload_group()
        primary, backups, _ = self.get_upload_remotes()
        return backups