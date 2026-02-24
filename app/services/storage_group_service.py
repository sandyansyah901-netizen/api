# File: app/services/storage_group_service.py
"""
Storage Group Service - Multi-Group Remote Routing
===================================================
Service untuk manage multi-group storage:
- Group 1: RCLONE_PRIMARY_REMOTE + RCLONE_BACKUP_REMOTES (path normal)
- Group 2: RCLONE_NEXT_PRIMARY_REMOTE + RCLONE_NEXT_BACKUP_REMOTES (path prefix @)

Routing logic:
- Path tanpa @ → gunakan Group 1 remotes
- Path dengan @ → gunakan Group 2 remotes
- Upload baru → cek quota group 1, kalau penuh → upload ke group 2 + mark @

Database TIDAK DIUBAH. Hanya string path yang berubah dengan prefix @.

Example:
  Group 1 path: "manga_library/crimson_reset/Chapter_001/001.jpg"
  Group 2 path: "@manga_library/crimson_reset/Chapter_002/001.jpg"
"""

import logging
import threading
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timedelta

from app.core.base import settings

logger = logging.getLogger(__name__)

# ==========================================
# PATH ROUTING CONSTANTS
# ==========================================

GROUP2_PREFIX = "@"


def is_group2_path(path: str) -> bool:
    """
    Check apakah path ada di Group 2 (ditandai prefix @).
    
    Args:
        path: File path dari database
        
    Returns:
        True jika Group 2, False jika Group 1
        
    Examples:
        "@manga_library/xxx/001.jpg" → True
        "manga_library/xxx/001.jpg"  → False
        "@manga_library/xxx/001.jpg" → True (dengan leading slash pun ok)
    """
    return str(path).startswith(GROUP2_PREFIX)


def clean_path(path: str) -> str:
    """
    Strip routing prefix dari path untuk dipakai ke rclone.
    
    Args:
        path: Raw path dari DB (mungkin ada prefix @)
        
    Returns:
        Clean path tanpa prefix
        
    Examples:
        "@manga_library/xxx/001.jpg" → "manga_library/xxx/001.jpg"
        "manga_library/xxx/001.jpg"  → "manga_library/xxx/001.jpg"
    """
    return path.lstrip(GROUP2_PREFIX)


def mark_as_group2(path: str) -> str:
    """
    Tambah prefix @ ke path untuk menandai file ada di Group 2.
    
    Args:
        path: Clean path tanpa prefix
        
    Returns:
        Path dengan prefix @
        
    Examples:
        "manga_library/xxx/006.jpg" → "@manga_library/xxx/006.jpg"
    """
    clean = clean_path(path)
    return f"{GROUP2_PREFIX}{clean}"


def get_group_for_path(path: str) -> int:
    """
    Get group number untuk path tertentu.
    
    Returns:
        1 = Group 1 (normal)
        2 = Group 2 (next group, prefix @)
    """
    return 2 if is_group2_path(path) else 1


# ==========================================
# QUOTA TRACKER
# ==========================================

