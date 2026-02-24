# File: app/services/cache_manager.py
"""
Cache Manager - Image Cache Management
=======================================
Service untuk mengelola cache gambar manga.

Fitur:
- Touch (reset timer) saat gambar diakses
- Auto-cleanup gambar yang sudah > 24 jam
- Protect halaman 1 (persistent)

✅ FIX #3: Import timezone dan ganti datetime.utcnow()
"""

import os
import shutil
import hashlib
from datetime import datetime, timedelta, timezone  # ✅ FIX #3: Added timezone import
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import and_
from pathlib import Path
import logging

from app.models.models import ImageCache
from app.core.base import settings

logger = logging.getLogger(__name__)  # ✅ Changed from print to logger


class CacheManager:
    """
    Service untuk mengelola cache gambar manga.
    
    Fitur:
    - Touch (reset timer) saat gambar diakses
    - Auto-cleanup gambar yang sudah > 24 jam
    - Protect halaman 1 (persistent)
    
    ✅ FIX #3: All datetime.utcnow() replaced with datetime.now(timezone.utc)
    """
    
    # PERBAIKAN: Support Windows path
    CACHE_DIR = Path(settings.RCLONE_CACHE_DIR).resolve()  # Gunakan dari settings
    CACHE_EXPIRY_HOURS = settings.RCLONE_CACHE_EXPIRY_HOURS
    
    def __init__(self, db: Session):
        self.db = db
        self._ensure_cache_dir()
    
    def _ensure_cache_dir(self):
        """Pastikan direktori cache exists"""
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"📁 Cache directory: {self.CACHE_DIR.absolute()}")
    
    def get_cache_path(self, gdrive_file_id: str, extension: str = "jpg") -> Path:
        """
        Generate path untuk file cache.
        
        Args:
            gdrive_file_id: ID/Path file dari GDrive (bisa panjang dari rclone)
            extension: Extension file (jpg, png, etc)
            
        Returns:
            Path object
        """
        # Hash file_id jika terlalu panjang untuk filesystem
        if len(gdrive_file_id) > 200:
            # Use hash untuk path yang sangat panjang
            file_hash = hashlib.md5(gdrive_file_id.encode()).hexdigest()
            filename = f"{file_hash}.{extension}"
        else:
            # Sanitize path (replace / \ : dengan _)
            safe_id = gdrive_file_id.replace('/', '_').replace('\\', '_').replace(':', '_')
            filename = f"{safe_id}.{extension}"
        
        return self.CACHE_DIR / filename
    
    def touch(self, gdrive_file_id: str) -> bool:
        """
        Reset timer cache (update last_accessed ke NOW).
        Dipanggil setiap kali user request gambar via API Proxy.
        
        ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
        
        Args:
            gdrive_file_id: ID file yang diakses
            
        Returns:
            True jika berhasil, False jika tidak ditemukan
        """
        cache_entry = self.db.query(ImageCache).filter(
            ImageCache.gdrive_file_id == gdrive_file_id
        ).first()
        
        if cache_entry:
            cache_entry.last_accessed = datetime.now(timezone.utc)  # ✅ FIXED
            self.db.commit()
            return True
        
        return False
    
    def add_to_cache(
        self,
        chapter_id: int,
        gdrive_file_id: str,
        local_path: str,
        page_order: int,
        is_persistent: bool = False
    ) -> ImageCache:
        """
        Tambahkan entry baru ke cache.
        
        ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
        
        Args:
            chapter_id: ID chapter
            gdrive_file_id: ID file dari GDrive
            local_path: Path file di server
            page_order: Nomor urut halaman
            is_persistent: True untuk halaman 1 (tidak akan dihapus)
            
        Returns:
            ImageCache object
        """
        # Check if already exists
        existing = self.db.query(ImageCache).filter(
            ImageCache.gdrive_file_id == gdrive_file_id
        ).first()
        
        if existing:
            # Update last_accessed
            existing.last_accessed = datetime.now(timezone.utc)  # ✅ FIXED
            self.db.commit()
            return existing
        
        # Create new entry
        cache_entry = ImageCache(
            chapter_id=chapter_id,
            gdrive_file_id=gdrive_file_id,
            local_path=local_path,
            page_order=page_order,
            is_persistent=is_persistent,
            last_accessed=datetime.now(timezone.utc)  # ✅ FIXED
        )
        
        self.db.add(cache_entry)
        self.db.commit()
        self.db.refresh(cache_entry)
        
        return cache_entry
    
    def get_cached_file(self, gdrive_file_id: str) -> Optional[ImageCache]:
        """
        Cek apakah file sudah ada di cache dan masih valid.
        
        Args:
            gdrive_file_id: ID file yang dicari
            
        Returns:
            ImageCache object jika ada, None jika tidak
        """
        cache_entry = self.db.query(ImageCache).filter(
            ImageCache.gdrive_file_id == gdrive_file_id
        ).first()
        
        if cache_entry and os.path.exists(cache_entry.local_path):
            # Touch untuk reset timer
            self.touch(gdrive_file_id)
            return cache_entry
        
        # Jika cache entry ada tapi file fisik tidak ada, hapus entry
        if cache_entry:
            self.db.delete(cache_entry)
            self.db.commit()
        
        return None
    
    def cleanup_expired(self) -> dict:
        """
        Hapus semua cache yang sudah expired (> 24 jam).
        KECUALI yang memiliki is_persistent=True atau page_order=1.
        
        Fungsi ini dipanggil oleh background task scheduler.
        
        ✅ FIX #3: Changed datetime.utcnow() to datetime.now(timezone.utc)
        
        Returns:
            Dictionary berisi statistik cleanup
        """
        expiry_time = datetime.now(timezone.utc) - timedelta(hours=self.CACHE_EXPIRY_HOURS)  # ✅ FIXED
        
        # Query untuk cari entry yang expired
        expired_entries = self.db.query(ImageCache).filter(
            and_(
                ImageCache.last_accessed < expiry_time,
                ImageCache.is_persistent == False,
                ImageCache.page_order != 1  # Extra protection untuk halaman 1
            )
        ).all()
        
        deleted_count = 0
        deleted_size = 0
        errors = []
        
        for entry in expired_entries:
            try:
                # Hapus file fisik
                if os.path.exists(entry.local_path):
                    file_size = os.path.getsize(entry.local_path)
                    os.remove(entry.local_path)
                    deleted_size += file_size
                
                # Hapus entry dari database
                self.db.delete(entry)
                deleted_count += 1
                
            except Exception as e:
                errors.append(f"Error deleting {entry.gdrive_file_id}: {str(e)}")
                logger.error(f"Cache cleanup error: {str(e)}", exc_info=True)
        
        # Commit semua deletion
        self.db.commit()
        
        return {
            "deleted_count": deleted_count,
            "deleted_size_mb": round(deleted_size / (1024 * 1024), 2),
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat()  # ✅ FIXED
        }
    
    def cleanup_chapter_cache(self, chapter_id: int) -> int:
        """
        Hapus semua cache untuk chapter tertentu.
        Berguna saat chapter dihapus atau di-reupload.
        
        Args:
            chapter_id: ID chapter yang akan dibersihkan
            
        Returns:
            Jumlah file yang dihapus
        """
        entries = self.db.query(ImageCache).filter(
            ImageCache.chapter_id == chapter_id
        ).all()
        
        deleted_count = 0
        
        for entry in entries:
            try:
                if os.path.exists(entry.local_path):
                    os.remove(entry.local_path)
                
                self.db.delete(entry)
                deleted_count += 1
                
            except Exception as e:
                logger.error(
                    f"Error deleting cache for chapter {chapter_id}: {str(e)}",
                    exc_info=True
                )
        
        self.db.commit()
        
        return deleted_count
    
    def get_cache_stats(self) -> dict:
        """
        Dapatkan statistik penggunaan cache.
        
        Returns:
            Dictionary berisi statistik
        """
        total_entries = self.db.query(ImageCache).count()
        persistent_entries = self.db.query(ImageCache).filter(
            ImageCache.is_persistent == True
        ).count()
        
        # Hitung total size
        total_size = 0
        if self.CACHE_DIR.exists():
            for file in self.CACHE_DIR.iterdir():
                if file.is_file():
                    total_size += file.stat().st_size
        
        return {
            "total_cached_images": total_entries,
            "persistent_images": persistent_entries,
            "temporary_images": total_entries - persistent_entries,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cache_directory": str(self.CACHE_DIR.absolute())
        }