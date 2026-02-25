# API Dokumentasi — User Features

> **Base URL:** `http://localhost:8000`  
> **Auth:** Semua endpoint bertanda 🔒 wajib menyertakan header:
> ```
> Authorization: Bearer <access_token>
> ```
> Token didapat dari `POST /api/v1/auth/login`.

---

## 📚 Daftar Isi

1. [Reading History](#1-reading-history)
2. [Bookmarks](#2-bookmarks)
3. [Reading Lists](#3-reading-lists)

---

## 1. Reading History

Fitur untuk menyimpan dan mengambil progress baca user per chapter.

---

### 1.1 Save Reading Progress

Menyimpan posisi halaman yang sedang dibaca user. Jika progress untuk manga + chapter yang sama sudah ada, maka akan di-**update** (bukan duplikat).

| | |
|---|---|
| **Method** | `POST` |
| **URL** | `/api/v1/reading/save` |
| **Auth** | 🔒 Required |

#### Request Body (JSON)

```json
{
  "manga_slug": "one-piece",
  "chapter_slug": "one-piece-chapter-1",
  "page_number": 5
}
```

| Field | Type | Required | Keterangan |
|---|---|---|---|
| `manga_slug` | string | ✅ | Slug manga yang sedang dibaca |
| `chapter_slug` | string | ✅ | Slug chapter yang sedang dibaca |
| `page_number` | integer | ✅ | Nomor halaman saat ini (mulai dari 1) |

#### Response `200 OK`

```json
{
  "success": true,
  "message": "Progress saved",
  "manga_slug": "one-piece",
  "chapter_slug": "one-piece-chapter-1",
  "page_number": 5
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid / tidak ada |
| `404 Not Found` | `manga_slug` atau `chapter_slug` tidak ditemukan |
| `400 Bad Request` | Chapter tidak termasuk manga tersebut |

#### Contoh Request (cURL)

```bash
curl -X POST http://localhost:8000/api/v1/reading/save \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"manga_slug": "one-piece", "chapter_slug": "one-piece-chapter-1", "page_number": 5}'
```

---

### 1.2 Get Last Read Chapter

Mengambil chapter terakhir yang dibaca user untuk suatu manga, beserta saran chapter berikutnya.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/reading/manga/{manga_slug}/last-read` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang ingin dicek |

#### Response `200 OK`

```json
{
  "manga_slug": "one-piece",
  "chapter_id": 10,
  "chapter_slug": "one-piece-chapter-5",
  "chapter_label": "Chapter 5",
  "page_number": 12,
  "total_pages": 20,
  "last_read_at": "2026-02-24T08:00:00Z",
  "next_chapter": {
    "id": 11,
    "chapter_label": "Chapter 6",
    "slug": "one-piece-chapter-6",
    "chapter_folder_name": "chapter-006",
    "volume_number": 1,
    "chapter_type": "regular",
    "preview_url": "/static/covers/preview-ch6.jpg",
    "created_at": "2026-01-10T00:00:00Z"
  }
}
```

> **Catatan:** `next_chapter` akan bernilai `null` jika chapter yang dibaca adalah chapter terakhir.

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid |
| `404 Not Found` | Manga tidak ditemukan atau belum ada history untuk manga ini |

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/api/v1/reading/manga/one-piece/last-read \
  -H "Authorization: Bearer <token>"
```

---

### 1.3 Get Reading History

Mengambil daftar semua manga yang pernah dibaca user, diurutkan berdasarkan waktu terakhir dibaca.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/reading/history` |
| **Auth** | 🔒 Required |

#### Query Parameters

| Parameter | Type | Default | Keterangan |
|---|---|---|---|
| `page` | integer | `1` | Nomor halaman |
| `page_size` | integer | `20` | Jumlah item per halaman (max: 100) |

#### Response `200 OK`

```json
{
  "items": [
    {
      "manga_id": 1,
      "manga_title": "One Piece",
      "manga_slug": "one-piece",
      "manga_cover": "/static/covers/one-piece.jpg",
      "chapter_id": 10,
      "chapter_label": "Chapter 5",
      "chapter_slug": "one-piece-chapter-5",
      "page_number": 12,
      "total_pages": 20,
      "last_read_at": "2026-02-24T08:00:00Z"
    }
  ],
  "pagination": {
    "total": 42,
    "page": 1,
    "page_size": 20,
    "total_pages": 3
  }
}
```

> **Catatan:** Setiap manga hanya muncul **sekali** (entry terakhir yang dibaca), bukan per chapter.

#### Contoh Request (cURL)

```bash
curl "http://localhost:8000/api/v1/reading/history?page=1&page_size=10" \
  -H "Authorization: Bearer <token>"
```

---

### 1.4 Delete Reading History

Menghapus seluruh history baca user untuk manga tertentu.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/reading/history/manga/{manga_slug}` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang historynya ingin dihapus |

#### Response `200 OK`

```json
{
  "success": true,
  "message": "Deleted 5 history entries",
  "manga_slug": "one-piece"
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid |
| `404 Not Found` | Manga tidak ditemukan |

#### Contoh Request (cURL)

```bash
curl -X DELETE http://localhost:8000/api/v1/reading/history/manga/one-piece \
  -H "Authorization: Bearer <token>"
```

---

## 2. Bookmarks

Fitur untuk menandai manga favorit user (seperti "Favorit" / "Wishlist").

---

### 2.1 Add Bookmark

Menambahkan manga ke daftar bookmark user. Jika sudah di-bookmark sebelumnya, tidak akan duplikat.

| | |
|---|---|
| **Method** | `POST` |
| **URL** | `/api/v1/bookmarks/manga/{manga_slug}` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang ingin di-bookmark |

#### Request Body

Tidak diperlukan body request.

#### Response `201 Created` (baru ditambahkan)

```json
{
  "success": true,
  "message": "Bookmark added",
  "manga_slug": "one-piece",
  "created_at": "2026-02-24T08:00:00Z"
}
```

#### Response `200 OK` (sudah ada sebelumnya)

```json
{
  "success": true,
  "message": "Already bookmarked",
  "manga_slug": "one-piece",
  "created_at": "2026-01-01T00:00:00Z"
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid |
| `404 Not Found` | Manga tidak ditemukan |

#### Contoh Request (cURL)

```bash
curl -X POST http://localhost:8000/api/v1/bookmarks/manga/one-piece \
  -H "Authorization: Bearer <token>"
```

---

### 2.2 Remove Bookmark

Menghapus manga dari daftar bookmark user.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/bookmarks/manga/{manga_slug}` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang ingin dihapus dari bookmark |

#### Response `200 OK`

```json
{
  "success": true,
  "message": "Bookmark removed",
  "manga_slug": "one-piece"
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid |
| `404 Not Found` | Manga tidak ditemukan atau belum di-bookmark |

#### Contoh Request (cURL)

```bash
curl -X DELETE http://localhost:8000/api/v1/bookmarks/manga/one-piece \
  -H "Authorization: Bearer <token>"
```

---

### 2.3 Get Bookmarks

Mengambil daftar semua manga yang di-bookmark oleh user, dengan support sorting dan pagination.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/bookmarks/` |
| **Auth** | 🔒 Required |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `page` | integer | `1` | ≥ 1 | Nomor halaman |
| `page_size` | integer | `20` | 1–100 | Jumlah item per halaman |
| `sort_by` | string | `created_at` | `created_at`, `title`, `updated_at` | Kolom urutan |
| `sort_order` | string | `desc` | `asc`, `desc` | Arah urutan |

#### Response `200 OK`

```json
{
  "items": [
    {
      "manga_id": 1,
      "manga_title": "One Piece",
      "manga_slug": "one-piece",
      "manga_cover": "/static/covers/one-piece.jpg",
      "total_chapters": 1100,
      "latest_chapter": "Chapter 1100",
      "created_at": "2026-02-24T08:00:00Z"
    }
  ],
  "pagination": {
    "total": 15,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### Contoh Request (cURL)

```bash
# Urutkan berdasarkan judul A-Z
curl "http://localhost:8000/api/v1/bookmarks/?sort_by=title&sort_order=asc" \
  -H "Authorization: Bearer <token>"
```

---

### 2.4 Check Bookmark

Mengecek apakah suatu manga sudah di-bookmark oleh user atau belum. Berguna untuk menentukan tampilan tombol bookmark di frontend.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/bookmarks/check/{manga_slug}` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang ingin dicek |

#### Response `200 OK` — sudah di-bookmark

```json
{
  "manga_slug": "one-piece",
  "is_bookmarked": true,
  "created_at": "2026-02-24T08:00:00Z"
}
```

#### Response `200 OK` — belum di-bookmark

```json
{
  "manga_slug": "one-piece",
  "is_bookmarked": false,
  "created_at": null
}
```

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/api/v1/bookmarks/check/one-piece \
  -H "Authorization: Bearer <token>"
```

---

## 3. Reading Lists

Fitur untuk mengkategorikan manga ke dalam daftar baca dengan status tertentu (mirip MAL/AniList).

### Status yang Tersedia

| Status | Keterangan |
|---|---|
| `plan_to_read` | Direncanakan akan dibaca |
| `reading` | Sedang dibaca |
| `completed` | Sudah selesai dibaca |
| `dropped` | Berhenti di tengah jalan |
| `on_hold` | Sementara ditunda |

---

### 3.1 Add to Reading List

Menambahkan manga ke reading list dengan status tertentu. Jika manga sudah ada di list, maka status/rating/notes akan **diperbarui**.

| | |
|---|---|
| **Method** | `POST` |
| **URL** | `/api/v1/lists/` |
| **Auth** | 🔒 Required |

#### Request Body (JSON)

```json
{
  "manga_slug": "one-piece",
  "status": "reading",
  "rating": 9,
  "notes": "Seru banget arc Wano!"
}
```

| Field | Type | Required | Keterangan |
|---|---|---|---|
| `manga_slug` | string | ✅ | Slug manga |
| `status` | string | ✅ | Salah satu dari: `plan_to_read`, `reading`, `completed`, `dropped`, `on_hold` |
| `rating` | integer | ❌ | Rating 1–10 (boleh null) |
| `notes` | string | ❌ | Catatan pribadi (boleh null) |

#### Response `201 Created` (entry baru)

```json
{
  "success": true,
  "message": "Added to reading list",
  "manga_slug": "one-piece",
  "status": "reading",
  "rating": 9
}
```

#### Response `200 OK` (update entry yang sudah ada)

```json
{
  "success": true,
  "message": "Reading list updated",
  "manga_slug": "one-piece",
  "status": "completed",
  "rating": 10
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid |
| `404 Not Found` | Manga tidak ditemukan |
| `422 Unprocessable Entity` | Status di luar nilai yang valid |

#### Contoh Request (cURL)

```bash
curl -X POST http://localhost:8000/api/v1/lists/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"manga_slug": "one-piece", "status": "reading", "rating": 9, "notes": "Arc Wano keren!"}'
```

---

### 3.2 Get Reading Lists

Mengambil daftar manga dalam reading list user, dengan filter status dan sorting.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/lists/` |
| **Auth** | 🔒 Required |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `status` | string | `null` | `plan_to_read`, `reading`, `completed`, `dropped`, `on_hold` | Filter by status (opsional) |
| `page` | integer | `1` | ≥ 1 | Nomor halaman |
| `page_size` | integer | `20` | 1–100 | Jumlah item per halaman |
| `sort_by` | string | `updated_at` | `updated_at`, `added_at`, `title`, `rating` | Kolom urutan |
| `sort_order` | string | `desc` | `asc`, `desc` | Arah urutan |

#### Response `200 OK`

```json
{
  "items": [
    {
      "manga_id": 1,
      "manga_title": "One Piece",
      "manga_slug": "one-piece",
      "manga_cover": "/static/covers/one-piece.jpg",
      "status": "reading",
      "rating": 9,
      "notes": "Arc Wano keren!",
      "total_chapters": 1100,
      "read_chapters": 85,
      "added_at": "2026-01-01T00:00:00Z",
      "updated_at": "2026-02-24T08:00:00Z"
    }
  ],
  "pagination": {
    "total": 30,
    "page": 1,
    "page_size": 20,
    "total_pages": 2
  }
}
```

> **Catatan:** `read_chapters` adalah jumlah chapter yang sudah ada di reading history user.

#### Contoh Request (cURL)

```bash
# Ambil semua manga yang statusnya "completed", urutkan rating tertinggi
curl "http://localhost:8000/api/v1/lists/?status=completed&sort_by=rating&sort_order=desc" \
  -H "Authorization: Bearer <token>"
```

---

### 3.3 Remove From Reading List

Menghapus manga dari reading list user.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/lists/manga/{manga_slug}` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang ingin dikeluarkan dari list |

#### Response `200 OK`

```json
{
  "success": true,
  "message": "Removed from reading list",
  "manga_slug": "one-piece"
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid |
| `404 Not Found` | Manga tidak ditemukan atau tidak ada di reading list |

#### Contoh Request (cURL)

```bash
curl -X DELETE http://localhost:8000/api/v1/lists/manga/one-piece \
  -H "Authorization: Bearer <token>"
```

---

### 3.4 Get Manga List Status

Mengecek status reading list untuk manga tertentu. Berguna untuk menentukan tampilan tombol status di frontend.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/lists/status/{manga_slug}` |
| **Auth** | 🔒 Required |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_slug` | string | Slug manga yang ingin dicek |

#### Response `200 OK` — ada di list

```json
{
  "manga_slug": "one-piece",
  "in_list": true,
  "status": "reading",
  "rating": 9,
  "notes": "Arc Wano keren!",
  "added_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-02-24T08:00:00Z"
}
```

#### Response `200 OK` — tidak ada di list

```json
{
  "manga_slug": "one-piece",
  "in_list": false,
  "status": null,
  "rating": null,
  "notes": null
}
```

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/api/v1/lists/status/one-piece \
  -H "Authorization: Bearer <token>"
```

---

### 3.5 Get Reading Stats

Mengambil statistik ringkasan aktivitas baca user: jumlah manga per status, total bookmark, dan total manga yang pernah dibaca.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/lists/stats` |
| **Auth** | 🔒 Required |

#### Request

Tidak ada parameter. Cukup kirim token.

#### Response `200 OK`

```json
{
  "reading_list": {
    "plan_to_read": 12,
    "reading": 5,
    "completed": 30,
    "dropped": 3,
    "on_hold": 2
  },
  "total_in_list": 52,
  "total_bookmarks": 18,
  "total_history": 67
}
```

| Field | Keterangan |
|---|---|
| `reading_list` | Jumlah manga per status di reading list |
| `total_in_list` | Total manga di semua kategori reading list |
| `total_bookmarks` | Total manga yang di-bookmark |
| `total_history` | Total manga unik yang pernah dibaca (ada di history) |

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/api/v1/lists/stats \
  -H "Authorization: Bearer <token>"
```

---

## ⚠️ Error Umum

| HTTP Status | Keterangan |
|---|---|
| `401 Unauthorized` | Token JWT tidak ada, expired, atau tidak valid |
| `403 Forbidden` | User tidak punya akses ke resource ini |
| `404 Not Found` | Resource (manga/chapter) tidak ditemukan |
| `422 Unprocessable Entity` | Input tidak valid (format salah, field wajib kosong, dll) |
| `500 Internal Server Error` | Kesalahan server (lihat log untuk detail) |

---

## 🔑 Cara Mendapatkan Token

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "yourusername", "password": "yourpassword"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

Gunakan `access_token` di header semua request yang butuh auth:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```