class GroupQuotaTracker:
    """
    Track penggunaan quota per group untuk auto-switch.
    
    Cara kerja:
    - Setiap upload berhasil → tambah ukuran ke total_uploaded
    - Kalau total_uploaded >= quota_limit → mark group 1 sebagai "full"
    - Kalau group 1 full → upload berikutnya ke group 2
    
    State ini IN-MEMORY (tidak persisten ke DB).
    Kalau server restart, tracker reset tapi file di GDrive tetap ada.
    Path prefix @ di DB yang jadi ground truth.
    """
    
    _instance: Optional['GroupQuotaTracker'] = None
    _lock = threading.Lock()
    
    def __init__(self):
        # Total bytes yang sudah diupload ke group 1 sejak tracker ini dibuat
        # (hanya tracking dalam sesi ini, bukan total historis)
        self._group1_uploaded_bytes: int = 0
        self._group1_full: bool = False
        self._group1_full_since: Optional[datetime] = None
        self._data_lock = threading.Lock()
        
        # Quota limit dalam bytes
        quota_gb = settings.RCLONE_GROUP1_QUOTA_GB
        self._quota_bytes: int = quota_gb * 1024 * 1024 * 1024 if quota_gb > 0 else 0
        
        logger.info(
            f"GroupQuotaTracker initialized: "
            f"quota={quota_gb}GB, "
            f"auto_switch={settings.RCLONE_AUTO_SWITCH_GROUP}, "
            f"next_group_configured={settings.has_next_group}"
        )
    
    @classmethod
    def get_instance(cls) -> 'GroupQuotaTracker':
        """Singleton accessor."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance
    
    def record_upload(self, bytes_uploaded: int):
        """Record bytes yang berhasil diupload ke group 1."""
        with self._data_lock:
            self._group1_uploaded_bytes += bytes_uploaded
            
            # Auto-check quota
            if (
                self._quota_bytes > 0
                and not self._group1_full
                and self._group1_uploaded_bytes >= self._quota_bytes
            ):
                self._group1_full = True
                self._group1_full_since = datetime.now()
                logger.warning(
                    f"⚠️ Group 1 quota reached! "
                    f"Uploaded: {self._group1_uploaded_bytes / (1024**3):.2f}GB / "
                    f"{self._quota_bytes / (1024**3):.2f}GB. "
                    f"Switching to Group 2 for new uploads."
                )
    
    def mark_group1_full(self, reason: str = "manual"):
        """Manual mark group 1 sebagai penuh."""
        with self._data_lock:
            if not self._group1_full:
                self._group1_full = True
                self._group1_full_since = datetime.now()
                logger.warning(f"⚠️ Group 1 marked as FULL (reason: {reason})")
    
    def is_group1_full(self) -> bool:
        """Check apakah group 1 sudah penuh."""
        with self._data_lock:
            return self._group1_full
    
    def get_active_upload_group(self) -> int:
        """
        Get group yang seharusnya dipakai untuk upload baru.
        
        Returns:
            1 = Upload ke Group 1
            2 = Upload ke Group 2
        """
        with self._data_lock:
            if (
                self._group1_full
                and settings.RCLONE_AUTO_SWITCH_GROUP
                and settings.has_next_group
            ):
                return 2
            return 1
    
    def get_stats(self) -> Dict:
        """Get statistics untuk admin endpoint."""
        with self._data_lock:
            quota_gb = self._quota_bytes / (1024**3) if self._quota_bytes > 0 else 0
            uploaded_gb = self._group1_uploaded_bytes / (1024**3)
            
            return {
                "group1_uploaded_gb": round(uploaded_gb, 3),
                "group1_quota_gb": round(quota_gb, 3),
                "group1_full": self._group1_full,
                "group1_full_since": self._group1_full_since.isoformat() if self._group1_full_since else None,
                "active_upload_group": self.get_active_upload_group(),
                "auto_switch_enabled": settings.RCLONE_AUTO_SWITCH_GROUP,
                "group2_configured": settings.has_next_group,
                "group2_primary": settings.get_next_primary_remote(),
                "group2_backups": settings.get_next_backup_remotes(),
            }
    
    def reset(self, group: int = 1):
        """Reset tracker (untuk testing atau manual reset)."""
        with self._data_lock:
            if group == 1:
                self._group1_uploaded_bytes = 0
                self._group1_full = False
                self._group1_full_since = None
                logger.info("GroupQuotaTracker: Group 1 reset")


# ==========================================
# STORAGE GROUP SERVICE
# ==========================================

class StorageGroupService:
    """
    Service untuk resolve remote name berdasarkan path group.
    
    Ini adalah routing layer:
    - Terima path dari DB
    - Detect group (berdasarkan prefix @)
    - Return remote name yang sesuai untuk operasi (read/write)
    
    Usage:
        sgs = StorageGroupService()
        
        # Untuk IMAGE PROXY (read):
        remote, clean = sgs.resolve_remote_for_read("@manga/xxx/001.jpg")
        # → ("gdrive11", "manga/xxx/001.jpg")
        
        # Untuk UPLOAD (write):
        remote, prefix = sgs.get_upload_remote()
        # → ("gdrive11", "@")  ← kalau group 1 penuh
        # → ("gdrive", "")     ← kalau group 1 masih ok
    """
    
    def __init__(self):
        self.quota_tracker = GroupQuotaTracker.get_instance()
    
    # ==========================================
    # READ ROUTING (Image Proxy)
    # ==========================================
    
    def resolve_remote_for_read(
        self,
        raw_path: str,
        strategy: str = "round_robin"
    ) -> Tuple[str, str]:
        """
        Resolve remote name untuk READ operation (image proxy).
        
        Args:
            raw_path: Raw path dari DB (mungkin ada prefix @)
            strategy: Load balancing strategy
            
        Returns:
            (remote_name, clean_path) tuple
            
        Examples:
            "manga/xxx/001.jpg"  → ("gdrive", "manga/xxx/001.jpg")
            "@manga/xxx/001.jpg" → ("gdrive11", "manga/xxx/001.jpg")
        """
        group = get_group_for_path(raw_path)
        clean = clean_path(raw_path)
        
        if group == 2:
            remote = self._pick_remote_group2(strategy)
            logger.debug(f"Read routing: Group 2 → {remote}:{clean}")
        else:
            remote = self._pick_remote_group1(strategy)
            logger.debug(f"Read routing: Group 1 → {remote}:{clean}")
        
        return remote, clean
    
    def resolve_serve_daemon_for_read(
        self,
        raw_path: str
    ) -> Tuple[Optional[str], str]:
        """
        Resolve serve daemon URL untuk READ via HTTPX.
        
        Args:
            raw_path: Raw path dari DB
            
        Returns:
            (daemon_url, clean_path) tuple
            daemon_url = None kalau serve tidak running
        """
        group = get_group_for_path(raw_path)
        clean = clean_path(raw_path)
        
        from app.services.rclone_service import RcloneService
        
        if group == 2:
            # Cari daemon dari group 2 remotes
            for remote_name in settings.get_next_group_remotes():
                rclone = RcloneService._instances.get(remote_name)
                if rclone and rclone.is_serve_running():
                    url = rclone.get_serve_url()
                    if url:
                        logger.debug(f"Serve daemon routing: Group 2 → {url}")
                        return url, clean
        else:
            # Cari daemon dari group 1 remotes
            for remote_name in settings.get_rclone_remotes():
                rclone = RcloneService._instances.get(remote_name)
                if rclone and rclone.is_serve_running():
                    url = rclone.get_serve_url()
                    if url:
                        logger.debug(f"Serve daemon routing: Group 1 → {url}")
                        return url, clean
        
        logger.debug(f"No serve daemon found for path group {group}")
        return None, clean
    
    # ==========================================
    # WRITE ROUTING (Upload)
    # ==========================================
    
    def get_upload_group(self) -> int:
        """
        Get group yang seharusnya dipakai untuk upload baru.
        
        Returns:
            1 = Upload ke Group 1 (normal path)
            2 = Upload ke Group 2 (path prefix @)
        """
        return self.quota_tracker.get_active_upload_group()
    
    def get_upload_remote(self) -> Tuple[str, str]:
        """
        Get primary remote + path prefix untuk upload.
        
        Returns:
            (remote_name, path_prefix) tuple
            
        Examples:
            → ("gdrive", "")   ← group 1 aktif
            → ("gdrive11", "@") ← group 2 aktif
        """
        group = self.get_upload_group()
        
        if group == 2:
            remote = settings.get_next_primary_remote()
            if not remote:
                logger.warning("Group 2 not configured, falling back to Group 1")
                return settings.get_primary_remote(), ""
            logger.info(f"Upload routing: Group 2 → {remote}")
            return remote, GROUP2_PREFIX
        else:
            remote = settings.get_primary_remote()
            logger.debug(f"Upload routing: Group 1 → {remote}")
            return remote, ""
    
    def get_backup_remotes_for_upload(self) -> List[str]:
        """
        Get backup remotes untuk upload (sesuai group aktif).
        
        Returns:
            List of backup remote names
        """
        group = self.get_upload_group()
        if group == 2:
            return settings.get_next_backup_remotes()
        return settings.get_secondary_remotes()
    
    def get_all_remotes_for_upload(self) -> Tuple[str, List[str], str]:
        """
        Get semua remotes yang diperlukan untuk upload.
        
        Returns:
            (primary_remote, backup_remotes, path_prefix) tuple
        """
        group = self.get_upload_group()
        
        if group == 2:
            primary = settings.get_next_primary_remote() or settings.get_primary_remote()
            backups = settings.get_next_backup_remotes()
            prefix = GROUP2_PREFIX
        else:
            primary = settings.get_primary_remote()
            backups = settings.get_secondary_remotes()
            prefix = ""
        
        return primary, backups, prefix
    
    def record_upload_size(self, bytes_size: int):
        """
        Record bytes yang berhasil diupload (untuk quota tracking).
        Hanya record kalau upload ke group 1.
        """
        if self.get_upload_group() == 1:
            self.quota_tracker.record_upload(bytes_size)
    
    def handle_quota_exceeded(self, remote_name: str):
        """
        Handle kalau remote melaporkan quota exceeded.
        
        Jika remote ada di group 1 → mark group 1 full → switch ke group 2.
        """
        group1_remotes = settings.get_rclone_remotes()
        
        if remote_name in group1_remotes:
            logger.warning(
                f"Quota exceeded on group 1 remote '{remote_name}', "
                f"marking Group 1 as full"
            )
            self.quota_tracker.mark_group1_full(reason=f"quota_exceeded:{remote_name}")
        else:
            logger.warning(
                f"Quota exceeded on group 2 remote '{remote_name}', "
                f"consider adding more remotes to RCLONE_NEXT_BACKUP_REMOTES"
            )
    
    # ==========================================
    # PRIVATE: Remote Selection
    # ==========================================
    
    def _pick_remote_group1(self, strategy: str = "round_robin") -> str:
        """Pick remote dari group 1 untuk read."""
        remotes = settings.get_rclone_remotes()
        if not remotes:
            return settings.RCLONE_PRIMARY_REMOTE
        
        # Simple round robin (untuk read, primary saja sudah cukup kalau tidak ada backup)
        return remotes[0]
    
    def _pick_remote_group2(self, strategy: str = "round_robin") -> str:
        """Pick remote dari group 2 untuk read."""
        remotes = settings.get_next_group_remotes()
        if not remotes:
            logger.warning("Group 2 remotes not configured!")
            return settings.RCLONE_NEXT_PRIMARY_REMOTE or settings.RCLONE_PRIMARY_REMOTE
        return remotes[0]
    
    # ==========================================
    # ADMIN HELPERS
    # ==========================================
    
    def get_status(self) -> Dict:
        """Get full status untuk admin endpoint."""
        return {
            "group1": {
                "primary": settings.get_primary_remote(),
                "backups": settings.get_secondary_remotes(),
                "all_remotes": settings.get_rclone_remotes(),
                "path_prefix": "none (normal path)",
            },
            "group2": {
                "primary": settings.get_next_primary_remote(),
                "backups": settings.get_next_backup_remotes(),
                "all_remotes": settings.get_next_group_remotes(),
                "path_prefix": GROUP2_PREFIX,
                "configured": settings.has_next_group,
            },
            "routing": {
                "active_upload_group": self.get_upload_group(),
                "auto_switch_enabled": settings.RCLONE_AUTO_SWITCH_GROUP,
            },
            "quota": self.quota_tracker.get_stats(),
        }


# ==========================================
# GLOBAL INSTANCE
# ==========================================

_storage_group_service: Optional[StorageGroupService] = None
_sgs_lock = threading.Lock()


def get_storage_group_service() -> StorageGroupService:
    """Get global StorageGroupService instance."""
    global _storage_group_service
    with _sgs_lock:
        if _storage_group_service is None:
            _storage_group_service = StorageGroupService()
        return _storage_group_service