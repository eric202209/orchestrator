"""Database initialization"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.models import Base

# Create database engine
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database tables"""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Dependency for getting database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============= TEST DATABASE HELPERS =============

def create_test_database(test_engine):
    """Create test database tables"""
    Base.metadata.create_all(bind=test_engine)


def cleanup_test_database(test_engine):
    """Drop all test database tables"""
    Base.metadata.drop_all(bind=test_engine)
