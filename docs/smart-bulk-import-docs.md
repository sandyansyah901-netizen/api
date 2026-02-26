# Smart Bulk Import API Documentation

> **Endpoint:** `POST /api/v1/upload/smart-import`
> **Auth:** Admin only (Bearer Token)
> **Content-Type:** multipart/form-data

---

## Overview

Smart Bulk Import memungkinkan import banyak manga sekaligus dari satu file ZIP. Service akan otomatis:
- Detect folder manga dan chapter
- Extract metadata dari file teks (cover, description, genres, alt titles, **status**, **type**)
- Generate slug dari nama folder
- Smart merge data (tidak overwrite data yang sudah ada)
- Upload ke Google Drive + backup

---

## 📁 ZIP Structure

```
upload.zip
│
├── 📁 Judul Komik A (Contoh: Kage no Jitsuryokusha ni Naritakute)
│    ├── 📄 alt_titles.txt      (Berisi judul alternatif berbagai bahasa)
│    ├── 📄 description.txt     (Sinopsis komik)
│    ├── 📄 genres.txt          (Action, Fantasy, dll)
│    ├── 📄 status.txt          (✨ BARU! Isi: Ongoing/Completed/Hiatus/Cancelled)
│    ├── 📄 type.txt            (✨ BARU! Isi: Manga/Manhwa/Manhua/Novel)
│    ├── 🖼️ cover.webp          (Gambar sampul komik)
│    │
│    ├── 📁 Chapter 01
│    │    ├── 🖼️ page_001.webp
│    │    ├── 🖼️ page_002.webp
│    │    └── 🖼️ preview.webp   (Optional: custom thumbnail)
│    │
│    └── 📁 Chapter 02
│         ├── 🖼️ page_001.webp
│         └── 🖼️ preview.webp
│
├── 📁 Judul Komik B (Contoh: Tower of God)
│    ├── 📄 status.txt          (Isi: Ongoing)
│    ├── 📄 type.txt            (Isi: Manhwa)
│    ├── 📄 description.txt
│    ├── 🖼️ cover.png
│    └── 📁 Chapter 01
│         └── 🖼️ page_001.webp
```

---

## 📄 Format File Metadata

### `status.txt` ✨ BARU

Isi file menentukan status publikasi manga. **Satu baris, case-insensitive.**

| Value | Deskripsi |
|-------|-----------|
| `Ongoing` | Masih terbit |
| `Completed` | Sudah tamat |
| `Hiatus` | Hiatus / berhenti sementara |
| `Cancelled` | Dibatalkan |

```
Ongoing
```

**Prioritas:** `status.txt` > parameter API `default_status`

---

### `type.txt` ✨ BARU

Isi file menentukan tipe/format komik. **Satu baris, case-insensitive.**

| Value | Deskripsi |
|-------|-----------|
| `Manga` | Komik Jepang |
| `Manhwa` | Komik Korea |
| `Manhua` | Komik Cina |
| `Novel` | Web Novel / Light Novel |
| `Doujinshi` | Doujinshi |
| `One-Shot` | One-shot manga |

```
Manhwa
```

**Prioritas:** `type.txt` > file marker (`manhwa.txt`) > parameter API `type_slug`

---

### `alt_titles.txt`

Judul alternatif dalam berbagai bahasa. Format: `title|lang_code` per baris.

```
ワンピース|ja
海贼王|zh
원피스|ko
One Piece|en
# Ini komentar (akan diskip)
```

- Language code: 2-5 huruf kecil (ISO 639)
- Baris kosong dan comment (`#`) otomatis diskip

---

### `genres.txt`

Genre dalam format slug, dipisahkan koma.

```
action,adventure,comedy,fantasy
```

> Slug harus match dengan genre yang sudah ada di database.

---

### `description.txt`

Sinopsis/deskripsi komik dalam plain text UTF-8. Support multi-line.

