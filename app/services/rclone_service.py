"""
Rclone Service - WITH SERVE HTTP DAEMON SUPPORT + HTTPX (ULTRA FAST!)
======================================================================
Service untuk interaksi dengan Google Drive menggunakan Rclone.

REVISI:
✅ ✨ ADDED: HttpxClientManager singleton (1 client per serve URL)
✅ ✨ CHANGED: download_file_to_memory → HTTPX dulu, fallback rclone cat
✅ ✨ ADDED: download_file_async (async native)
✅ ✨ ADDED: stream_file_async (true streaming, tidak load ke memory)
✅ ✨ CHANGED: _check_serve_daemon via httpx.Client (bukan requests)
✅ ✨ REMOVED: import requests (diganti httpx sepenuhnya)
✅ ✨ FIXED: Double daemon - _start_serve_daemon_once() cek dulu sebelum start
✅ ✨ ADDED: shutdown_all() async untuk lifespan cleanup
✅ ✨ ADDED: _clean_env_for_rclone() - strip RCLONE_* OS env sebelum subprocess

✅ ✨ ADDED: upload_folder_bulk() - Upload seluruh folder sekaligus (PERFORMANCE!)
            Menggantikan upload file satu-satu dengan rclone copy --transfers 8
            Jauh lebih cepat: 150 file yang tadinya 4 menit → ~20-40 detik

Semua logic lama TETAP ADA:
- Windows + Linux compatible
- Singleton pattern per remote
- Retry mechanism
- Natural sorting
- Executor stats
- Semua rclone command (list, download, delete, dll)
"""

import subprocess
import json
import re
import logging
import shutil
import threading
import atexit
import signal
import time
import asyncio
import httpx
import os
from typing import List, Optional, Dict, Any
from pathlib import Path

from app.core.base import settings

logger = logging.getLogger(__name__)

# Pre-compile regex for performance
NUMBER_PATTERN = re.compile(r'([0-9]+)')


class RcloneError(Exception):
    """Custom exception for Rclone errors"""
    pass


# ==========================================
# ✅ HTTPX CLIENT MANAGER - SINGLETON PER URL
# ==========================================

class HttpxClientManager:
    """
    Singleton HTTPX AsyncClient per base_url.

    Kenapa singleton:
    - Reuse connection pool (tidak buka TCP baru tiap request)
    - Keepalive connections ke serve daemon
    - Hemat resource, latency lebih rendah

    Usage:
        client = HttpxClientManager.get_client("http://127.0.0.1:8180")
        resp = await client.get("/path/to/file.jpg")
    """
    _clients: Dict[str, httpx.AsyncClient] = {}
    _lock = threading.Lock()

    @classmethod
    def get_client(cls, base_url: str) -> httpx.AsyncClient:
        """Get or create singleton AsyncClient untuk base_url."""
        with cls._lock:
            if base_url not in cls._clients:
                cls._clients[base_url] = httpx.AsyncClient(
                    base_url=base_url,
                    timeout=httpx.Timeout(
                        connect=5.0,
                        read=30.0,
                        write=10.0,
                        pool=5.0
                    ),
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=100,
                        keepalive_expiry=30.0
                    ),
                    follow_redirects=True
                )
                logger.info(f"✅ HTTPX AsyncClient created for: {base_url}")
            return cls._clients[base_url]

    @classmethod
    async def close_all(cls):
        """Tutup semua HTTPX clients saat shutdown."""
        with cls._lock:
            for url, client in cls._clients.items():
                try:
                    await client.aclose()
                    logger.info(f"✅ HTTPX client closed: {url}")
                except Exception as e:
                    logger.error(f"Error closing HTTPX client {url}: {e}")
            cls._clients.clear()
            logger.info("✅ All HTTPX clients closed")


# ==========================================
# ✅ EXECUTOR STATS (TIDAK BERUBAH)
# ==========================================

def get_executor_stats() -> Dict:
    """Get statistics about thread pool executor usage."""
    try:
        import psutil

        active_threads = threading.active_count()

        process = psutil.Process()
        memory_mb = process.memory_info().rss / (1024 * 1024)
        cpu_percent = process.cpu_percent(interval=0.1)

        if active_threads < 100:
            thread_status = "healthy"
            recommendation = "Running normally"
        elif active_threads < 500:
            thread_status = "good"
            recommendation = "Moderate load"
        elif active_threads < 1000:
            thread_status = "warning"
            recommendation = "High load - consider monitoring"
        else:
            thread_status = "critical"
            recommendation = "Very high load - consider rate limiting or scaling"

        return {
            "active_threads": active_threads,
            "memory_usage_mb": round(memory_mb, 2),
            "cpu_percent": round(cpu_percent, 2),
            "status": thread_status,
            "recommendation": recommendation,
            "executor_type": "ThreadPoolExecutor (unlimited workers)"
        }
    except ImportError:
        return {
            "active_threads": threading.active_count(),
            "status": "unknown",
            "error": "psutil not installed (run: pip install psutil)",
            "executor_type": "ThreadPoolExecutor (unlimited workers)"
        }
    except Exception as e:
        return {
            "error": str(e),
            "active_threads": threading.active_count(),
            "status": "error"
        }


# ==========================================
# ✅ NEW: Clean RCLONE_* env vars sebelum subprocess
# ==========================================

def _clean_env_for_rclone() -> dict:
    """
    Remove all RCLONE_* environment variables from subprocess env.

    WHY:
    - rclone.exe auto-reads RCLONE_* vars from OS environment as CLI flags
    - e.g., RCLONE_TIMEOUT=30 is invalid (rclone needs '30s' unit)
    - This can silently override our explicit CLI flags
    - By cleaning env, rclone ONLY uses flags we explicitly pass via CLI args

    EXAMPLE BUG WITHOUT THIS FIX:
    - OS has: RCLONE_TIMEOUT=30
    - rclone reads: --timeout 30  (invalid, needs --timeout 30s)
    - Result: all rclone commands fail with timeout error

    Returns:
        Clean environment dict without any RCLONE_* keys
    """
    clean_env = os.environ.copy()
    removed = []

    for key in list(clean_env.keys()):
        if key.startswith("RCLONE_"):
            del clean_env[key]
            removed.append(key)

    if removed:
        logger.debug(f"Cleaned RCLONE_* env vars before subprocess: {removed}")

    return clean_env


