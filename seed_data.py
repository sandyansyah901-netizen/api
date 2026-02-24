"""
Seed Data Script
================
Script untuk populate initial data:
- Roles (admin, user)
- Admin user (username: admin, password: admin123)
- Storage sources
- Manga types
- Genres
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session
from app.core.base import SessionLocal, get_password_hash
from app.models.models import (
    Role, User, StorageSource, MangaType, Genre
)

def seed_roles(db: Session):
    """Create default roles."""
    print("🔐 Seeding roles...")
    
    roles_data = [
        {"name": "admin"},
        {"name": "user"},
        {"name": "uploader"}
    ]
    
    for role_data in roles_data:
        existing = db.query(Role).filter(Role.name == role_data["name"]).first()
        if not existing:
            role = Role(**role_data)
            db.add(role)
            print(f"  ✅ Created role: {role_data['name']}")
        else:
            print(f"  ⏭️  Role already exists: {role_data['name']}")
    
    db.commit()


def seed_admin_user(db: Session):
    """Create default admin user."""
    print("\n👤 Seeding admin user...")
    
    # Check if admin exists
    existing = db.query(User).filter(User.username == "admin").first()
    if existing:
        print("  ⏭️  Admin user already exists")
        return
    
    # Get admin role
    admin_role = db.query(Role).filter(Role.name == "admin").first()
    if not admin_role:
        print("  ❌ Admin role not found! Run seed_roles first.")
        return
    
    # Create admin user
    admin = User(
        username="admin",
        email="admin@mangareader.local",
        password_hash=get_password_hash("admin123"),
        is_active=True
    )
    admin.roles = [admin_role]
    
    db.add(admin)
    db.commit()
    
    print("  ✅ Created admin user:")
    print(f"     Username: admin")
    print(f"     Password: admin123")
    print(f"     Email: admin@mangareader.local")


def seed_storage_sources(db: Session):
    """Create default storage sources."""
    print("\n📦 Seeding storage sources...")
    
    storages = [
        {
            "source_name": "Google Drive - Main",
            "base_folder_id": "manga_library",
            "status": "active"
        }
    ]
    
    for storage_data in storages:
        existing = db.query(StorageSource).filter(
            StorageSource.source_name == storage_data["source_name"]
        ).first()
        
        if not existing:
            storage = StorageSource(**storage_data)
            db.add(storage)
            print(f"  ✅ Created storage: {storage_data['source_name']}")
        else:
            print(f"  ⏭️  Storage already exists: {storage_data['source_name']}")
    
    db.commit()


def seed_manga_types(db: Session):
    """Create default manga types."""
    print("\n📚 Seeding manga types...")
    
    types_data = [
        {"name": "Manga", "slug": "manga"},
        {"name": "Manhwa", "slug": "manhwa"},
        {"name": "Manhua", "slug": "manhua"},
        {"name": "Novel", "slug": "novel"},
        {"name": "Doujinshi", "slug": "doujinshi"},
        {"name": "One-shot", "slug": "one-shot"}
    ]
    
    for type_data in types_data:
        existing = db.query(MangaType).filter(
            MangaType.slug == type_data["slug"]
        ).first()
        
        if not existing:
            manga_type = MangaType(**type_data)
            db.add(manga_type)
            print(f"  ✅ Created type: {type_data['name']}")
        else:
            print(f"  ⏭️  Type already exists: {type_data['name']}")
    
    db.commit()


def seed_genres(db: Session):
    """Create default genres."""
    print("\n🎭 Seeding genres...")
    
    genres_data = [
        {"name": "Action", "slug": "action"},
        {"name": "Adventure", "slug": "adventure"},
        {"name": "Comedy", "slug": "comedy"},
        {"name": "Drama", "slug": "drama"},
        {"name": "Fantasy", "slug": "fantasy"},
        {"name": "Horror", "slug": "horror"},
        {"name": "Mystery", "slug": "mystery"},
        {"name": "Romance", "slug": "romance"},
        {"name": "Sci-Fi", "slug": "sci-fi"},
        {"name": "Slice of Life", "slug": "slice-of-life"},
        {"name": "Sports", "slug": "sports"},
        {"name": "Supernatural", "slug": "supernatural"},
        {"name": "Thriller", "slug": "thriller"},
        {"name": "Psychological", "slug": "psychological"},
        {"name": "Historical", "slug": "historical"},
        {"name": "School Life", "slug": "school-life"},
        {"name": "Martial Arts", "slug": "martial-arts"},
        {"name": "Isekai", "slug": "isekai"},
        {"name": "Harem", "slug": "harem"},
        {"name": "Ecchi", "slug": "ecchi"}
    ]
    
    for genre_data in genres_data:
        existing = db.query(Genre).filter(
            Genre.slug == genre_data["slug"]
        ).first()
        
        if not existing:
            genre = Genre(**genre_data)
            db.add(genre)
            print(f"  ✅ Created genre: {genre_data['name']}")
        else:
            print(f"  ⏭️  Genre already exists: {genre_data['name']}")
    
    db.commit()


def main():
    """Main seed function."""
    print("=" * 60)
    print("🌱 SEEDING DATABASE")
    print("=" * 60)
    
    db = SessionLocal()
    
    try:
        # Seed in order (roles first, then users yang depend on roles)
        seed_roles(db)
        seed_admin_user(db)
        seed_storage_sources(db)
        seed_manga_types(db)
        seed_genres(db)
        
        print("\n" + "=" * 60)
        print("✅ DATABASE SEEDING COMPLETED!")
        print("=" * 60)
        print("\n📝 LOGIN CREDENTIALS:")
        print("   URL: http://localhost:8000/docs")
        print("   Username: admin")
        print("   Password: admin123")
        print("\n🔗 TEST LOGIN:")
        print("   POST /api/v1/auth/login")
        print('   Body: {"username": "admin", "password": "admin123"}')
        print("\n")
        
    except Exception as e:
        print(f"\n❌ Error seeding database: {str(e)}")
        db.rollback()
        raise
    
    finally:
        db.close()


if __name__ == "__main__":
    main()