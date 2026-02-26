# File: app/utils/slug_utils.py
"""
Slug Normalization Utility
==========================
Shared utility untuk normalize slug agar SEO-friendly.

Rules:
- Replace underscore (_) dan spasi dengan hyphen (-)
- Lowercase
- Remove karakter spesial (hanya keep a-z, 0-9, -)
- Remove multiple dashes (--- → -)
- Strip leading/trailing dashes
"""

import re


def normalize_slug(value: str) -> str:
    """
    Normalize slug: underscore/spasi → hyphen, lowercase, clean.

    Contoh:
    - "crimson_reset"     → "crimson-reset"
    - "One Piece"         → "one-piece"
    - "solo  leveling"    → "solo-leveling"
    - "Naruto_Shippuden"  → "naruto-shippuden"
    - "action"            → "action" (tidak berubah)
    """
    if not value:
        return value

    # Replace underscore dan spasi dengan dash
    slug = value.replace("_", "-").replace(" ", "-")

    # Lowercase
    slug = slug.lower()

    # Remove special characters (keep alphanumeric dan dash)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)

    # Remove multiple dashes
    slug = re.sub(r'-+', '-', slug)

    # Remove leading/trailing dashes
    slug = slug.strip('-')

    return slug
