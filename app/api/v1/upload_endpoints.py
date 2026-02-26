# File: app/api/v1/upload_endpoints.py
"""
Upload Endpoints - COMPLETE IMPLEMENTATION + SMART IMPORT + AUTO-THUMBNAIL + ALT TITLES + PREVIEW
===================================================================================================
All upload functionality untuk chapter, manga, bulk upload, dll.

REVISI BESAR:
✅ Tambah SMART IMPORT endpoint (auto-extract metadata dari ZIP)
✅ Support cover, description, genres extraction
✅ ✨ Support alt_titles.txt extraction (BARU!)
✅ ✨ Support custom preview.jpg per chapter (BARU!)
✅ Smart merge (skip existing data)
✅ Auto-generate slug dari nama folder
✅ ✨ AUTO-GENERATE THUMBNAIL 16:9 saat upload
✅ GROUP-AWARE: thumbnail upload ke remote yang sesuai active group
✅ GROUP-AWARE: response mencantumkan active_group dan path_prefix
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, status
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
import asyncio
import logging
import uuid

from app.core.base import get_db, get_current_user, require_role, settings
from app.models.models import User, Manga, Chapter, Page
from app.services.upload_service import UploadService
from app.services.bulk_upload_service import (
    BulkUploadService, upload_progress_store, resume_token_store,
    generate_chapter_slug
)

# ✅ IMPORT THUMBNAIL SERVICE (BARU)
from app.services.thumbnail_service import ThumbnailService

logger = logging.getLogger(__name__)

upload_router = APIRouter()

# ==========================================
# ⚡ SMART IMPORT JOB STORE (in-memory)
# Key: job_id (str), Value: job status dict
# NOTE: Ini in-memory — jika server restart, history hilang.
# Cocok untuk single worker. Jika multi-worker, upgrade ke Redis.
# ==========================================
_smart_import_jobs: Dict[str, dict] = {}


# ==========================================
# ✅ GROUP-AWARE HELPER
# ==========================================

def _get_active_group_info() -> dict:
    """
    ✅ GROUP-AWARE: Ambil info active group dari MultiRemoteService.

    Returns:
        dict berisi:
            - group (int): 1 atau 2
            - primary_remote (str): nama remote primary untuk group aktif
            - path_prefix (str): "" untuk group 1, "@" untuk group 2
            - is_group2 (bool): True kalau group 2 aktif
    """
    try:
        import main
        if main.multi_remote_service and main.multi_remote_service.is_initialized:
            group = main.multi_remote_service.get_active_upload_group()
            primary, _, prefix = main.multi_remote_service.get_upload_remotes()
            return {
                "group": group,
                "primary_remote": primary,
                "path_prefix": prefix,
                "is_group2": group == 2,
            }
    except Exception as e:
        logger.warning(f"Cannot get active group from MultiRemoteService: {e}")

    # Fallback ke group 1
    return {
        "group": 1,
        "primary_remote": settings.RCLONE_PRIMARY_REMOTE,
        "path_prefix": "",
        "is_group2": False,
    }


# ==========================================
# SINGLE CHAPTER UPLOAD (WITH AUTO-THUMBNAIL + GROUP-AWARE)
# ==========================================

@upload_router.post("/chapter", status_code=status.HTTP_201_CREATED)
async def upload_chapter(
    manga_slug: str = Form(..., description="Slug manga yang sudah ada"),
    chapter_main: int = Form(..., ge=0, description="Main chapter number"),
    chapter_sub: int = Form(0, ge=0, description="Sub chapter number"),
    chapter_label: str = Form(..., min_length=1, description="Chapter label (e.g. 'Chapter 1')"),
    chapter_folder_name: str = Form(..., min_length=1, description="Folder name di GDrive"),
    volume_number: Optional[int] = Form(None, ge=1, description="Volume number"),
    files: List[UploadFile] = File(..., description="Chapter image files"),
    preserve_filenames: bool = Form(False, description="Keep original filenames"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("uploader", "admin"))
):
    """
    Upload single chapter dengan images + AUTO-GENERATE THUMBNAIL 16:9.
    
    - Validate manga exists
    - Check chapter conflict
    - Upload images ke Google Drive
    - ✅ Auto-generate thumbnail 16:9 dari page 1
    - ✅ GROUP-AWARE: thumbnail diupload ke remote group yang aktif
    - Auto-mirror ke backup remotes (jika enabled)
    - Create chapter & page records
    """
    
    try:
        # 1. Validate manga exists
        manga = db.query(Manga).filter(Manga.slug == manga_slug).first()
        if not manga:
            raise HTTPException(
                status_code=404, 
                detail=f"Manga '{manga_slug}' tidak ditemukan"
            )
        
        # 2. Check chapter conflict
        existing = db.query(Chapter).filter(
            Chapter.manga_id == manga.id,
            Chapter.chapter_main == chapter_main,
            Chapter.chapter_sub == chapter_sub
        ).first()
        
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Chapter {chapter_main}.{chapter_sub} sudah ada untuk manga '{manga_slug}'"
            )
        
        # 3. Validate files
        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="Minimal 1 file image diperlukan")
        
        # 4. Prepare files
        file_list = []
        for upload_file in files:
            content = await upload_file.read()
            file_list.append((content, upload_file.filename))
        
        logger.info(f"Uploading {len(file_list)} files for {manga_slug} - Chapter {chapter_main}.{chapter_sub}")

        # ✅ GROUP-AWARE: Ambil info active group sebelum upload
        group_info = _get_active_group_info()
        active_group = group_info["group"]
        path_prefix = group_info["path_prefix"]
        primary_remote = group_info["primary_remote"]

        logger.info(
            f"Active upload group: {active_group}, "
            f"primary_remote: {primary_remote}, "
            f"path_prefix: '{path_prefix}'"
        )
        
        # 5. Upload service
        upload_service = UploadService()
        
        result = await upload_service.process_chapter_upload(
            manga_slug=manga_slug,
            chapter_folder_name=chapter_folder_name,
            base_folder_id=manga.storage_source.base_folder_id,
            files=file_list,
            preserve_filenames=preserve_filenames,
            enable_mirror=settings.is_mirror_upload_enabled  # Auto-mirror
        )
        
        if not result["success"]:
            raise HTTPException(
                status_code=500, 
                detail=result.get("error", "Upload failed")
            )
        
        # 6. Create chapter record
        slug = generate_chapter_slug(manga_slug, chapter_main, chapter_sub)
        
        # Handle duplicate slug
        base_slug = slug
        counter = 1
        while db.query(Chapter).filter(Chapter.slug == slug).first():
            slug = f"{base_slug}-v{counter}"
            counter += 1
        
        new_chapter = Chapter(
            manga_id=manga.id,
            chapter_main=chapter_main,
            chapter_sub=chapter_sub,
            chapter_label=chapter_label,
            slug=slug,
            chapter_folder_name=chapter_folder_name,
            volume_number=volume_number,
            uploaded_by=current_user.id
        )
        
        db.add(new_chapter)
        db.flush()
        
        # 7. Create page records
        # ✅ GROUP-AWARE: path_prefix ditambahkan ke gdrive_file_id yang disimpan ke DB
        first_page_db_path = None
        for page_info in result["uploaded_files"]:
            # ✅ Tambah prefix kalau group 2
            db_path = f"{path_prefix}{page_info['gdrive_path']}" if path_prefix else page_info['gdrive_path']
            if page_info["page_order"] == 1:
                first_page_db_path = db_path

            page = Page(
                chapter_id=new_chapter.id,
                gdrive_file_id=db_path,
                page_order=page_info["page_order"],
                is_anchor=(page_info["page_order"] == 1)
            )
            db.add(page)
        
        # ✅ 8. AUTO-GENERATE THUMBNAIL 16:9 (GROUP-AWARE!)
        thumbnail_generated = False
        thumbnail_path = None
        thumbnail_db_path = None  # ✅ path dengan prefix untuk DB

        if result["uploaded_files"]:
            try:
                first_page = result["uploaded_files"][0]
                chapter_folder = result["gdrive_folder_path"]
                thumbnail_path_clean = f"{chapter_folder}/thumbnail.jpg"

                # ✅ GROUP-AWARE: ThumbnailService pakai remote group yang aktif
                thumbnail_service = ThumbnailService(remote_name=primary_remote)
                
                logger.info(
                    f"🎨 Auto-generating 16:9 thumbnail for chapter {chapter_label} "
                    f"(remote: {primary_remote})..."
                )

                # ✅ Source path tanpa prefix (untuk rclone langsung)
                source_clean = first_page["gdrive_path"]
                
                success = thumbnail_service.generate_16_9_thumbnail(
                    source_clean,
                    thumbnail_path_clean
                )
                
                if success:
                    # ✅ GROUP-AWARE: path di DB pakai prefix kalau group 2
                    thumbnail_db_path = f"{path_prefix}{thumbnail_path_clean}" if path_prefix else thumbnail_path_clean
                    thumbnail_path = thumbnail_db_path

                    new_chapter.anchor_path = thumbnail_db_path
                    new_chapter.preview_url = f"/api/v1/image-proxy/image/{thumbnail_db_path}"
                    thumbnail_generated = True
                    logger.info(f"✅ Custom thumbnail generated: {thumbnail_db_path}")
                else:
                    # Fallback to page 1
                    fallback_db_path = first_page_db_path or first_page["gdrive_path"]
                    new_chapter.anchor_path = fallback_db_path
                    new_chapter.preview_url = f"/api/v1/image-proxy/image/{fallback_db_path}"
                    logger.warning(f"⚠️ Thumbnail generation failed, using page 1: {fallback_db_path}")
                    thumbnail_path = fallback_db_path
                    
            except Exception as e:
                # Fallback to page 1 on error
                logger.error(f"❌ Error generating thumbnail: {str(e)}, using page 1 as fallback")
                fallback_db_path = first_page_db_path or result["uploaded_files"][0]["gdrive_path"]
                new_chapter.anchor_path = fallback_db_path
                new_chapter.preview_url = f"/api/v1/image-proxy/image/{fallback_db_path}"
                thumbnail_path = fallback_db_path
        
        db.commit()
        db.refresh(new_chapter)
        
        logger.info(
            f"✅ Chapter uploaded successfully: {manga_slug} - {chapter_label} "
            f"(ID: {new_chapter.id}, {len(result['uploaded_files'])} pages, "
            f"thumbnail: {'16:9 custom' if thumbnail_generated else 'page 1 fallback'}, "
            f"group: {active_group})"
        )
        
        return {
            "success": True,
            "message": f"Chapter {chapter_label} uploaded successfully",
            "chapter_id": new_chapter.id,
            "chapter_slug": new_chapter.slug,
            "chapter_label": new_chapter.chapter_label,
            "gdrive_path": result["gdrive_folder_path"],
            "total_pages": len(result["uploaded_files"]),
            "stats": result["stats"],
            "mirror": result.get("mirror", {}),
            "primary_remote": primary_remote,
            # ✅ GROUP-AWARE: tambah info group di response
            "storage_group": {
                "active_group": active_group,
                "path_prefix": path_prefix,
                "remote": primary_remote,
            },
            "thumbnail": {
                "generated": thumbnail_generated,
                "type": "custom_16_9" if thumbnail_generated else "page_1_original",
                "path": thumbnail_path,
                "preview_url": new_chapter.preview_url
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Upload chapter failed: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Gagal upload chapter: {str(e)}"
        )


# ==========================================
# BULK UPLOAD - CHAPTERS FROM ZIP
# ==========================================

@upload_router.post("/bulk-chapters")
async def bulk_upload_chapters(
    manga_slug: str = Form(...),
    zip_file: UploadFile = File(..., description="ZIP file berisi folders chapter"),
    start_chapter: Optional[int] = Form(None, description="Filter: mulai dari chapter nomor ini"),
    end_chapter: Optional[int] = Form(None, description="Filter: sampai chapter nomor ini"),
    naming_pattern: str = Form(r"[Cc]hapter[_\s]?(\d+(?:\.\d+)?)", description="Regex pattern untuk detect nomor chapter"),
    conflict_strategy: str = Form("skip", description="skip | overwrite | error"),
    dry_run: bool = Form(False, description="True = preview only, tidak upload"),
    parallel: bool = Form(False, description="Upload chapters secara parallel"),
    preserve_filenames: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("uploader", "admin"))
):
    """
    Bulk upload chapters dari ZIP file + AUTO-GENERATE THUMBNAILS.
    
    **ZIP Structure:**
    ```
    archive.zip
    ├── Chapter_01/
    │   ├── 001.jpg
    │   ├── 002.jpg
    ├── Chapter_02/
    │   ├── 001.jpg
    ```
    
    **Features:**
    - Auto-detect chapter numbers dari folder name
    - Filter by chapter range
    - Dry run untuk preview
    - Conflict resolution strategy
    - Progress tracking
    - ✅ Auto-generate thumbnail 16:9 untuk setiap chapter
    """
    
    try:
        # Read ZIP content
        zip_content = await zip_file.read()
        
        logger.info(
            f"Bulk upload request: {manga_slug}, "
            f"ZIP size: {len(zip_content)/(1024*1024):.2f}MB, "
            f"dry_run: {dry_run}"
        )
        
        # Initialize bulk upload service
        bulk_service = BulkUploadService(db)
        
        # Process bulk upload
        result = await bulk_service.bulk_upload_chapters(
            manga_slug=manga_slug,
            zip_content=zip_content,
            uploader_id=current_user.id,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            naming_pattern=naming_pattern,
            conflict_strategy=conflict_strategy,
            dry_run=dry_run,
            parallel=parallel,
            preserve_filenames=preserve_filenames
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Bulk upload failed: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Bulk upload failed: {str(e)}"
        )


# ==========================================
# BULK UPLOAD - FROM JSON METADATA
# ==========================================

@upload_router.post("/bulk-json")
async def bulk_upload_from_json(
    metadata: str = Form(..., description="JSON string dengan chapter metadata"),
    zip_file: UploadFile = File(...),
    conflict_strategy_manga: str = Form("skip", description="on_manga_exists: skip | error"),
    conflict_strategy_chapter: str = Form("skip", description="on_chapter_exists: skip | overwrite | error"),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("uploader", "admin"))
):
    """
    Bulk upload dengan JSON metadata + AUTO-GENERATE THUMBNAILS.
    
    **JSON Format:**
    ```json
    {
      "manga_slug": "one-piece",
      "chapters": [
        {
          "chapter_main": 1,
          "chapter_sub": 0,
          "chapter_label": "Chapter 1",
          "chapter_folder_name": "Chapter_01"
        }
      ]
    }
    ```
    """
    
    try:
        import json
        
        # Parse JSON metadata
        try:
            metadata_dict = json.loads(metadata)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON metadata: {str(e)}"
            )
        
        zip_content = await zip_file.read()
        
        bulk_service = BulkUploadService(db)
        
        conflict_strategy = {
            "on_manga_exists": conflict_strategy_manga,
            "on_chapter_exists": conflict_strategy_chapter
        }
        
        result = await bulk_service.bulk_upload_from_json(
            metadata=metadata_dict,
            zip_content=zip_content,
            uploader_id=current_user.id,
            conflict_strategy=conflict_strategy,
            dry_run=dry_run
        )
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk JSON upload failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# VALIDATE JSON BEFORE UPLOAD
# ==========================================

@upload_router.post("/validate-json")
async def validate_json_config(
    config: str = Form(..., description="JSON config to validate"),
    check_existing: bool = Form(True, description="Check conflicts with existing data"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("uploader", "admin"))
):
    """
    Validate JSON config sebelum upload.
    
    Returns validation errors dan warnings tanpa melakukan upload apapun.
    """
    
    try:
        import json
        
        try:
            config_dict = json.loads(config)
        except json.JSONDecodeError as e:
            return {
                "valid": False,
                "errors": [f"Invalid JSON: {str(e)}"],
                "can_proceed": False
            }
        
        bulk_service = BulkUploadService(db)
        
        result = bulk_service.validate_json_config(
            config=config_dict,
            check_existing=check_existing
        )
        
        return result
        
    except Exception as e:
        logger.error(f"JSON validation failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# MULTIPLE MANGA UPLOAD
# ==========================================

@upload_router.post("/multiple-manga")
async def bulk_upload_multiple_manga(
    config: str = Form(..., description="JSON config dengan multiple manga"),
    zip_file: UploadFile = File(...),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))  # Admin only
):
    """
    Upload multiple manga sekaligus dari JSON + ZIP + AUTO-THUMBNAILS.
    
    **JSON Format:**
    ```json
    {
      "manga_list": [
        {
          "title": "One Piece",
          "slug": "one-piece",
          "storage_id": 1,
          "type_slug": "manga",
          "genre_slugs": ["action", "adventure"],
          "chapters": [
            {
              "chapter_main": 1,
              "chapter_folder_name": "Chapter_01"
            }
          ]
        }
      ]
    }
    ```
    """
    
    try:
        import json
        
        try:
            config_dict = json.loads(config)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
        
        zip_content = await zip_file.read()
        
        bulk_service = BulkUploadService(db)
        
        result = await bulk_service.bulk_upload_multiple_manga(
            config=config_dict,
            zip_content=zip_content,
            uploader_id=current_user.id,
            dry_run=dry_run
        )
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Multiple manga upload failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ⚡ HELPER: Background job runner untuk smart import
# ==========================================

async def _run_smart_import_background(
    job_id: str,
    zip_content: bytes,
    uploader_id: int,
    storage_id: int,
    type_slug: str,
    default_status: str,
    db_session_factory
):
    """
    Background task yang menjalankan smart import.
    Update _smart_import_jobs[job_id] sepanjang proses berjalan.
    """
    from app.services.smart_bulk_import_service import SmartBulkImportService

    _smart_import_jobs[job_id]["status"] = "running"
    _smart_import_jobs[job_id]["message"] = "Mengekstrak ZIP dan memproses manga..."

    try:
        db = db_session_factory()
        try:
            smart_import = SmartBulkImportService(db)
            result = await smart_import.smart_import_from_zip(
                zip_content=zip_content,
                uploader_id=uploader_id,
                storage_id=storage_id,
                type_slug=type_slug,
                default_status=default_status,
                dry_run=False
            )

            _smart_import_jobs[job_id]["status"] = "completed" if result.get("success") else "failed"
            _smart_import_jobs[job_id]["message"] = "Import selesai"
            _smart_import_jobs[job_id]["result"] = result
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Smart import background job {job_id} failed: {str(e)}", exc_info=True)
        _smart_import_jobs[job_id]["status"] = "failed"
        _smart_import_jobs[job_id]["message"] = str(e)
        _smart_import_jobs[job_id]["result"] = {"success": False, "error": str(e)}


# ==========================================
# ✅ SMART BULK IMPORT - AUTO METADATA EXTRACTION
# ==========================================

@upload_router.post("/smart-import")
async def smart_bulk_import(
    zip_file: UploadFile = File(..., description="ZIP dengan struktur: Manga/cover+description+genres+alt_titles+chapters"),
    storage_id: int = Form(1, description="Storage source ID"),
    type_slug: str = Form("manga", description="manga | manhwa | manhua | novel"),
    default_status: str = Form("ongoing", description="ongoing | completed"),
    dry_run: bool = Form(False, description="Preview tanpa upload"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin"))  # Admin only
):
    """
    ✅ SMART BULK IMPORT: Auto-import manga dari ZIP dengan metadata extraction + AUTO-THUMBNAILS.

    ⚡ ASYNC: Endpoint langsung return job_id. Import berjalan di background.
    Gunakan GET /smart-import/status/{job_id} untuk polling progress.

    **Dry run** tetap synchronous (ringan, tidak upload).
    """

    try:
        from app.core.base import SessionLocal

        # Baca ZIP content (wajib selesai sebelum return)
        zip_content = await zip_file.read()

        logger.info(
            f"Smart import request: ZIP size {len(zip_content)/(1024*1024):.2f}MB, "
            f"storage_id={storage_id}, type={type_slug}, dry_run={dry_run}"
        )

        # DRY RUN: langsung proses (ringan, tidak upload apapun)
        if dry_run:
            from app.services.smart_bulk_import_service import SmartBulkImportService
            smart_import = SmartBulkImportService(db)
            result = await smart_import.smart_import_from_zip(
                zip_content=zip_content,
                uploader_id=current_user.id,
                storage_id=storage_id,
                type_slug=type_slug,
                default_status=default_status,
                dry_run=True
            )
            return result

        # REAL IMPORT: jalankan di background, return job_id langsung
        job_id = str(uuid.uuid4())
        _smart_import_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "message": "Import dijadwalkan, akan segera dimulai...",
            "uploader_id": current_user.id,
            "storage_id": storage_id,
            "type_slug": type_slug,
            "result": None,
        }

        background_tasks.add_task(
            _run_smart_import_background,
            job_id=job_id,
            zip_content=zip_content,
            uploader_id=current_user.id,
            storage_id=storage_id,
            type_slug=type_slug,
            default_status=default_status,
            db_session_factory=SessionLocal
        )

        logger.info(f"⚡ Smart import job {job_id} queued (background). Returning immediately.")

        return {
            "job_id": job_id,
            "status": "queued",
            "message": "Import berjalan di background. Gunakan GET /upload/smart-import/status/{job_id} untuk cek progress.",
            "poll_url": f"/api/v1/upload/smart-import/status/{job_id}"
        }

    except Exception as e:
        logger.error(f"Smart import failed: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Smart import failed: {str(e)}"
        )


@upload_router.get("/smart-import/status/{job_id}")
async def get_smart_import_status(
    job_id: str,
    current_user: User = Depends(require_role("admin"))
):
    """
    ⚡ Polling endpoint untuk cek status background smart import job.

    Status lifecycle: queued → running → completed | failed
    """
    job = _smart_import_jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' tidak ditemukan. Job history hilang jika server restart."
        )
    return job



@upload_router.get("/smart-import/example")
def get_smart_import_example():
    """
    Get contoh struktur ZIP untuk smart import.
    
    Returns detail format dan examples untuk smart import feature.
    """
    return {
        "example_structure": {
            "root": "upload.zip",
            "folders": [
                {
                    "manga": "One Piece",
                    "files": [
                        "cover.jpg (required: cover image)",
                        "description.txt (optional: manga description)",
                        "genres.txt (optional: comma-separated genre slugs)",
                        "alt_titles.txt (optional: alternative titles, format: title|lang)",
                        "status.txt (✨ optional: manga status, e.g. Ongoing/Completed)",
                        "type.txt (✨ optional: manga type, e.g. Manga/Manhwa/Manhua)"
                    ],
                    "chapters": [
                        {
                            "folder": "Chapter_01",
                            "files": [
                                "preview.jpg (optional: custom thumbnail)",
                                "001.jpg",
                                "002.jpg",
                                "003.jpg"
                            ]
                        },
                        {
                            "folder": "Chapter_02",
                            "files": [
                                "preview.jpg (optional: custom thumbnail)",
                                "001.jpg",
                                "002.jpg"
                            ]
                        }
                    ]
                },
                {
                    "manga": "Tower of God",
                    "files": [
                        "cover.png",
                        "description.txt",
                        "genres.txt",
                        "alt_titles.txt",
                        "status.txt (✨ isi: Ongoing)",
                        "type.txt (✨ isi: Manhwa)"
                    ],
                    "chapters": [
                        {
                            "folder": "Chapter_01",
                            "files": ["001.jpg", "002.jpg"]
                        }
                    ]
                }
            ]
        },
        "file_formats": {
            "status_txt": {
                "description": "✨ Manga publication status",
                "format": "Single line, case-insensitive",
                "valid_values": ["Ongoing", "Completed", "Hiatus", "Cancelled"],
                "example": "Ongoing",
                "priority": "status.txt > default_status parameter",
                "note": "✨ NEW! Override default_status per manga"
            },
            "type_txt": {
                "description": "✨ Manga type/format",
                "format": "Single line, case-insensitive",
                "valid_values": ["Manga", "Manhwa", "Manhua", "Novel", "Doujinshi", "One-Shot"],
                "example": "Manhwa",
                "priority": "type.txt > file marker (manhwa.txt) > type_slug parameter",
                "note": "✨ NEW! Highest priority for type detection"
            },
            "alt_titles_txt": {
                "description": "Alternative titles in different languages",
                "format": "title|lang (one per line)",
                "example": "ワンピース|ja\n海贼王|zh\n원피스|ko\n# This is a comment",
                "rules": [
                    "One title per line",
                    "Format: title|language_code",
                    "Language code: 2-5 lowercase letters (ja, en, zh, ko, etc)",
                    "Lines starting with # are comments",
                    "Empty lines are skipped"
                ],
                "note": "Auto-merged with existing alt titles"
            },
            "genres_txt": {
                "description": "Comma-separated genre slugs",
                "example": "action,adventure,comedy,shounen",
                "note": "Slugs harus match dengan genre yang sudah ada di database"
            },
            "description_txt": {
                "description": "Plain text description",
                "example": "Monkey D. Luffy adalah seorang bajak laut yang ingin menjadi Raja Bajak Laut.",
                "note": "Support multi-line, encoding UTF-8"
            },
            "cover_image": {
                "description": "Manga cover image",
                "formats": ["cover.jpg", "cover.jpeg", "cover.png", "cover.webp"],
                "max_size_mb": 5,
                "note": "Will be optimized automatically (resize + compress)"
            },
            "preview_image": {
                "description": "Custom chapter thumbnail/preview",
                "formats": ["preview.jpg", "preview.jpeg", "preview.png", "preview.webp"],
                "location": "Inside chapter folder",
                "behavior": [
                    "If exists → used as chapter thumbnail/anchor",
                    "If not exists → auto-use first page",
                    "Excluded from page images",
                    "Recommended aspect ratio: 16:9"
                ],
                "note": "Custom preview per chapter"
            }
        },
        "type_detection_priority": {
            "description": "✨ Type resolution order (highest to lowest)",
            "priority_1": "type.txt (isi file, e.g. 'Manhwa') — ✨ NEW",
            "priority_2": "File marker (manhwa.txt, manga.txt, dll) — dari nama file",
            "priority_3": "Parameter API type_slug — fallback terakhir"
        },
        "status_detection_priority": {
            "description": "✨ Status resolution order (highest to lowest)",
            "priority_1": "status.txt (isi file, e.g. 'Ongoing') — ✨ NEW",
            "priority_2": "Parameter API default_status — fallback terakhir"
        },
        "slug_generation_examples": {
            "One Piece": "one-piece",
            "Naruto Shippuden": "naruto-shippuden",
            "One_Piece": "one-piece",
            "Attack on Titan": "attack-on-titan",
            "Re:Zero": "rezero"
        },
        "smart_merge_rules": {
            "manga_new": {
                "action": "Create new manga",
                "fields": "title, slug, description, cover, genres, alt_titles, type, storage, status"
            },
            "manga_exists": {
                "description_empty": "Add description from description.txt",
                "description_exists": "Skip (tidak overwrite)",
                "cover_empty": "Upload cover from cover.jpg",
                "cover_exists": "Skip (tidak overwrite)",
                "genres_empty": "Add genres from genres.txt",
                "genres_exists": "Skip (tidak overwrite)",
                "alt_titles": "Add new alt titles (skip duplicates)",
                "status": "✨ Use status.txt if available, else keep existing",
                "type": "✨ Use type.txt if available, else keep existing"
            },
            "chapter_new": {
                "action": "Upload to GDrive + create DB record + set preview",
                "fields": "chapter_main, chapter_sub, chapter_label, slug, pages, preview/anchor",
                "preview_behavior": "Use preview.jpg if exists, else use page 1"
            },
            "chapter_exists": {
                "action": "Skip upload",
                "check": "Same chapter_main + chapter_sub for same manga"
            }
        },
        "usage_examples": {
            "dry_run": {
                "description": "Preview tanpa upload (check structure & conflicts)",
                "curl": 'curl -X POST "http://localhost:8000/api/v1/upload/smart-import" -H "Authorization: Bearer TOKEN" -F "zip_file=@manga.zip" -F "dry_run=true"',
                "response_includes": [
                    "Detected manga & chapters",
                    "Alt titles preview",
                    "Custom previews detected",
                    "Conflicts with existing data",
                    "✨ Detected type (with source: type.txt / marker / api_default)",
                    "✨ Detected status (from status.txt or default)"
                ]
            },
            "actual_import": {
                "description": "Actual import dengan upload + auto-preview",
                "curl": 'curl -X POST "http://localhost:8000/api/v1/upload/smart-import" -H "Authorization: Bearer TOKEN" -F "zip_file=@manga.zip" -F "storage_id=1" -F "type_slug=manga" -F "default_status=ongoing"',
                "response_includes": [
                    "Import results per manga",
                    "Alt titles added count",
                    "Previews uploaded count",
                    "Chapters uploaded/skipped"
                ]
            }
        },
        "tips": [
            "Gunakan dry_run=true untuk preview sebelum upload sebenarnya",
            "Pastikan genre slugs di genres.txt sudah ada di database",
            "Nama folder manga akan otomatis jadi slug (spasi → dash, lowercase)",
            "Chapter folder name akan auto-detect nomor chapter (Chapter_01, Ch 1, etc)",
            "Cover akan di-optimize otomatis (resize + compress)",
            "Alt titles format: title|lang (contoh: ワンピース|ja)",
            "Custom preview: tambah preview.jpg di folder chapter untuk thumbnail kustom",
            "Preview auto-exclude dari page images",
            "Smart merge memastikan data existing tidak di-overwrite",
            "✨ BARU: Tambah status.txt untuk set status per manga (Ongoing/Completed/Hiatus/Cancelled)",
            "✨ BARU: Tambah type.txt untuk set type per manga (Manga/Manhwa/Manhua/Novel)",
            "✨ type.txt prioritas lebih tinggi dari file marker (manhwa.txt dll)"
        ],
        "new_features": {
            "status_file_support": {
                "status": "✨ NEW",
                "description": "Read manga status from status.txt file content",
                "valid_values": ["ongoing", "completed", "hiatus", "cancelled"],
                "example_file": "Ongoing",
                "priority": "status.txt > default_status API parameter"
            },
            "type_file_support": {
                "status": "✨ NEW",
                "description": "Read manga type from type.txt file content (highest priority)",
                "valid_values": ["manga", "manhwa", "manhua", "novel", "doujinshi", "one-shot"],
                "example_file": "Manhwa",
                "priority": "type.txt > file marker > type_slug API parameter"
            },
            "alt_titles_support": {
                "status": "EXISTING",
                "description": "Auto-import alternative titles from alt_titles.txt",
                "format": "title|lang",
                "example_file": "ワンピース|ja\n海贼王|zh\n원피스|ko"
            },
            "custom_preview_support": {
                "status": "EXISTING",
                "description": "Custom thumbnail/preview per chapter",
                "filename": "preview.jpg/png/webp",
                "location": "Inside chapter folder",
                "behavior": "Used as anchor if exists, else fallback to page 1"
            }
        }
    }


# ==========================================
# UPLOAD PROGRESS TRACKING
# ==========================================

@upload_router.get("/progress/{upload_id}")
def get_upload_progress(
    upload_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get real-time upload progress.
    
    Returns:
    - Current status
    - Progress percentage
    - Current file being uploaded
    - Total files
    - Errors (if any)
    """
    
    progress = upload_progress_store.get(upload_id)
    
    if not progress:
        raise HTTPException(
            status_code=404, 
            detail=f"Upload ID '{upload_id}' tidak ditemukan atau sudah expired"
        )
    
    return progress


