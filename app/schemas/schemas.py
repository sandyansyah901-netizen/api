# File: app/schemas/schemas.py
"""
Pydantic Schemas - All in One + Reading Features
=================================================
Gabungan semua schemas + Reading, Bookmarks, Lists

REVISI: cover_image_url → cover_image_path
"""

from pydantic import BaseModel, EmailStr, Field, validator
from typing import List, Optional
from datetime import datetime


# ==========================================
# AUTH SCHEMAS
# ==========================================

class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    username: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    avatar_url: Optional[str] = None
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


# ==========================================
# STORAGE SCHEMAS
# ==========================================

class StorageSourceCreate(BaseModel):
    source_name: str
    base_folder_id: str
    status: str = "active"


class StorageSourceResponse(BaseModel):
    id: int
    source_name: str
    base_folder_id: str
    status: str
    created_at: datetime
    
    class Config:
        from_attributes = True


class StorageSourceBase(BaseModel):
    id: int
    source_name: str
    base_folder_id: str
    status: str
    
    class Config:
        from_attributes = True


# ==========================================
# MANGA SCHEMAS
# ==========================================

class GenreBase(BaseModel):
    id: int
    name: str
    slug: str
    
    class Config:
        from_attributes = True


class MangaTypeBase(BaseModel):
    id: int
    name: str
    slug: str
    
    class Config:
        from_attributes = True


class AltTitleBase(BaseModel):
    title: str
    lang: str
    
    class Config:
        from_attributes = True


class ChapterSummary(BaseModel):
    id: int
    chapter_label: str
    slug: str
    chapter_folder_name: str
    volume_number: Optional[int] = None
    chapter_type: str = "regular"
    preview_url: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class MangaCreate(BaseModel):
    title: str
    slug: str
    description: Optional[str] = None
    cover_image_path: Optional[str] = None  # ✅ UBAH dari cover_image_url
    storage_id: int
    type_slug: str
    status: str = "ongoing"
    genre_slugs: Optional[List[str]] = []
    alt_titles: Optional[List[dict]] = []


class MangaListResponse(BaseModel):
    id: int
    title: str
    slug: str
    description: Optional[str] = None
    cover_url: Optional[str] = None  # ✅ Full URL untuk frontend
    status: str
    preview_url: Optional[str] = None
    manga_type: MangaTypeBase
    storage_source: StorageSourceBase
    genres: List[GenreBase] = []
    latest_chapter: Optional[ChapterSummary] = None
    total_chapters: int
    created_at: datetime
    
    class Config:
        from_attributes = True


class MangaDetailResponse(BaseModel):
    id: int
    title: str
    slug: str
    description: Optional[str] = None
    cover_url: Optional[str] = None  # ✅ Full URL untuk frontend
    status: str
    manga_type: MangaTypeBase
    storage_source: StorageSourceBase
    genres: List[GenreBase] = []
    alt_titles: List[AltTitleBase] = []
    chapters: List[ChapterSummary] = []
    created_at: datetime
    
    class Config:
        from_attributes = True


# ==========================================
# CHAPTER SCHEMAS
# ==========================================

class PageBase(BaseModel):
    gdrive_file_id: str
    page_order: int
    is_anchor: bool = False


class PageResponse(BaseModel):
    id: int
    gdrive_file_id: str
    page_order: int
    is_anchor: bool
    
    class Config:
        from_attributes = True


class ChapterCreate(BaseModel):
    manga_slug: str
    chapter_main: int
    chapter_sub: int = 0
    chapter_label: str
    slug: str
    chapter_folder_name: str
    volume_number: Optional[int] = None
    chapter_type: str = "regular"
    anchor_path: Optional[str] = None
    preview_url: Optional[str] = None


class ChapterResponse(BaseModel):
    id: int
    chapter_label: str
    slug: str
    chapter_folder_name: str
    volume_number: Optional[int] = None
    chapter_type: str
    anchor_path: Optional[str] = None
    preview_url: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class ChapterDetailResponse(BaseModel):
    id: int
    chapter_label: str
    slug: str
    chapter_folder_name: str
    volume_number: Optional[int] = None
    chapter_type: str
    anchor_path: Optional[str] = None
    preview_url: Optional[str] = None
    created_at: datetime
    manga_title: str
    manga_slug: str
    storage_base_folder_id: str
    pages: List[PageResponse] = []
    
    class Config:
        from_attributes = True


