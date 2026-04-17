"""Database initialization."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.models import Base
from app.db_migrations import run_schema_migrations

# Create database engine with optimized pool settings
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=5,  # Keep 5 connections in pool
    max_overflow=10,  # Allow up to 10 additional connections
    pool_recycle=3600,  # Recycle connections after 1 hour
    pool_pre_ping=True,  # Verify connection before use
    connect_args=(
        {"check_same_thread": False, "timeout": 30}
        if "sqlite" in settings.DATABASE_URL
        else {}
    ),
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database tables and apply tracked schema migrations."""
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)


def get_db():
    """Dependency for getting database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session():
    """Get database session for background tasks (Celery workers)"""
    return SessionLocal()


# ============= TEST DATABASE HELPERS =============


def create_test_database(test_engine):
    """Create test database tables"""
    Base.metadata.create_all(bind=test_engine)


def cleanup_test_database(test_engine):
    """Drop all test database tables"""
    Base.metadata.drop_all(bind=test_engine)
