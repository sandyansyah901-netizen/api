# File: app/services/storage_group_service.py
"""
Storage Group Service - N-Group Remote Routing
===============================================
Service untuk manage multi-group storage dengan N group:
- Group 1: RCLONE_PRIMARY_REMOTE + RCLONE_BACKUP_REMOTES (path normal, tanpa prefix)
- Group 2: RCLONE_NEXT_PRIMARY_REMOTE + RCLONE_NEXT_BACKUP_REMOTES (prefix @2/)
- Group 3: RCLONE_GROUP_3_PRIMARY + RCLONE_GROUP_3_BACKUPS (prefix @3/)
- Group N: RCLONE_GROUP_N_PRIMARY + RCLONE_GROUP_N_BACKUPS (prefix @N/)

Routing logic:
- Path tanpa prefix      → Group 1
- Path dengan @2/ prefix → Group 2
- Path dengan @3/ prefix → Group 3
- Path dengan @ prefix (legacy, tanpa angka) → Group 2 (backward compat)
- Upload baru → cek quota group 1 → kalau penuh → group 2 → dst.

Database TIDAK DIUBAH strukturnya. Hanya string path yang berubah dengan prefix @N/.

Example:
  Group 1 path: "manga_library/crimson_reset/Chapter_001/001.jpg"
  Group 2 path: "@2/manga_library/crimson_reset/Chapter_002/001.jpg"
  Group 3 path: "@3/manga_library/crimson_reset/Chapter_003/001.jpg"

BACKWARD COMPAT:
  Path lama "@manga_library/xxx/001.jpg" (tanpa angka) → dianggap Group 2
"""

import logging
import re
import threading
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from datetime import datetime

from app.core.base import settings

logger = logging.getLogger(__name__)

# ==========================================
# PERSISTENT STATE: active_group.txt
# ==========================================

# Lokasi file: {project_root}/storage/active_group.txt
# project_root = 3 level di atas file ini (services → app → api_root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_FILE = _PROJECT_ROOT / "storage" / "active_group.txt"


def _read_active_group_file() -> Optional[int]:
    """
    Baca active group dari file persisten.
    Return None jika file tidak ada atau invalid.
    """
    try:
        if not _STATE_FILE.exists():
            return None
        content = _STATE_FILE.read_text(encoding="utf-8").strip()
        val = int(content)
        if val >= 1:
            logger.info(f"📂 Restored active_upload_group={val} dari {_STATE_FILE}")
            return val
    except Exception as e:
        logger.warning(f"⚠️ Gagal baca active_group.txt: {e}")
    return None


def _write_active_group_file(group: int) -> bool:
    """
    Simpan active group ke file persisten.
    Return True jika berhasil.
    """
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(str(group), encoding="utf-8")
        logger.info(f"💾 Saved active_upload_group={group} ke {_STATE_FILE}")
        return True
    except Exception as e:
        logger.error(f"❌ Gagal simpan active_group.txt: {e}")
        return False



# ==========================================
# PATH ROUTING CONSTANTS & HELPERS
# ==========================================

# Pattern untuk numeric prefix: @2/, @3/, @4/, dst.
_GROUP_PREFIX_RE = re.compile(r'^@(\d+)/')

# Prefix untuk group 2+ (format baru)
def get_group_prefix(group: int) -> str:
    """
    Get path prefix untuk group N.

    Returns:
        ""     untuk group 1 (tanpa prefix)
        "@2/"  untuk group 2
        "@3/"  untuk group 3
        dst.

    Examples:
        get_group_prefix(1) → ""
        get_group_prefix(2) → "@2/"
        get_group_prefix(3) → "@3/"
    """
    if group <= 1:
        return ""
    return f"@{group}/"


def get_group_for_path(path: str) -> int:
    """
    Detect group number dari path di database.

    Mendukung:
    - Path tanpa prefix          → Group 1
    - "@2/manga/..."             → Group 2 (format baru)
    - "@3/manga/..."             → Group 3 (format baru)
    - "@manga/..." (legacy)     → Group 2 (backward compat)

    Args:
        path: File path dari database

    Returns:
        Group number (1, 2, 3, ...)

    Examples:
        "manga_library/xxx/001.jpg"    → 1
        "@2/manga_library/xxx/001.jpg" → 2
        "@3/manga_library/xxx/001.jpg" → 3
        "@manga_library/xxx/001.jpg"   → 2 (legacy)
    """
    if not path:
        return 1
    m = _GROUP_PREFIX_RE.match(str(path))
    if m:
        return int(m.group(1))
    # Legacy: path mulai dengan @ tapi tanpa angka → group 2
    if str(path).startswith("@"):
        return 2
    return 1


