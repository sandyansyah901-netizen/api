# File: app/services/thumbnail_service.py
"""
Thumbnail Service - Generate Custom 16:9 Thumbnails
====================================================
Service untuk generate thumbnail 16:9 dari page komik.

Features:
✅ Auto-crop ke aspect ratio 16:9
✅ Resize ke 1280x720 (optimized)
✅ Upload ke GDrive
✅ Optimize file size (JPEG quality 85)
✅ GROUP-AWARE: support custom remote_name untuk upload ke group yang benar
"""

from PIL import Image
import io
import logging
import subprocess
from pathlib import Path
from typing import Optional

from app.core.base import settings
from app.services.rclone_service import RcloneService

logger = logging.getLogger(__name__)


class ThumbnailService:
    """Service untuk generate custom thumbnail 16:9"""
    
    # Thumbnail specifications
    TARGET_RATIO = (16, 9)
    TARGET_WIDTH = 1280
    TARGET_HEIGHT = 720
    QUALITY = 85
    
    def __init__(self, remote_name: Optional[str] = None):
        """
        Initialize ThumbnailService.

        ✅ GROUP-AWARE: Jika remote_name diberikan, pakai remote itu.
        Jika None, pakai default primary remote (settings.RCLONE_PRIMARY_REMOTE).

        Args:
            remote_name: Optional rclone remote name (e.g. "gdrive11" untuk group 2).
                         Kalau None, pakai default primary remote.
        """
        # ✅ GROUP-AWARE: pilih remote sesuai group yang diminta
        if remote_name:
            self.rclone = RcloneService(remote_name=remote_name)
        else:
            self.rclone = RcloneService()
    
    def generate_16_9_thumbnail(
        self, 
        source_gdrive_path: str,
        output_gdrive_path: str,
        source_remote_name: Optional[str] = None,
        output_remote_name: Optional[str] = None,
    ) -> bool:
        """
        Generate thumbnail 16:9 dari source image.
        
        Process:
        1. Download source dari GDrive
        2. Crop ke aspect ratio 16:9 (center crop)
        3. Resize ke 1280x720
        4. Compress (JPEG quality 85)
        5. Upload ke GDrive
        
        Args:
            source_gdrive_path: Path image source (e.g., first page)
            output_gdrive_path: Path untuk save thumbnail
            source_remote_name: Optional remote untuk download source.
                                 Jika None, pakai self.rclone (sudah di-set saat init).
            output_remote_name: Optional remote untuk upload output.
                                 Jika None, pakai self.rclone (sudah di-set saat init).
            
        Returns:
            True if success, False if failed
        """
        try:
            # ✅ GROUP-AWARE: Pilih rclone instance untuk download source
            if source_remote_name:
                source_rclone = RcloneService(remote_name=source_remote_name)
            else:
                source_rclone = self.rclone

            # ✅ GROUP-AWARE: Pilih rclone instance untuk upload output
            if output_remote_name:
                output_rclone = RcloneService(remote_name=output_remote_name)
            else:
                output_rclone = self.rclone

            # 1. Download source image dari GDrive
            logger.info(
                f"📥 Downloading source image: {source_gdrive_path} "
                f"(remote: {source_rclone.remote_name})"
            )
            source_bytes = source_rclone.download_file_to_memory(source_gdrive_path)
            
            if not source_bytes:
                logger.error("❌ Failed to download source image")
                return False
            
            logger.info(f"✅ Downloaded {len(source_bytes)} bytes")
            
            # 2. Open with PIL
            img = Image.open(io.BytesIO(source_bytes))
            original_size = img.size
            logger.info(f"📐 Original size: {original_size[0]}x{original_size[1]}")
            
            # Convert RGBA to RGB if needed
            if img.mode in ('RGBA', 'LA', 'P'):
                logger.info(f"🔄 Converting {img.mode} to RGB")
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode == 'RGBA':
                    background.paste(img, mask=img.split()[-1])
                else:
                    background.paste(img)
                img = background
            
            # 3. Crop to 16:9 aspect ratio (center crop)
            img = self._crop_to_16_9(img)
            logger.info(f"✂️ Cropped to: {img.size[0]}x{img.size[1]}")
            
            # 4. Resize to target size
            img = img.resize((self.TARGET_WIDTH, self.TARGET_HEIGHT), Image.LANCZOS)
            logger.info(f"📏 Resized to: {self.TARGET_WIDTH}x{self.TARGET_HEIGHT}")
            
            # 5. Save to bytes (optimized JPEG)
            output_buffer = io.BytesIO()
            img.save(output_buffer, 'JPEG', quality=self.QUALITY, optimize=True)
            output_bytes = output_buffer.getvalue()
            
            original_mb = len(source_bytes) / (1024 * 1024)
            thumbnail_mb = len(output_bytes) / (1024 * 1024)
            reduction = ((len(source_bytes) - len(output_bytes)) / len(source_bytes)) * 100
            
            logger.info(
                f"💾 Size: {original_mb:.2f}MB → {thumbnail_mb:.2f}MB "
                f"({reduction:.1f}% reduction)"
            )
            
            # 6. Upload to GDrive via rclone rcat
            # ✅ GROUP-AWARE: pakai output_rclone (bisa group 1 atau group 2)
            logger.info(
                f"📤 Uploading thumbnail to GDrive: {output_gdrive_path} "
                f"(remote: {output_rclone.remote_name})"
            )
            
            result = subprocess.run([
                output_rclone.rclone_exe,
                "rcat",
                f"{output_rclone.remote_name}:{output_gdrive_path}",
                "--progress"
            ], input=output_bytes, capture_output=True, timeout=60)
            
            if result.returncode == 0:
                logger.info(f"✅ Thumbnail uploaded successfully: {output_gdrive_path}")
                return True
            else:
                error_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else "Unknown error"
                logger.error(f"❌ Failed to upload thumbnail: {error_msg}")
                return False
            
        except Exception as e:
            logger.error(f"❌ Error generating thumbnail: {str(e)}", exc_info=True)
            return False
    
    def _crop_to_16_9(self, img: Image.Image) -> Image.Image:
        """
        Crop image ke aspect ratio 16:9 (center crop).
        
        Args:
            img: PIL Image object
            
        Returns:
            Cropped PIL Image
        """
        img_width, img_height = img.size
        target_ratio = self.TARGET_WIDTH / self.TARGET_HEIGHT  # 16/9 = 1.777...
        current_ratio = img_width / img_height
        
        if abs(current_ratio - target_ratio) < 0.01:
            # Already 16:9, no crop needed
            return img
        
        if current_ratio > target_ratio:
            # Image too wide, crop width (sides)
            new_width = int(img_height * target_ratio)
            left = (img_width - new_width) // 2
            img = img.crop((left, 0, left + new_width, img_height))
            logger.info(f"✂️ Cropped width: {img_width} → {new_width} (removed {img_width - new_width}px from sides)")
        else:
            # Image too tall, crop height (top/bottom)
            new_height = int(img_width / target_ratio)
            top = (img_height - new_height) // 2
            img = img.crop((0, top, img_width, top + new_height))
            logger.info(f"✂️ Cropped height: {img_height} → {new_height} (removed {img_height - new_height}px from top/bottom)")
        
        return img
    
    def validate_thumbnail_image(
        self, 
        filename: str, 
        file_size: int, 
        content_type: str
    ) -> tuple[bool, Optional[str]]:
        """
        Validate uploaded thumbnail image.
        
        Args:
            filename: File name
            file_size: File size in bytes
            content_type: MIME type
            
        Returns:
            (is_valid, error_message)
        """
        # Check content type
        allowed_types = {'image/jpeg', 'image/png', 'image/webp'}
        if content_type not in allowed_types:
            return False, f"Invalid type. Allowed: {', '.join(allowed_types)}"
        
        # Check file extension
        ext = Path(filename).suffix.lower()
        if ext not in ['.jpg', '.jpeg', '.png', '.webp']:
            return False, "Invalid extension. Allowed: .jpg, .png, .webp"
        
        # Check file size (max 5MB for thumbnail)
        max_size = 5 * 1024 * 1024
        if file_size > max_size:
            return False, f"File too large. Max: 5MB"
        
        return True, None