class RcloneService:
    """
    Service untuk interaksi dengan Google Drive menggunakan Rclone.

    ✅ ✨ HTTPX: Semua HTTP request ke serve daemon pakai HTTPX (bukan requests)
    ✅ ✨ SINGLETON PER REMOTE: 1 instance = 1 daemon, tidak double-start
    ✅ ✨ stream_file_async: True streaming tanpa buffer ke memory
    ✅ ✨ download_file_async: Async native via HTTPX
    ✅ ✨ _clean_env_for_rclone: Strip RCLONE_* OS env sebelum subprocess
    ✅ ✨ upload_folder_bulk: Upload SELURUH FOLDER sekaligus (PERFORMANCE BOOST!)
    ✅ Singleton Pattern (tetap sama)
    ✅ Retry mechanism (tetap sama)
    ✅ Natural sorting (tetap sama)
    """

    # ==========================================
    # ✅ SINGLETON IMPLEMENTATION (TIDAK BERUBAH)
    # ==========================================
    _instances: Dict[str, 'RcloneService'] = {}
    _instances_lock = threading.Lock()
    _rclone_exe_cache: Optional[str] = None
    _rclone_exe_verified: bool = False

    # ✅ SERVE HTTP DAEMON REGISTRY
    # {remote_name: {process, port, url, started_at}}
    _serve_daemons: Dict[str, Dict] = {}
    _serve_lock = threading.Lock()
    _serve_port_counter = 0
    _shutdown_registered = False

    def __new__(cls, remote_name: Optional[str] = None):
        """Singleton: Return existing instance if already created."""
        if remote_name is None:
            remote_name = settings.RCLONE_PRIMARY_REMOTE

        with cls._instances_lock:
            if remote_name not in cls._instances:
                logger.info(f"🆕 Creating NEW RcloneService instance for remote '{remote_name}'")
                instance = super(RcloneService, cls).__new__(cls)
                cls._instances[remote_name] = instance
                instance._initialized = False
            else:
                logger.debug(f"♻️ Reusing existing RcloneService instance for remote '{remote_name}'")

            return cls._instances[remote_name]

    def __init__(self, remote_name: Optional[str] = None):
        """Initialize Rclone service with validation."""
        if getattr(self, '_initialized', False):
            return

        if remote_name is None:
            remote_name = settings.RCLONE_PRIMARY_REMOTE
        self.remote_name = remote_name

        # ✅ Validate rclone installation
        self._validate_rclone_installation()

        # ✅ Validate remote configuration
        self._validate_remote_configuration()

        # ✅ ✨ FIXED: Start serve daemon hanya jika belum running untuk remote ini
        if settings.RCLONE_SERVE_HTTP_ENABLED:
            self._start_serve_daemon_once()

        # ✅ Register shutdown handler (once globally)
        if not RcloneService._shutdown_registered:
            atexit.register(RcloneService._shutdown_all_daemons_sync)

            try:
                signal.signal(signal.SIGTERM, RcloneService._signal_handler)
                signal.signal(signal.SIGINT, RcloneService._signal_handler)
            except (ValueError, OSError):
                logger.warning("Cannot register signal handlers (not main thread)")

            RcloneService._shutdown_registered = True

        self._initialized = True

        logger.info(
            f"✅ RcloneService initialized successfully for remote '{self.remote_name}'",
            extra={
                "remote": self.remote_name,
                "executable": self.rclone_exe,
                "serve_enabled": settings.RCLONE_SERVE_HTTP_ENABLED,
                "serve_url": self.get_serve_url() or "disabled"
            }
        )

    # ==========================================
    # ✅ TIMEOUT CONVERSION HELPER (TIDAK BERUBAH)
    # ==========================================

    @staticmethod
    def _format_timeout(timeout_seconds: int) -> str:
        """
        Convert integer timeout to rclone duration string.
        e.g. 30 → "30s"
        """
        return f"{timeout_seconds}s"

    # ==========================================
    # ✅ ✨ SERVE DAEMON - START ONCE (FIXED DOUBLE START)
    # ==========================================

    def _start_serve_daemon_once(self):
        """
        ✅ NON-BLOCKING: Start rclone serve http di background thread.

        Server startup tidak menunggu daemon siap.
        Daemon health check dilakukan di background:
        - Polling tiap 0.5s hingga 60s
        - Jika berhasil → URL tersimpan di _serve_daemons, siap dipakai
        - Jika gagal → fallback ke 'rclone cat' tetap berjalan

        Thread-safe: gunakan _serve_lock saat akses _serve_daemons.
        """
        with self._serve_lock:
            # ✅ Cek apakah daemon untuk remote ini sudah running
            if self.remote_name in self._serve_daemons:
                existing = self._serve_daemons[self.remote_name]
                if existing["process"].poll() is None:
                    logger.info(
                        f"♻️ Serve daemon already running for '{self.remote_name}' "
                        f"at {existing['url']}, skipping"
                    )
                    return
                else:
                    logger.warning(f"⚠️ Serve daemon for '{self.remote_name}' was dead, restarting...")
                    del self._serve_daemons[self.remote_name]

            # Allocate port — worker-aware formula:
            # PORT = BASE_PORT + (worker_index * 20) + remote_counter
            # - worker_index: from WORKER_INDEX env var (set by gunicorn post_fork)
            #   → 0 when not set = single worker mode (uvicorn dev), backward compat
            # - 20 port slots per worker = supports up to 20 remotes per worker
            # - remote_counter: increments per remote within this worker process
            # Example with 3 workers + 3 remotes:
            #   Worker 0: gdrive→8180, gdrive1→8181, gdrive2→8182
            #   Worker 1: gdrive→8200, gdrive1→8201, gdrive2→8202
            #   Worker 2: gdrive→8220, gdrive1→8221, gdrive2→8222
            _worker_index = int(os.environ.get("WORKER_INDEX", "0"))
            _worker_port_offset = _worker_index * int(os.environ.get("WORKER_PORT_SLOTS", "20"))
            port = settings.RCLONE_SERVE_HTTP_PORT_START + _worker_port_offset + self._serve_port_counter
            RcloneService._serve_port_counter += 1

            if _worker_index > 0:
                logger.info(
                    f"🔢 Worker {_worker_index}: port offset={_worker_port_offset}, "
                    f"allocated port={port} for '{self.remote_name}'"
                )

            host = settings.RCLONE_SERVE_HTTP_HOST
            addr = f"{host}:{port}"
            url = f"http://{addr}"

            # Build command
            cmd = [
                self.rclone_exe,
                "serve", "http",
                f"{self.remote_name}:",
                "--addr", addr,
                "--vfs-cache-mode", settings.RCLONE_SERVE_HTTP_VFS_CACHE_MODE,
                "--buffer-size", settings.RCLONE_SERVE_HTTP_BUFFER_SIZE,
                "--vfs-cache-max-size", settings.RCLONE_SERVE_HTTP_VFS_CACHE_MAX_SIZE,
                "--vfs-cache-max-age", settings.RCLONE_SERVE_HTTP_VFS_CACHE_MAX_AGE,
                "--log-level", "ERROR",
            ]

            if settings.RCLONE_SERVE_HTTP_NO_CHECKSUM:
                cmd.append("--no-checksum")

            if settings.RCLONE_SERVE_HTTP_READ_ONLY:
                cmd.append("--read-only")

            if settings.RCLONE_SERVE_HTTP_AUTH:
                parts = settings.RCLONE_SERVE_HTTP_AUTH.split(":", 1)
                if len(parts) == 2:
                    cmd.extend(["--user", parts[0], "--pass", parts[1]])

            try:
                logger.info(f"🚀 Starting serve daemon for '{self.remote_name}' on port {port}...")

                clean_env = _clean_env_for_rclone()

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=clean_env
                )

                # ✅ NON-BLOCKING: simpan process dulu dengan status "starting"
                # Background thread akan update ke "running" setelah health check OK
                self._serve_daemons[self.remote_name] = {
                    "process": process,
                    "port": port,
                    "url": None,  # None = belum siap, image proxy pakai fallback
                    "started_at": time.time(),
                    "status": "starting",
                }

            except Exception as e:
                logger.error(
                    f"❌ Failed to launch serve daemon for '{self.remote_name}': {str(e)}",
                    exc_info=True
                )
                return

        # ✅ Health check di background thread (tidak block __init__)
        remote_name = self.remote_name
        startup_timeout = settings.RCLONE_SERVE_HTTP_STARTUP_TIMEOUT

        def _background_health_check():
            deadline = time.time() + startup_timeout
            while time.time() < deadline:
                with self._serve_lock:
                    entry = self._serve_daemons.get(remote_name)
                    if not entry:
                        return  # Daemon dihapus (shutdown)
                    process = entry["process"]

                if process.poll() is not None:
                    # Process sudah exit → gagal
                    try:
                        stderr_out = process.stderr.read()
                        error_msg = stderr_out.decode('utf-8', errors='ignore').strip()
                    except Exception:
                        error_msg = "(no stderr)"
                    logger.error(
                        f"❌ Serve daemon for '{remote_name}' exited early. "
                        f"stderr: {error_msg[:300]}"
                    )
                    with self._serve_lock:
                        self._serve_daemons.pop(remote_name, None)
                    return

                # HTTP health check
                try:
                    with httpx.Client(timeout=2.0) as client:
                        resp = client.get(url)
                        if resp.status_code < 500:
                            with self._serve_lock:
                                if remote_name in self._serve_daemons:
                                    self._serve_daemons[remote_name]["url"] = url
                                    self._serve_daemons[remote_name]["status"] = "running"
                            logger.info(f"✅ Serve daemon ready: {url} (remote: {remote_name})")
                            return
                except Exception:
                    pass

                time.sleep(0.5)

            # Timeout habis
            logger.error(
                f"❌ Serve daemon for '{remote_name}' not ready within {startup_timeout}s. "
                f"Image proxy will use rclone cat fallback."
            )
            # Jangan terminate — biarkan tetap jalan, mungkin baru butuh lebih lama
            # Jika diakses lagi nanti lewat get_serve_url() → None → pakai fallback

        thread = threading.Thread(
            target=_background_health_check,
            name=f"daemon-health-{remote_name}",
            daemon=True
        )
        thread.start()



    def _stop_serve_daemon(self):
        """Stop serve daemon untuk remote ini."""
        with self._serve_lock:
            if self.remote_name not in self._serve_daemons:
                return

            daemon = self._serve_daemons[self.remote_name]
            process = daemon["process"]

            try:
                logger.info(f"🛑 Stopping serve daemon for '{self.remote_name}'...")
                process.terminate()
                process.wait(timeout=5)
                logger.info(f"✅ Serve daemon stopped for '{self.remote_name}'")
            except subprocess.TimeoutExpired:
                logger.warning(f"Force killing serve daemon for '{self.remote_name}'")
                process.kill()
            except Exception as e:
                logger.error(f"Error stopping serve daemon: {str(e)}")
            finally:
                del self._serve_daemons[self.remote_name]

    def is_serve_running(self) -> bool:
        """Check apakah serve daemon sudah SIAP (bukan sekedar starting)."""
        daemon = self._serve_daemons.get(self.remote_name)
        if not daemon:
            return False
        # ✅ Harus process running DAN url sudah tersedia (health check OK)
        return daemon["process"].poll() is None and daemon.get("url") is not None

    def get_serve_url(self) -> Optional[str]:
        """Get URL serve daemon. None jika tidak running ATAU masih starting."""
        daemon = self._serve_daemons.get(self.remote_name)
        if not daemon:
            return None
        if daemon["process"].poll() is not None:
            return None
        # ✅ Kembalikan URL hanya jika health check sudah OK (url != None)
        return daemon.get("url")  # None jika masih starting

    def get_serve_daemon_status(self) -> Dict:
        """Get serve daemon status untuk remote ini."""
        daemon = self._serve_daemons.get(self.remote_name)
        if not daemon:
            return {"running": False, "status": "not_started", "remote": self.remote_name}

        is_alive = daemon["process"].poll() is None
        is_ready = is_alive and daemon.get("url") is not None
        daemon_status = daemon.get("status", "unknown")

        return {
            "running": is_ready,
            "status": daemon_status if is_alive else "dead",
            "remote": self.remote_name,
            "url": daemon.get("url") if is_ready else None,
            "port": daemon["port"],
            "uptime_seconds": round(time.time() - daemon["started_at"], 2) if is_alive else 0
        }



    # ==========================================
    # ✅ ✨ DOWNLOAD VIA HTTPX (ASYNC - METHODS BARU)
    # ==========================================

    async def download_file_async(self, file_path: str) -> Optional[bytes]:
        """
        ✅ ✨ NEW: Download file ke memory via HTTPX async.

        Priority:
        1. rclone serve http → HTTPX AsyncClient (fastest, non-blocking)
        2. rclone cat via subprocess → fallback (jika serve tidak running)

        Args:
            file_path: Path file di GDrive (tanpa remote prefix)

        Returns:
            File content as bytes, None jika gagal
        """
        serve_url = self.get_serve_url()

        if serve_url:
            try:
                # ✅ Gunakan singleton HTTPX client
                client = HttpxClientManager.get_client(serve_url)
                resp = await client.get(f"/{file_path}")

                if resp.status_code == 200:
                    logger.debug(
                        f"✅ HTTPX download (serve): {file_path} ({len(resp.content)} bytes)"
                    )
                    return resp.content
                elif resp.status_code == 404:
                    logger.warning(f"File not found via serve: {file_path}")
                    return None
                else:
                    logger.warning(
                        f"HTTPX serve returned HTTP {resp.status_code} for {file_path}, "
                        f"falling back to cat..."
                    )
            except Exception as e:
                logger.warning(f"HTTPX serve error for {file_path}: {str(e)}, falling back to cat...")

        # Fallback: rclone cat (di thread pool agar tidak block event loop)
        if settings.RCLONE_SERVE_HTTP_FALLBACK or not settings.RCLONE_SERVE_HTTP_ENABLED:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._download_via_cat, file_path)

        logger.error(f"Serve failed and fallback disabled for: {file_path}")
        return None

    async def stream_file_async(self, file_path: str):
        """
        ✅ ✨ NEW: Stream file via HTTPX untuk StreamingResponse FastAPI.

        True streaming: tidak load semua ke memory sekaligus.
        Cocok untuk file gambar besar + banyak concurrent user.

        Usage di endpoint:
            return StreamingResponse(rclone.stream_file_async(path), media_type="image/jpeg")

        Args:
            file_path: Path file di GDrive (tanpa remote prefix)

        Yields:
            bytes chunks (64KB per chunk)
        """
        serve_url = self.get_serve_url()

        if serve_url:
            try:
                client = HttpxClientManager.get_client(serve_url)
                async with client.stream("GET", f"/{file_path}") as resp:
                    if resp.status_code == 200:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):  # 64KB
                            yield chunk
                        return
                    elif resp.status_code == 404:
                        logger.warning(f"File not found (stream): {file_path}")
                        return
                    else:
                        logger.warning(
                            f"HTTPX stream HTTP {resp.status_code} for {file_path}, "
                            f"falling back to cat..."
                        )
            except Exception as e:
                logger.warning(f"HTTPX stream error: {str(e)}, falling back to cat...")

        # Fallback: download semua via cat lalu yield dalam chunks
        if settings.RCLONE_SERVE_HTTP_FALLBACK or not settings.RCLONE_SERVE_HTTP_ENABLED:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, self._download_via_cat, file_path)
            if content:
                chunk_size = 65536
                for i in range(0, len(content), chunk_size):
                    yield content[i:i + chunk_size]

    def _download_via_cat(self, file_path: str) -> Optional[bytes]:
        """
        Fallback: download via rclone cat (sync, untuk run_in_executor).
        """
        try:
            file_path = self._validate_path(file_path)
            remote_path = f"{self.remote_name}:{file_path}"

            result = self._run_command(
                ["cat", remote_path],
                capture_output=True,
                as_text=False,
                timeout=settings.APP_RCLONE_TIMEOUT
            )

            if result.returncode == 0:
                logger.debug(
                    f"✅ rclone cat: {file_path} ({len(result.stdout)} bytes)"
                )
                return result.stdout

            logger.warning(f"rclone cat failed for {file_path}: {result.stderr}")
            return None

        except Exception as e:
            logger.error(f"_download_via_cat error for {file_path}: {str(e)}")
            return None

    # ==========================================
    # ✅ HYBRID DOWNLOAD (TETAP ADA UNTUK BACKWARD COMPAT)
    # ==========================================

    def download_file_to_memory(
        self,
        file_path: str,
        max_retries: int = 2
    ) -> Optional[bytes]:
        """
        Download file ke memory.

        ✅ ✨ CHANGED: Sekarang pakai httpx.Client (sync) untuk serve,
        bukan requests. Logic retry tetap sama.

        Priority:
        1. rclone serve http → httpx.Client (sync)
        2. rclone cat → fallback

        Args:
            file_path: Path file di GDrive
            max_retries: Maximum retry attempts

        Returns:
            File content as bytes, None jika gagal
        """
        serve_url = self.get_serve_url()

        if serve_url:
            try:
                # ✅ CHANGED: httpx.Client bukan requests
                with httpx.Client(timeout=30.0) as client:
                    resp = client.get(f"{serve_url}/{file_path}")

                    if resp.status_code == 200:
                        logger.info(
                            f"✅ Downloaded via serve http (FAST!): {len(resp.content)} bytes",
                            extra={
                                "file": file_path,
                                "remote": self.remote_name,
                                "method": "httpx_serve_http"
                            }
                        )
                        return resp.content

                    elif resp.status_code == 404:
                        logger.warning(f"File not found via serve http: {file_path}")
                        return None

                    else:
                        logger.warning(
                            f"Serve http HTTP {resp.status_code}, falling back to cat..."
                        )

            except Exception as e:
                logger.warning(f"Serve http error: {str(e)}, falling back to cat...")

        # Fallback to traditional 'rclone cat'
        if settings.RCLONE_SERVE_HTTP_FALLBACK or not settings.RCLONE_SERVE_HTTP_ENABLED:
            return self._download_file_to_memory_cat(file_path, max_retries)
        else:
            logger.error("Serve http failed and fallback disabled")
            return None

    def _download_file_to_memory_cat(
        self,
        file_path: str,
        max_retries: int = 2
    ) -> Optional[bytes]:
        """
        Traditional download via 'rclone cat' (FALLBACK method).
        Logic TIDAK BERUBAH dari versi original.
        """
        try:
            file_path = self._validate_path(file_path)
            remote_path = f"{self.remote_name}:{file_path}"

            for attempt in range(max_retries):
                try:
                    args = ["cat", remote_path]

                    result = self._run_command(
                        args,
                        capture_output=True,
                        as_text=False,
                        timeout=settings.APP_RCLONE_TIMEOUT
                    )

                    if result.returncode == 0:
                        logger.info(
                            f"Downloaded file to memory (via cat)",
                            extra={
                                "file": file_path,
                                "size": len(result.stdout),
                                "attempt": attempt + 1,
                                "method": "rclone_cat"
                            }
                        )
                        return result.stdout

                    logger.warning(
                        f"Memory download attempt {attempt + 1} failed",
                        extra={"file": file_path}
                    )

                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(f"Retry {attempt + 1}/{max_retries} after error: {str(e)}")

            return None

        except Exception as e:
            logger.error(f"Failed to download file to memory {file_path}: {str(e)}")
            return None

    # ==========================================
    # ✅ ✨ NEW: BULK FOLDER UPLOAD (PERFORMANCE BOOST!)
    #
    # Menggantikan upload file satu-satu dengan rclone copy seluruh folder.
    # Jauh lebih cepat karena:
    # - Batch request ke Google Drive API
    # - Parallel transfers (--transfers 8)
    # - Parallel checkers (--checkers 8)
    # - Larger chunk size (--drive-chunk-size 64M)
    # - Minimal TCP handshake overhead
    #
    # Sebelum: 150 file × ~1.5s = ~225s (4 menit)
    # Sesudah: 1 folder copy dengan 8 parallel transfers ≈ 20-40s
    # ==========================================

    def upload_folder_bulk(
        self,
        local_folder: Path,
        remote_folder_path: str,
        transfers: int = 8,
        checkers: int = 8,
        drive_chunk_size: str = "64M",
        timeout: int = 600,
        exclude_patterns: Optional[List[str]] = None
    ) -> Dict:
        """
        ✅ ✨ NEW: Upload seluruh folder ke GDrive sekaligus (BULK).

        Menggunakan rclone copy dengan parallel transfers untuk performa maksimal.
        Jauh lebih cepat dibanding upload file satu per satu.

        Args:
            local_folder: Path folder lokal yang akan diupload
            remote_folder_path: Path tujuan di GDrive (tanpa remote prefix)
                                 Contoh: "manga_library/one-piece/Chapter_001"
            transfers: Jumlah parallel file transfers (default 8)
            checkers: Jumlah parallel checkers (default 8)
            drive_chunk_size: Ukuran chunk untuk Google Drive (default "64M")
            timeout: Timeout dalam detik (default 600 = 10 menit)
            exclude_patterns: List pattern file yang dikecualikan (misal: ["preview.*"])

        Returns:
            Dict dengan keys:
                success: bool
                files_uploaded: int (jumlah file yang diupload)
                remote_path: str
                duration_seconds: float
                error: str (jika gagal)

        Example:
            result = rclone.upload_folder_bulk(
                local_folder=Path("/tmp/chapter_001"),
                remote_folder_path="manga_library/one-piece/Chapter_001"
            )
            if result["success"]:
                print(f"Uploaded {result['files_uploaded']} files")
        """
        start_time = time.time()

        try:
            if not local_folder.exists():
                return {
                    "success": False,
                    "files_uploaded": 0,
                    "remote_path": remote_folder_path,
                    "duration_seconds": 0,
                    "error": f"Local folder not found: {local_folder}"
                }

            # Hitung jumlah file sebelum upload (untuk logging)
            local_files = [
                f for f in local_folder.iterdir()
                if f.is_file()
            ]
            file_count = len(local_files)

            if file_count == 0:
                return {
                    "success": False,
                    "files_uploaded": 0,
                    "remote_path": remote_folder_path,
                    "duration_seconds": 0,
                    "error": f"No files found in: {local_folder}"
                }

            # Destination: remote:path
            remote_dest = f"{self.remote_name}:{remote_folder_path}"

            # Build rclone copy command dengan performance flags
            cmd = [
                self.rclone_exe,
                "copy",
                str(local_folder),  # source: local folder
                remote_dest,        # dest: remote folder
                "--transfers", str(transfers),
                "--checkers", str(checkers),
                "--drive-chunk-size", drive_chunk_size,
                "--fast-list",
                "--no-traverse",
                "--create-empty-src-dirs",
                "--progress",
                "--stats", "10s",
                "--log-level", "ERROR",
            ]

            # Tambah exclude patterns jika ada
            if exclude_patterns:
                for pattern in exclude_patterns:
                    cmd.extend(["--exclude", pattern])

            logger.info(
                f"📦 Bulk uploading {file_count} files from '{local_folder.name}' "
                f"→ {remote_dest} "
                f"(transfers={transfers}, checkers={checkers}, chunk={drive_chunk_size})"
            )

            # Clean RCLONE_* env vars
            clean_env = _clean_env_for_rclone()

            # Jalankan rclone copy
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=clean_env
            )

            duration = round(time.time() - start_time, 2)

            if result.returncode == 0:
                logger.info(
                    f"✅ Bulk upload complete: {file_count} files → {remote_dest} "
                    f"({duration}s, ~{round(file_count/duration, 1)} files/sec)"
                )
                return {
                    "success": True,
                    "files_uploaded": file_count,
                    "remote_path": remote_folder_path,
                    "duration_seconds": duration,
                    "error": None
                }
            else:
                error_msg = result.stderr or "Unknown rclone error"
                logger.error(
                    f"❌ Bulk upload failed: {remote_dest} "
                    f"returncode={result.returncode}, error={error_msg}"
                )
                return {
                    "success": False,
                    "files_uploaded": 0,
                    "remote_path": remote_folder_path,
                    "duration_seconds": duration,
                    "error": error_msg
                }

        except subprocess.TimeoutExpired:
            duration = round(time.time() - start_time, 2)
            logger.error(f"❌ Bulk upload timed out after {timeout}s: {remote_folder_path}")
            return {
                "success": False,
                "files_uploaded": 0,
                "remote_path": remote_folder_path,
                "duration_seconds": duration,
                "error": f"Upload timed out after {timeout} seconds"
            }
        except Exception as e:
            duration = round(time.time() - start_time, 2)
            logger.error(f"❌ Bulk upload error: {str(e)}", exc_info=True)
            return {
                "success": False,
                "files_uploaded": 0,
                "remote_path": remote_folder_path,
                "duration_seconds": duration,
                "error": str(e)
            }

    def upload_folder_bulk_with_rename(
        self,
        local_folder: Path,
        remote_folder_path: str,
        image_files: List[Path],
        transfers: int = 8,
        checkers: int = 8,
        drive_chunk_size: str = "64M",
        timeout: int = 600,
        temp_dir: Optional[Path] = None
    ) -> Dict:
        """
        ✅ ✨ NEW: Upload folder dengan rename file 001.jpg, 002.jpg, dst (BULK).

        Sama seperti upload_folder_bulk() tapi files di-rename dulu ke
        format 001.jpg, 002.jpg, 003.jpg sebelum diupload.

        Ini diperlukan karena bulk_upload_service me-rename files saat upload.
        Caranya: buat temp folder, copy+rename files ke sana, lalu bulk upload.

        Args:
            local_folder: Path folder lokal asal
            remote_folder_path: Path tujuan di GDrive
            image_files: List sorted image files yang akan diupload
                         (urutan dalam list = page_order)
            transfers: Jumlah parallel file transfers
            checkers: Jumlah parallel checkers
            drive_chunk_size: Ukuran chunk untuk Google Drive
            timeout: Timeout dalam detik
            temp_dir: Direktori temp untuk staging (jika None, pakai system temp)

        Returns:
            Dict dengan keys:
                success: bool
                files_uploaded: int
                remote_path: str
                duration_seconds: float
                uploaded_files: List[Dict] dengan gdrive_path dan page_order
                error: str (jika gagal)
        """
        import tempfile
        import shutil as shutil_module

        start_time = time.time()
        staging_dir = None

        try:
            # Buat staging directory untuk file yang sudah di-rename
            if temp_dir:
                staging_dir = temp_dir / f"staging_{int(time.time() * 1000)}"
            else:
                staging_dir = Path(tempfile.mkdtemp(prefix="rclone_bulk_"))

            staging_dir.mkdir(parents=True, exist_ok=True)

            # Copy & rename files ke staging dir
            renamed_files = []
            for idx, img_file in enumerate(image_files, start=1):
                new_name = f"{idx:03d}{img_file.suffix.lower()}"
                dest = staging_dir / new_name
                shutil_module.copy2(str(img_file), str(dest))
                renamed_files.append({
                    "original_name": img_file.name,
                    "new_name": new_name,
                    "gdrive_path": f"{remote_folder_path}/{new_name}",
                    "page_order": idx
                })

            logger.info(
                f"📋 Staging {len(renamed_files)} files in '{staging_dir.name}' "
                f"(renamed to 001.jpg format)"
            )

            # Bulk upload dari staging dir
            upload_result = self.upload_folder_bulk(
                local_folder=staging_dir,
                remote_folder_path=remote_folder_path,
                transfers=transfers,
                checkers=checkers,
                drive_chunk_size=drive_chunk_size,
                timeout=timeout
            )

            duration = round(time.time() - start_time, 2)

            if upload_result["success"]:
                return {
                    "success": True,
                    "files_uploaded": len(renamed_files),
                    "remote_path": remote_folder_path,
                    "duration_seconds": duration,
                    "uploaded_files": renamed_files,
                    "error": None
                }
            else:
                return {
                    "success": False,
                    "files_uploaded": 0,
                    "remote_path": remote_folder_path,
                    "duration_seconds": duration,
                    "uploaded_files": [],
                    "error": upload_result["error"]
                }

        except Exception as e:
            duration = round(time.time() - start_time, 2)
            logger.error(f"❌ Bulk upload with rename error: {str(e)}", exc_info=True)
            return {
                "success": False,
                "files_uploaded": 0,
                "remote_path": remote_folder_path,
                "duration_seconds": duration,
                "uploaded_files": [],
                "error": str(e)
            }
        finally:
            # Cleanup staging dir
            if staging_dir and staging_dir.exists():
                try:
                    import shutil as _shutil
                    _shutil.rmtree(str(staging_dir), ignore_errors=True)
                    logger.debug(f"Cleaned up staging dir: {staging_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup staging dir: {e}")

    # ==========================================
    # ✅ SHUTDOWN (TAMBAH async shutdown_all)
    # ==========================================

    @classmethod
    def _shutdown_all_daemons_sync(cls):
        """Shutdown semua serve daemons (sync, untuk atexit)."""
        with cls._serve_lock:
            if not cls._serve_daemons:
                return

            logger.info(f"🛑 Shutting down {len(cls._serve_daemons)} serve daemons...")

            for remote_name, daemon in list(cls._serve_daemons.items()):
                try:
                    process = daemon["process"]
                    process.terminate()
                    process.wait(timeout=3)
                    logger.info(f"✅ Stopped daemon: {remote_name}")
                except Exception as e:
                    logger.error(f"Error stopping daemon {remote_name}: {str(e)}")

            cls._serve_daemons.clear()
            logger.info("✅ All serve daemons shut down")

    @classmethod
    async def shutdown_all(cls):
        """
        ✅ ✨ NEW: Full async shutdown untuk FastAPI lifespan.
        Stop semua daemons + tutup semua HTTPX clients.
        """
        cls._shutdown_all_daemons_sync()
        await HttpxClientManager.close_all()
        logger.info("✅ RcloneService full shutdown complete (daemons + HTTPX clients)")

    @classmethod
    def _signal_handler(cls, signum, frame):
        """Handle termination signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down serve daemons...")
        cls._shutdown_all_daemons_sync()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    @classmethod
    def get_all_serve_daemon_status(cls) -> Dict[str, Dict]:
        """Get status semua serve daemons."""
        with cls._serve_lock:
            status = {}
            for remote_name, daemon in cls._serve_daemons.items():
                process = daemon["process"]
                is_running = process.poll() is None

                status[remote_name] = {
                    "running": is_running,
                    "url": daemon["url"] if is_running else None,
                    "port": daemon["port"],
                    "uptime_seconds": round(time.time() - daemon["started_at"], 2) if is_running else 0
                }

            return status

    # ==========================================
    # ✅ RCLONE VALIDATION (TIDAK BERUBAH)
    # ==========================================

    def _validate_rclone_installation(self) -> None:
        """Validate that rclone is installed."""
        if RcloneService._rclone_exe_verified:
            self.rclone_exe = RcloneService._rclone_exe_cache
            return

        try:
            rclone_path = settings.RCLONE_EXECUTABLE

            try:
                result = subprocess.run(
                    [rclone_path, "version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False
                )

                if result.returncode == 0:
                    version_info = result.stdout.split('\n')[0] if result.stdout else "unknown"
                    logger.info(f"✅ Rclone found: {version_info}")

                    RcloneService._rclone_exe_cache = rclone_path
                    RcloneService._rclone_exe_verified = True
                    self.rclone_exe = rclone_path
                    return

            except FileNotFoundError:
                logger.debug(f"Rclone not found at: {rclone_path}")
            except Exception as e:
                logger.debug(f"Error checking rclone directly: {str(e)}")

            rclone_in_path = shutil.which(rclone_path)

            if rclone_in_path:
                logger.info(f"✅ Rclone found in PATH: {rclone_in_path}")

                RcloneService._rclone_exe_cache = rclone_in_path
                RcloneService._rclone_exe_verified = True
                self.rclone_exe = rclone_in_path
                return

            raise FileNotFoundError(
                f"❌ Rclone executable not found: '{rclone_path}'\n\n"
                f"📥 Installation steps:\n"
                f"1. Download from https://rclone.org/downloads/\n"
                f"2. Extract to accessible location\n"
                f"3. Option A: Add rclone to system PATH (recommended)\n"
                f"4. Option B: Set full path in .env file:\n"
                f"   RCLONE_EXECUTABLE=C:\\path\\to\\rclone.exe (Windows)\n"
                f"   RCLONE_EXECUTABLE=/usr/local/bin/rclone (Linux)\n\n"
                f"🔍 Current search path: {rclone_path}"
            )

        except FileNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to validate rclone installation: {str(e)}")
            raise RcloneError(f"Rclone validation failed: {str(e)}")

    def _validate_remote_configuration(self) -> None:
        """Validate that remote is properly configured."""
        if not self.test_connection():
            raise RcloneError(
                f"❌ Failed to connect to Rclone remote '{self.remote_name}'\n\n"
                f"🔧 Troubleshooting:\n"
                f"1. Run 'rclone config' to setup remote\n"
                f"2. Verify remote name matches: '{self.remote_name}'\n"
                f"3. Test access: 'rclone lsjson {self.remote_name}:'\n"
                f"4. Check your Google Drive credentials\n\n"
                f"📖 Setup guide: https://rclone.org/drive/"
            )

    # ==========================================
    # ✅ RCLONE COMMANDS - _run_command UPDATED
    # ==========================================

    def _run_command(
        self,
        args: List[str],
        capture_output: bool = True,
        as_text: bool = True,
        timeout: int = None
    ) -> subprocess.CompletedProcess:
        """
        Run rclone command dengan timeout dan error handling.

        ✅ FIX: Clean RCLONE_* env vars sebelum subprocess dipanggil.
        Mencegah OS environment variable seperti RCLONE_TIMEOUT=30
        (tanpa unit 's') mengoverride CLI flags kita secara diam-diam.
        """
        if timeout is None:
            timeout = settings.APP_RCLONE_TIMEOUT

        timeout_duration = self._format_timeout(timeout)
        cmd = [self.rclone_exe] + args + ["--timeout", timeout_duration]

        # ✅ FIX: Clean RCLONE_* dari OS env agar tidak contaminate subprocess
        clean_env = _clean_env_for_rclone()

        try:
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=as_text,
                timeout=timeout + 5,
                check=False,
                env=clean_env  # ✅ ADDED: pakai clean env
            )

            if result.returncode != 0 and capture_output:
                error_msg = result.stderr if as_text else result.stderr.decode('utf-8', errors='ignore')
                logger.error(
                    f"Rclone command failed",
                    extra={
                        "command": ' '.join(cmd),
                        "return_code": result.returncode,
                        "error": error_msg
                    }
                )

            return result

        except subprocess.TimeoutExpired:
            logger.error(f"Rclone command timed out after {timeout}s: {' '.join(cmd)}")
            raise TimeoutError(f"Rclone command timed out after {timeout} seconds")
        except Exception as e:
            logger.error(f"Error running rclone command: {str(e)}")
            raise RcloneError(f"Failed to execute rclone command: {str(e)}")

    def test_connection(self) -> bool:
        """Test koneksi ke Rclone remote. TIDAK BERUBAH."""
        try:
            result = self._run_command(["listremotes"], timeout=10)

            if result.returncode == 0 and f"{self.remote_name}:" in result.stdout:
                logger.info(f"✅ Rclone connection test: SUCCESS for '{self.remote_name}'")
                return True
            else:
                logger.warning(f"⚠️ Rclone connection test: FAILED - Remote '{self.remote_name}' not found")
                return False

        except Exception as e:
            logger.error(f"❌ Rclone connection test: FAILED - {str(e)}")
            return False

    def get_about_info(self, timeout: int = 30) -> Dict:
        """
        Get real GDrive usage via 'rclone about {remote}: --json'.

        Returns dict berisi:
          - total_bytes: total kapasitas (bytes)
          - used_bytes: terpakai (bytes)
          - free_bytes: sisa (bytes)
          - total_gb, used_gb, free_gb: versi GB (float, 2 decimal)
          - remote: nama remote
          - error: None atau string error

        Ini blocking call (~1-3 detik per remote).
        Panggil dari run_in_executor agar tidak block event loop.
        """
        import json as _json

        try:
            result = self._run_command(
                ["about", f"{self.remote_name}:", "--json"],
                timeout=timeout
            )

            if result.returncode != 0:
                err = result.stderr.strip() or "rclone about returned non-zero"
                logger.warning(f"rclone about failed for '{self.remote_name}': {err}")
                return {
                    "remote": self.remote_name,
                    "total_bytes": 0, "used_bytes": 0, "free_bytes": 0,
                    "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0,
                    "error": err,
                }

            data = _json.loads(result.stdout)
            total = data.get("total", 0) or 0
            used = data.get("used", 0) or 0
            free = data.get("free", 0) or (total - used)
            trashed = data.get("trashed", 0) or 0

            def to_gb(b: int) -> float:
                return round(b / (1024 ** 3), 2)

            return {
                "remote": self.remote_name,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "trashed_bytes": trashed,
                "total_gb": to_gb(total),
                "used_gb": to_gb(used),
                "free_gb": to_gb(free),
                "trashed_gb": to_gb(trashed),
                "error": None,
            }

        except Exception as e:
            logger.error(f"get_about_info failed for '{self.remote_name}': {str(e)}")
            return {
                "remote": self.remote_name,
                "total_bytes": 0, "used_bytes": 0, "free_bytes": 0,
                "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0,
                "error": str(e),
            }



    def _validate_path(self, path: str) -> str:
        """Validate and sanitize file path. TIDAK BERUBAH."""
        path = path.strip('/')

        if '..' in path or path.startswith('/') or '\\' in path:
            raise ValueError(f"Invalid path: {path}")

        if not path or len(path) < 3:
            raise ValueError(f"Path too short: {path}")

        return path

    def _natural_sort_key(self, text: str) -> List:
        """Generate sort key for natural sorting. TIDAK BERUBAH."""
        def convert(part):
            return int(part) if part.isdigit() else part.lower()

        return [convert(c) for c in NUMBER_PATTERN.split(text)]

    def list_files_in_folder(
        self,
        folder_id: str,
        mime_type_filter: Optional[str] = None,
        sort: bool = True
    ) -> List[Dict[str, Any]]:
        """List semua files dalam folder dengan natural sorting. TIDAK BERUBAH."""
        try:
            folder_id = self._validate_path(folder_id)
            remote_path = f"{self.remote_name}:{folder_id}"

            args = [
                "lsjson",
                "--files-only",
                remote_path
            ]

            result = self._run_command(args, timeout=30)

            if result.returncode != 0:
                raise RcloneError(f"Failed to list files in folder: {folder_id}")

            files = json.loads(result.stdout) if result.stdout else []

            if mime_type_filter:
                files = [
                    f for f in files
                    if mime_type_filter in f.get('MimeType', '')
                ]

            formatted_files = []
            for f in files:
                full_path = f"{folder_id}/{f['Name']}"

                formatted_files.append({
                    'id': full_path,
                    'name': f['Name'],
                    'mimeType': f.get('MimeType', ''),
                    'size': f.get('Size', 0),
                    'modTime': f.get('ModTime', '')
                })

            if sort:
                formatted_files.sort(key=lambda x: self._natural_sort_key(x['name']))

            logger.info(
                f"Listed {len(formatted_files)} files in folder",
                extra={"folder": folder_id, "count": len(formatted_files)}
            )

            return formatted_files

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse rclone output: {str(e)}")
            raise RcloneError(f"Invalid JSON response from rclone")
        except Exception as e:
            logger.error(f"Error listing files in folder {folder_id}: {str(e)}")
            raise

    def download_file(
        self,
        file_path: str,
        destination_path: Path,
        max_retries: int = 3
    ) -> bool:
        """Download file dari Google Drive ke local storage dengan retry. TIDAK BERUBAH."""
        try:
            file_path = self._validate_path(file_path)
            remote_path = f"{self.remote_name}:{file_path}"

            destination_path.parent.mkdir(parents=True, exist_ok=True)

            for attempt in range(max_retries):
                try:
                    args = [
                        "copyto",
                        "--progress",
                        remote_path,
                        str(destination_path)
                    ]

                    result = self._run_command(args, timeout=60)

                    if result.returncode == 0:
                        logger.info(
                            f"Downloaded file successfully",
                            extra={
                                "file": file_path,
                                "size": destination_path.stat().st_size,
                                "attempt": attempt + 1
                            }
                        )
                        return True

                    logger.warning(
                        f"Download attempt {attempt + 1} failed",
                        extra={"file": file_path}
                    )

                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(f"Retry {attempt + 1}/{max_retries} after error: {str(e)}")

            return False

        except Exception as e:
            logger.error(f"Failed to download file {file_path}: {str(e)}")
            return False

    def get_file_metadata(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Dapatkan metadata file. TIDAK BERUBAH."""
        try:
            file_path = self._validate_path(file_path)

            parent_path = str(Path(file_path).parent)
            if parent_path == ".":
                parent_path = ""

            remote_parent = f"{self.remote_name}:{parent_path}"

            args = ["lsjson", remote_parent]

            result = self._run_command(args, timeout=15)

            if result.returncode != 0:
                return None

            files = json.loads(result.stdout) if result.stdout else []

            file_name = Path(file_path).name
            for f in files:
                if f['Name'] == file_name:
                    return {
                        'id': file_path,
                        'name': f['Name'],
                        'mimeType': f.get('MimeType', ''),
                        'size': f.get('Size', 0),
                        'modifiedTime': f.get('ModTime', ''),
                    }

            return None

        except Exception as e:
            logger.error(f"Error getting metadata for file {file_path}: {str(e)}")
            return None

    def construct_chapter_folder_path(
        self,
        base_folder_id: str,
        manga_slug: str,
        chapter_folder_name: str
    ) -> Optional[str]:
        """Konstruksi dan validasi path lengkap ke folder chapter. TIDAK BERUBAH."""
        try:
            base_folder_id = self._validate_path(base_folder_id)
            manga_slug = self._validate_path(manga_slug)
            chapter_folder_name = self._validate_path(chapter_folder_name)

            chapter_path = f"{base_folder_id}/{manga_slug}/{chapter_folder_name}"
            remote_path = f"{self.remote_name}:{chapter_path}"

            args = ["lsjson", remote_path]

            result = self._run_command(args, timeout=15)

            if result.returncode == 0:
                logger.info(f"Chapter folder found: {chapter_path}")
                return chapter_path
            else:
                logger.warning(f"Chapter folder not found: {chapter_path}")
                return None

        except Exception as e:
            logger.error(f"Error constructing chapter path: {str(e)}")
            return None

    def get_folder_size(self, folder_path: str) -> int:
        """Get total size of folder. TIDAK BERUBAH."""
        try:
            folder_path = self._validate_path(folder_path)
            remote_path = f"{self.remote_name}:{folder_path}"

            args = ["size", "--json", remote_path]

            result = self._run_command(args, timeout=30)

            if result.returncode == 0:
                data = json.loads(result.stdout)
                return data.get('bytes', 0)

            return 0

        except Exception as e:
            logger.error(f"Error getting folder size: {str(e)}")
            return 0

    def delete_path(self, path: str, is_directory: bool = False) -> bool:
        """Hapus file atau folder dari Google Drive via rclone. TIDAK BERUBAH."""
        try:
            path = self._validate_path(path)
            remote_path = f"{self.remote_name}:{path}"

            if is_directory:
                result = self._run_command(
                    ["purge", remote_path],
                    timeout=120
                )
            else:
                result = self._run_command(
                    ["deletefile", remote_path],
                    timeout=30
                )

            if result.returncode == 0:
                logger.info(
                    f"Deleted {'directory' if is_directory else 'file'}: {path}"
                )
                return True

            logger.error(f"Failed to delete {path}: {result.stderr}")
            return False

        except Exception as e:
            logger.error(f"Error deleting path {path}: {str(e)}", exc_info=True)
            return False

    # ==========================================
    # ✅ SINGLETON UTILITY METHODS (TIDAK BERUBAH)
    # ==========================================

    @classmethod
    def clear_cache(cls, remote_name: Optional[str] = None):
        """Clear cached instance(s)."""
        with cls._instances_lock:
            if remote_name:
                if remote_name in cls._instances:
                    instance = cls._instances[remote_name]
                    instance._stop_serve_daemon()
                    del cls._instances[remote_name]
                    logger.info(f"🗑️ Cleared cache for remote '{remote_name}'")
            else:
                cls._shutdown_all_daemons_sync()
                cls._instances.clear()
                logger.info("🗑️ Cleared all RcloneService cache")

    @classmethod
    def get_cached_instances(cls) -> List[str]:
        """Get list of cached remote names."""
        with cls._instances_lock:
            return list(cls._instances.keys())