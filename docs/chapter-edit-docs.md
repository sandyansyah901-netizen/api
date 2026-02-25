# 📖 Chapter Page Management API

Dokumentasi untuk endpoint admin pengelolaan halaman chapter.

**Base URL:** `/api/v1/admin`  
**Autentikasi:** Semua endpoint memerlukan `Bearer Token` dengan role `admin`.

---

## Daftar Isi

- [1. Tambah Halaman](#1-tambah-halaman)
- [2. Hapus Halaman](#2-hapus-halaman)
- [3. Swap 2 Halaman](#3-swap-2-halaman)
- [4. Bulk Reorder (Drag & Drop)](#4-bulk-reorder-drag--drop)
- [Flow Frontend Drag & Drop](#flow-frontend-drag--drop)
- [Konsep Penting](#konsep-penting)

---

## 1. Tambah Halaman

```
POST /admin/chapter/{chapter_id}/pages/add
Content-Type: multipart/form-data
Authorization: Bearer <admin_token>
```

Menambahkan satu atau lebih gambar ke chapter yang sudah ada.  
File akan di-upload ke folder GDrive chapter yang sudah ada via rclone.

### Parameter

| Nama | Tipe | Wajib | Deskripsi |
|------|------|-------|-----------|
| `chapter_id` | path | ✅ | ID chapter |
| `files` | form-data | ✅ | Satu atau lebih file gambar (JPG/PNG/WEBP) |
| `insert_after` | query | ❌ | Sisipkan setelah page order ke-N. Default: `null` (tambah di akhir) |

**`insert_after` values:**
- `null` / tidak diisi → tambah di akhir chapter
- `0` → jadikan halaman pertama (semua halaman lama geser +N)
- `5` → sisipkan setelah halaman ke-5

### Contoh Request

```bash
# Tambah 2 file di akhir chapter
curl -X POST "http://localhost:8000/api/v1/admin/chapter/42/pages/add" \
  -H "Authorization: Bearer <token>" \
  -F "files=@halaman_baru_1.jpg" \
  -F "files=@halaman_baru_2.jpg"

# Sisipkan 1 file setelah halaman ke-5
curl -X POST "http://localhost:8000/api/v1/admin/chapter/42/pages/add?insert_after=5" \
  -H "Authorization: Bearer <token>" \
  -F "files=@halaman_sisipan.jpg"
```

### Response 200 OK

```json
{
  "success": true,
  "chapter_id": 42,
  "chapter_label": "Chapter 10",
  "pages_added": 2,
  "insert_after": null,
  "total_pages_before": 18,
  "total_pages_after": 20,
  "uploaded_pages": [
    {
      "page_order": 19,
      "gdrive_file_id": "base_folder/one-piece/Chapter_10/019.jpg",
      "original_filename": "halaman_baru_1.jpg"
    },
    {
      "page_order": 20,
      "gdrive_file_id": "base_folder/one-piece/Chapter_10/020.jpg",
      "original_filename": "halaman_baru_2.jpg"
    }
  ],
  "storage_group": 1,
  "message": "Berhasil menambahkan 2 halaman ke 'Chapter 10'"
}
```

### Contoh Penyisipan di Tengah

Chapter punya 10 halaman (order 1-10). Upload 2 file dengan `insert_after=5`:

```
Sebelum:  1, 2, 3, 4, 5, 6, 7, 8, 9, 10
Sesudah:  1, 2, 3, 4, 5, [6_baru], [7_baru], 8, 9, 10, 11, 12
```

---

## 2. Hapus Halaman

```
DELETE /admin/chapter/{chapter_id}/pages/{page_id}
Authorization: Bearer <admin_token>
```

Menghapus satu halaman dari chapter. Secara default juga menghapus
file fisik dari Google Drive via rclone.

### Parameter

| Nama | Tipe | Default | Deskripsi |
|------|------|---------|-----------|
| `chapter_id` | path | - | ID chapter |
| `page_id` | path | - | ID page yang akan dihapus |
| `delete_from_gdrive` | query | `true` | `true` = hapus file fisik dari GDrive via rclone |
| `renumber` | query | `true` | `true` = renumber page_order agar berurutan setelah delete |

### Contoh Request

```bash
# Hapus page ID 158 dari chapter ID 42 (hapus GDrive + renumber)
curl -X DELETE "http://localhost:8000/api/v1/admin/chapter/42/pages/158" \
  -H "Authorization: Bearer <token>"

# Hapus dari DB saja (file GDrive tidak disentuh)
curl -X DELETE "http://localhost:8000/api/v1/admin/chapter/42/pages/158?delete_from_gdrive=false" \
  -H "Authorization: Bearer <token>"
```

### Response 200 OK

```json
{
  "success": true,
  "chapter_id": 42,
  "chapter_label": "Chapter 10",
  "deleted_page_id": 158,
  "deleted_page_order": 7,
  "deleted_gdrive_path": "base_folder/one-piece/Chapter_10/007.jpg",
  "gdrive_file_deleted": true,
  "renumbered": true,
  "total_pages_before": 18,
  "total_pages_after": 17,
  "message": "Halaman order 7 berhasil dihapus dari 'Chapter 10'"
}
```

### Efek `renumber=true`

```
Sebelum delete (page order 7):  1, 2, 3, 4, 5, 6, [7], 8, 9, 10
Sesudah delete + renumber:      1, 2, 3, 4, 5, 6, 7, 8, 9
(halaman lama 8-10 geser jadi 7-9)
```

### Error Cases

| Kode | Kondisi |
|------|---------|
| `400` | Chapter hanya punya 1 halaman (tidak bisa dihapus) |
| `404` | Chapter tidak ditemukan |
| `404` | Page tidak ditemukan di chapter |

---

## 3. Swap 2 Halaman

```
POST /admin/chapter/{chapter_id}/pages/swap
Content-Type: application/json
Authorization: Bearer <admin_token>
```

Menukar posisi 2 halaman. Hanya update `page_order` di database —
**tidak ada perubahan file di Google Drive**.

### Body

```json
{
  "page_id_1": 150,
  "page_id_2": 158
}
```

### Contoh Request

```bash
curl -X POST "http://localhost:8000/api/v1/admin/chapter/42/pages/swap" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"page_id_1": 150, "page_id_2": 158}'
```

### Response 200 OK

```json
{
  "success": true,
  "chapter_id": 42,
  "chapter_label": "Chapter 10",
  "swapped": [
    {
      "page_id": 150,
      "gdrive_file_id": "base_folder/one-piece/Chapter_10/002.jpg",
      "old_order": 2,
      "new_order": 8
    },
    {
      "page_id": 158,
      "gdrive_file_id": "base_folder/one-piece/Chapter_10/008.jpg",
      "old_order": 8,
      "new_order": 2
    }
  ],
  "message": "Berhasil swap halaman order 2 <> 8"
}
```

---

## 4. Bulk Reorder (Drag & Drop)

```
PUT /admin/chapter/{chapter_id}/pages/reorder
Content-Type: application/json
Authorization: Bearer <admin_token>
```

Mengatur ulang urutan **semua** halaman sekaligus. Digunakan oleh frontend
setelah user selesai drag & drop. Tidak ada perubahan file di Google Drive.

### Validasi

- Jumlah item dalam `page_orders` harus **sama persis** dengan total halaman chapter
- `new_order` harus berurutan dari **1 sampai N** tanpa duplikat
- Semua `page_id` harus milik chapter ini

### Body

```json
{
  "page_orders": [
    { "page_id": 155, "new_order": 1 },
    { "page_id": 150, "new_order": 2 },
    { "page_id": 158, "new_order": 3 },
    { "page_id": 152, "new_order": 4 }
  ]
}
```

### Contoh Request

```bash
curl -X PUT "http://localhost:8000/api/v1/admin/chapter/42/pages/reorder" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "page_orders": [
      { "page_id": 155, "new_order": 1 },
      { "page_id": 150, "new_order": 2 },
      { "page_id": 158, "new_order": 3 }
    ]
  }'
```

### Response 200 OK

```json
{
  "success": true,
  "chapter_id": 42,
  "chapter_label": "Chapter 10",
  "total_pages_reordered": 3,
  "pages": [
    { "page_id": 155, "gdrive_file_id": "base_folder/one-piece/Chapter_10/005.jpg", "new_order": 1 },
    { "page_id": 150, "gdrive_file_id": "base_folder/one-piece/Chapter_10/002.jpg", "new_order": 2 },
    { "page_id": 158, "gdrive_file_id": "base_folder/one-piece/Chapter_10/008.jpg", "new_order": 3 }
  ],
  "message": "Berhasil reorder 3 halaman di 'Chapter 10'"
}
```

---

## Flow Frontend Drag & Drop

```
1. Load chapter detail
   GET /api/v1/chapter/{chapter_slug}
   → Dapat list pages[] dengan page_id dan page_order

2. Tampilkan grid gambar (sortable/draggable)

3. User drag-drop halaman ke posisi baru
   → UI update urutan secara lokal (belum ke server)

4. User klik tombol "Simpan Urutan"
   → Kirim seluruh urutan baru ke:
   PUT /api/v1/admin/chapter/{chapter_id}/pages/reorder
   Body: { "page_orders": [ {page_id, new_order}, ... ] }

5. Server update page_order di database
   → Response: daftar pages dengan new_order
```

### Contoh Implementasi Frontend (JavaScript)

```javascript
// Setelah drag & drop selesai
async function savePageOrder(chapterId, pages) {
  const pageOrders = pages.map((page, index) => ({
    page_id: page.id,
    new_order: index + 1  // 1-based
  }));

  const response = await fetch(`/api/v1/admin/chapter/${chapterId}/pages/reorder`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${adminToken}`
    },
    body: JSON.stringify({ page_orders: pageOrders })
  });

  const result = await response.json();
  if (result.success) {
    console.log(`Reordered ${result.total_pages_reordered} pages`);
  }
}
```

---

## Konsep Penting

### File di GDrive vs Urutan di Database

```
File fisik di GDrive (tidak berubah saat swap/reorder):
  Chapter_10/001.jpg   ← nama file tidak berubah
  Chapter_10/002.jpg
  Chapter_10/008.jpg

Database (page_order yang diubah):
  page_id=150, gdrive=001.jpg, page_order=3   ← urutan berubah
  page_id=155, gdrive=002.jpg, page_order=1
  page_id=158, gdrive=008.jpg, page_order=2
```

> **Swap dan Reorder** hanya mengubah `page_order` di database.  
> File di GDrive **tidak disentuh sama sekali**.

### Kapan rclone digunakan?

| Operasi | rclone? | Keterangan |
|---------|---------|------------|
| Swap 2 halaman | ❌ | Hanya update DB |
| Bulk reorder | ❌ | Hanya update DB |
| **Tambah halaman** | ✅ | Upload file baru ke GDrive |
| **Hapus halaman** | ✅ (default) | `rclone deletefile` untuk hapus fisik |

### Group 2 Path Prefix

Jika sistem menggunakan storage group 2, path di database akan punya prefix `@`:

```
Group 1: base_folder/manga/Chapter_01/001.jpg
Group 2: @base_folder/manga/Chapter_01/001.jpg  ← ada @
```

Prefix `@` otomatis ditambahkan/dihapus saat upload/delete via rclone.
