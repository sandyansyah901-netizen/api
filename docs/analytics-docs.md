# API Dokumentasi — Analytics & System

> **Base URL:** `http://localhost:8000`  
> **Auth:** Semua endpoint Analytics bertanda 🔒 wajib menyertakan header:
> ```
> Authorization: Bearer <access_token>
> ```
> Token didapat dari `POST /api/v1/auth/login`.  
> **Role:** Semua endpoint Analytics membutuhkan role `admin`.

---

## 📚 Daftar Isi

1. [Analytics](#1-analytics)
   - [1.1 Get Analytics Overview](#11-get-analytics-overview)
   - [1.2 Get Manga Views](#12-get-manga-views)
   - [1.3 Get User Growth](#13-get-user-growth)
   - [1.4 Get Popular Genres](#14-get-popular-genres)
   - [1.5 Get Top Manga](#15-get-top-manga)
   - [1.6 Get Recent Activity](#16-get-recent-activity)
   - [1.7 Delete Manga Views by Period](#17-delete-manga-views-by-period)
   - [1.8 Delete Manga Views by Manga](#18-delete-manga-views-by-manga)
   - [1.9 Delete All Manga Views](#19-delete-all-manga-views)
   - [1.10 Delete Chapter Views by Period](#110-delete-chapter-views-by-period)
   - [1.11 Delete Chapter Views by Chapter](#111-delete-chapter-views-by-chapter)
   - [1.12 Delete All Chapter Views](#112-delete-all-chapter-views)
2. [System](#2-system)
   - [2.1 Root](#21-root)
   - [2.2 Health Check](#22-health-check)
   - [2.3 List Routes](#23-list-routes)
   - [2.4 List Features](#24-list-features)

---

## 1. Analytics

Kumpulan endpoint untuk dashboard admin: statistik platform, tren user, dan aktivitas terkini.

---

### 1.1 Get Analytics Overview

Mengembalikan ringkasan lengkap statistik platform: total user, manga, chapter, views, genres terpopuler, dan tren pertumbuhan user.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/admin/analytics/overview` |
| **Auth** | 🔒 Required (Admin) |

#### Request

Tidak ada parameter. Cukup kirim token admin.

#### Response `200 OK`

```json
{
  "database": {
    "total_users": 1250,
    "active_users_today": 87,
    "active_users_week": 430,
    "total_manga": 320,
    "manga_ongoing": 210,
    "manga_completed": 110,
    "total_chapters": 8500
  },
  "views": {
    "total_manga_views": 152000,
    "total_chapter_views": 98000,
    "views_today": 1200,
    "views_week": 8700,
    "views_month": 34000
  },
  "engagement": {
    "total_bookmarks": 5400,
    "total_reading_lists": 3200
  },
  "popular_genres": [
    { "name": "Action", "slug": "action", "manga_count": 85 },
    { "name": "Romance", "slug": "romance", "manga_count": 62 }
  ],
  "user_growth": {
    "labels": ["2026-01-25", "2026-01-26", "2026-01-27"],
    "data": [12, 8, 15]
  },
  "timestamp": "2026-02-25T04:41:00+00:00"
}
```

| Field | Keterangan |
|---|---|
| `database` | Ringkasan total data di database |
| `database.active_users_today` | User yang login hari ini |
| `database.active_users_week` | User yang login dalam 7 hari terakhir |
| `views` | Statistik views manga (bukan chapter) |
| `engagement` | Total bookmark dan reading list |
| `popular_genres` | Top 10 genre berdasarkan jumlah manga |
| `user_growth` | Data registrasi user harian (30 hari terakhir) |
| `timestamp` | Waktu data diambil (UTC) |

#### Error Responses

| Status | Kondisi |
|---|---|
| `401 Unauthorized` | Token tidak valid / tidak ada |
| `403 Forbidden` | User bukan admin |

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/api/v1/admin/analytics/overview \
  -H "Authorization: Bearer <token>"
```

---

### 1.2 Get Manga Views

Mengembalikan statistik views per manga dengan breakdown harian, mingguan, bulanan, dan total unique viewers. Mendukung filter periode dan sorting.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/admin/analytics/manga-views` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `page` | integer | `1` | ≥ 1 | Nomor halaman |
| `page_size` | integer | `20` | 1–100 | Jumlah item per halaman |
| `period` | string | `month` | `today`, `week`, `month`, `year`, `all` | Filter periode untuk `total_views` |
| `sort_by` | string | `total_views` | `total_views`, `title` | Kolom urutan |

#### Response `200 OK`

```json
{
  "items": [
    {
      "manga_id": 1,
      "manga_title": "One Piece",
      "manga_slug": "one-piece",
      "total_views": 4500,
      "views_today": 120,
      "views_week": 850,
      "views_month": 4500,
      "unique_viewers": 980
    }
  ],
  "pagination": {
    "total": 320,
    "page": 1,
    "page_size": 20,
    "total_pages": 16
  },
  "period": "month"
}
```

| Field | Keterangan |
|---|---|
| `total_views` | Views sesuai filter `period` yang dipilih |
| `views_today` | Views hari ini (selalu dihitung, tidak dipengaruhi `period`) |
| `views_week` | Views 7 hari terakhir |
| `views_month` | Views 30 hari terakhir |
| `unique_viewers` | Jumlah user unik yang melihat manga ini |

#### Contoh Request (cURL)

```bash
# Top manga views minggu ini
curl "http://localhost:8000/api/v1/admin/analytics/manga-views?period=week&sort_by=total_views" \
  -H "Authorization: Bearer <token>"
```

---

### 1.3 Get User Growth

Mengembalikan data tren registrasi user harian beserta nilai kumulatif, untuk rentang waktu yang dapat dikonfigurasi.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/admin/analytics/user-growth` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `days` | integer | `30` | 1–365 | Jumlah hari ke belakang |

#### Response `200 OK`

```json
{
  "period_days": 30,
  "total_new_users": 187,
  "data": [
    {
      "date": "2026-01-26",
      "new_users": 8,
      "total_users": 8
    },
    {
      "date": "2026-01-27",
      "new_users": 15,
      "total_users": 23
    }
  ]
}
```

| Field | Keterangan |
|---|---|
| `period_days` | Rentang waktu yang diminta |
| `total_new_users` | Total registrasi baru dalam periode |
| `data[].new_users` | Registrasi baru pada tanggal tersebut |
| `data[].total_users` | Total kumulatif sejak awal periode |

> **Catatan:** `total_users` adalah akumulasi dalam periode yang dipilih saja, bukan total seluruh user di database.

#### Contoh Request (cURL)

```bash
# Tren 7 hari terakhir
curl "http://localhost:8000/api/v1/admin/analytics/user-growth?days=7" \
  -H "Authorization: Bearer <token>"
```

---

### 1.4 Get Popular Genres

Mengembalikan daftar genre terpopuler berdasarkan jumlah manga, total views, dan total bookmarks.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/admin/analytics/popular-genres` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `limit` | integer | `10` | 1–50 | Jumlah genre yang dikembalikan |

#### Response `200 OK`

```json
{
  "genres": [
    {
      "id": 1,
      "name": "Action",
      "slug": "action",
      "manga_count": 85,
      "total_views": 42000,
      "bookmarks": 1200
    },
    {
      "id": 2,
      "name": "Romance",
      "slug": "romance",
      "manga_count": 62,
      "total_views": 38000,
      "bookmarks": 980
    }
  ],
  "total_genres": 25
}
```

| Field | Keterangan |
|---|---|
| `manga_count` | Jumlah manga dengan genre ini |
| `total_views` | Total views semua manga dalam genre ini |
| `bookmarks` | Total bookmark dari manga dalam genre ini |
| `total_genres` | Total genre yang ada di database |

#### Contoh Request (cURL)

```bash
# Top 5 genre
curl "http://localhost:8000/api/v1/admin/analytics/popular-genres?limit=5" \
  -H "Authorization: Bearer <token>"
```

---

### 1.5 Get Top Manga

Mengembalikan ranking manga teratas berdasarkan metrik yang dipilih: views, bookmarks, atau reading lists, dalam periode waktu tertentu.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/admin/analytics/top-manga` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `metric` | string | `views` | `views`, `bookmarks`, `reading_lists` | Metrik untuk ranking |
| `period` | string | `month` | `today`, `week`, `month`, `all` | Filter periode (hanya berlaku untuk `metric=views`) |
| `limit` | integer | `10` | 1–50 | Jumlah manga dalam ranking |

#### Response `200 OK` — `metric=views`

```json
{
  "metric": "views",
  "period": "month",
  "items": [
    {
      "rank": 1,
      "manga_id": 5,
      "manga_title": "Naruto",
      "manga_slug": "naruto",
      "views": 8200
    },
    {
      "rank": 2,
      "manga_id": 1,
      "manga_title": "One Piece",
      "manga_slug": "one-piece",
      "views": 7100
    }
  ]
}
```

#### Response `200 OK` — `metric=bookmarks`

```json
{
  "metric": "bookmarks",
  "period": "month",
  "items": [
    {
      "rank": 1,
      "manga_id": 3,
      "manga_title": "Attack on Titan",
      "manga_slug": "attack-on-titan",
      "bookmarks": 560
    }
  ]
}
```

#### Response `200 OK` — `metric=reading_lists`

```json
{
  "metric": "reading_lists",
  "period": "month",
  "items": [
    {
      "rank": 1,
      "manga_id": 7,
      "manga_title": "Demon Slayer",
      "manga_slug": "demon-slayer",
      "in_reading_lists": 430
    }
  ]
}
```

> **Catatan:** Field metrik pada tiap item berbeda tergantung `metric` yang dipilih: `views`, `bookmarks`, atau `in_reading_lists`.

#### Contoh Request (cURL)

```bash
# Top 5 manga paling banyak di-bookmark
curl "http://localhost:8000/api/v1/admin/analytics/top-manga?metric=bookmarks&limit=5" \
  -H "Authorization: Bearer <token>"

# Top 10 manga views minggu ini
curl "http://localhost:8000/api/v1/admin/analytics/top-manga?metric=views&period=week" \
  -H "Authorization: Bearer <token>"
```

---

### 1.6 Get Recent Activity

Mengembalikan log aktivitas terkini dari user: views manga dan penambahan bookmark, digabungkan dan diurutkan berdasarkan waktu terbaru.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/api/v1/admin/analytics/recent-activity` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `limit` | integer | `50` | 1–200 | Jumlah aktivitas yang dikembalikan |

#### Response `200 OK`

```json
{
  "recent_activity": [
    {
      "type": "view",
      "username": "sandy123",
      "manga_title": "One Piece",
      "timestamp": "2026-02-25T04:35:00+00:00"
    },
    {
      "type": "bookmark",
      "username": "reader99",
      "manga_title": "Naruto",
      "timestamp": "2026-02-25T04:30:00+00:00"
    },
    {
      "type": "view",
      "username": "Anonymous",
      "manga_title": "Attack on Titan",
      "timestamp": "2026-02-25T04:28:00+00:00"
    }
  ]
}
```

| Field | Keterangan |
|---|---|
| `type` | Jenis aktivitas: `view` (lihat manga) atau `bookmark` (tambah bookmark) |
| `username` | Username user; `"Anonymous"` jika user tidak login |
| `manga_title` | Judul manga yang terlibat |
| `timestamp` | Waktu aktivitas terjadi (UTC) |

> **Catatan:** Response menggabungkan max `limit` views terbaru + max 20 bookmark terbaru, lalu diurutkan bersama berdasarkan timestamp.

#### Contoh Request (cURL)

```bash
curl "http://localhost:8000/api/v1/admin/analytics/recent-activity?limit=20" \
  -H "Authorization: Bearer <token>"
```

---

## 🗑️ Views Cleanup (Pruning)

Endpoint untuk menghapus data views yang sudah menumpuk. Berguna untuk menjaga ukuran tabel `manga_views` dan `chapter_views` agar tidak terlalu besar.

> ⚠️ **Semua operasi delete tidak bisa dibatalkan.** Pastikan sudah yakin sebelum menjalankan.

---

### 1.7 Delete Manga Views by Period

Menghapus manga views yang lebih tua dari N hari. **Paling aman untuk maintenance rutin.**

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/admin/analytics/manga-views` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `older_than_days` | integer | `30` | 1–3650 | Hapus views yang berumur lebih dari N hari |

#### Response `200 OK`

```json
{
  "success": true,
  "deleted_count": 12450,
  "message": "Deleted 12450 manga views older than 30 days",
  "cutoff_date": "2026-01-26T05:01:56+00:00"
}
```

#### Contoh Request (cURL)

```bash
# Hapus views lebih tua dari 90 hari
curl -X DELETE "http://localhost:8000/api/v1/admin/analytics/manga-views?older_than_days=90" \
  -H "Authorization: Bearer <token>"
```

---

### 1.8 Delete Manga Views by Manga

Menghapus **semua** views untuk satu manga tertentu. Berguna untuk reset view count atau menghapus spam views pada manga spesifik.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/admin/analytics/manga-views/manga/{manga_id}` |
| **Auth** | 🔒 Required (Admin) |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `manga_id` | integer | ID manga yang views-nya ingin dihapus |

#### Response `200 OK`

```json
{
  "success": true,
  "deleted_count": 4500,
  "manga_id": 1,
  "manga_title": "One Piece",
  "manga_slug": "one-piece",
  "message": "Deleted 4500 views for manga 'One Piece'"
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `404 Not Found` | Manga ID tidak ditemukan |

#### Contoh Request (cURL)

```bash
curl -X DELETE http://localhost:8000/api/v1/admin/analytics/manga-views/manga/1 \
  -H "Authorization: Bearer <token>"
```

---

### 1.9 Delete All Manga Views

Menghapus **seluruh** data tabel `manga_views`. Wajib menyertakan `?confirm=true` sebagai safeguard.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/admin/analytics/manga-views/all` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Keterangan |
|---|---|---|---|
| `confirm` | boolean | `false` | **Wajib `true`** untuk mengeksekusi. Tanpa ini, request akan ditolak |

#### Response `200 OK`

```json
{
  "success": true,
  "deleted_count": 152000,
  "message": "Deleted ALL 152000 manga views from database"
}
```

#### Response `400 Bad Request` (tanpa confirm)

```json
{
  "detail": "Tambahkan query param '?confirm=true' untuk konfirmasi. Aksi ini akan menghapus SEMUA data manga views dan tidak bisa dibatalkan."
}
```

#### Contoh Request (cURL)

```bash
# Tanpa confirm → 400 Bad Request
curl -X DELETE http://localhost:8000/api/v1/admin/analytics/manga-views/all \
  -H "Authorization: Bearer <token>"

# Dengan confirm=true → eksekusi
curl -X DELETE "http://localhost:8000/api/v1/admin/analytics/manga-views/all?confirm=true" \
  -H "Authorization: Bearer <token>"
```

---

### 1.10 Delete Chapter Views by Period

Menghapus chapter views yang lebih tua dari N hari. **Paling aman untuk maintenance rutin.**

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/admin/analytics/chapter-views` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Nilai Valid | Keterangan |
|---|---|---|---|---|
| `older_than_days` | integer | `30` | 1–3650 | Hapus views yang berumur lebih dari N hari |

#### Response `200 OK`

```json
{
  "success": true,
  "deleted_count": 8700,
  "message": "Deleted 8700 chapter views older than 30 days",
  "cutoff_date": "2026-01-26T05:01:56+00:00"
}
```

#### Contoh Request (cURL)

```bash
curl -X DELETE "http://localhost:8000/api/v1/admin/analytics/chapter-views?older_than_days=60" \
  -H "Authorization: Bearer <token>"
```

---

### 1.11 Delete Chapter Views by Chapter

Menghapus **semua** views untuk satu chapter tertentu.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/admin/analytics/chapter-views/chapter/{chapter_id}` |
| **Auth** | 🔒 Required (Admin) |

#### Path Parameter

| Parameter | Type | Keterangan |
|---|---|---|
| `chapter_id` | integer | ID chapter yang views-nya ingin dihapus |

#### Response `200 OK`

```json
{
  "success": true,
  "deleted_count": 320,
  "chapter_id": 10,
  "chapter_label": "Chapter 5",
  "chapter_slug": "one-piece-chapter-5",
  "manga_id": 1,
  "message": "Deleted 320 views for chapter 'Chapter 5'"
}
```

#### Error Responses

| Status | Kondisi |
|---|---|
| `404 Not Found` | Chapter ID tidak ditemukan |

#### Contoh Request (cURL)

```bash
curl -X DELETE http://localhost:8000/api/v1/admin/analytics/chapter-views/chapter/10 \
  -H "Authorization: Bearer <token>"
```

---

### 1.12 Delete All Chapter Views

Menghapus **seluruh** data tabel `chapter_views`. Wajib menyertakan `?confirm=true` sebagai safeguard.

| | |
|---|---|
| **Method** | `DELETE` |
| **URL** | `/api/v1/admin/analytics/chapter-views/all` |
| **Auth** | 🔒 Required (Admin) |

#### Query Parameters

| Parameter | Type | Default | Keterangan |
|---|---|---|---|
| `confirm` | boolean | `false` | **Wajib `true`** untuk mengeksekusi |

#### Response `200 OK`

```json
{
  "success": true,
  "deleted_count": 98000,
  "message": "Deleted ALL 98000 chapter views from database"
}
```

#### Contoh Request (cURL)

```bash
curl -X DELETE "http://localhost:8000/api/v1/admin/analytics/chapter-views/all?confirm=true" \
  -H "Authorization: Bearer <token>"
```

---

## 2. System

Endpoint utilitas untuk memonitor status server dan eksplorasi API. Tidak membutuhkan autentikasi.

---

### 2.1 Root

Mengembalikan informasi dasar aplikasi: nama, versi, environment, status, daftar fitur, dan peta endpoint.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/` |
| **Auth** | ❌ Public |

#### Response `200 OK`

```json
{
  "name": "Manga Reader API",
  "version": "1.0.0",
  "environment": "development",
  "status": "running",
  "features": [
    "Manga Management",
    "Reading History",
    "Bookmarks & Favorites",
    "Reading Lists",
    "Analytics Dashboard",
    "View Tracking"
  ],
  "endpoints": {
    "docs": "/docs",
    "health": "/health",
    "auth": "/api/v1/auth",
    "manga": "/api/v1/manga",
    "chapter": "/api/v1/chapter",
    "reading": "/api/v1/reading",
    "bookmarks": "/api/v1/bookmarks",
    "lists": "/api/v1/lists",
    "upload": "/api/v1/upload",
    "admin": "/api/v1/admin",
    "analytics": "/api/v1/admin/analytics",
    "image_proxy": "/api/v1/image-proxy",
    "static_covers": "/static/covers",
    "covers_fallback": "/covers"
  }
}
```

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/
```

---

### 2.2 Health Check

Mengecek kesehatan server secara keseluruhan, termasuk koneksi database dan status service lainnya.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/health` |
| **Auth** | ❌ Public |

#### Response `200 OK` — Server sehat

```json
{
  "status": "healthy",
  "timestamp": 1740455260.123,
  "version": "1.0.0",
  "environment": "development",
  "checks": {
    "database": "healthy"
  }
}
```

#### Response `200 OK` — Server degraded (sebagian service bermasalah)

```json
{
  "status": "degraded",
  "timestamp": 1740455260.123,
  "version": "1.0.0",
  "environment": "production",
  "checks": {
    "database": "unhealthy"
  }
}
```

| Field | Keterangan |
|---|---|
| `status` | `healthy` (semua OK) atau `degraded` (ada yang bermasalah) |
| `timestamp` | Unix timestamp waktu check dilakukan |
| `checks.database` | Status koneksi database: `healthy` atau `unhealthy` |

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/health
```

---

### 2.3 List Routes

Menampilkan semua route yang terdaftar di aplikasi. Berguna untuk eksplorasi API secara programatik.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/routes` |
| **Auth** | ❌ Public |

#### Response `200 OK`

```json
{
  "routes": [
    {
      "path": "/api/v1/auth/login",
      "methods": ["POST"],
      "name": "login"
    },
    {
      "path": "/api/v1/manga/",
      "methods": ["GET"],
      "name": "list_manga"
    }
  ],
  "total": 42
}
```

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/routes
```

---

### 2.4 List Features

Mengembalikan daftar fitur yang aktif di aplikasi saat ini.

| | |
|---|---|
| **Method** | `GET` |
| **URL** | `/features` |
| **Auth** | ❌ Public |

#### Response `200 OK`

```json
{
  "features": [
    "Manga Management",
    "Smart Caching",
    "Image Proxy",
    "Cover Images (Local + GDrive Backup)",
    "Upload to Google Drive",
    "Bulk Upload (ZIP + JSON)",
    "Admin CRUD",
    "Reading History",
    "Bookmarks & Favorites",
    "Reading Lists",
    "Analytics Dashboard",
    "View Tracking"
  ],
  "total": 12
}
```

#### Contoh Request (cURL)

```bash
curl http://localhost:8000/features
```

---

## ⚠️ Error Umum

| HTTP Status | Keterangan |
|---|---|
| `400 Bad Request` | `confirm=true` tidak disertakan pada endpoint delete all |
| `401 Unauthorized` | Token JWT tidak ada, expired, atau tidak valid |
| `403 Forbidden` | User tidak punya role `admin` |
| `404 Not Found` | Manga ID / Chapter ID tidak ditemukan |
| `422 Unprocessable Entity` | Query parameter tidak valid (nilai di luar range, tipe salah, dll) |
| `500 Internal Server Error` | Kesalahan server (lihat log untuk detail) |

---

## 🔑 Cara Mendapatkan Token Admin

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "adminuser", "password": "adminpassword"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

Gunakan `access_token` di header semua request analytics:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

> **Catatan:** Pastikan akun yang digunakan memiliki role `admin`. Login dengan akun biasa akan menghasilkan `403 Forbidden` pada semua endpoint analytics.