class PagesCreate(BaseModel):
    chapter_slug: str
    pages: List[PageBase]


# ==========================================
# ADMIN SCHEMAS
# ==========================================

class MangaUpdateRequest(BaseModel):
    """Schema untuk update data manga."""
    title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    cover_image_path: Optional[str] = None  # ✅ UBAH dari cover_image_url
    status: Optional[str] = None
    type_slug: Optional[str] = None
    storage_id: Optional[int] = None
    genre_slugs: Optional[List[str]] = None


class AdminMangaResponse(BaseModel):
    id: int
    title: str
    slug: str
    status: str
    total_chapters: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChapterUpdateRequest(BaseModel):
    """Schema untuk update data chapter."""
    chapter_main: Optional[int] = None
    chapter_sub: Optional[int] = None
    chapter_label: Optional[str] = None
    slug: Optional[str] = None
    chapter_folder_name: Optional[str] = None
    volume_number: Optional[int] = None
    chapter_type: Optional[str] = None
    anchor_path: Optional[str] = None
    preview_url: Optional[str] = None


class AdminChapterResponse(BaseModel):
    id: int
    chapter_main: int
    chapter_sub: int
    chapter_label: str
    slug: str
    chapter_folder_name: str
    volume_number: Optional[int] = None
    chapter_type: str
    total_pages: int
    created_at: datetime

    class Config:
        from_attributes = True


class UserRoleUpdateRequest(BaseModel):
    """Schema untuk update role user."""
    roles: List[str]


class UserStatusUpdateRequest(BaseModel):
    """Schema untuk toggle status user."""
    is_active: bool


