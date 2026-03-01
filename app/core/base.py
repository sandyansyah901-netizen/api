# File: app/core/base.py
"""
Core Application Base - All in One + Multi-Remote Support + Serve HTTP Mode
============================================================================
Gabungan: Config, Database, Security, Middleware, Logging

REVISI BESAR:
✅ Tambah Multi-Remote Configuration (PRIMARY + BACKUP)
✅ Support dynamic remote detection
✅ Backward compatible (bisa 1 remote atau banyak)
✅ Primary remote untuk upload, auto-copy ke backups
✅ ✨ ENHANCED Database Configuration (SSL, Connection Pooling, Flexible DB)
✅ ✨ FIXED: QueuePool di semua environment (no more NullPool error)
✅ ✨ FIXED: RCLONE_BACKUP_REMOTES validator handle empty string
✅ ✨ FIX #3 & #5: Import timezone dan ganti datetime.utcnow()
✅ ✨✨ NEW: Rclone Serve HTTP Mode Configuration (PERFORMANCE BOOST)
✅ ✨✨ FIXED: Standardized to RCLONE_SERVE_HTTP_* with property aliases
✅ ✨✨ NEW: Multi-Group Storage Configuration (Group 1 + Group 2)
            - RCLONE_NEXT_PRIMARY_REMOTE: primary remote group 2
            - RCLONE_NEXT_BACKUP_REMOTES: backup remotes group 2
            - RCLONE_GROUP1_QUOTA_GB: quota threshold sebelum switch ke group 2
            - RCLONE_AUTO_SWITCH_GROUP: enable/disable auto switch
            - GROUP2_PATH_PREFIX: prefix untuk path file di group 2 (default '@')
            - Helper methods: get_next_group_remotes(), is_group2_path(), clean_path(),
              make_group2_path(), get_next_primary_remote(), get_next_backup_remotes()
            - Properties: is_next_group_configured, is_group2_enabled,
              get_group_for_path(), active_upload_group
✅ ✨ ADDED: has_next_group property alias (untuk kompatibilitas storage_group_service.py)
✅ ✨ ADDED: get_active_upload_group() dan set_active_upload_group() method di Settings
            (wrapper ke module-level functions, agar bisa dipanggil via settings.xxx)
✅ ✨ ADDED: get_next_secondary_remotes() alias untuk get_next_backup_remotes()
            (dipanggil di admin_endpoints.py get_groups_status())
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator, model_validator
from typing import List, Optional, Union
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime, timedelta, timezone  # ✅ FIX #5: Added timezone import
from pathlib import Path
import logging
import logging.config
import sys
import json
import uuid
import time
import secrets
import threading  # ✅ NEW: untuk thread-safe active_upload_group

# ==========================================
# CONFIGURATION
# ==========================================

class Settings(BaseSettings):
    """Application settings dengan validation + Multi-Remote Support + Serve HTTP Mode + Multi-Group"""
    
    # Application
    APP_NAME: str = "Manga Reader API"
    VERSION: str = "3.1.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "production"
    
    # ==========================================
    # DATABASE CONFIGURATION
    # ==========================================
    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600
    DB_ECHO: bool = False
    
    # Connection & Charset Settings
    DB_CONNECT_TIMEOUT: int = 10
    DB_CHARSET: str = "utf8mb4"
    
    # SSL Configuration (Optional - for production)
    DB_SSL_ENABLED: bool = False
    DB_SSL_CA: Optional[str] = None
    DB_SSL_CERT: Optional[str] = None
    DB_SSL_KEY: Optional[str] = None
    
    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080
    BCRYPT_ROUNDS: int = 12
    
    # CORS
    CORS_ORIGINS: Union[List[str], str] = [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:3000"
    ]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: List[str] = ["*"]
    CORS_ALLOW_HEADERS: List[str] = ["*"]
    
    # ==========================================
    # RCLONE - MULTI-REMOTE WITH AUTO SERVER-SIDE COPY
    # ==========================================
    
    # Primary remote (untuk upload)
    RCLONE_PRIMARY_REMOTE: str = "gdrive"
    
    # Backup remotes (comma-separated, untuk auto-copy + load balancing reads)
    RCLONE_BACKUP_REMOTES: str = ""
    
    # Enable auto server-side copy setelah upload
    RCLONE_AUTO_BACKUP_ENABLED: bool = True
    
    # DEPRECATED FIELDS (backward compatibility - jangan hapus!)
    RCLONE_REMOTE_NAME: str = "gdrive"
    RCLONE_REMOTE_NAMES: str = ""
    RCLONE_UPLOAD_STRATEGY: str = "mirror"
    RCLONE_AUTO_COPY_DELAY: int = 5
    
    # Load balancing strategy (untuk READ operations)
    RCLONE_LOAD_BALANCING_STRATEGY: str = "round_robin"
    
    # General rclone settings
    RCLONE_EXECUTABLE: str = "rclone"
    RCLONE_CACHE_DIR: str = "./storage/temp"
    RCLONE_CACHE_EXPIRY_HOURS: int = 24
    RCLONE_MAX_RETRIES: int = 3
    APP_RCLONE_TIMEOUT: int = 30
    
    # Auto recovery
    RCLONE_AUTO_RECOVERY_ENABLED: bool = True
    RCLONE_QUOTA_RESET_HOURS: int = 24
    
    # Background sync settings
    RCLONE_ENABLE_BACKGROUND_SYNC: bool = True
    RCLONE_SYNC_DELAY_SECONDS: int = 5
    
    # ==========================================
    # ✅ ✨ NEW: Multi-Group Storage Configuration
    # ==========================================

    # Group 2 primary remote (dipakai saat group 1 penuh)
    RCLONE_NEXT_PRIMARY_REMOTE: str = ""

    # Group 2 backup remotes (comma-separated)
    RCLONE_NEXT_BACKUP_REMOTES: str = ""

    # Quota threshold group 1 dalam GB sebelum auto-switch ke group 2
    RCLONE_GROUP1_QUOTA_GB: int = 0

    # Quota threshold group 2 dalam GB sebelum auto-switch ke group 3
    RCLONE_GROUP2_QUOTA_GB: int = 1900

    # Enable/disable auto switch ke group berikutnya saat group aktif penuh
    RCLONE_AUTO_SWITCH_GROUP: bool = False

    # Prefix lama group 2 (backward compat — '@' tanpa angka)
    # Format baru: @2/, @3/, @4/ (numeric prefix)
    GROUP2_PATH_PREFIX: str = "@"

    # ==========================================
    # ✅ GROUP 3 Configuration
    # Set RCLONE_GROUP_3_PRIMARY di .env untuk aktifkan group 3
    # ==========================================
    RCLONE_GROUP_3_PRIMARY: str = ""
    RCLONE_GROUP_3_BACKUPS: str = ""
    RCLONE_GROUP_3_QUOTA_GB: int = 1900

    # ==========================================
    # ✅ GROUP 4 Configuration
    # ==========================================
    RCLONE_GROUP_4_PRIMARY: str = ""
    RCLONE_GROUP_4_BACKUPS: str = ""
    RCLONE_GROUP_4_QUOTA_GB: int = 1900

    # ==========================================
    # ✅ GROUP 5 Configuration
    # ==========================================
    RCLONE_GROUP_5_PRIMARY: str = ""
    RCLONE_GROUP_5_BACKUPS: str = ""
    RCLONE_GROUP_5_QUOTA_GB: int = 1900

    # ==========================================
    # ✅ ✨ RCLONE SERVE HTTP MODE (PRIMARY FIELDS)
    # ==========================================
    
    # Main enable flag
    RCLONE_SERVE_HTTP_ENABLED: bool = False
    
    # Port configuration
    RCLONE_SERVE_HTTP_PORT_START: int = 8180
    RCLONE_SERVE_HTTP_HOST: str = "127.0.0.1"
    
    # Fallback & timeout
    RCLONE_SERVE_HTTP_FALLBACK: bool = True
    RCLONE_SERVE_HTTP_TIMEOUT: int = 30
    
    # VFS settings
    RCLONE_SERVE_HTTP_VFS_CACHE_MODE: str = "full"
    RCLONE_SERVE_HTTP_BUFFER_SIZE: str = "256M"
    RCLONE_SERVE_HTTP_VFS_CACHE_MAX_SIZE: str = "1G"
    RCLONE_SERVE_HTTP_VFS_CACHE_MAX_AGE: str = "1h"
    
    # Health & restart
    RCLONE_SERVE_HTTP_HEALTH_CHECK_INTERVAL: int = 30
    RCLONE_SERVE_HTTP_AUTO_RESTART: bool = True
    RCLONE_SERVE_HTTP_MAX_RESTART_ATTEMPTS: int = 3
    RCLONE_SERVE_HTTP_STARTUP_TIMEOUT: int = 10
    
    # Auth & security
    RCLONE_SERVE_HTTP_AUTH: Optional[str] = None
    RCLONE_SERVE_HTTP_READ_ONLY: bool = True
    RCLONE_SERVE_HTTP_NO_CHECKSUM: bool = True
    
    # ==========================================
    # ✅ PROPERTY ALIASES (untuk backward compatibility)
    # ==========================================
    
    @property
    def RCLONE_SERVE_ENABLED(self) -> bool:
        """Alias untuk RCLONE_SERVE_HTTP_ENABLED"""
        return self.RCLONE_SERVE_HTTP_ENABLED
    
    @property
    def RCLONE_SERVE_BASE_PORT(self) -> int:
        """Alias untuk RCLONE_SERVE_HTTP_PORT_START"""
        return self.RCLONE_SERVE_HTTP_PORT_START
    
    @property
    def RCLONE_SERVE_HOST(self) -> str:
        """Alias untuk RCLONE_SERVE_HTTP_HOST"""
        return self.RCLONE_SERVE_HTTP_HOST
    
    @property
    def RCLONE_SERVE_ADDR(self) -> str:
        """Alias untuk RCLONE_SERVE_HTTP_HOST"""
        return self.RCLONE_SERVE_HTTP_HOST
    
    @property
    def RCLONE_SERVE_FALLBACK_ENABLED(self) -> bool:
        """Alias untuk RCLONE_SERVE_HTTP_FALLBACK"""
        return self.RCLONE_SERVE_HTTP_FALLBACK
    
    @property
    def RCLONE_SERVE_TIMEOUT(self) -> int:
        """Alias untuk RCLONE_SERVE_HTTP_TIMEOUT"""
        return self.RCLONE_SERVE_HTTP_TIMEOUT
    
    @property
    def RCLONE_SERVE_VFS_CACHE_MODE(self) -> str:
        """Alias untuk RCLONE_SERVE_HTTP_VFS_CACHE_MODE"""
        return self.RCLONE_SERVE_HTTP_VFS_CACHE_MODE
    
    @property
    def RCLONE_SERVE_AUTH(self) -> Optional[str]:
        """Alias untuk RCLONE_SERVE_HTTP_AUTH"""
        return self.RCLONE_SERVE_HTTP_AUTH
    
    @property
    def RCLONE_SERVE_READ_ONLY(self) -> bool:
        """Alias untuk RCLONE_SERVE_HTTP_READ_ONLY"""
        return self.RCLONE_SERVE_HTTP_READ_ONLY
    
    @property
    def RCLONE_SERVE_NO_CHECKSUM(self) -> bool:
        """Alias untuk RCLONE_SERVE_HTTP_NO_CHECKSUM"""
        return self.RCLONE_SERVE_HTTP_NO_CHECKSUM
    
    # COVER IMAGES (LOCAL + GDRIVE BACKUP)
    COVERS_DIR: str = "./storage/covers"
    COVERS_BACKUP_GDRIVE_PATH: str = "manga_covers"
    COVERS_MAX_SIZE_MB: int = 5
    COVERS_ALLOWED_TYPES: List[str] = ["image/jpeg", "image/png", "image/webp"]
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 100
    RATE_LIMIT_PER_HOUR: int = 1000
    
    # Cache
    CACHE_PERSISTENT_PAGES: bool = True
    CACHE_CLEANUP_INTERVAL_HOURS: int = 1
    CACHE_MAX_SIZE_GB: int = 10
    
    # Pagination
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100
    
    # Monitoring & Logging
    ENABLE_METRICS: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"
    LOG_REQUESTS: bool = True
    LOG_REQUEST_BODY: bool = False
    LOG_RESPONSE_BODY: bool = False
    
    # Sentry (Optional)
    SENTRY_DSN: Optional[str] = None
    SENTRY_ENVIRONMENT: Optional[str] = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1
    
    # Redis (Optional)
    REDIS_ENABLED: bool = False
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL: int = 300
    
    # File Upload
    MAX_UPLOAD_SIZE_MB: int = 10
    ALLOWED_IMAGE_TYPES: List[str] = ["image/jpeg", "image/png", "image/webp"]
    
    # Background Tasks
    BACKGROUND_TASK_ENABLED: bool = True
    BACKGROUND_DOWNLOAD_SKIP_FIRST: bool = True
    
    # ==========================================
    # VALIDATORS
    # ==========================================
    
    @field_validator('CORS_ORIGINS', mode='before')
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            if ',' in v:
                return [origin.strip() for origin in v.split(',')]
            return [v]
        return v
    
    @field_validator('SECRET_KEY')
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if not v or len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long")
        weak_keys = ["your-secret-key", "change-this", "secret", "password", "12345"]
        if any(weak in v.lower() for weak in weak_keys):
            raise ValueError("SECRET_KEY appears to be a default/weak value")
        return v
    
    @field_validator('DATABASE_URL')
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v:
            raise ValueError("DATABASE_URL is required")
        valid_dialects = ('mysql://', 'mysql+pymysql://', 'postgresql://', 'sqlite:///')
        if not v.startswith(valid_dialects):
            raise ValueError(f"DATABASE_URL must start with valid dialect: {valid_dialects}")
        return v
    
    @field_validator('RCLONE_CACHE_DIR')
    @classmethod
    def validate_cache_dir(cls, v: str) -> str:
        cache_path = Path(v)
        try:
            cache_path.mkdir(parents=True, exist_ok=True)
            # ✅ FIX: Use unique filename per process to avoid race condition
            # when multiple uvicorn workers spawn simultaneously
            import os
            test_file = cache_path / f'.write_test_{os.getpid()}'
            test_file.touch()
            test_file.unlink(missing_ok=True)
        except Exception as e:
            raise ValueError(f"RCLONE_CACHE_DIR '{v}' is not writable: {str(e)}")
        return str(cache_path.absolute())
    
    @field_validator('COVERS_DIR')
    @classmethod
    def validate_covers_dir(cls, v: str) -> str:
        covers_path = Path(v)
        try:
            covers_path.mkdir(parents=True, exist_ok=True)
            # ✅ FIX: Use unique filename per process to avoid race condition
            # when multiple uvicorn workers spawn simultaneously
            import os
            test_file = covers_path / f'.write_test_{os.getpid()}'
            test_file.touch()
            test_file.unlink(missing_ok=True)
        except Exception as e:
            raise ValueError(f"COVERS_DIR '{v}' is not writable: {str(e)}")
        return str(covers_path.absolute())
    
    @field_validator('RCLONE_PRIMARY_REMOTE')
    @classmethod
    def validate_primary_remote(cls, v: str) -> str:
        """Validate primary remote name"""
        if not v or len(v) < 2:
            raise ValueError("RCLONE_PRIMARY_REMOTE must be at least 2 characters")
        return v.strip()
    
    @field_validator('RCLONE_BACKUP_REMOTES', mode='before')
    @classmethod
    def parse_backup_remotes(cls, v):
        """
        Parse comma-separated backup remotes.
        ✅ FIX: Handle empty string properly
        """
        if isinstance(v, str):
            if not v or not v.strip():
                return ""
            remotes = [r.strip() for r in v.split(',') if r.strip()]
            return ','.join(remotes) if remotes else ""
        return v

    @field_validator('RCLONE_NEXT_BACKUP_REMOTES', mode='before')
    @classmethod
    def parse_next_backup_remotes(cls, v):
        """
        Parse comma-separated next backup remotes.
        Handle empty string properly - sama seperti RCLONE_BACKUP_REMOTES.
        """
        if isinstance(v, str):
            if not v or not v.strip():
                return ""
            remotes = [r.strip() for r in v.split(',') if r.strip()]
            return ','.join(remotes) if remotes else ""
        return v

    @field_validator('GROUP2_PATH_PREFIX')
    @classmethod
    def validate_group2_prefix(cls, v: str) -> str:
        """
        Validate prefix tidak kosong dan tidak mengandung karakter path berbahaya.
        """
        if not v or not v.strip():
            raise ValueError("GROUP2_PATH_PREFIX cannot be empty")
        if '/' in v or '\\' in v:
            raise ValueError("GROUP2_PATH_PREFIX cannot contain path separators (/ or \\)")
        return v.strip()
    
    @field_validator('RCLONE_LOAD_BALANCING_STRATEGY')
    @classmethod
    def validate_load_balancing_strategy(cls, v: str) -> str:
        valid_strategies = ['round_robin', 'weighted', 'random', 'least_used']
        if v not in valid_strategies:
            raise ValueError(f"RCLONE_LOAD_BALANCING_STRATEGY must be one of: {', '.join(valid_strategies)}")
        return v
    
    # ==========================================
    # ✅ ✨ VALIDATORS FOR SERVE HTTP MODE
    # ==========================================
    
    @field_validator('RCLONE_SERVE_HTTP_VFS_CACHE_MODE')
    @classmethod
    def validate_vfs_cache_mode(cls, v: str) -> str:
        """Validate VFS cache mode"""
        valid_modes = ['off', 'minimal', 'writes', 'full']
        if v not in valid_modes:
            raise ValueError(f"RCLONE_SERVE_HTTP_VFS_CACHE_MODE must be one of: {', '.join(valid_modes)}")
        return v
    
    @field_validator('RCLONE_SERVE_HTTP_PORT_START')
    @classmethod
    def validate_port_start(cls, v: int) -> int:
        """Validate port range"""
        if v < 1024:
            raise ValueError("RCLONE_SERVE_HTTP_PORT_START must be >= 1024 (avoid privileged ports)")
        if v > 65000:
            raise ValueError("RCLONE_SERVE_HTTP_PORT_START must be <= 65000 (leave room for multiple remotes)")
        return v
    
    @field_validator('RCLONE_SERVE_HTTP_STARTUP_TIMEOUT')
    @classmethod
    def validate_startup_timeout(cls, v: int) -> int:
        """Validate startup timeout"""
        if v < 3:
            raise ValueError("RCLONE_SERVE_HTTP_STARTUP_TIMEOUT must be >= 3 seconds")
        if v > 60:
            raise ValueError("RCLONE_SERVE_HTTP_STARTUP_TIMEOUT should not exceed 60 seconds")
        return v
    
    @field_validator('RCLONE_SERVE_HTTP_HEALTH_CHECK_INTERVAL')
    @classmethod
    def validate_health_check_interval(cls, v: int) -> int:
        """Validate health check interval"""
        if v < 10:
            raise ValueError("RCLONE_SERVE_HTTP_HEALTH_CHECK_INTERVAL must be >= 10 seconds")
        if v > 300:
            raise ValueError("RCLONE_SERVE_HTTP_HEALTH_CHECK_INTERVAL should not exceed 300 seconds")
        return v
    
    @field_validator('RCLONE_SERVE_HTTP_MAX_RESTART_ATTEMPTS')
    @classmethod
    def validate_max_restart_attempts(cls, v: int) -> int:
        """Validate max restart attempts"""
        if v < 1:
            raise ValueError("RCLONE_SERVE_HTTP_MAX_RESTART_ATTEMPTS must be >= 1")
        if v > 10:
            raise ValueError("RCLONE_SERVE_HTTP_MAX_RESTART_ATTEMPTS should not exceed 10")
        return v
    
    # ==========================================
    # PROPERTIES & HELPERS (LAMA - tidak berubah)
    # ==========================================
    
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"
    
    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"
    
    @property
    def database_config(self) -> dict:
        """Generate database engine configuration dengan SSL support"""
        config = {
            "pool_pre_ping": True,
            "poolclass": QueuePool,
            "pool_size": self.DB_POOL_SIZE,
            "max_overflow": self.DB_MAX_OVERFLOW,
            "pool_recycle": self.DB_POOL_RECYCLE,
            "echo": self.DB_ECHO,
            "connect_args": {
                "connect_timeout": self.DB_CONNECT_TIMEOUT,
                "charset": self.DB_CHARSET,
            }
        }
        
        if self.DB_SSL_ENABLED and self.DB_SSL_CA:
            ssl_config = {"ca": self.DB_SSL_CA}
            if self.DB_SSL_CERT:
                ssl_config["cert"] = self.DB_SSL_CERT
            if self.DB_SSL_KEY:
                ssl_config["key"] = self.DB_SSL_KEY
            config["connect_args"]["ssl"] = ssl_config
        
        return config
    
    @property
    def cors_config(self) -> dict:
        return {
            "allow_origins": self.CORS_ORIGINS,
            "allow_credentials": self.CORS_ALLOW_CREDENTIALS,
            "allow_methods": self.CORS_ALLOW_METHODS,
            "allow_headers": self.CORS_ALLOW_HEADERS
        }
    
    def get_rclone_remotes(self) -> List[str]:
        """
        Get list of ALL rclone remote names (primary + backups) untuk Group 1.
        TIDAK BERUBAH - tetap group 1 saja.
        """
        remotes = [self.RCLONE_PRIMARY_REMOTE]
        
        if self.RCLONE_BACKUP_REMOTES:
            backup_remotes = [
                r.strip() for r in self.RCLONE_BACKUP_REMOTES.split(',') 
                if r.strip()
            ]
            remotes.extend(backup_remotes)
        
        return [r for r in remotes if r and r.strip()]
    
    def get_primary_remote(self) -> str:
        """Get primary remote untuk upload (group 1). TIDAK BERUBAH."""
        return self.RCLONE_PRIMARY_REMOTE
    
    def get_secondary_remotes(self) -> List[str]:
        """
        Get backup/secondary remotes (group 1). TIDAK BERUBAH.
        """
        if not self.RCLONE_BACKUP_REMOTES or not self.RCLONE_BACKUP_REMOTES.strip():
            return []
        
        return [
            r.strip() for r in self.RCLONE_BACKUP_REMOTES.split(',') 
            if r.strip()
        ]
    
    @property
    def is_multi_remote_enabled(self) -> bool:
        """Check if multi-remote mode is enabled. TIDAK BERUBAH."""
        return len(self.get_rclone_remotes()) > 1
    
    @property
    def is_mirror_upload_enabled(self) -> bool:
        """Check if auto-backup/mirror upload is enabled. TIDAK BERUBAH."""
        return (
            self.is_multi_remote_enabled and 
            self.RCLONE_AUTO_BACKUP_ENABLED
        )
    
    # ==========================================
    # ✅ ✨ NEW: Multi-Group Properties & Helpers
    # ==========================================

    @property
    def is_next_group_configured(self) -> bool:
        """
        Check apakah group 2 sudah dikonfigurasi (ada RCLONE_NEXT_PRIMARY_REMOTE).

        Returns:
            True jika RCLONE_NEXT_PRIMARY_REMOTE diset dan tidak kosong.
        """
        return bool(self.RCLONE_NEXT_PRIMARY_REMOTE and self.RCLONE_NEXT_PRIMARY_REMOTE.strip())

    @property
    def has_next_group(self) -> bool:
        """
        ✅ ✨ ADDED: Alias untuk is_next_group_configured.

        Dipakai oleh storage_group_service.py yang masih pakai settings.has_next_group.
        Agar backward compatible tanpa harus ubah storage_group_service.py.

        Returns:
            True jika group 2 dikonfigurasi (sama dengan is_next_group_configured).
        """
        return self.is_next_group_configured

    @property
    def is_group2_enabled(self) -> bool:
        """
        Check apakah group 2 bisa dipakai.

        Group 2 enabled jika:
        1. is_next_group_configured = True
        2. RCLONE_AUTO_SWITCH_GROUP = True (auto) ATAU manual switch sudah diaktifkan

        Catatan: property ini hanya cek konfigurasi statis.
        Status aktif upload group ada di MultiRemoteService.
        """
        return self.is_next_group_configured and self.RCLONE_AUTO_SWITCH_GROUP

    def get_next_group_remotes(self) -> List[str]:
        """
        Get ALL remote names untuk group 2 (next primary + next backups).

        Returns:
            List of remote names untuk group 2.
            Empty list jika tidak dikonfigurasi.

        Examples:
            RCLONE_NEXT_PRIMARY_REMOTE=gdrive11
            RCLONE_NEXT_BACKUP_REMOTES=gdrive12,gdrive13
            → ["gdrive11", "gdrive12", "gdrive13"]
        """
        if not self.is_next_group_configured:
            return []

        remotes = [self.RCLONE_NEXT_PRIMARY_REMOTE.strip()]

        if self.RCLONE_NEXT_BACKUP_REMOTES and self.RCLONE_NEXT_BACKUP_REMOTES.strip():
            backup_remotes = [
                r.strip() for r in self.RCLONE_NEXT_BACKUP_REMOTES.split(',')
                if r.strip()
            ]
            remotes.extend(backup_remotes)

        return [r for r in remotes if r and r.strip()]

    def get_next_primary_remote(self) -> Optional[str]:
        """
        Get primary remote untuk group 2.

        Returns:
            Remote name string atau None jika tidak dikonfigurasi.
        """
        if not self.is_next_group_configured:
            return None
        return self.RCLONE_NEXT_PRIMARY_REMOTE.strip()

    def get_next_backup_remotes(self) -> List[str]:
        """
        Get backup remotes untuk group 2.

        Returns:
            List of backup remote names untuk group 2.
            Empty list jika tidak dikonfigurasi.
        """
        if not self.RCLONE_NEXT_BACKUP_REMOTES or not self.RCLONE_NEXT_BACKUP_REMOTES.strip():
            return []

        return [
            r.strip() for r in self.RCLONE_NEXT_BACKUP_REMOTES.split(',')
            if r.strip()
        ]

    def get_next_secondary_remotes(self) -> List[str]:
        """
        ✅ ✨ ADDED: Alias untuk get_next_backup_remotes().

        Dipanggil di admin_endpoints.py get_groups_status():
            settings.get_next_secondary_remotes()

        Menghindari AttributeError karena method ini sebelumnya tidak ada.
        Sama persis dengan get_next_backup_remotes().

        Returns:
            List of backup remote names untuk group 2.
            Empty list jika tidak dikonfigurasi.
        """
        return self.get_next_backup_remotes()

    def is_group2_path(self, path: str) -> bool:
        """
        Check apakah path adalah path group 2 (ada prefix GROUP2_PATH_PREFIX).

        Args:
            path: Database path string

        Returns:
            True jika path dimulai dengan GROUP2_PATH_PREFIX.

        Examples:
            >>> settings.is_group2_path("@manga_library/xxx/001.jpg")
            True
            >>> settings.is_group2_path("manga_library/xxx/001.jpg")
            False
        """
        if not path:
            return False
        return path.startswith(self.GROUP2_PATH_PREFIX)

    def clean_path(self, path: str) -> str:
        """
        Strip group prefix dari path agar bisa dikirim ke rclone.

        ✅ PENTING: Gunakan method ini (bukan lstrip()) untuk strip prefix.
        lstrip() akan strip semua karakter yang ada di prefix string,
        satu per satu (character set strip), bukan strip string prefix.

        Contoh salah: "@abc".lstrip("@") → "abc" (kebetulan sama karena @ 1 char)
        Tapi: "@@abc".lstrip("@") → "abc" (double @ keduanya kena strip)
        Sementara settings.clean_path("@@abc") → "@abc" (hanya strip sekali)

        Prefix hanya di-strip jika path dimulai dengan GROUP2_PATH_PREFIX.
        Path group 1 (tanpa prefix) dikembalikan as-is.

        Args:
            path: Database path (mungkin ada prefix '@')

        Returns:
            Clean path tanpa prefix.

        Examples:
            >>> settings.clean_path("@manga_library/xxx/001.jpg")
            "manga_library/xxx/001.jpg"
            >>> settings.clean_path("manga_library/xxx/001.jpg")
            "manga_library/xxx/001.jpg"
        """
        if not path:
            return path
        if path.startswith(self.GROUP2_PATH_PREFIX):
            return path[len(self.GROUP2_PATH_PREFIX):]
        return path

    def make_group2_path(self, clean_path: str) -> str:
        """
        Tambahkan GROUP2_PATH_PREFIX ke clean path untuk disimpan ke database.

        Args:
            clean_path: Path tanpa prefix (yang akan di-upload ke rclone)

        Returns:
            Path dengan prefix GROUP2_PATH_PREFIX untuk disimpan ke DB.

        Examples:
            >>> settings.make_group2_path("manga_library/xxx/001.jpg")
            "@manga_library/xxx/001.jpg"
        """
        if not clean_path:
            return clean_path
        # Jangan double prefix
        if clean_path.startswith(self.GROUP2_PATH_PREFIX):
            return clean_path
        return f"{self.GROUP2_PATH_PREFIX}{clean_path}"

    def get_group_for_path(self, path: str) -> int:
        """
        Determine group untuk path tertentu berdasarkan prefix.

        Args:
            path: Database path (mungkin ada prefix '@')

        Returns:
            2 jika path adalah group 2, 1 jika group 1.

        Examples:
            >>> settings.get_group_for_path("manga_library/xxx/001.jpg")
            1
            >>> settings.get_group_for_path("@manga_library/xxx/001.jpg")
            2
        """
        return 2 if self.is_group2_path(path) else 1

    # ==========================================
    # ✅ ✨ NEW: Settings-level wrappers untuk active upload group
    #
    # Tujuan: agar bisa dipanggil via settings.get_active_upload_group()
    # atau settings.set_active_upload_group(group) dari manapun.
    # Actual state disimpan di module-level _active_upload_group (thread-safe).
    # ==========================================

    def get_active_upload_group(self) -> int:
        """
        ✅ ✨ ADDED: Get active upload group via settings instance.

        Wrapper ke module-level get_active_upload_group() function.
        Agar code yang pakai settings.get_active_upload_group() tetap bisa jalan.

        Returns:
            1 jika upload ke group 1 (gdrive..gdrive10)
            2 jika upload ke group 2 (gdrive11..gdrive20)
        """
        # Delegate ke module-level function
        # (module-level function didefinisikan setelah class ini)
        return _get_active_upload_group_impl()

    def set_active_upload_group(self, group: int) -> None:
        """
        ✅ ✨ ADDED: Set active upload group via settings instance.

        Wrapper ke module-level set_active_upload_group() function.
        Agar code yang pakai settings.set_active_upload_group(group) tetap bisa jalan.

        Args:
            group: 1 atau 2

        Raises:
            ValueError: jika group bukan 1 atau 2
        """
        # Delegate ke module-level function
        _set_active_upload_group_impl(group)

    # ==========================================
    # ✅ ✨ PROPERTIES FOR SERVE HTTP MODE (tidak berubah)
    # ==========================================
    
    @property
    def is_serve_http_mode(self) -> bool:
        """Check if serve http mode is enabled"""
        return self.RCLONE_SERVE_HTTP_ENABLED
    
    def get_serve_http_port(self, remote_index: int = 0) -> int:
        """Get port number for serve http daemon."""
        return self.RCLONE_SERVE_HTTP_PORT_START + remote_index
    
    def get_serve_http_url(self, remote_index: int = 0) -> str:
        """Get full HTTP URL for serve daemon."""
        port = self.get_serve_http_port(remote_index)
        return f"http://{self.RCLONE_SERVE_HTTP_HOST}:{port}"
    
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        case_sensitive = True
        # ✅ 'ignore' agar env vars grup N+ (RCLONE_GROUP_6_PRIMARY, dll)
        # yang belum didefinisikan tidak menyebabkan ValidationError
        extra = 'ignore'


# ==========================================
# ✅ ✨ MODULE-LEVEL IMPLEMENTATION FUNCTIONS
# (Didefinisikan SEBELUM Settings di-instantiate, tapi SESUDAH class Settings)
# Fungsi-fungsi ini adalah implementasi actual dari get/set active upload group.
# Settings.get_active_upload_group() dan Settings.set_active_upload_group()
# delegate ke sini agar thread-safe state ada di satu tempat.
# ==========================================

_active_upload_group_internal: int = 1
_active_upload_group_lock_internal = threading.Lock()


def _get_active_upload_group_impl() -> int:
    """Internal implementation untuk get active upload group."""
    with _active_upload_group_lock_internal:
        return _active_upload_group_internal


def _set_active_upload_group_impl(group: int) -> None:
    """Internal implementation untuk set active upload group."""
    global _active_upload_group_internal
    if group not in (1, 2):
        raise ValueError(f"Invalid upload group: {group}. Must be 1 or 2.")
    with _active_upload_group_lock_internal:
        _active_upload_group_internal = group
        logging.getLogger(__name__).info(
            f"✅ Active upload group switched to Group {group} (internal state)"
        )


# Load settings
try:
    settings = Settings()
except Exception as e:
    print(f"❌ Failed to load settings: {str(e)}")
    print("\n🔑 To generate a secret key, run:")
    print("  python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    raise


# ==========================================
# ✅ ✨ NEW: Global Active Upload Group State (thread-safe)
# ==========================================
# State ini dipakai oleh upload_service.py dan admin_endpoints.py
# untuk menentukan upload ke group mana saat ini.
# Bisa di-switch secara manual via admin endpoint atau otomatis
# saat group 1 penuh.

_active_upload_group: int = 1
_active_upload_group_lock = threading.Lock()


def get_active_upload_group() -> int:
    """
    Get active upload group saat ini (1 atau 2).

    Thread-safe.

    Returns:
        1 jika upload ke group 1 (gdrive..gdrive10)
        2 jika upload ke group 2 (gdrive11..gdrive20)

    Note:
        Ini adalah module-level function yang BERBEDA dari
        _get_active_upload_group_impl(). Keduanya thread-safe
        dan sync satu sama lain via _sync_group_state().
        Untuk konsistensi, kedua state disinkronisasi saat set.
    """
    with _active_upload_group_lock:
        return _active_upload_group


def set_active_upload_group(group: int) -> None:
    """
    Set active upload group.

    Thread-safe. Dipanggil oleh:
    - Admin endpoint /admin/storage/switch-group
    - Auto-switch logic saat group 1 penuh

    Args:
        group: 1 atau 2

    Raises:
        ValueError: jika group bukan 1 atau 2
    """
    global _active_upload_group
    if group not in (1, 2):
        raise ValueError(f"Invalid upload group: {group}. Must be 1 or 2.")
    with _active_upload_group_lock:
        _active_upload_group = group

    # ✅ Sync ke internal state juga (untuk settings.get_active_upload_group())
    _set_active_upload_group_impl(group)

    logging.getLogger(__name__).info(
        f"✅ Active upload group switched to Group {group}"
    )


# ==========================================
# DATABASE ENGINE
# ==========================================

engine = create_engine(
    settings.DATABASE_URL,
    **settings.database_config
)

# Add connection event listeners
@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    """Called when a new DB connection is created"""
    logging.getLogger(__name__).debug("✅ Database connection established")

@event.listens_for(engine, "close")
def receive_close(dbapi_conn, connection_record):
    """Called when a DB connection is closed"""
    logging.getLogger(__name__).debug("Database connection closed")

@event.listens_for(engine, "checkin")
def receive_checkin(dbapi_conn, connection_record):
    """Called when connection is returned to pool"""
    try:
        dbapi_conn.rollback()
    except:
        pass

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Database session dependency with error handling"""
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        logging.getLogger(__name__).error(f"Database session error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()


# ==========================================
# SECURITY
# ==========================================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash password"""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """
    Create JWT access token.
    
    ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str):
    """Decode JWT token"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db = Depends(get_db)
):
    """Get current authenticated user"""
    from app.models.models import User
    
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        token = credentials.credentials
        payload = decode_access_token(token)
        
        if payload is None:
            raise credentials_exception
        
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        
    except JWTError:
        raise credentials_exception
    
    user = db.query(User).filter(User.username == username).first()
    
    if user is None:
        raise credentials_exception
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive"
        )
    
    return user


def require_role(*allowed_roles: str):
    """Dependency untuk check user role"""
    def role_checker(current_user = Depends(get_current_user)):
        user_roles = [role.name for role in current_user.roles]
        
        if not any(role in user_roles for role in allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User does not have required role. Required: {', '.join(allowed_roles)}"
            )
        
        return current_user
    
    return role_checker


# ==========================================
# LOGGING
# ==========================================

class RequestIDFilter(logging.Filter):
    """Add request_id to log records"""
    def filter(self, record):
        if not hasattr(record, 'request_id'):
            record.request_id = "-"
        return True


def setup_logging():
    """Setup logging configuration"""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s %(request_id)s %(pathname)s %(lineno)d"
            },
            "detailed": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            }
        },
        "filters": {
            "request_id": {
                "()": RequestIDFilter
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": settings.LOG_LEVEL,
                "formatter": "json" if settings.LOG_FORMAT == "json" else "detailed",
                "stream": sys.stdout,
                "filters": ["request_id"]
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": settings.LOG_LEVEL,
                "formatter": "json" if settings.LOG_FORMAT == "json" else "detailed",
                "filename": "logs/app.log",
                "maxBytes": 10485760,
                "backupCount": 5,
                "filters": ["request_id"]
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "ERROR",
                "formatter": "json" if settings.LOG_FORMAT == "json" else "detailed",
                "filename": "logs/error.log",
                "maxBytes": 10485760,
                "backupCount": 5,
                "filters": ["request_id"]
            }
        },
        "loggers": {
            "": {
                "level": settings.LOG_LEVEL,
                "handlers": ["console", "file", "error_file"],
                "propagate": False
            },
            "app": {
                "level": settings.LOG_LEVEL,
                "handlers": ["console", "file", "error_file"],
                "propagate": False
            },
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False
            }
        }
    }
    
    logging.config.dictConfig(logging_config)
    logger = logging.getLogger(__name__)
    logger.info(
        "Logging configured",
        extra={
            "log_level": settings.LOG_LEVEL,
            "log_format": settings.LOG_FORMAT,
            "environment": settings.ENVIRONMENT
        }
    )


# ==========================================
# MIDDLEWARE
# ==========================================

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to each request"""
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        logger = logging.getLogger(__name__)
        logger.info(
            "Incoming request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "client": request.client.host if request.client else "unknown"
            }
        )
        
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        
        response.headers["X-Request-ID"] = request_id
        
        logger.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "process_time_ms": round(process_time * 1000, 2)
            }
        )
        
        return response


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Add Cache-Control headers"""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        
        if "/image-proxy/image/" in path:
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers"""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        return response