def clean_path(path: str) -> str:
    """
    Strip group prefix dari path untuk dipakai ke rclone.

    Mendukung format baru (@N/) dan legacy (@).

    Args:
        path: Raw path dari DB (mungkin ada prefix @N/)

    Returns:
        Clean path tanpa prefix apapun

    Examples:
        "@2/manga/xxx/001.jpg" → "manga/xxx/001.jpg"
        "@3/manga/xxx/001.jpg" → "manga/xxx/001.jpg"
        "@manga/xxx/001.jpg"   → "manga/xxx/001.jpg"  (legacy)
        "manga/xxx/001.jpg"    → "manga/xxx/001.jpg"
    """
    if not path:
        return path
    # Format baru: @2/, @3/, dst.
    m = _GROUP_PREFIX_RE.match(str(path))
    if m:
        return str(path)[m.end():]
    # Legacy: strip single @
    if str(path).startswith("@"):
        return str(path)[1:]
    return str(path)


def mark_as_group(path: str, group: int) -> str:
    """
    Tambah prefix @N/ ke path untuk menandai file ada di Group N.

    Args:
        path:  Clean path tanpa prefix (actual rclone path)
        group: Group number (1, 2, 3, ...)

    Returns:
        Path dengan prefix @N/ (atau tanpa prefix jika group 1)

    Examples:
        mark_as_group("manga/xxx/001.jpg", 1) → "manga/xxx/001.jpg"
        mark_as_group("manga/xxx/001.jpg", 2) → "@2/manga/xxx/001.jpg"
        mark_as_group("manga/xxx/001.jpg", 3) → "@3/manga/xxx/001.jpg"
    """
    clean = clean_path(path)
    prefix = get_group_prefix(group)
    return f"{prefix}{clean}"


# ---- Backward-compat aliases ----

def is_group2_path(path: str) -> bool:
    """Check apakah path ada di Group 2 (termasuk legacy @manga/...)."""
    return get_group_for_path(path) == 2


def mark_as_group2(path: str) -> str:
    """Mark path sebagai Group 2 dengan format baru (@2/)."""
    return mark_as_group(path, 2)


# ==========================================
# GROUP CONFIG READER (dari env vars)
# ==========================================