```
Monkey D. Luffy adalah seorang bajak laut yang bermimpi
menjadi Raja Bajak Laut dengan menemukan harta karun
legendaris One Piece.
```

---

### Cover Image

Gambar sampul komik. Nama file harus `cover.{jpg|jpeg|png|webp}`.

Akan otomatis di-optimize (resize + compress). Format file asli dipertahankan.

---

### Preview Image (per Chapter)

Thumbnail custom per chapter. Letakkan di dalam folder chapter dengan nama `preview.{jpg|jpeg|png|webp}`.

- Jika ada → digunakan sebagai thumbnail chapter
- Jika tidak ada → otomatis pakai halaman pertama
- File preview **tidak** dihitung sebagai page

---

## 🔄 Prioritas Resolusi

### Type Detection

```
┌─────────────────────────────────┐
│ 1. type.txt (isi file)          │  ← ✨ BARU (prioritas tertinggi)
│    Contoh isi: "Manhwa"         │
├─────────────────────────────────┤
│ 2. File Marker (nama file)      │
│    Contoh: manhwa.txt, manga.txt│
├─────────────────────────────────┤
│ 3. Parameter API type_slug      │  ← fallback terakhir
│    Default: "manga"             │
└─────────────────────────────────┘
```

### Status Detection

```
┌─────────────────────────────────┐
│ 1. status.txt (isi file)        │  ← ✨ BARU (prioritas tertinggi)
│    Contoh isi: "Completed"      │
├─────────────────────────────────┤
│ 2. Parameter API default_status │  ← fallback terakhir
│    Default: "ongoing"           │
└─────────────────────────────────┘
```

---

## 📡 API Request

### Parameters (Form Data)

| Parameter | Type | Required | Default | Deskripsi |
|-----------|------|----------|---------|-----------|
| `zip_file` | File | ✅ | - | ZIP file sesuai struktur di atas |
| `storage_id` | int | ❌ | `1` | Storage source ID |
| `type_slug` | string | ❌ | `"manga"` | Default type (fallback jika tidak ada type.txt/marker) |
| `default_status` | string | ❌ | `"ongoing"` | Default status (fallback jika tidak ada status.txt) |
| `dry_run` | bool | ❌ | `false` | `true` = preview only, tidak upload |

### Contoh Request (cURL)

**Dry Run (Preview):**
```bash
curl -X POST "http://localhost:8000/api/v1/upload/smart-import" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "zip_file=@manga_collection.zip" \
  -F "dry_run=true"
```

**Actual Import:**
```bash
curl -X POST "http://localhost:8000/api/v1/upload/smart-import" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "zip_file=@manga_collection.zip" \
  -F "storage_id=1" \
  -F "type_slug=manga" \
  -F "default_status=ongoing"
```

---

## 📤 API Response

### Dry Run Response

```json
{
  "dry_run": true,
  "total_manga": 2,
  "preview": [
    {
      "title": "Kage no Jitsuryokusha ni Naritakute",
      "slug": "kage-no-jitsuryokusha-ni-naritakute",
      "exists": false,
      "has_cover": true,
      "cover_format": ".webp",
      "has_description": true,
      "genres": ["action", "fantasy"],
      "alt_titles": [
        {"title": "陰の実力者になりたくて", "lang": "ja"}
      ],
      "detected_type": "manga",
      "type_source": "type.txt",
      "detected_status": "ongoing",
      "status_from_file": true,
      "total_chapters": 2,
      "chapters": [
        {"chapter_label": "Chapter 1", "file_count": 15, "has_preview": true},
        {"chapter_label": "Chapter 2", "file_count": 12, "has_preview": false}
      ]
    },
    {
      "title": "Tower of God",
      "slug": "tower-of-god",
      "exists": true,
      "has_cover": true,
      "cover_format": ".png",
      "has_description": true,
      "genres": ["action", "adventure"],
      "alt_titles": [],
      "detected_type": "manhwa",
      "type_source": "type.txt",
      "detected_status": "ongoing",
      "status_from_file": true,
      "total_chapters": 1,
      "chapters": [
        {"chapter_label": "Chapter 1", "file_count": 8, "has_preview": false}
      ]
    }
  ]
}
```