class AdminUserResponse(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool
    roles: List[str] = []
    total_uploads: int = 0
    created_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class DeleteResponse(BaseModel):
    success: bool
    message: str
    deleted_id: int
    gdrive_folder_deleted: bool = False


class BulkDeleteRequest(BaseModel):
    """Schema untuk bulk delete."""
    ids: List[int]
    delete_gdrive: bool = False


# ==========================================
# READING FEATURE SCHEMAS
# ==========================================

class SaveProgressRequest(BaseModel):
    """Save reading progress."""
    manga_slug: str
    chapter_slug: str
    page_number: int = Field(ge=1, description="Current page number")


class ReadingHistoryResponse(BaseModel):
    """Reading history entry."""
    manga_id: int
    manga_title: str
    manga_slug: str
    manga_cover: Optional[str] = None
    chapter_id: int
    chapter_label: str
    chapter_slug: str
    page_number: int
    total_pages: int
    last_read_at: datetime
    
    class Config:
        from_attributes = True


class LastReadResponse(BaseModel):
    """Last read chapter for a manga."""
    manga_slug: str
    chapter_id: int
    chapter_slug: str
    chapter_label: str
    page_number: int
    total_pages: int
    last_read_at: datetime
    next_chapter: Optional[ChapterSummary] = None


class BookmarkResponse(BaseModel):
    """Bookmark entry."""
    manga_id: int
    manga_title: str
    manga_slug: str
    manga_cover: Optional[str] = None
    total_chapters: int
    latest_chapter: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class ReadingListRequest(BaseModel):
    """Add/update reading list entry."""
    manga_slug: str
    status: str = Field(..., description="plan_to_read | reading | completed | dropped | on_hold")
    rating: Optional[int] = Field(None, ge=1, le=10, description="Rating 1-10")
    notes: Optional[str] = Field(None, max_length=500)
    
    @validator('status')
    def validate_status(cls, v):
        valid = ['plan_to_read', 'reading', 'completed', 'dropped', 'on_hold']
        if v not in valid:
            raise ValueError(f"Status harus salah satu: {', '.join(valid)}")
        return v


class ReadingListResponse(BaseModel):
    """Reading list entry."""
    manga_id: int
    manga_title: str
    manga_slug: str
    manga_cover: Optional[str] = None
    status: str
    rating: Optional[int] = None
    notes: Optional[str] = None
    total_chapters: int
    read_chapters: int = 0
    added_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ==========================================
# ANALYTICS SCHEMAS
# ==========================================

class AnalyticsOverviewResponse(BaseModel):
    """Admin analytics overview."""
    total_users: int
    active_users_today: int
    total_manga: int
    total_chapters: int
    total_views_today: int
    total_views_week: int
    total_views_month: int
    popular_genres: List[dict]
    user_growth: dict


class MangaViewsResponse(BaseModel):
    """Manga views analytics."""
    manga_id: int
    manga_title: str
    manga_slug: str
    total_views: int
    views_today: int
    views_week: int
    views_month: int
    unique_viewers: int


class UserGrowthResponse(BaseModel):
    """User growth analytics."""
    date: str
    new_users: int
    total_users: int


# ==========================================
# UPLOAD SCHEMAS
# ==========================================

class ChapterUploadRequest(BaseModel):
    """Request schema untuk upload chapter images."""
    manga_slug: str = Field(..., description="Slug manga")
    chapter_main: int = Field(..., ge=0, description="Main chapter number")
    chapter_sub: int = Field(default=0, ge=0, description="Sub chapter number")
    chapter_label: str = Field(..., min_length=1, max_length=100, description="Chapter label")
    chapter_folder_name: str = Field(..., min_length=1, max_length=100, description="Folder name in GDrive")
    volume_number: Optional[int] = Field(None, ge=1, description="Volume number")
    chapter_type: str = Field(default="regular", description="regular | special | extra | omake | side_story")
    preserve_filenames: bool = Field(default=False, description="Keep original filenames atau auto-rename")
    
    @validator('chapter_folder_name')
    def validate_folder_name(cls, v):
        """Validate folder name tidak mengandung karakter ilegal."""
        illegal_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        for char in illegal_chars:
            if char in v:
                raise ValueError(f"Folder name tidak boleh mengandung: {', '.join(illegal_chars)}")
        return v
    
    @validator('chapter_type')
    def validate_chapter_type(cls, v):
        valid = ['regular', 'special', 'extra', 'omake', 'side_story']
        if v not in valid:
            raise ValueError(f"chapter_type harus: {', '.join(valid)}")
        return v


class UploadedFileInfo(BaseModel):
    """Info file yang sudah diupload."""
    original_name: str
    gdrive_path: str
    page_order: int
    size: int
    
    class Config:
        from_attributes = True


class UploadStatsResponse(BaseModel):
    """Statistics dari upload."""
    total_files: int
    total_size_mb: float
    files: List[dict]


class ChapterUploadResponse(BaseModel):
    """Response untuk chapter upload."""
    success: bool
    message: str
    chapter_id: Optional[int] = None
    chapter_slug: Optional[str] = None
    gdrive_folder_path: Optional[str] = None
    uploaded_files: List[UploadedFileInfo] = []
    stats: Optional[UploadStatsResponse] = None
    error: Optional[str] = None


class MangaUploadRequest(BaseModel):
    """Request untuk upload manga baru + first chapter sekaligus."""
    title: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    storage_id: int = Field(..., ge=1)
    type_slug: str = Field(..., description="manga/manhwa/manhua")
    status: str = Field(default="ongoing", description="ongoing/completed")
    genre_slugs: Optional[str] = Field(default="", description="Comma-separated genre slugs")
    chapter_label: str = Field(..., description="Label chapter pertama")
    chapter_folder_name: str = Field(..., description="Nama folder chapter pertama")
    
    @validator('genre_slugs')
    def parse_genre_slugs(cls, v):
        """Parse comma-separated genre slugs."""
        if not v:
            return []
        return [slug.strip() for slug in v.split(',') if slug.strip()]


class MangaUploadResponse(BaseModel):
    """Response untuk manga + chapter upload."""
    success: bool
    message: str
    manga_id: Optional[int] = None
    manga_slug: Optional[str] = None
    chapter_id: Optional[int] = None
    chapter_slug: Optional[str] = None
    stats: Optional[UploadStatsResponse] = None
    error: Optional[str] = None


class BatchUploadStatus(BaseModel):
    """Status untuk batch upload (multiple chapters)."""
    total_chapters: int
    successful: int
    failed: int
    chapters: List[dict]


class UploadProgressResponse(BaseModel):
    """Response untuk tracking upload progress."""
    upload_id: str
    status: str
    progress: int
    current_file: Optional[str] = None
    total_files: int
    uploaded_files: int
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None


class FolderStructureResponse(BaseModel):
    """Response untuk check folder structure."""
    exists: bool
    path: str
    file_count: Optional[int] = None
    total_size_mb: Optional[float] = None
    files: Optional[List[dict]] = None


class ImageValidationError(BaseModel):
    """Error info untuk image validation."""
    filename: str
    error: str
    field: str