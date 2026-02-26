# File: app/services/multi_remote_upload_service.py
"""
Multi-Remote Upload Service - Upload dengan Auto Backup
========================================================
Service untuk upload file ke Google Drive dengan strategi multi-remote.

Supported Strategies:
1. single: Upload ke 1 remote saja (fastest)
2. parallel: Upload ke semua remote sekaligus (slower)
3. single_with_sync: Upload ke 1 remote + background sync (RECOMMENDED)
"""

import logging
import threading
import time
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from app.core.base import settings
from app.services.multi_remote_service import MultiRemoteService
from app.services.rclone_service import RcloneService

logger = logging.getLogger(__name__)


class UploadResult:
    """Result dari upload operation"""
    def __init__(self, remote_name: str, success: bool, error: Optional[str] = None):
        self.remote_name = remote_name
        self.success = success
        self.error = error
        self.uploaded_at = datetime.utcnow()


class BackgroundSyncTask:
    """Background task untuk sync file ke remote lain"""
    def __init__(self):
        self.sync_queue: List[Dict] = []
        self.lock = threading.Lock()
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False
    
    def add_to_queue(
        self, 
        local_path: Path, 
        remote_path: str, 
        source_remote: str, 
        target_remotes: List[str]
    ):
        """Add file to sync queue"""
        with self.lock:
            self.sync_queue.append({
                "local_path": local_path,
                "remote_path": remote_path,
                "source_remote": source_remote,
                "target_remotes": target_remotes,
                "added_at": datetime.utcnow()
            })
            logger.info(f"Added to sync queue: {remote_path} → {len(target_remotes)} remotes")
    
    def start_worker(self):
        """Start background worker thread"""
        if self.is_running:
            return
        
        self.is_running = True
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        logger.info("✅ Background sync worker started")
    
    def stop_worker(self):
        """Stop background worker"""
        self.is_running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
        logger.info("🛑 Background sync worker stopped")
    
    def _worker_loop(self):
        """Worker loop untuk process sync queue"""
        while self.is_running:
            try:
                # Get item from queue
                sync_item = None
                with self.lock:
                    if self.sync_queue:
                        sync_item = self.sync_queue.pop(0)
                
                if sync_item:
                    self._process_sync_item(sync_item)
                else:
                    # No items, sleep
                    time.sleep(settings.RCLONE_SYNC_DELAY_SECONDS)
                    
            except Exception as e:
                logger.error(f"Error in sync worker: {str(e)}", exc_info=True)
                time.sleep(5)
    
    def _process_sync_item(self, item: Dict):
        """Process satu sync item"""
        local_path = item["local_path"]
        remote_path = item["remote_path"]
        target_remotes = item["target_remotes"]
        
        logger.info(f"🔄 Syncing {remote_path} to {len(target_remotes)} remotes...")
        
        for remote_name in target_remotes:
            try:
                rclone = RcloneService(remote_name=remote_name)
                
                # Upload file
                full_remote_path = f"{remote_name}:{remote_path}"
                
                result = rclone._run_command([
                    "copyto",
                    str(local_path),
                    full_remote_path,
                    "--progress"
                ], timeout=120)
                
                if result.returncode == 0:
                    logger.info(f"✅ Synced to remote '{remote_name}': {remote_path}")
                else:
                    logger.error(f"❌ Failed to sync to '{remote_name}': {result.stderr}")
                    
            except Exception as e:
                logger.error(f"Error syncing to '{remote_name}': {str(e)}")


# Global background sync task instance
background_sync_task = BackgroundSyncTask()