def _get_group_config(group: int) -> Optional[Dict]:
    """
    Baca konfigurasi remote untuk group N dari environment/settings.

    Group 1 → pakai RCLONE_PRIMARY_REMOTE + RCLONE_BACKUP_REMOTES
    Group 2 → pakai RCLONE_NEXT_PRIMARY_REMOTE + RCLONE_NEXT_BACKUP_REMOTES
    Group N → pakai RCLONE_GROUP_N_PRIMARY + RCLONE_GROUP_N_BACKUPS

    Returns:
        Dict {"primary": str, "backups": List[str], "quota_gb": int}
        atau None jika group tidak dikonfigurasi.
    """
    if group == 1:
        primary = settings.RCLONE_PRIMARY_REMOTE
        if not primary:
            return None
        backups_str = settings.RCLONE_BACKUP_REMOTES or ""
        backups = [r.strip() for r in backups_str.split(",") if r.strip()]
        quota_gb = settings.RCLONE_GROUP1_QUOTA_GB
        return {"primary": primary, "backups": backups, "quota_gb": quota_gb}

    if group == 2:
        primary = settings.RCLONE_NEXT_PRIMARY_REMOTE
        if not primary or not primary.strip():
            return None
        backups_str = settings.RCLONE_NEXT_BACKUP_REMOTES or ""
        backups = [r.strip() for r in backups_str.split(",") if r.strip()]
        quota_gb = settings.RCLONE_GROUP2_QUOTA_GB
        return {"primary": primary.strip(), "backups": backups, "quota_gb": quota_gb}

    # Group 3: baca dari settings terlebih dulu, fallback ke os.environ
    if group == 3:
        primary = settings.RCLONE_GROUP_3_PRIMARY.strip()
        if not primary:
            return None
        backups_str = settings.RCLONE_GROUP_3_BACKUPS or ""
        backups = [r.strip() for r in backups_str.split(",") if r.strip()]
        return {"primary": primary, "backups": backups, "quota_gb": settings.RCLONE_GROUP_3_QUOTA_GB}

    # Group 4: baca dari settings terlebih dulu
    if group == 4:
        primary = settings.RCLONE_GROUP_4_PRIMARY.strip()
        if not primary:
            return None
        backups_str = settings.RCLONE_GROUP_4_BACKUPS or ""
        backups = [r.strip() for r in backups_str.split(",") if r.strip()]
        return {"primary": primary, "backups": backups, "quota_gb": settings.RCLONE_GROUP_4_QUOTA_GB}

    # Group 5: baca dari settings terlebih dulu
    if group == 5:
        primary = settings.RCLONE_GROUP_5_PRIMARY.strip()
        if not primary:
            return None
        backups_str = settings.RCLONE_GROUP_5_BACKUPS or ""
        backups = [r.strip() for r in backups_str.split(",") if r.strip()]
        return {"primary": primary, "backups": backups, "quota_gb": settings.RCLONE_GROUP_5_QUOTA_GB}

    # Group 6+: baca dari os.environ secara dinamis
    primary = os.environ.get(f"RCLONE_GROUP_{group}_PRIMARY", "").strip()
    if not primary:
        return None
    backups_str = os.environ.get(f"RCLONE_GROUP_{group}_BACKUPS", "")
    backups = [r.strip() for r in backups_str.split(",") if r.strip()]
    quota_gb = int(os.environ.get(f"RCLONE_GROUP_{group}_QUOTA_GB", "0"))
    return {"primary": primary, "backups": backups, "quota_gb": quota_gb}


def _get_all_configured_groups() -> List[int]:
    """
    Scan semua group yang dikonfigurasi (1, 2, 3, ...).

    Scan dilakukan sequential dari group 1. Berhenti saat menemukan
    group yang tidak dikonfigurasi (gap = stop).

    Returns:
        List of configured group numbers, e.g. [1, 2, 3]
    """
    groups = []
    for n in range(1, 20):  # Max 20 groups
        cfg = _get_group_config(n)
        if cfg is None:
            break
        groups.append(n)
    return groups if groups else [1]


# ==========================================
# QUOTA TRACKER (N-GROUP)
# ==========================================

