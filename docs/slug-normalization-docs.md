# Slug Normalization - Dokumentasi Frontend

## Overview

Semua slug di API sekarang otomatis dinormalisasi:
- Spasi → hyphen (`-`)
- Underscore (`_`) → hyphen (`-`)  
- Lowercase
- Karakter spesial dihapus

**Contoh:**
| Input | Output |
|-------|--------|
| `crimson_reset` | `crimson-reset` |
| `One Piece` | `one-piece` |
| `solo  leveling` | `solo-leveling` |
| `Naruto_Shippuden` | `naruto-shippuden` |
| `action` | `action` (tidak berubah) |

---

## Berlaku Untuk

Slug berikut **otomatis dinormalisasi oleh API**:

| Entity | Field | Contoh |
|--------|-------|--------|
| Manga | `slug` | `crimson-reset` |
| Chapter | `slug` | `crimson-reset-chapter-1` |
| Genre | `slug` | `slice-of-life` |
| Manga Type | `slug` | `manga` |

---

## Perubahan di Frontend

### 1. URL Pattern

**Sebelum (lama):**
```
/manga/crimson_reset
/manga/crimson_reset/chapter/crimson_reset-chapter-1
```

**Sesudah (baru):**
```
/manga/crimson-reset
/manga/crimson-reset/chapter/crimson-reset-chapter-1
```

### 2. API Request

API sekarang **menerima kedua format** dan auto-normalize:

```javascript
// Keduanya menghasilkan response yang sama:
fetch('/api/v1/manga/crimson_reset')   // ✅ tetap bisa
fetch('/api/v1/manga/crimson-reset')   // ✅ format baru (recommended)
```

### 3. Rekomendasi Frontend

```javascript
// Helper function untuk normalize slug di frontend
function normalizeSlug(slug) {
  return slug
    .replace(/_/g, '-')
    .replace(/ /g, '-')
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

// Gunakan saat generate URL
const mangaUrl = `/manga/${normalizeSlug(manga.slug)}`;
```

---

## Endpoint yang Terpengaruh

### Input (Write) - Slug Auto-Normalized

| Method | Endpoint | Field |
|--------|----------|-------|
| POST | `/api/v1/admin/manga` | `slug` |
| PUT | `/api/v1/admin/manga/{id}` | `slug` |
| PUT | `/api/v1/admin/chapter/{id}` | `slug` |
| POST | `/api/v1/admin/genres` | `slug` |
| POST | `/api/v1/admin/manga-types` | `slug` |
| POST | `/api/v1/upload/chapter` | auto-generated |
| POST | `/api/v1/upload/smart-import` | auto-generated |

### Output (Read) - Slug Lookup Normalized

| Method | Endpoint | Parameter |
|--------|----------|-----------|
| GET | `/api/v1/manga/{manga_slug}` | `manga_slug` |
| GET | `/api/v1/manga/cover/{manga_slug}` | `manga_slug` |
| GET | `/api/v1/chapters/manga/{manga_slug}` | `manga_slug` |
| GET | `/api/v1/chapters/{chapter_slug}` | `chapter_slug` |

---

## Response API

Slug di response API **selalu dalam format yang sudah dinormalisasi**:

```json
{
  "id": 1,
  "title": "Crimson Reset",
  "slug": "crimson-reset",
  "chapters": [
    {
      "id": 1,
      "chapter_label": "Chapter 1",
      "slug": "crimson-reset-chapter-1"
    }
  ]
}
```

---

## Migrasi Data Lama

Data slug lama yang sudah ada di database **tidak otomatis berubah**. 
Jika ada slug lama dengan underscore, API tetap bisa diakses karena:
- Lookup slug di endpoint public sudah auto-normalize input
- Contoh: request ke `/manga/crimson_reset` → API cari `crimson-reset` di DB

> **Catatan:** Jika data lama masih pakai underscore di DB, endpoint akan coba 
> cari versi normalized-nya. Pastikan data di DB sudah diupdate ke format baru.

---

## Checklist Frontend

- [ ] Update semua link/URL manga agar pakai `-` bukan `_`
- [ ] Update chapter URL format
- [ ] Tambah helper `normalizeSlug()` di frontend
- [ ] Test akses manga dengan slug lama (`_`) dan baru (`-`)
- [ ] Update sitemap/SEO tags agar pakai format slug baru
