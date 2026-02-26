# File: app/services/smart_bulk_import_service.py
"""
Smart Bulk Import Service - ENHANCED VERSION
=============================================
Auto-import manga dari ZIP dengan metadata extraction.

FITUR:
✅ Auto-detect folder struktur
✅ Extract cover, description, genres dari file
✅ Extract alt titles dari alt_titles.txt
✅ Support custom preview.jpg per chapter
✅ Auto-detect manga type dari file marker (manga.txt/manhwa.txt/dll)
✅ ✨ Read type dari type.txt (BARU! prioritas di atas file marker)
✅ ✨ Read status dari status.txt (BARU! override default_status)
✅ Skip existing data (smart merge)
✅ Auto-generate slug dari nama folder
✅ Batch upload ke GDrive + DB
✅ FIX #12: Import create_upload_id dari module level (bukan dari instance)
✅ ⚡ PERF: _upload_chapter_with_preview() pakai rclone copy (folder batch)
           bukan upload per-file. --transfers 8 --checkers 8 --drive-chunk-size 64M
           + auto-mirror ke backup remote setelah upload selesai

REVISI COVER:
✅ save_cover_local() sekarang preserve format asli (jpg/png/webp)
✅ backup_cover_to_gdrive() sekarang pakai nama file asli

REVISI TYPE DETECTION (PRIORITAS):
  1. type.txt    → baca ISI file ("Manhwa" → "manhwa") ✨ BARU
  2. manga.txt / manhwa.txt / dll → deteksi dari NAMA file marker
  3. Parameter API type_slug → fallback terakhir

REVISI STATUS DETECTION (PRIORITAS):
  1. status.txt  → baca ISI file ("Ongoing" → "ongoing") ✨ BARU
  2. Parameter API default_status → fallback terakhir
"""

import asyncio
import logging
import shutil
import re
import os
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from app.core.base import settings
from app.services.bulk_upload_service import (
    BulkUploadService,
    create_upload_id,  # ✅ FIX #12: Import dari module level
    generate_chapter_slug
)
from app.services.cover_service import CoverService
from app.services.natural_sorter import NaturalSorter

logger = logging.getLogger(__name__)