### Actual Import Response

```json
{
  "success": true,
  "total_manga": 2,
  "imported": 2,
  "failed": 0,
  "total_alt_titles_added": 1,
  "total_previews_uploaded": 1,
  "results": [
    {
      "success": true,
      "manga_title": "Kage no Jitsuryokusha ni Naritakute",
      "manga_slug": "kage-no-jitsuryokusha-ni-naritakute",
      "is_new": true,
      "chapters_uploaded": 2,
      "chapters_skipped": 0,
      "alt_titles_added": 1,
      "previews_uploaded": 1,
      "chapter_results": [
        {
          "success": true,
          "chapter_id": 42,
          "chapter_slug": "kage-no-jitsuryokusha-ni-naritakute-chapter-1",
          "chapter_label": "Chapter 1",
          "total_pages": 15,
          "preview_uploaded": true,
          "preview_type": "custom",
          "status": "success"
        }
      ]
    }
  ],
  "stats": {
    "duration_seconds": 45.23
  }
}
```

---

## 🔀 Smart Merge Rules

| Kondisi | Aksi |
|---------|------|
| Manga baru | Create dengan semua metadata |
| Manga ada + description kosong | Tambah description |
| Manga ada + description ada | Skip (tidak overwrite) |
| Manga ada + cover kosong | Upload cover |
| Manga ada + cover ada | Skip |
| Manga ada + genres kosong | Tambah genres |
| Manga ada + genres ada | Skip |
| Manga ada + alt titles | Tambah yang baru (skip duplikat) |
| Chapter baru | Upload + create record |
| Chapter ada | Skip |

---

## 📊 Changelog

### v3 (26 Feb 2026) — ✨ REVISI TERBARU

**Fitur Baru:**
- ✅ `status.txt` — set status per manga dari isi file
- ✅ `type.txt` — set type per manga dari isi file (prioritas tertinggi)

**Perubahan di Service Layer (`smart_bulk_import_service.py`):**

```diff
+ _read_type_from_file()    # Baca type.txt → e.g. "Manhwa" → "manhwa"
+ _read_status()            # Baca status.txt → e.g. "Ongoing" → "ongoing"
+ VALID_TYPE_SLUGS          # Set valid type values
+ VALID_STATUSES            # Set valid status values
  _analyze_manga_folder()   # Sekarang baca type.txt + status.txt
  smart_import_from_zip()   # Resolve status per manga
  _process_single_manga()   # Terima resolved_status
  dry_run preview           # Tambah type_source, detected_status, status_from_file
```

**Perubahan di API Layer (`upload_endpoints.py`):**

```diff
  smart_bulk_import()       # Updated docstring + ZIP structure
  get_smart_import_example()# Tambah status_txt, type_txt di file_formats
+ smart_import_status_file  # Health check feature flag
+ smart_import_type_file    # Health check feature flag
```

### v2 — Alt Titles & Custom Preview

- ✅ `alt_titles.txt` — alternative titles per bahasa
- ✅ `preview.webp` — custom thumbnail per chapter
- ✅ Auto-detect type dari file marker (`manhwa.txt`, `manga.txt`, dll)

### v1 — Initial Release

- ✅ Auto-detect folder structure
- ✅ Extract cover, description, genres
- ✅ Smart merge
- ✅ Batch upload ke GDrive

---

## 🔗 Related Endpoints

| Endpoint | Method | Deskripsi |
|----------|--------|-----------|
| `/api/v1/upload/smart-import` | POST | Smart bulk import |
| `/api/v1/upload/smart-import/example` | GET | Lihat contoh format & structure |
| `/api/v1/upload/health` | GET | Check upload service health |
