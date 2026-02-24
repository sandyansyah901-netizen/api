# File: app/services/cover_service.py

"""
Cover Service - Local Storage + GDrive Backup
==============================================
Service untuk manage cover manga:
- Upload cover ke local server
- Backup ke GDrive HANYA saat upload/edit (BUKAN auto terus-menerus)
- Download semua cover dari GDrive (migration tool)
- Serve cover ke public

REVISI:
✅ save_cover_local() sekarang preserve format asli (jpg/png/webp)
   Sebelumnya selalu hardcode .jpg sehingga PNG/WEBP gagal/corrupt
✅ optimize_cover_preserve_format() — method baru, optimize tanpa ubah format
✅ backup_cover_to_gdrive() — pakai extension asli, bukan hardcode .jpg
✅ get_cover_stats() — scan semua format image yang didukung
"""

import logging
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from PIL import Image
from datetime import datetime

from app.core.base import settings
from app.services.rclone_service import RcloneService

logger = logging.getLogger(__name__)


class CoverService:
    """Service untuk manage manga cover images."""
    
    COVERS_DIR = Path(settings.COVERS_DIR).resolve()
    COVERS_BACKUP_PATH = settings.COVERS_BACKUP_GDRIVE_PATH
    MAX_SIZE_MB = settings.COVERS_MAX_SIZE_MB
    ALLOWED_TYPES = settings.COVERS_ALLOWED_TYPES
    
    # Optimized cover size
    MAX_WIDTH = 800
    MAX_HEIGHT = 1200
    QUALITY = 85

    # ✅ Format map untuk Pillow save
    _PIL_FORMAT_MAP = {
        ".jpg":  ("JPEG", "image/jpeg"),
        ".jpeg": ("JPEG", "image/jpeg"),
        ".png":  ("PNG",  "image/png"),
        ".webp": ("WEBP", "image/webp"),
    }
    
    def __init__(self):
        self.rclone = RcloneService()
        self._ensure_covers_dir()
    
    def _ensure_covers_dir(self):
        """Pastikan directory covers exists."""
        self.COVERS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"📁 Covers directory: {self.COVERS_DIR}")
    
    def validate_cover_image(
        self, 
        filename: str, 
        file_size: int, 
        content_type: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate cover image file.
        
        Returns:
            (is_valid, error_message)
        """
        # Check content type
        if content_type not in self.ALLOWED_TYPES:
            return False, f"Invalid type. Allowed: {', '.join(self.ALLOWED_TYPES)}"
        
        # Check file extension
        ext = Path(filename).suffix.lower()
        if ext not in ['.jpg', '.jpeg', '.png', '.webp']:
            return False, "Invalid extension. Allowed: .jpg, .png, .webp"
        
        # Check file size
        size_mb = file_size / (1024 * 1024)
        if size_mb > self.MAX_SIZE_MB:
            return False, f"File too large. Max: {self.MAX_SIZE_MB}MB"
        
        return True, None
    
    def optimize_cover(self, input_path: Path, output_path: Path) -> bool:
        """
        Optimize cover image (resize + compress).
        Output SELALU JPEG (method lama, tetap ada untuk backward compat).
        
        Args:
            input_path: Original image path
            output_path: Optimized image path
            
        Returns:
            True if success, False if failed
        """
        try:
            img = Image.open(input_path)
            
            # Convert RGBA to RGB if needed
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            
            # Resize if too large
            if img.width > self.MAX_WIDTH or img.height > self.MAX_HEIGHT:
                img.thumbnail((self.MAX_WIDTH, self.MAX_HEIGHT), Image.Resampling.LANCZOS)
                logger.info(f"Resized cover to {img.width}x{img.height}")
            
            # Save optimized (always JPEG)
            img.save(output_path, 'JPEG', quality=self.QUALITY, optimize=True)
            
            original_size = input_path.stat().st_size
            optimized_size = output_path.stat().st_size
            reduction = ((original_size - optimized_size) / original_size) * 100
            
            logger.info(f"Optimized cover: {reduction:.1f}% size reduction")
            return True
            
        except Exception as e:
            logger.error(f"Failed to optimize cover: {str(e)}", exc_info=True)
            return False

    def optimize_cover_preserve_format(
        self,
        input_path: Path,
        output_path: Path
    ) -> bool:
        """
        ✅ NEW: Optimize cover image sambil preserve format asli (jpg/png/webp).

        Berbeda dari optimize_cover() yang selalu output JPEG,
        method ini menjaga format sesuai extension output_path.

        Kenapa perlu:
        - PNG/WebP bisa punya alpha channel (transparansi). Konversi ke JPEG
          akan membuat background transparent jadi putih secara tidak terduga.
        - WebP biasanya lebih kecil dari JPEG untuk kualitas yang sama.
        - Mempertahankan format asli = lebih "lossless" secara semantic.

        Args:
            input_path:  Original image path (sumber)
            output_path: Optimized image path (tujuan, extension menentukan format)

        Returns:
            True if success, False if failed
        """
        try:
            ext = output_path.suffix.lower()
            pil_format, _ = self._PIL_FORMAT_MAP.get(ext, ("JPEG", "image/jpeg"))

            img = Image.open(input_path)

            # Untuk JPEG: konversi RGBA/P ke RGB (JPEG tidak support alpha)
            if pil_format == "JPEG" and img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img,
                    mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
                )
                img = background
            elif pil_format in ("PNG", "WEBP") and img.mode == "P":
                # PNG/WebP bisa handle RGBA, convert P → RGBA dulu
                img = img.convert("RGBA")

            # Resize jika terlalu besar
            if img.width > self.MAX_WIDTH or img.height > self.MAX_HEIGHT:
                img.thumbnail(
                    (self.MAX_WIDTH, self.MAX_HEIGHT),
                    Image.Resampling.LANCZOS
                )
                logger.info(f"Resized cover to {img.width}x{img.height}")

            # Save dengan format yang sesuai
            save_kwargs: Dict = {"optimize": True}
            if pil_format == "JPEG":
                save_kwargs["quality"] = self.QUALITY
            elif pil_format == "WEBP":
                save_kwargs["quality"] = self.QUALITY
            # PNG tidak pakai quality (pakai compress_level, default fine)

            img.save(output_path, pil_format, **save_kwargs)

            original_size = input_path.stat().st_size
            optimized_size = output_path.stat().st_size
            reduction = (
                ((original_size - optimized_size) / original_size) * 100
                if original_size > 0 else 0
            )

            logger.info(
                f"Optimized cover ({pil_format}): {reduction:.1f}% size reduction "
                f"→ {output_path.name}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to optimize cover (preserve format): {str(e)}",
                exc_info=True
            )
            return False

    def save_cover_local(
        self, 
        file_content: bytes, 
        manga_slug: str,
        optimize: bool = True,
        source_filename: Optional[str] = None,
    ) -> Optional[str]:
        """
        Save cover ke local server.

        ✅ REVISI: Sekarang preserve format asli (jpg/png/webp).
        Sebelumnya selalu hardcode filename ke '{slug}.jpg' sehingga
        file PNG/WEBP disimpan dengan ekstensi salah atau corrupt saat optimize.

        Args:
            file_content:    Image file content (bytes)
            manga_slug:      Manga slug (untuk nama file)
            optimize:        Apply optimization (resize + compress)
            source_filename: Original filename untuk detect ekstensi
                             (opsional; jika None, default ke .jpg untuk
                             backward compatibility)

        Returns:
            Relative path (covers/manga-slug.ext) atau None jika gagal
        """
        try:
            # ✅ Tentukan ekstensi dari source_filename (jika ada)
            if source_filename:
                src_ext = Path(source_filename).suffix.lower()
                if src_ext not in self._PIL_FORMAT_MAP:
                    src_ext = ".jpg"  # fallback ke JPEG jika tidak dikenal
            else:
                src_ext = ".jpg"  # default lama (backward compat)

            # ✅ Generate filename dengan ekstensi yang benar
            filename = f"{manga_slug}{src_ext}"
            temp_path = self.COVERS_DIR / f"temp_{filename}"
            final_path = self.COVERS_DIR / filename
            
            # Save temporary
            with open(temp_path, 'wb') as f:
                f.write(file_content)
            
            logger.info(f"Saved temporary cover: {temp_path}")
            
            # Optimize
            if optimize:
                # ✅ Pakai optimize_cover_preserve_format agar format tidak berubah
                success = self.optimize_cover_preserve_format(temp_path, final_path)
                if not success:
                    # Fallback: move as-is tanpa optimization
                    shutil.move(str(temp_path), str(final_path))
                    logger.warning(
                        f"Optimization failed, saved as-is: {final_path.name}"
                    )
                else:
                    # Hapus temp file (optimize sudah buat final_path)
                    if temp_path.exists():
                        temp_path.unlink()
            else:
                shutil.move(str(temp_path), str(final_path))
            
            # Return relative path
            relative_path = f"covers/{filename}"
            logger.info(f"✅ Cover saved: {relative_path}")
            
            return relative_path
            
        except Exception as e:
            logger.error(f"Failed to save cover locally: {str(e)}", exc_info=True)
            # Cleanup temp file jika ada
            try:
                if 'temp_path' in locals() and temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return None
    
    def backup_cover_to_gdrive(self, local_path: str, manga_slug: str) -> bool:
        """
        ✅ Backup cover ke Google Drive.
        
        DIPANGGIL HANYA SAAT:
        1. Upload cover baru
        2. Edit/replace cover
        3. Create manga baru dengan cover
        
        BUKAN auto backup terus-menerus!

        ✅ REVISI: Pakai extension asli dari local_path, bukan hardcode .jpg.
        Sehingga cover.webp tetap disimpan sebagai .webp di GDrive.
        
        Args:
            local_path: Relative path (covers/manga-slug.ext)
            manga_slug: Manga slug
            
        Returns:
            True if success, False if failed
        """
        try:
            full_local_path = self.COVERS_DIR / Path(local_path).name
            
            if not full_local_path.exists():
                logger.error(f"Local cover not found: {full_local_path}")
                return False

            # ✅ Preserve extension dari filename asli (bukan hardcode .jpg)
            cover_filename = full_local_path.name  # e.g. "one-piece.png"
            gdrive_path = f"{self.COVERS_BACKUP_PATH}/{cover_filename}"
            remote_path = f"{self.rclone.remote_name}:{gdrive_path}"
            
            # Create backup folder
            backup_folder = f"{self.rclone.remote_name}:{self.COVERS_BACKUP_PATH}"
            self.rclone._run_command(["mkdir", backup_folder])
            
            # Upload to GDrive
            logger.info(f"🔄 Backing up cover to GDrive: {gdrive_path}")
            result = self.rclone._run_command([
                "copyto",
                str(full_local_path),
                remote_path,
                "--progress"
            ], timeout=60)
            
            if result.returncode == 0:
                logger.info(f"✅ Cover backed up to GDrive: {gdrive_path}")
                return True
            else:
                logger.error(f"Failed to backup cover: {result.stderr}")
                return False
            
        except Exception as e:
            logger.error(f"Error backing up cover to GDrive: {str(e)}", exc_info=True)
            return False
    
    def download_cover_from_gdrive(self, manga_slug: str) -> Optional[str]:
        """
        Download cover dari GDrive ke local server.
        
        Args:
            manga_slug: Manga slug
            
        Returns:
            Relative path jika berhasil, None jika gagal
        """
        try:
            gdrive_path = f"{self.COVERS_BACKUP_PATH}/{manga_slug}.jpg"
            remote_path = f"{self.rclone.remote_name}:{gdrive_path}"
            
            local_filename = f"{manga_slug}.jpg"
            local_path = self.COVERS_DIR / local_filename
            
            logger.info(f"Downloading cover from GDrive: {gdrive_path}")
            
            result = self.rclone._run_command([
                "copyto",
                remote_path,
                str(local_path),
                "--progress"
            ], timeout=60)
            
            if result.returncode == 0 and local_path.exists():
                relative_path = f"covers/{local_filename}"
                logger.info(f"✅ Cover downloaded: {relative_path}")
                return relative_path
            else:
                logger.error(f"Failed to download cover: {result.stderr}")
                return None
            
        except Exception as e:
            logger.error(f"Error downloading cover from GDrive: {str(e)}", exc_info=True)
            return None
    
    def sync_all_covers_from_gdrive(self) -> Dict[str, any]:
        """
        🚀 MIGRATION TOOL: Download semua cover dari GDrive ke local server.
        
        Digunakan saat pindah server baru.
        
        Returns:
            Statistics dict
        """
        try:
            logger.info("🔄 Starting cover sync from GDrive...")
            
            # List all covers in GDrive backup folder
            backup_path = f"{self.rclone.remote_name}:{self.COVERS_BACKUP_PATH}"
            
            result = self.rclone._run_command([
                "lsjson",
                "--files-only",
                backup_path
            ], timeout=30)
            
            if result.returncode != 0:
                logger.error("Failed to list covers in GDrive")
                return {"success": False, "error": "Failed to list GDrive covers"}
            
            import json
            covers = json.loads(result.stdout) if result.stdout else []
            
            if not covers:
                return {
                    "success": True,
                    "message": "No covers found in GDrive backup",
                    "downloaded": 0,
                    "failed": 0
                }
            
            # Download each cover
            downloaded = 0
            failed = 0
            results = []
            
            for cover in covers:
                cover_name = cover['Name']
                manga_slug = Path(cover_name).stem  # Remove extension
                
                gdrive_file_path = f"{self.COVERS_BACKUP_PATH}/{cover_name}"
                remote_path = f"{self.rclone.remote_name}:{gdrive_file_path}"
                local_path = self.COVERS_DIR / cover_name
                
                try:
                    result = self.rclone._run_command([
                        "copyto",
                        remote_path,
                        str(local_path)
                    ], timeout=60)
                    
                    if result.returncode == 0:
                        downloaded += 1
                        results.append({
                            "manga_slug": manga_slug,
                            "status": "success",
                            "path": f"covers/{cover_name}"
                        })
                        logger.info(f"✅ Downloaded: {cover_name}")
                    else:
                        failed += 1
                        results.append({
                            "manga_slug": manga_slug,
                            "status": "failed",
                            "error": result.stderr
                        })
                        logger.error(f"❌ Failed: {cover_name}")
                        
                except Exception as e:
                    failed += 1
                    results.append({
                        "manga_slug": manga_slug,
                        "status": "failed",
                        "error": str(e)
                    })
            
            logger.info(f"🎉 Cover sync completed: {downloaded} success, {failed} failed")
            
            return {
                "success": True,
                "total_covers": len(covers),
                "downloaded": downloaded,
                "failed": failed,
                "results": results
            }
            
        except Exception as e:
            logger.error(f"Error syncing covers from GDrive: {str(e)}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def delete_cover(self, local_path: str, delete_gdrive: bool = True) -> bool:
        """
        Delete cover dari local server (dan optional GDrive backup).
        
        Args:
            local_path: Relative path (covers/manga-slug.ext)
            delete_gdrive: Also delete from GDrive backup
            
        Returns:
            True if success
        """
        try:
            # Delete local
            full_path = self.COVERS_DIR / Path(local_path).name
            if full_path.exists():
                full_path.unlink()
                logger.info(f"Deleted local cover: {local_path}")
            
            # Delete GDrive backup
            if delete_gdrive:
                # ✅ Pakai nama file asli (preserve extension), bukan hardcode .jpg
                cover_filename = Path(local_path).name  # e.g. "one-piece.png"
                gdrive_path = f"{self.COVERS_BACKUP_PATH}/{cover_filename}"
                remote_path = f"{self.rclone.remote_name}:{gdrive_path}"
                
                result = self.rclone._run_command([
                    "deletefile",
                    remote_path
                ], timeout=30)
                
                if result.returncode == 0:
                    logger.info(f"Deleted GDrive backup: {gdrive_path}")
                else:
                    # Coba juga .jpg fallback jika file dengan extension asli tidak ditemukan
                    # (untuk backward compat dengan cover lama yang disimpan sebagai .jpg)
                    manga_slug = Path(local_path).stem
                    fallback_gdrive_path = f"{self.COVERS_BACKUP_PATH}/{manga_slug}.jpg"
                    if gdrive_path != fallback_gdrive_path:
                        fallback_remote_path = f"{self.rclone.remote_name}:{fallback_gdrive_path}"
                        self.rclone._run_command(["deletefile", fallback_remote_path], timeout=30)
            
            return True
            
        except Exception as e:
            logger.error(f"Error deleting cover: {str(e)}", exc_info=True)
            return False
    
    def get_cover_stats(self) -> Dict:
        """
        Get statistics of local covers.

        ✅ REVISI: Scan semua format image yang didukung,
        bukan hanya .jpg seperti sebelumnya.
        """
        try:
            # ✅ Match semua format gambar yang didukung, bukan hanya .jpg
            covers = [
                f for f in self.COVERS_DIR.iterdir()
                if f.is_file() and f.suffix.lower() in self._PIL_FORMAT_MAP
            ]
            total_size = sum(c.stat().st_size for c in covers)
            
            return {
                "total_covers": len(covers),
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "covers_directory": str(self.COVERS_DIR)
            }
            
        except Exception as e:
            logger.error(f"Error getting cover stats: {str(e)}")
            return {"error": str(e)}