class SmartBulkImportService:
    """Service untuk smart bulk import manga dari ZIP."""

    ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "cover.webp"}
    PREVIEW_NAMES = {"preview.jpg", "preview.jpeg", "preview.png", "preview.webp"}  # ✨ BARU

    # ✨ BARU: Mapping file marker → type_slug
    TYPE_MARKER_FILES = {
        "manga.txt": "manga",
        "manhwa.txt": "manhwa",
        "manhua.txt": "manhua",
        "novel.txt": "novel",
        "doujinshi.txt": "doujinshi",
        "one-shot.txt": "one-shot",
    }

    def __init__(self, db):
        self.db = db
        self.bulk_service = BulkUploadService(db)
        self.cover_service = CoverService()

    def generate_slug(self, title: str) -> str:
        """
        Generate slug dari title.

        Contoh:
        - "One Piece" → "one-piece"
        - "One_Piece" → "one-piece"
        - "Naruto Shippuden" → "naruto-shippuden"
        """
        from app.utils.slug_utils import normalize_slug
        return normalize_slug(title)

    def detect_manga_folders(self, extract_dir: Path) -> List[Dict]:
        """
        Auto-detect manga folders dari hasil extract ZIP.

        Returns:
            List of manga info dicts
        """
        manga_folders = []

        # Cek semua folder di root extract_dir
        for item in extract_dir.iterdir():
            if not item.is_dir():
                continue

            manga_info = self._analyze_manga_folder(item)
            if manga_info:
                manga_folders.append(manga_info)

        logger.info(f"Detected {len(manga_folders)} manga folders")
        return manga_folders

    def _analyze_manga_folder(self, folder: Path) -> Optional[Dict]:
        """
        Analyze satu manga folder untuk extract metadata.

        Returns:
            Manga info dict atau None jika invalid
        """
        try:
            title = folder.name
            slug = self.generate_slug(title)

            # Detect cover
            cover_path = self._find_cover(folder)

            # Read description
            description = self._read_description(folder)

            # Read genres
            genres = self._read_genres(folder)

            # Read alt titles
            alt_titles = self._read_alt_titles(folder)

            # ✨ Read type dari type.txt (prioritas 1)
            type_from_file = self._read_type_from_file(folder)

            # Detect manga type dari file marker (prioritas 2)
            type_from_marker = self._read_manga_type(folder)

            # Resolve: type.txt > file marker > API default
            detected_type_slug = type_from_file or type_from_marker

            # ✨ Read status dari status.txt
            detected_status = self._read_status(folder)

            # Detect chapters
            chapters = self._detect_chapters(folder)

            if not chapters:
                logger.warning(f"No chapters found in '{title}', skipping")
                return None

            return {
                "title": title,
                "slug": slug,
                "cover_path": cover_path,
                "description": description,
                "genres": genres,
                "alt_titles": alt_titles,
                "detected_type_slug": detected_type_slug,
                "type_source": "type.txt" if type_from_file else ("marker" if type_from_marker else None),
                "detected_status": detected_status,
                "chapters": chapters,
                "folder_path": folder
            }

        except Exception as e:
            logger.error(f"Error analyzing folder {folder.name}: {str(e)}")
            return None

    def _find_cover(self, folder: Path) -> Optional[Path]:
        """Find cover image file."""
        for cover_name in self.COVER_NAMES:
            cover_path = folder / cover_name
            if cover_path.exists():
                return cover_path

        # Case-insensitive search
        for file in folder.iterdir():
            if file.is_file() and file.name.lower() in self.COVER_NAMES:
                return file

        return None

    def _read_description(self, folder: Path) -> Optional[str]:
        """Read description from description.txt"""
        desc_file = folder / "description.txt"

        if desc_file.exists():
            try:
                with open(desc_file, 'r', encoding='utf-8') as f:
                    description = f.read().strip()
                    if description:
                        return description
            except Exception as e:
                logger.warning(f"Failed to read description: {str(e)}")

        return None

    def _read_genres(self, folder: Path) -> List[str]:
        """
        Read genres from genres.txt

        Format: comma-separated genre slugs
        Contoh: action,adventure,comedy
        """
        genres_file = folder / "genres.txt"

        if genres_file.exists():
            try:
                with open(genres_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        # Split by comma dan clean whitespace
                        genres = [g.strip() for g in content.split(',') if g.strip()]
                        return genres
            except Exception as e:
                logger.warning(f"Failed to read genres: {str(e)}")

        return []

    def _read_alt_titles(self, folder: Path) -> List[Dict[str, str]]:
        """
        ✨ BARU: Read alternative titles from alt_titles.txt

        Format (per line):
            title|lang

        Example:
            ワンピース|ja
            海贼王|zh
            원피스|ko
            # This is a comment (will be skipped)

        Returns:
            List of dicts: [{"title": "ワンピース", "lang": "ja"}, ...]
        """
        alt_titles_file = folder / "alt_titles.txt"
        alt_titles = []

        if not alt_titles_file.exists():
            return alt_titles

        try:
            with open(alt_titles_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()

                    # Skip empty lines
                    if not line:
                        continue

                    # Skip comments
                    if line.startswith('#'):
                        continue

                    # Parse: title|lang
                    if '|' not in line:
                        logger.warning(
                            f"Invalid alt_titles.txt format at line {line_num}: '{line}' "
                            f"(expected: title|lang)"
                        )
                        continue

                    parts = line.split('|', 1)
                    if len(parts) != 2:
                        logger.warning(f"Invalid alt_titles.txt format at line {line_num}: '{line}'")
                        continue

                    title = parts[0].strip()
                    lang = parts[1].strip()

                    if not title or not lang:
                        logger.warning(f"Empty title or lang at line {line_num}: '{line}'")
                        continue

                    # Validate lang code (2-5 chars, alphanumeric)
                    if not re.match(r'^[a-z]{2,5}$', lang.lower()):
                        logger.warning(
                            f"Invalid language code '{lang}' at line {line_num} "
                            f"(expected: 2-5 lowercase letters)"
                        )
                        continue

                    alt_titles.append({
                        "title": title,
                        "lang": lang.lower()
                    })

            if alt_titles:
                logger.info(f"✅ Loaded {len(alt_titles)} alternative titles from alt_titles.txt")

        except Exception as e:
            logger.error(f"Failed to read alt_titles.txt: {str(e)}", exc_info=True)

        return alt_titles

    # ==========================================
    # ✨ BARU: _read_manga_type
    # Deteksi type manga dari file marker di dalam folder manga.
    #
    # Cara pakai di ZIP:
    #   One Piece/
    #     manga.txt       ← isi bebas atau kosong, yang penting nama filenya
    #     cover.jpg
    #     ...
    #
    #   Tower of God/
    #     manhwa.txt      ← otomatis type = manhwa
    #     cover.jpg
    #     ...
    #
    # Supported markers:
    #   manga.txt     → "manga"
    #   manhwa.txt    → "manhwa"
    #   manhua.txt    → "manhua"
    #   novel.txt     → "novel"
    #   doujinshi.txt → "doujinshi"
    #   one-shot.txt  → "one-shot"
    # ==========================================

    def _read_manga_type(self, folder: Path) -> Optional[str]:
        """
        ✨ BARU: Auto-detect manga type dari file marker di folder manga.

        Cari file seperti manga.txt / manhwa.txt / manhua.txt / dll.
        Isi file tidak penting, yang penting nama filenya.

        Args:
            folder: Manga folder path

        Returns:
            type_slug string (e.g. "manga", "manhwa", "manhua") atau None
            jika tidak ada file marker ditemukan.
        """
        # Cek exact match dulu (case-sensitive)
        for marker_filename, type_slug in self.TYPE_MARKER_FILES.items():
            marker_path = folder / marker_filename
            if marker_path.exists() and marker_path.is_file():
                logger.info(
                    f"✅ Detected manga type from marker '{marker_filename}': {type_slug}"
                )
                return type_slug

        # Case-insensitive fallback
        try:
            for file in folder.iterdir():
                if not file.is_file():
                    continue
                filename_lower = file.name.lower()
                if filename_lower in self.TYPE_MARKER_FILES:
                    type_slug = self.TYPE_MARKER_FILES[filename_lower]
                    logger.info(
                        f"✅ Detected manga type from marker '{file.name}' (case-insensitive): {type_slug}"
                    )
                    return type_slug
        except Exception as e:
            logger.warning(f"Error scanning for type markers: {str(e)}")

        return None  # Tidak ada marker → pakai default dari parameter API

    # ==========================================
    # ✨ BARU: _read_type_from_file
    # Baca type dari ISI file type.txt
    #
    # Cara pakai di ZIP:
    #   One Piece/
    #     type.txt        ← isi: "Manga" atau "manga"
    #     cover.jpg
    #     ...
    #
    # Prioritas:
    #   1. type.txt (isi file)
    #   2. manga.txt/manhwa.txt/dll (nama file marker)
    #   3. Parameter API type_slug (fallback)
    # ==========================================

    # Valid type slugs yang diterima
    VALID_TYPE_SLUGS = {"manga", "manhwa", "manhua", "novel", "doujinshi", "one-shot"}

    def _read_type_from_file(self, folder: Path) -> Optional[str]:
        """
        ✨ BARU: Read manga type dari isi file type.txt.

        Isi file di-normalize ke lowercase slug.
        Contoh: "Manhwa" → "manhwa", "Manga" → "manga"

        Args:
            folder: Manga folder path

        Returns:
            type_slug string atau None jika file tidak ada / invalid
        """
        type_file = folder / "type.txt"

        if not type_file.exists():
            return None

        try:
            with open(type_file, 'r', encoding='utf-8') as f:
                content = f.read().strip().lower()

            if not content:
                logger.warning(f"type.txt is empty in '{folder.name}'")
                return None

            # Normalize: hapus whitespace ekstra
            type_slug = re.sub(r'\s+', '-', content)

            if type_slug in self.VALID_TYPE_SLUGS:
                logger.info(f"✅ Detected type from type.txt: '{type_slug}' (folder: {folder.name})")
                return type_slug
            else:
                logger.warning(
                    f"⚠️ Invalid type '{content}' in type.txt (folder: {folder.name}). "
                    f"Valid types: {', '.join(sorted(self.VALID_TYPE_SLUGS))}"
                )
                return None

        except Exception as e:
            logger.error(f"Failed to read type.txt: {str(e)}")
            return None

    # ==========================================
    # ✨ BARU: _read_status
    # Baca status dari ISI file status.txt
    #
    # Cara pakai di ZIP:
    #   One Piece/
    #     status.txt      ← isi: "Ongoing" atau "ongoing"
    #     cover.jpg
    #     ...
    #
    # Valid values:
    #   ongoing, completed, hiatus, cancelled
    # ==========================================

    VALID_STATUSES = {"ongoing", "completed", "hiatus", "cancelled"}

    def _read_status(self, folder: Path) -> Optional[str]:
        """
        ✨ BARU: Read manga status dari isi file status.txt.

        Isi file di-normalize ke lowercase.
        Contoh: "Ongoing" → "ongoing", "Completed" → "completed"

        Args:
            folder: Manga folder path

        Returns:
            status string atau None jika file tidak ada / invalid
        """
        status_file = folder / "status.txt"

        if not status_file.exists():
            return None

        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                content = f.read().strip().lower()

            if not content:
                logger.warning(f"status.txt is empty in '{folder.name}'")
                return None

            if content in self.VALID_STATUSES:
                logger.info(f"✅ Detected status from status.txt: '{content}' (folder: {folder.name})")
                return content
            else:
                logger.warning(
                    f"⚠️ Invalid status '{content}' in status.txt (folder: {folder.name}). "
                    f"Valid statuses: {', '.join(sorted(self.VALID_STATUSES))}"
                )
                return None

        except Exception as e:
            logger.error(f"Failed to read status.txt: {str(e)}")
            return None

    def _find_preview_in_chapter(self, chapter_folder: Path) -> Optional[Path]:
        """
        ✨ BARU: Find preview.jpg in chapter folder.

        Args:
            chapter_folder: Chapter folder path

        Returns:
            Path to preview file or None
        """
        for preview_name in self.PREVIEW_NAMES:
            preview_path = chapter_folder / preview_name
            if preview_path.exists():
                logger.info(f"✅ Found custom preview: {preview_path.name}")
                return preview_path

        # Case-insensitive search
        for file in chapter_folder.iterdir():
            if file.is_file() and file.name.lower() in self.PREVIEW_NAMES:
                logger.info(f"✅ Found custom preview: {file.name}")
                return file

        return None

    def _detect_chapters(self, folder: Path) -> List[Dict]:
        """
        Detect chapter folders dan images.

        ✨ ENHANCED: Support custom preview.jpg per chapter

        Returns:
            List of chapter info dicts
        """
        chapters = []

        for item in folder.iterdir():
            if not item.is_dir():
                continue

            # Skip metadata files
            if item.name.lower() in ['metadata', 'extras', 'covers']:
                continue

            # ✨ Check for preview.jpg (BARU)
            preview_path = self._find_preview_in_chapter(item)

            # Cek apakah ini chapter folder (has images)
            image_files = [
                f for f in item.iterdir()
                if f.is_file()
                and f.suffix.lower() in self.ALLOWED_IMAGE_EXTS
                and f.name.lower() not in self.PREVIEW_NAMES  # ✨ Exclude preview from pages
            ]

            if not image_files:
                # No page images (only preview exists maybe)
                if preview_path:
                    logger.warning(
                        f"Chapter {item.name} has preview but no page images, skipping"
                    )
                continue

            # Auto-detect chapter info
            from app.services.bulk_upload_service import auto_detect_chapter_info

            chapter_info = auto_detect_chapter_info(item.name)

            # Sort images naturally
            image_files = sorted(
                image_files,
                key=lambda x: NaturalSorter.extract_numbers(x.name)
            )

            chapter_info.update({
                "local_path": item,
                "files": image_files,
                "preview_path": preview_path,  # ✨ BARU
                "has_preview": preview_path is not None,  # ✨ BARU
                "file_count": len(image_files),
                "total_size_bytes": sum(f.stat().st_size for f in image_files)
            })

            chapters.append(chapter_info)

        # Sort chapters by number
        chapters.sort(key=lambda c: (c['chapter_main'], c.get('chapter_sub', 0)))

        return chapters

    async def smart_import_from_zip(
        self,
        zip_content: bytes,
        uploader_id: int,
        storage_id: int = 1,
        type_slug: str = "manga",
        default_status: str = "ongoing",
        dry_run: bool = False
    ) -> Dict:
        """
        🚀 SMART IMPORT: Auto-detect dan import manga dari ZIP.

        Features:
        - Auto-generate slug dari nama folder
        - Extract cover, description, genres
        - ✨ Extract alt titles dari alt_titles.txt (BARU!)
        - ✨ Support custom preview.jpg per chapter (BARU!)
        - ✅ Cover preserve format asli (jpg/png/webp) - REVISI
        - ✨ Auto-detect type dari file marker (manga.txt/manhwa.txt/dll) (BARU!)
        - Skip existing data (smart merge)
        - Upload cover ke local + GDrive backup
        - Upload chapters ke GDrive
        - Create DB records

        Args:
            zip_content: ZIP file content
            uploader_id: ID user yang upload
            storage_id: Storage source ID (default: 1)
            type_slug: Default manga type slug jika tidak ada marker (default: "manga")
            default_status: Default manga status (default: "ongoing")
            dry_run: Preview only tanpa upload

        Returns:
            Import result dict
        """
        from app.models.models import Manga, MangaType, Genre, StorageSource

        # ✅ FIX #12: Gunakan create_upload_id dari module-level import
        session_id = create_upload_id()
        started_at = datetime.utcnow()
        results = []

        try:
            # 1. Extract ZIP
            logger.info("📦 Extracting ZIP file...")
            extract_dir = self.bulk_service.extract_zip(zip_content, session_id)

            if not extract_dir:
                raise ValueError("Failed to extract ZIP file")

            # 2. Detect manga folders
            logger.info("🔍 Detecting manga folders...")
            manga_folders = self.detect_manga_folders(extract_dir)

            if not manga_folders:
                raise ValueError("No valid manga folders found in ZIP")

            logger.info(f"Found {len(manga_folders)} manga folders")

            # 3. Validate storage
            storage = self.db.query(StorageSource).filter(
                StorageSource.id == storage_id
            ).first()
            if not storage:
                raise ValueError(f"Storage ID {storage_id} tidak ditemukan")

            # ✨ BARU: Validate default type (fallback)
            default_manga_type = self.db.query(MangaType).filter(
                MangaType.slug == type_slug
            ).first()
            if not default_manga_type:
                raise ValueError(f"Default manga type '{type_slug}' tidak ditemukan")

            base_folder_id = storage.base_folder_id

            # DRY RUN MODE
            if dry_run:
                preview = []
                for manga_info in manga_folders:
                    existing = self.db.query(Manga).filter(
                        Manga.slug == manga_info['slug']
                    ).first()

                    cover_format = None
                    if manga_info['cover_path']:
                        cover_format = manga_info['cover_path'].suffix.lower()

                    # Resolved type & status untuk dry run
                    resolved_type = manga_info['detected_type_slug'] or type_slug
                    resolved_status = manga_info['detected_status'] or default_status

                    preview.append({
                        "title": manga_info['title'],
                        "slug": manga_info['slug'],
                        "exists": existing is not None,
                        "has_cover": manga_info['cover_path'] is not None,
                        "cover_format": cover_format,
                        "has_description": manga_info['description'] is not None,
                        "genres": manga_info['genres'],
                        "alt_titles": manga_info['alt_titles'],
                        "detected_type": resolved_type,
                        "type_source": manga_info.get('type_source') or "api_default",
                        "detected_status": resolved_status,
                        "status_from_file": manga_info['detected_status'] is not None,
                        "total_chapters": len(manga_info['chapters']),
                        "chapters": [
                            {
                                "chapter_label": ch['chapter_label'],
                                "file_count": ch['file_count'],
                                "has_preview": ch.get('has_preview', False)
                            }
                            for ch in manga_info['chapters']
                        ]
                    })

                self.bulk_service.cleanup_session(session_id)

                return {
                    "dry_run": True,
                    "total_manga": len(manga_folders),
                    "preview": preview
                }

            # 4. Process setiap manga
            for manga_info in manga_folders:
                # Resolve type per manga: type.txt > file marker > API default
                resolved_type_slug = manga_info['detected_type_slug'] or type_slug

                resolved_manga_type = self.db.query(MangaType).filter(
                    MangaType.slug == resolved_type_slug
                ).first()

                if not resolved_manga_type:
                    logger.warning(
                        f"⚠️ Type '{resolved_type_slug}' tidak ditemukan di DB untuk manga "
                        f"'{manga_info['title']}', fallback ke default '{type_slug}'"
                    )
                    resolved_manga_type = default_manga_type

                # ✨ Resolve status per manga: status.txt > API default
                resolved_status = manga_info['detected_status'] or default_status

                result = await self._process_single_manga(
                    manga_info,
                    storage_id,
                    resolved_manga_type.id,
                    base_folder_id,
                    resolved_status,
                    uploader_id
                )
                results.append(result)

            # 5. Summary
            duration = (datetime.utcnow() - started_at).total_seconds()
            successful = [r for r in results if r.get('success')]

            # ✨ Count stats
            total_alt_titles = sum(r.get('alt_titles_added', 0) for r in successful)
            total_previews = sum(r.get('previews_uploaded', 0) for r in successful)

            return {
                "success": True,
                "total_manga": len(manga_folders),
                "imported": len(successful),
                "failed": len(results) - len(successful),
                "total_alt_titles_added": total_alt_titles,  # ✨ BARU
                "total_previews_uploaded": total_previews,   # ✨ BARU
                "results": results,
                "stats": {
                    "duration_seconds": round(duration, 2)
                }
            }

        except Exception as e:
            logger.error(f"Smart import failed: {str(e)}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "partial_results": results
            }

        finally:
            self.bulk_service.cleanup_session(session_id)

    async def _process_single_manga(
        self,
        manga_info: Dict,
        storage_id: int,
        type_id: int,
        base_folder_id: str,
        default_status: str,
        uploader_id: int
    ) -> Dict:
        """
        Process import untuk satu manga.

        ✅ Smart merge: hanya update field yang belum ada
        ✨ ENHANCED: Support alt titles & custom preview
        ✅ REVISI COVER: preserve format asli (jpg/png/webp)
        ✨ REVISI TYPE: type_id sudah di-resolve per manga sebelum memanggil ini
        """
        from app.models.models import Manga, Genre, Chapter, MangaAltTitle

        title = manga_info['title']
        slug = manga_info['slug']

        try:
            # 1. Check existing manga
            manga = self.db.query(Manga).filter(Manga.slug == slug).first()

            is_new = manga is None

            if is_new:
                # Create new manga
                manga = Manga(
                    title=title,
                    slug=slug,
                    storage_id=storage_id,
                    type_id=type_id,
                    status=default_status
                )
                self.db.add(manga)
                self.db.flush()
                logger.info(f"✅ Created new manga: {title}")
            else:
                logger.info(f"📌 Manga exists: {title}, merging data...")

            # 2. ✅ Update description (only if empty)
            if manga_info['description'] and not manga.description:
                manga.description = manga_info['description']
                logger.info(f"  ✅ Added description")

            # 3. ✅ Update genres (only if empty)
            if manga_info['genres'] and not manga.genres:
                genre_slugs = manga_info['genres']
                genres = self.db.query(Genre).filter(
                    Genre.slug.in_(genre_slugs)
                ).all()
                manga.genres = genres
                logger.info(f"  ✅ Added genres: {', '.join(genre_slugs)}")

            # 4. ✨ Add alt titles (BARU!)
            alt_titles_added = 0
            if manga_info['alt_titles']:
                for alt_title_data in manga_info['alt_titles']:
                    # Check if alt title already exists
                    existing_alt = self.db.query(MangaAltTitle).filter(
                        MangaAltTitle.manga_id == manga.id,
                        MangaAltTitle.title == alt_title_data['title'],
                        MangaAltTitle.lang == alt_title_data['lang']
                    ).first()

                    if not existing_alt:
                        alt_title = MangaAltTitle(
                            manga_id=manga.id,
                            title=alt_title_data['title'],
                            lang=alt_title_data['lang']
                        )
                        self.db.add(alt_title)
                        alt_titles_added += 1
                        logger.info(
                            f"  ✅ Added alt title: '{alt_title_data['title']}' ({alt_title_data['lang']})"
                        )

            # 5. ✅ Upload cover (only if not exists)
            # ✅ REVISI: pass source_filename agar format cover asli dipertahankan
            if manga_info['cover_path'] and not manga.cover_image_path:
                cover_path = manga_info['cover_path']

                with open(cover_path, 'rb') as f:
                    cover_content = f.read()

                # ⚡ FIX: run_in_executor agar event loop tidak blocked
                loop = asyncio.get_event_loop()
                local_cover_path = await loop.run_in_executor(
                    None,
                    lambda: self.cover_service.save_cover_local(
                        cover_content,
                        slug,
                        optimize=True,
                        source_filename=cover_path.name
                    )
                )

                if local_cover_path:
                    manga.cover_image_path = local_cover_path

                    # ⚡ FIX: backup_cover_to_gdrive juga dioffload ke threadpool
                    _lcp = local_cover_path
                    await loop.run_in_executor(
                        None,
                        lambda: self.cover_service.backup_cover_to_gdrive(_lcp, slug)
                    )
                    logger.info(
                        f"  ✅ Uploaded cover ({cover_path.suffix.upper()} format preserved): "
                        f"{local_cover_path}"
                    )

            self.db.commit()
            self.db.refresh(manga)

            # 6. ✅ Upload chapters (skip existing)
            chapters_uploaded = 0
            chapters_skipped = 0
            previews_uploaded = 0  # ✨ BARU
            chapter_results = []

            for ch_info in manga_info['chapters']:
                # Check if chapter exists
                existing_ch = self.db.query(Chapter).filter(
                    Chapter.manga_id == manga.id,
                    Chapter.chapter_main == ch_info['chapter_main'],
                    Chapter.chapter_sub == ch_info.get('chapter_sub', 0)
                ).first()

                if existing_ch:
                    chapters_skipped += 1
                    chapter_results.append({
                        "chapter_label": ch_info['chapter_label'],
                        "status": "skipped",
                        "reason": "already_exists"
                    })
                    continue

                # ✨ Upload chapter WITH custom preview support (BARU!)
                upload_result = await self._upload_chapter_with_preview(
                    slug,
                    base_folder_id,
                    ch_info,
                    manga.id,
                    uploader_id
                )

                chapter_results.append(upload_result)

                if upload_result.get('success'):
                    chapters_uploaded += 1
                    if upload_result.get('preview_uploaded'):
                        previews_uploaded += 1

            return {
                "success": True,
                "manga_title": title,
                "manga_slug": slug,
                "is_new": is_new,
                "chapters_uploaded": chapters_uploaded,
                "chapters_skipped": chapters_skipped,
                "alt_titles_added": alt_titles_added,  # ✨ BARU
                "previews_uploaded": previews_uploaded,  # ✨ BARU
                "chapter_results": chapter_results
            }

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to process manga '{title}': {str(e)}", exc_info=True)
            return {
                "success": False,
                "manga_title": title,
                "manga_slug": slug,
                "error": str(e)
            }

    # ==========================================
    # ⚡ REVISED: _upload_chapter_with_preview
    #
    # SEBELUMNYA (LAMBAT):
    #   - Upload file satu per satu pakai rclone copyto per file
    #   - 150 file × 1.5 detik = ~225 detik (3-4 menit)
    #
    # SEKARANG (CEPAT):
    #   - Siapkan temp folder dengan file yang sudah di-rename (001.jpg, 002.jpg, ...)
    #   - Upload 1 folder sekaligus pakai rclone copy --transfers 8 --checkers 8
    #   - Auto-mirror ke backup remotes setelah upload selesai
    #   - Preview file tetap diupload terpisah (copyto) karena hanya 1 file
    #
    # TIDAK ADA LOGIC LAIN YANG BERUBAH:
    #   - Chapter/Page record creation: sama
    #   - anchor_path / preview_url assignment: sama
    #   - Return dict structure: sama
    #   - Error handling: sama
    # ==========================================

    async def _upload_chapter_with_preview(
        self,
        manga_slug: str,
        base_folder_id: str,
        chapter_info: Dict,
        manga_id: int,
        uploader_id: int
    ) -> Dict:
        """
        ⚡ FAST: Upload chapter pakai rclone copy (folder batch) bukan per-file.

        Revisi dari versi lama yang upload satu-satu.

        Process (BARU):
        1. Rename file ke 001.jpg, 002.jpg, ... di temp folder lokal
        2. Upload SEMUA file sekaligus: rclone copy temp_folder remote:chapter_folder
           dengan --transfers 8 --checkers 8 --drive-chunk-size 64M
        3. Auto-mirror ke backup remotes (background, rclone copy)
        4. Upload preview.jpg terpisah (hanya 1 file, copyto)
        5. Create chapter & page records di DB
        6. Set anchor_path & preview_url

        Args:
            manga_slug: Manga slug
            base_folder_id: Base folder ID di GDrive
            chapter_info: Chapter info dict (with preview_path)
            manga_id: Manga database ID
            uploader_id: Uploader user ID

        Returns:
            Upload result dict (struktur sama dengan versi lama)
        """
        from app.models.models import Chapter, Page

        chapter_folder_name = chapter_info["chapter_folder_name"]
        chapter_main = chapter_info["chapter_main"]
        chapter_sub = chapter_info.get("chapter_sub", 0)
        chapter_label = chapter_info["chapter_label"]
        image_files: List[Path] = chapter_info["files"]
        preview_path: Optional[Path] = chapter_info.get("preview_path")

        # ⚡ Gunakan rclone service dari bulk_service (sudah tervalidasi)
        rclone = self.bulk_service.rclone

        # GDrive destination paths
        manga_folder = f"{base_folder_id}/{manga_slug}"
        chapter_folder = f"{manga_folder}/{chapter_folder_name}"

        # Temporary folder untuk staging renamed files
        temp_stage_dir = None

        try:
            # --------------------------------------------------
            # STEP 1: Buat temp folder & rename file ke 001.jpg dst
            # --------------------------------------------------
            temp_stage_dir = Path(tempfile.mkdtemp(prefix="chapter_upload_"))
            logger.info(
                f"⚡ Staging {len(image_files)} files to temp folder: {temp_stage_dir}"
            )

            staged_files: List[Dict] = []
            for idx, img_file in enumerate(image_files, start=1):
                remote_name = f"{idx:03d}{img_file.suffix.lower()}"
                dest = temp_stage_dir / remote_name
                shutil.copy2(str(img_file), str(dest))
                staged_files.append({
                    "gdrive_path": f"{chapter_folder}/{remote_name}",
                    "page_order": idx,
                    "original_name": img_file.name
                })

            logger.info(
                f"✅ Staged {len(staged_files)} files. Starting batch upload to GDrive..."
            )

            # --------------------------------------------------
            # STEP 2: Buat folder di GDrive (mkdir)
            # ⚡ FIX: run_in_executor agar event loop tidak blocked
            # --------------------------------------------------
            loop = asyncio.get_event_loop()
            _remote_manga = f"{rclone.remote_name}:{manga_folder}"
            _remote_chapter = f"{rclone.remote_name}:{chapter_folder}"
            await loop.run_in_executor(
                None,
                lambda: rclone._run_command(["mkdir", _remote_manga])
            )
            await loop.run_in_executor(
                None,
                lambda: rclone._run_command(["mkdir", _remote_chapter])
            )

            # --------------------------------------------------
            # STEP 3: ⚡ Upload SATU FOLDER sekaligus (jauh lebih cepat!)
            #
            # rclone copy <local_temp_dir> <remote>:<chapter_folder>
            #   --transfers 8       → 8 file paralel
            #   --checkers 8        → 8 checker paralel
            #   --drive-chunk-size 64M → chunk besar = fewer requests
            #   --fast-list          → kurangi API calls
            #   --no-traverse        → skip directory scan (kita sudah tahu isinya)
            # --------------------------------------------------
            logger.info(
                f"📤 Batch uploading folder to {rclone.remote_name}:{chapter_folder} "
                f"(transfers=8, checkers=8, chunk=64M)..."
            )

            # ⚡ FIX: rclone copy (operasi paling lama) dioffload ke threadpool
            _stage_dir_str = str(temp_stage_dir)
            _chapter_remote = f"{rclone.remote_name}:{chapter_folder}"
            upload_result = await loop.run_in_executor(
                None,
                lambda: rclone._run_command(
                    [
                        "copy",
                        _stage_dir_str,
                        _chapter_remote,
                        "--transfers", "8",
                        "--checkers", "8",
                        "--drive-chunk-size", "64M",
                        "--fast-list",
                        "--no-traverse",
                    ],
                    timeout=300  # 5 menit timeout untuk folder besar
                )
            )

            if upload_result.returncode != 0:
                error_msg = upload_result.stderr or "Unknown rclone error"
                logger.error(f"❌ Batch folder upload failed: {error_msg}")
                return {
                    "success": False,
                    "chapter_label": chapter_label,
                    "chapter_number": chapter_main,
                    "status": "failed",
                    "error": f"Batch upload failed: {error_msg}"
                }

            logger.info(
                f"✅ Batch upload complete: {len(staged_files)} files "
                f"→ {rclone.remote_name}:{chapter_folder}"
            )

            # --------------------------------------------------
            # STEP 4: ⚡ Auto-mirror ke backup remotes (paralel)
            #
            # Pakai rclone copy dari source remote ke backup remote
            # (server-side copy, tidak perlu download ulang ke lokal)
            # --------------------------------------------------
            backup_remotes = settings.get_secondary_remotes()
            if backup_remotes:
                logger.info(
                    f"🔄 Mirroring to {len(backup_remotes)} backup remote(s): "
                    f"{', '.join(backup_remotes)}"
                )
                for backup_remote in backup_remotes:
                    try:
                        # ⚡ FIX: mirror juga dioffload ke threadpool
                        _src = f"{rclone.remote_name}:{chapter_folder}"
                        _dst = f"{backup_remote}:{chapter_folder}"
                        mirror_result = await loop.run_in_executor(
                            None,
                            lambda: rclone._run_command(
                                [
                                    "copy",
                                    _src,
                                    _dst,
                                    "--transfers", "8",
                                    "--checkers", "8",
                                    "--fast-list",
                                ],
                                timeout=300
                            )
                        )
                        if mirror_result.returncode == 0:
                            logger.info(f"  ✅ Mirrored to '{backup_remote}'")
                        else:
                            logger.warning(
                                f"  ⚠️ Mirror to '{backup_remote}' failed "
                                f"(non-fatal): {mirror_result.stderr}"
                            )
                    except Exception as mirror_err:
                        # Mirror failure is non-fatal — log dan lanjut
                        logger.warning(
                            f"  ⚠️ Mirror error for '{backup_remote}' "
                            f"(non-fatal): {str(mirror_err)}"
                        )

            # --------------------------------------------------
            # STEP 5: Upload preview.jpg (BARU — hanya 1 file, tetap copyto)
            # --------------------------------------------------
            preview_uploaded = False
            preview_gdrive_path = None

            if preview_path and preview_path.exists():
                try:
                    preview_remote_name = f"preview{preview_path.suffix.lower()}"
                    preview_gdrive_path = f"{chapter_folder}/{preview_remote_name}"
                    preview_remote_path = f"{rclone.remote_name}:{preview_gdrive_path}"

                    logger.info(f"📤 Uploading custom preview: {preview_path.name}")

                    # ⚡ FIX: preview upload juga dioffload ke threadpool
                    _prev_src = str(preview_path)
                    _prev_dst = preview_remote_path
                    result = await loop.run_in_executor(
                        None,
                        lambda: rclone._run_command(
                            ["copyto", _prev_src, _prev_dst],
                            timeout=60
                        )
                    )

                    if result.returncode == 0:
                        preview_uploaded = True
                        logger.info(f"✅ Preview uploaded: {preview_gdrive_path}")
                    else:
                        logger.warning(f"⚠️ Preview upload failed: {result.stderr}")

                except Exception as e:
                    logger.error(f"❌ Error uploading preview: {str(e)}")

            # --------------------------------------------------
            # STEP 6: Create chapter record di DB
            # --------------------------------------------------
            chapter_slug = generate_chapter_slug(manga_slug, chapter_main, chapter_sub)

            # Handle duplicate slug
            base_slug = chapter_slug
            counter = 1
            while self.db.query(Chapter).filter(Chapter.slug == chapter_slug).first():
                chapter_slug = f"{base_slug}-v{counter}"
                counter += 1

            new_chapter = Chapter(
                manga_id=manga_id,
                chapter_main=chapter_main,
                chapter_sub=chapter_sub,
                chapter_label=chapter_label,
                slug=chapter_slug,
                chapter_folder_name=chapter_folder_name,
                uploaded_by=uploader_id
            )

            self.db.add(new_chapter)
            self.db.flush()

            # --------------------------------------------------
            # STEP 7: Create page records
            # --------------------------------------------------
            for page_info in staged_files:
                page = Page(
                    chapter_id=new_chapter.id,
                    gdrive_file_id=page_info["gdrive_path"],
                    page_order=page_info["page_order"],
                    is_anchor=(page_info["page_order"] == 1)
                )
                self.db.add(page)

            # --------------------------------------------------
            # STEP 8: ✨ Set anchor_path & preview_url
            # --------------------------------------------------
            if preview_uploaded and preview_gdrive_path:
                # Use custom preview
                new_chapter.anchor_path = preview_gdrive_path
                new_chapter.preview_url = f"/api/v1/image-proxy/image/{preview_gdrive_path}"
                preview_type = "custom"
            else:
                # Fallback to page 1
                if staged_files:
                    new_chapter.anchor_path = staged_files[0]["gdrive_path"]
                    new_chapter.preview_url = f"/api/v1/image-proxy/image/{staged_files[0]['gdrive_path']}"
                    preview_type = "page_1"
                else:
                    preview_type = "none"

            self.db.commit()

            return {
                "success": True,
                "chapter_id": new_chapter.id,
                "chapter_slug": chapter_slug,
                "chapter_label": chapter_label,
                "chapter_number": chapter_main,
                "gdrive_path": chapter_folder,
                "total_pages": len(staged_files),
                "preview_uploaded": preview_uploaded,  # ✨ BARU
                "preview_type": preview_type,           # ✨ BARU
                "status": "success"
            }

        except Exception as e:
            logger.error(f"Failed to upload chapter {chapter_label}: {str(e)}", exc_info=True)
            if self.db:
                self.db.rollback()
            return {
                "success": False,
                "chapter_label": chapter_label,
                "chapter_number": chapter_main,
                "status": "failed",
                "error": str(e)
            }

        finally:
            # --------------------------------------------------
            # CLEANUP: Hapus temp staging folder
            # --------------------------------------------------
            if temp_stage_dir and temp_stage_dir.exists():
                try:
                    shutil.rmtree(str(temp_stage_dir))
                    logger.debug(f"🗑️ Cleaned up temp stage dir: {temp_stage_dir}")
                except Exception as cleanup_err:
                    logger.warning(f"⚠️ Failed to clean temp dir: {cleanup_err}")