class MultiRemoteUploadService:
    """
    Service untuk upload file dengan multi-remote support.
    """
    
    def __init__(self):
        """Initialize upload service"""
        self.multi_remote = MultiRemoteService()
        self.upload_strategy = settings.RCLONE_UPLOAD_STRATEGY
        
        # Start background sync worker if enabled
        if settings.RCLONE_ENABLE_BACKGROUND_SYNC and self.upload_strategy == "single_with_sync":
            background_sync_task.start_worker()
        
        logger.info(f"MultiRemoteUploadService initialized with strategy: {self.upload_strategy}")
    
    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        preserve_filename: bool = False
    ) -> Dict:
        """
        Upload file ke Google Drive dengan strategy yang dipilih.
        
        Args:
            local_path: Path file lokal
            remote_path: Path tujuan di remote (tanpa remote prefix)
            preserve_filename: Keep filename asli
            
        Returns:
            Upload result dict
        """
        if self.upload_strategy == "single":
            return self._upload_single(local_path, remote_path)
        elif self.upload_strategy == "parallel":
            return self._upload_parallel(local_path, remote_path)
        else:  # single_with_sync
            return self._upload_single_with_sync(local_path, remote_path)
    
    def _upload_single(self, local_path: Path, remote_path: str) -> Dict:
        """
        Upload ke 1 remote saja (fastest).
        
        Returns:
            {"success": bool, "remote": str, "path": str, "error": str}
        """
        try:
            # Get best remote
            remote_name, rclone = self.multi_remote.get_best_remote()
            
            full_remote_path = f"{remote_name}:{remote_path}"
            
            logger.info(f"Uploading to remote '{remote_name}': {remote_path}")
            
            result = rclone._run_command([
                "copyto",
                str(local_path),
                full_remote_path,
                "--progress"
            ], timeout=120)
            
            if result.returncode == 0:
                self.multi_remote.remote_status[remote_name].mark_success()
                
                logger.info(f"✅ Upload success to '{remote_name}': {remote_path}")
                
                return {
                    "success": True,
                    "strategy": "single",
                    "primary_remote": remote_name,
                    "remote_path": remote_path,
                    "backup_remotes": [],
                    "message": f"Uploaded to {remote_name}"
                }
            else:
                self.multi_remote.remote_status[remote_name].mark_failure()
                raise Exception(f"Upload failed: {result.stderr}")
                
        except Exception as e:
            logger.error(f"Upload failed: {str(e)}")
            return {
                "success": False,
                "strategy": "single",
                "error": str(e),
                "remote_path": remote_path
            }
    
    def _upload_parallel(self, local_path: Path, remote_path: str) -> Dict:
        """
        Upload ke SEMUA remote sekaligus (slower tapi langsung backup).
        
        Returns:
            Upload result dengan info semua remote
        """
        results = []
        
        with ThreadPoolExecutor(max_workers=len(self.multi_remote.remotes)) as executor:
            futures = {}
            
            for remote_name, rclone in self.multi_remote.remotes.items():
                if not self.multi_remote.remote_status[remote_name].is_available:
                    continue
                
                future = executor.submit(
                    self._upload_to_remote,
                    local_path,
                    remote_path,
                    remote_name,
                    rclone
                )
                futures[future] = remote_name
            
            for future in as_completed(futures):
                remote_name = futures[future]
                result = future.result()
                results.append(result)
        
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        if not successful:
            return {
                "success": False,
                "strategy": "parallel",
                "error": "All remotes failed",
                "remote_path": remote_path,
                "failed_remotes": [r.remote_name for r in failed]
            }
        
        return {
            "success": True,
            "strategy": "parallel",
            "primary_remote": successful[0].remote_name,
            "backup_remotes": [r.remote_name for r in successful[1:]],
            "remote_path": remote_path,
            "total_uploaded": len(successful),
            "failed_remotes": [r.remote_name for r in failed],
            "message": f"Uploaded to {len(successful)}/{len(results)} remotes"
        }
    
    def _upload_single_with_sync(self, local_path: Path, remote_path: str) -> Dict:
        """
        Upload ke 1 remote + background sync ke lainnya (RECOMMENDED).
        
        Returns:
            Upload result + sync info
        """
        # Upload ke primary remote dulu (cepat)
        upload_result = self._upload_single(local_path, remote_path)
        
        if not upload_result["success"]:
            return upload_result
        
        primary_remote = upload_result["primary_remote"]
        
        # Get other remotes untuk backup
        other_remotes = [
            name for name in self.multi_remote.remotes.keys()
            if name != primary_remote and self.multi_remote.remote_status[name].is_available
        ]
        
        if other_remotes and settings.RCLONE_ENABLE_BACKGROUND_SYNC:
            # Add to background sync queue
            background_sync_task.add_to_queue(
                local_path,
                remote_path,
                primary_remote,
                other_remotes
            )
            
            upload_result["backup_remotes"] = other_remotes
            upload_result["backup_status"] = "queued"
            upload_result["message"] = f"Uploaded to {primary_remote}, syncing to {len(other_remotes)} remotes in background"
        
        return upload_result
    
    def _upload_to_remote(
        self,
        local_path: Path,
        remote_path: str,
        remote_name: str,
        rclone: RcloneService
    ) -> UploadResult:
        """
        Upload file ke specific remote.
        
        Returns:
            UploadResult object
        """
        try:
            full_remote_path = f"{remote_name}:{remote_path}"
            
            result = rclone._run_command([
                "copyto",
                str(local_path),
                full_remote_path,
                "--progress"
            ], timeout=120)
            
            if result.returncode == 0:
                self.multi_remote.remote_status[remote_name].mark_success()
                logger.info(f"✅ Upload success to '{remote_name}'")
                return UploadResult(remote_name, True)
            else:
                self.multi_remote.remote_status[remote_name].mark_failure()
                return UploadResult(remote_name, False, result.stderr)
                
        except Exception as e:
            logger.error(f"Upload to '{remote_name}' failed: {str(e)}")
            return UploadResult(remote_name, False, str(e))
    
    def get_upload_stats(self) -> Dict:
        """Get upload statistics"""
        return {
            "strategy": self.upload_strategy,
            "background_sync_enabled": settings.RCLONE_ENABLE_BACKGROUND_SYNC,
            "remotes": self.multi_remote.get_health_status(),
            "sync_queue_size": len(background_sync_task.sync_queue) if hasattr(background_sync_task, 'sync_queue') else 0
        }


# Global instance
multi_remote_upload_service = MultiRemoteUploadService()