class GroupQuotaTracker:
    """
    Track penggunaan quota per group untuk auto-switch.

    Cara kerja:
    - Setiap upload berhasil → tambah ukuran ke total_uploaded group tersebut
    - Kalau total_uploaded >= quota_limit → mark group sebagai "full"
    - Kalau group N full → upload berikutnya ke group N+1

    State ini IN-MEMORY (tidak persisten ke DB).
    Kalau server restart, tracker reset tapi file di GDrive tetap ada.
    Path prefix @N/ di DB yang jadi ground truth.
    """

    _instance: Optional['GroupQuotaTracker'] = None
    _lock = threading.Lock()

    def __init__(self):
        self._data_lock = threading.Lock()
        # Per-group state: { group_num: {"uploaded_bytes": int, "is_full": bool, "full_since": datetime|None} }
        self._group_state: Dict[int, Dict] = {}
        self._init_groups()

        logger.info(
            f"GroupQuotaTracker initialized: "
            f"configured_groups={list(self._group_state.keys())}, "
            f"auto_switch={settings.RCLONE_AUTO_SWITCH_GROUP}"
        )

    def _init_groups(self):
        """Initialize state untuk semua configured groups, lalu restore active group dari file."""
        configured = _get_all_configured_groups()
        for g in configured:
            cfg = _get_group_config(g)
            quota_bytes = (cfg["quota_gb"] * 1024 * 1024 * 1024) if cfg and cfg["quota_gb"] > 0 else 0
            self._group_state[g] = {
                "uploaded_bytes": 0,
                "is_full": False,
                "full_since": None,
                "quota_bytes": quota_bytes,
            }

        # ✅ Restore active group dari file persisten
        saved_group = _read_active_group_file()
        if saved_group and saved_group > 1 and saved_group in self._group_state:
            # Mark semua group sebelumnya sebagai full agar tracker otomatis arahkan ke saved_group
            for g in configured:
                if g < saved_group:
                    self._group_state[g]["is_full"] = True
                    self._group_state[g]["full_since"] = datetime.now()
            logger.info(f"✅ Active upload group restored: Group {saved_group} (from file)")

    @classmethod
    def get_instance(cls) -> 'GroupQuotaTracker':
        """Singleton accessor."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def record_upload(self, bytes_uploaded: int, group: int = 1):
        """Record bytes yang berhasil diupload ke group tertentu."""
        with self._data_lock:
            if group not in self._group_state:
                return
            state = self._group_state[group]
            state["uploaded_bytes"] += bytes_uploaded

            # Auto-check quota
            quota = state["quota_bytes"]
            if (
                quota > 0
                and not state["is_full"]
                and state["uploaded_bytes"] >= quota
            ):
                state["is_full"] = True
                state["full_since"] = datetime.now()
                logger.warning(
                    f"⚠️ Group {group} quota reached! "
                    f"Uploaded: {state['uploaded_bytes'] / (1024**3):.2f}GB / "
                    f"{quota / (1024**3):.2f}GB. "
                    f"Will switch to Group {group + 1} for new uploads."
                )

    def mark_group_full(self, group: int, reason: str = "manual"):
        """Manual mark group N sebagai penuh."""
        with self._data_lock:
            if group not in self._group_state:
                # Tambah state on-the-fly
                self._group_state[group] = {
                    "uploaded_bytes": 0,
                    "is_full": True,
                    "full_since": datetime.now(),
                    "quota_bytes": 0,
                }
            elif not self._group_state[group]["is_full"]:
                self._group_state[group]["is_full"] = True
                self._group_state[group]["full_since"] = datetime.now()
                logger.warning(f"⚠️ Group {group} marked as FULL (reason: {reason})")

    def is_group_full(self, group: int) -> bool:
        """Check apakah group N sudah penuh."""
        with self._data_lock:
            if group not in self._group_state:
                return False
            return self._group_state[group]["is_full"]

    # Backward compat alias
    def mark_group1_full(self, reason: str = "manual"):
        self.mark_group_full(1, reason)

    def is_group1_full(self) -> bool:
        return self.is_group_full(1)

    def get_active_upload_group(self) -> int:
        """
        Get group yang seharusnya dipakai untuk upload baru.

        Iterasi dari group 1 ke atas. Return group pertama yang belum penuh
        dan masih dikonfigurasi.

        Returns:
            Group number (1, 2, 3, ...) yang aktif untuk upload
        """
        if not settings.RCLONE_AUTO_SWITCH_GROUP:
            # ✅ Auto switch off: baca dari file state untuk restore group setelah restart
            saved = _read_active_group_file()
            if saved and saved >= 1 and saved in self._group_state:
                return saved
            return 1

        with self._data_lock:
            configured = sorted(self._group_state.keys())
            for g in configured:
                if not self._group_state[g]["is_full"]:
                    return g
            # Semua penuh → return group terakhir (terpaksa)
            return configured[-1] if configured else 1

    def get_stats(self) -> Dict:
        """Get statistics untuk admin endpoint. Lock-safe: tidak blocking di dalam lock."""
        # ✅ Ambil config DULU di luar lock (tidak blocking)
        configured = list(self._group_state.keys())
        group_configs = {g: _get_group_config(g) for g in configured}

        # ✅ Baru lock untuk baca state (cepat, tidak ada IO)
        with self._data_lock:
            snapshot = {g: dict(state) for g, state in self._group_state.items()}

        # ✅ Build result di luar lock
        groups_stats = {}
        for g in configured:
            state = snapshot.get(g, {})
            cfg = group_configs.get(g)
            groups_stats[g] = {
                "group": g,
                "primary": cfg["primary"] if cfg else None,
                "backups": cfg["backups"] if cfg else [],
                "quota_gb": round(state.get("quota_bytes", 0) / (1024**3), 3) if state.get("quota_bytes", 0) > 0 else 0,
                "uploaded_gb": round(state.get("uploaded_bytes", 0) / (1024**3), 3),
                "is_full": state.get("is_full", False),
                "full_since": state["full_since"].isoformat() if state.get("full_since") else None,
                "prefix": get_group_prefix(g),
            }

        return {

                "active_upload_group": self.get_active_upload_group(),
                "auto_switch_enabled": settings.RCLONE_AUTO_SWITCH_GROUP,
                "groups": groups_stats,
            }

    def reset(self, group: int = 1):
        """Reset tracker untuk group tertentu."""
        with self._data_lock:
            if group in self._group_state:
                self._group_state[group]["uploaded_bytes"] = 0
                self._group_state[group]["is_full"] = False
                self._group_state[group]["full_since"] = None
                logger.info(f"GroupQuotaTracker: Group {group} reset")


# ==========================================
# STORAGE GROUP SERVICE
# ==========================================

class StorageGroupService:
    """
    Service untuk resolve remote name berdasarkan path group.

    Ini adalah routing layer:
    - Terima path dari DB
    - Detect group (berdasarkan prefix @N/)
    - Return remote name yang sesuai untuk operasi (read/write)

    Usage:
        sgs = StorageGroupService()

        # Untuk IMAGE PROXY (read):
        remote, clean = sgs.resolve_remote_for_read("@2/manga/xxx/001.jpg")
        # → ("gdrive11", "manga/xxx/001.jpg")

        # Untuk UPLOAD (write):
        remote, prefix = sgs.get_upload_remote()
        # → ("gdrive11", "@2/")  ← kalau group 2 aktif
        # → ("gdrive", "")       ← kalau group 1 aktif
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
            raw_path: Raw path dari DB (mungkin ada prefix @N/)
            strategy: Load balancing strategy

        Returns:
            (remote_name, clean_path) tuple

        Examples:
            "manga/xxx/001.jpg"    → ("gdrive", "manga/xxx/001.jpg")
            "@2/manga/xxx/001.jpg" → ("gdrive11", "manga/xxx/001.jpg")
            "@manga/xxx/001.jpg"   → ("gdrive11", "manga/xxx/001.jpg")  # legacy
        """
        group = get_group_for_path(raw_path)
        clean = clean_path(raw_path)
        cfg = _get_group_config(group)

        if cfg:
            remote = cfg["primary"]
            logger.debug(f"Read routing: Group {group} → {remote}:{clean}")
        else:
            # Fallback ke group 1
            remote = settings.RCLONE_PRIMARY_REMOTE
            logger.warning(f"Group {group} not configured, falling back to Group 1")

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
        cfg = _get_group_config(group)

        from app.services.rclone_service import RcloneService

        # Cari daemon dari remotes group ini
        remotes_to_check = []
        if cfg:
            remotes_to_check = [cfg["primary"]] + cfg["backups"]
        else:
            remotes_to_check = [settings.RCLONE_PRIMARY_REMOTE]

        for remote_name in remotes_to_check:
            rclone = RcloneService._instances.get(remote_name)
            if rclone and rclone.is_serve_running():
                url = rclone.get_serve_url()
                if url:
                    logger.debug(f"Serve daemon routing: Group {group} → {url}")
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
            Group number (1, 2, 3, ...) yang aktif untuk upload
        """
        return self.quota_tracker.get_active_upload_group()

    def get_upload_remote(self) -> Tuple[str, str]:
        """
        Get primary remote + path prefix untuk upload.

        Returns:
            (remote_name, path_prefix) tuple

        Examples:
            → ("gdrive", "")    ← group 1 aktif
            → ("gdrive11", "@2/") ← group 2 aktif
            → ("gdrive21", "@3/") ← group 3 aktif
        """
        group = self.get_upload_group()
        cfg = _get_group_config(group)

        if not cfg:
            # Fallback ke group 1
            logger.warning(f"Group {group} not configured, falling back to Group 1")
            return settings.get_primary_remote(), ""

        prefix = get_group_prefix(group)
        logger.info(f"Upload routing: Group {group} → {cfg['primary']} (prefix='{prefix}')")
        return cfg["primary"], prefix

    def get_backup_remotes_for_upload(self) -> List[str]:
        """
        Get backup remotes untuk upload (sesuai group aktif).

        Returns:
            List of backup remote names
        """
        group = self.get_upload_group()
        cfg = _get_group_config(group)
        if cfg:
            return cfg["backups"]
        return settings.get_secondary_remotes()

    def get_all_remotes_for_upload(self) -> Tuple[str, List[str], str]:
        """
        Get semua remotes yang diperlukan untuk upload.

        Returns:
            (primary_remote, backup_remotes, path_prefix) tuple
        """
        group = self.get_upload_group()
        cfg = _get_group_config(group)

        if not cfg:
            return settings.get_primary_remote(), settings.get_secondary_remotes(), ""

        prefix = get_group_prefix(group)
        return cfg["primary"], cfg["backups"], prefix

    def make_db_path(self, clean_gdrive_path: str) -> str:
        """
        Buat path untuk disimpan ke DB (dengan prefix group aktif).

        Args:
            clean_gdrive_path: Path tanpa prefix (actual rclone path)

        Returns:
            Path dengan prefix @N/ sesuai active group
        """
        group = self.get_upload_group()
        return mark_as_group(clean_gdrive_path, group)

    def record_upload_size(self, bytes_size: int):
        """
        Record bytes yang berhasil diupload (untuk quota tracking).
        """
        group = self.get_upload_group()
        self.quota_tracker.record_upload(bytes_size, group)

    def handle_quota_exceeded(self, remote_name: str):
        """
        Handle kalau remote melaporkan quota exceeded.
        """
        # Cari group yang berisi remote ini
        for n in _get_all_configured_groups():
            cfg = _get_group_config(n)
            if cfg and remote_name in ([cfg["primary"]] + cfg["backups"]):
                logger.warning(
                    f"Quota exceeded on group {n} remote '{remote_name}', "
                    f"marking Group {n} as full"
                )
                self.quota_tracker.mark_group_full(n, reason=f"quota_exceeded:{remote_name}")
                return

        logger.warning(f"Remote '{remote_name}' not found in any configured group")

    # ==========================================
    # ADMIN HELPERS
    # ==========================================

    def get_status(self) -> Dict:
        """Get full status untuk admin endpoint."""
        configured = _get_all_configured_groups()
        groups_info = {}

        for g in configured:
            cfg = _get_group_config(g)
            if cfg:
                groups_info[g] = {
                    "group": g,
                    "primary": cfg["primary"],
                    "backups": cfg["backups"],
                    "all_remotes": [cfg["primary"]] + cfg["backups"],
                    "path_prefix": get_group_prefix(g) or "none (normal path)",
                    "quota_gb": cfg["quota_gb"],
                    "configured": True,
                }

        quota_stats = self.quota_tracker.get_stats()

        return {
            "active_upload_group": self.get_upload_group(),
            "auto_switch_enabled": settings.RCLONE_AUTO_SWITCH_GROUP,
            "configured_groups": configured,
            "groups": groups_info,
            "quota": quota_stats,
        }

    def switch_upload_group(self, group: int) -> Dict:
        """
        Manual switch active upload group ke group N.

        Args:
            group: Target group number

        Returns:
            Result dict dengan previous_group, active_group, success
        """
        configured = _get_all_configured_groups()
        if group not in configured:
            return {
                "success": False,
                "error": f"Group {group} tidak dikonfigurasi. Configured groups: {configured}"
            }

        previous = self.get_upload_group()

        # Update module-level state di base.py
        try:
            settings.set_active_upload_group(group)
        except ValueError:
            pass  # base.py masih validate 1 atau 2; kita handle via quota tracker

        # Tandai semua group sebelumnya sebagai "full" agar otomatis pakai group target
        for g in configured:
            if g < group:
                self.quota_tracker.mark_group_full(g, reason=f"manual_switch_to_group_{group}")
            elif g == group:
                # Reset group target agar tidak dianggap full
                self.quota_tracker.reset(g)

        # ✅ Persist ke file agar tidak reset saat server restart
        _write_active_group_file(group)

        cfg = _get_group_config(group)
        prefix = get_group_prefix(group)

        logger.info(f"✅ Manual switch: Group {previous} → Group {group} (persisted to file)")


        return {
            "success": True,
            "previous_group": previous,
            "active_group": group,
            "remote": cfg["primary"] if cfg else None,
            "prefix": prefix,
            "message": f"Switched to Group {group} (prefix: '{prefix or 'none'}')"
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