# ==========================================
# RESUME FAILED UPLOAD
# ==========================================

@upload_router.post("/resume/{resume_token}")
async def resume_upload(
    resume_token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("uploader", "admin"))
):
    """
    Resume upload yang gagal menggunakan resume token.
    
    Resume token didapat dari response upload yang failed.
    """
    
    if resume_token not in resume_token_store:
        raise HTTPException(
            status_code=404,
            detail=f"Resume token tidak valid atau sudah expired"
        )
    
    try:
        bulk_service = BulkUploadService(db)
        
        result = await bulk_service.resume_upload(
            resume_token=resume_token,
            uploader_id=current_user.id
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Resume upload failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# HEALTH CHECK
# ==========================================

@upload_router.get("/health")
def upload_service_health():
    """Check upload service health."""
    
    try:
        upload_service = UploadService()
        thumbnail_service = ThumbnailService()

        # ✅ GROUP-AWARE: tampilkan info active group di health check
        group_info = _get_active_group_info()
        
        return {
            "status": "healthy",
            "primary_remote": settings.RCLONE_PRIMARY_REMOTE,
            "backup_remotes": settings.get_secondary_remotes(),
            "mirror_enabled": settings.is_mirror_upload_enabled,
            "temp_dir": str(UploadService.TEMP_UPLOAD_DIR),
            "max_file_size_mb": UploadService.MAX_FILE_SIZE_MB,
            "allowed_extensions": list(UploadService.ALLOWED_EXTENSIONS),
            # ✅ GROUP-AWARE: tambah info active group
            "active_storage_group": {
                "group": group_info["group"],
                "primary_remote": group_info["primary_remote"],
                "path_prefix": group_info["path_prefix"],
                "group2_configured": settings.is_next_group_configured,
                "auto_switch_enabled": settings.RCLONE_AUTO_SWITCH_GROUP,
            },
            "thumbnail": {
                "enabled": True,
                "target_size": f"{thumbnail_service.TARGET_WIDTH}x{thumbnail_service.TARGET_HEIGHT}",
                "aspect_ratio": "16:9",
                "quality": thumbnail_service.QUALITY
            },
            "features": {
                "single_chapter_upload": True,
                "bulk_chapters_upload": True,
                "json_metadata_upload": True,
                "multiple_manga_upload": True,
                "smart_bulk_import": True,
                "smart_import_alt_titles": True,
                "smart_import_custom_preview": True,
                "smart_import_status_file": True,
                "smart_import_type_file": True,
                "progress_tracking": True,
                "resume_upload": True,
                "auto_thumbnail_generation": True,
                "group_aware_upload": True,
            }
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }