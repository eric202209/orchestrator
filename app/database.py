"""Database initialization."""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from app.config import settings
from app.models import Base
from app.db_migrations import run_schema_migrations

is_sqlite = "sqlite" in settings.DATABASE_URL
engine_kwargs = {
    "pool_pre_ping": True,
}
if is_sqlite:
    engine_kwargs.update(
        {
            "poolclass": NullPool,
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }
    )
else:
    engine_kwargs.update(
        {
            "pool_size": 5,
            "max_overflow": 10,
            "pool_recycle": 3600,
        }
    )

# Create database engine with backend-appropriate pool settings.
engine = create_engine(settings.DATABASE_URL, **engine_kwargs)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database tables and apply tracked schema migrations."""
    if is_sqlite:
        database_path = settings.DATABASE_URL.removeprefix("sqlite:///")
        if database_path and database_path != settings.DATABASE_URL:
            from pathlib import Path

            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    if is_sqlite:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
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