# ==========================================
# UTILITY FUNCTIONS
# ==========================================

def generate_secret_key() -> str:
    """Generate a secure secret key"""
    return secrets.token_urlsafe(32)


# ==========================================
# STARTUP INFO
# ==========================================

def print_startup_info():
    """Print startup configuration info"""
    logger = logging.getLogger(__name__)
    
    # Hide password in DATABASE_URL for logging
    db_url_safe = settings.DATABASE_URL
    if "@" in db_url_safe:
        parts = db_url_safe.split("@")
        if ":" in parts[0]:
            user_part = parts[0].split("://")[1].split(":")[0]
            db_url_safe = f"{parts[0].split('://')[0]}://{user_part}:***@{parts[1]}"
    
    remotes = settings.get_rclone_remotes()
    primary = settings.get_primary_remote()
    secondaries = settings.get_secondary_remotes()
    
    logger.info(
        "Application Configuration",
        extra={
            "app_name": settings.APP_NAME,
            "version": settings.VERSION,
            "environment": settings.ENVIRONMENT,
            "debug": settings.DEBUG,
            "database_url": db_url_safe,
            "db_pool_size": settings.DB_POOL_SIZE,
            "db_ssl_enabled": settings.DB_SSL_ENABLED,
            "rclone_mode": "multi-remote" if settings.is_multi_remote_enabled else "single-remote",
            "rclone_primary": primary,
            "rclone_backups": secondaries,
            "auto_backup_enabled": settings.RCLONE_AUTO_BACKUP_ENABLED,
            "load_balancing_strategy": settings.RCLONE_LOAD_BALANCING_STRATEGY if settings.is_multi_remote_enabled else "N/A",
            "serve_http_mode": settings.is_serve_http_mode,
            # ✅ NEW: group 2 info
            "group2_configured": settings.is_next_group_configured,
            "group2_primary": settings.get_next_primary_remote(),
            "group2_auto_switch": settings.RCLONE_AUTO_SWITCH_GROUP,
        }
    )
    
    if settings.is_multi_remote_enabled:
        logger.info(
            f"✅ Multi-Remote Mode ENABLED with {len(remotes)} remotes (Group 1)"
        )
        logger.info(f"📤 Primary Remote: {primary}")
        if secondaries:
            logger.info(f"💾 Backup Remotes: {', '.join(secondaries)}")
            if settings.RCLONE_AUTO_BACKUP_ENABLED:
                logger.info(f"🔄 Auto-backup: ENABLED")
            else:
                logger.info(f"🔄 Auto-backup: DISABLED")
    else:
        logger.info(
            f"ℹ️ Single-Remote Mode using: {remotes[0]}"
        )

    # ✅ NEW: Log group 2 status
    if settings.is_next_group_configured:
        g2_remotes = settings.get_next_group_remotes()
        logger.info(
            f"✅ Group 2 configured: {len(g2_remotes)} remote(s) "
            f"[{', '.join(g2_remotes)}]"
        )
        logger.info(
            f"  Auto-switch: {'ENABLED' if settings.RCLONE_AUTO_SWITCH_GROUP else 'DISABLED (manual)'}"
        )
        logger.info(
            f"  Quota threshold: "
            f"{settings.RCLONE_GROUP1_QUOTA_GB}GB "
            f"{'(0=manual only)' if settings.RCLONE_GROUP1_QUOTA_GB == 0 else ''}"
        )
        logger.info(f"  Path prefix: '{settings.GROUP2_PATH_PREFIX}'")
    else:
        logger.info("ℹ️ Group 2 not configured (single-group mode)")
    
    if settings.is_serve_http_mode:
        logger.info("🌐 Rclone Serve HTTP Mode: ENABLED")
        logger.info(f"  📡 Port range: {settings.RCLONE_SERVE_HTTP_PORT_START}-{settings.RCLONE_SERVE_HTTP_PORT_START + len(remotes) - 1}")
        logger.info(f"  💾 VFS cache mode: {settings.RCLONE_SERVE_HTTP_VFS_CACHE_MODE}")
        logger.info(f"  🔄 Auto-restart: {'ENABLED' if settings.RCLONE_SERVE_HTTP_AUTO_RESTART else 'DISABLED'}")
        logger.info(f"  🛡️ Fallback to cat: {'ENABLED' if settings.RCLONE_SERVE_HTTP_FALLBACK else 'DISABLED'}")
    else:
        logger.info("📦 Rclone Serve HTTP Mode: DISABLED (using 'cat' mode)")