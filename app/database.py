"""Database initialization."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.models import Base
from app.db_migrations import run_schema_migrations

_IS_SQLITE = "sqlite" in settings.DATABASE_URL

# Create database engine with optimized pool settings
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=3600,  # Recycle connections after 1 hour
    pool_pre_ping=True,  # Verify connection before use
    connect_args=({"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {}),
)

if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _):
        """WAL mode lets concurrent readers (API requests) proceed while a
        writer (Celery worker, of which there are many processes, each with
        its own connection pool onto this same file) commits, instead of
        every connection blocking on a single exclusive file lock. Without
        this, the default `journal_mode=delete` serializes all readers and
        writers against each other, and connections held waiting on that
        lock exhaust each process's own pool -- see
        docs/roadmap/done/phase18/phase18l-r-runtime-verification-report.md,
        "DB Connection Pool Exhaustion".
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


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


def get_pool_status() -> dict:
    """Report this process's connection pool state for operational diagnosis.

    Each Celery worker process and the API process hold their own engine/pool
    (see docs/roadmap/done/phase18/phase18l-r-runtime-verification-report.md,
    "DB Connection Pool Exhaustion"), so this only reflects the calling
    process, not the system as a whole.
    """
    pool = engine.pool
    # Pools other than QueuePool (e.g. SingletonThreadPool/StaticPool, used for
    # in-memory test databases) don't expose all of these methods.
    return {
        "size": pool.size() if hasattr(pool, "size") else None,
        "checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None,
        "overflow": pool.overflow() if hasattr(pool, "overflow") else None,
        "checked_in": pool.checkedin() if hasattr(pool, "checkedin") else None,
    }


# ============= TEST DATABASE HELPERS =============


def create_test_database(test_engine):
    """Create test database tables"""
    Base.metadata.create_all(bind=test_engine)


def cleanup_test_database(test_engine):
    """Drop all test database tables"""
    Base.metadata.drop_all(bind=test_engine)
