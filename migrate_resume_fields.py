"""Database migration to add resume functionality fields.

This script updates the database schema to support full session resumption.
Run this AFTER creating the new models (SessionState, TaskCheckpoint).

Usage:
    python migrate_resume_fields.py
"""

import sys
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from app.models import Base, SessionState, TaskCheckpoint
from app.config import settings

# Database URL from config
DATABASE_URL = settings.DATABASE_URL


def check_existing_tables():
    """Check if tables already exist"""
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    
    existing_tables = inspector.get_table_names()
    
    has_session_states = 'session_states' in existing_tables
    has_task_checkpoints = 'task_checkpoints' in existing_tables
    
    print(f"Existing tables: {len(existing_tables)}")
    print(f"- session_states table exists: {has_session_states}")
    print(f"- task_checkpoints table exists: {has_task_checkpoints}")
    
    return has_session_states, has_task_checkpoints


def run_migration():
    """Run database migration"""
    print("=" * 60)
    print("🔧 DATABASE MIGRATION - Resume Functionality")
    print("=" * 60)
    
    # Check existing tables
    has_session_states, has_task_checkpoints = check_existing_tables()
    
    if has_session_states and has_task_checkpoints:
        print("\n✅ All required tables already exist!")
        print("No migration needed.")
        return True
    
    # Create engine and session
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    try:
        db = SessionLocal()
        
        # Check if tables need to be created
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        
        # Create SessionState table if missing
        if 'session_states' not in existing_tables:
            print("\n📦 Creating session_states table...")
            SessionState.__table__.create(bind=engine, checkfirst=True)
            print("✅ session_states table created")
        
        # Create TaskCheckpoint table if missing
        if 'task_checkpoints' not in existing_tables:
            print("\n📦 Creating task_checkpoints table...")
            TaskCheckpoint.__table__.create(bind=engine, checkfirst=True)
            print("✅ task_checkpoints table created")
        
        # Verify creation
        new_inspector = inspect(engine)
        new_tables = new_inspector.get_table_names()
        
        if 'session_states' in new_tables and 'task_checkpoints' in new_tables:
            print("\n" + "=" * 60)
            print("✅ MIGRATION COMPLETED SUCCESSFULLY!")
            print("=" * 60)
            print(f"\nNew tables created:")
            print("- session_states")
            print("- task_checkpoints")
            return True
        
        db.close()
        
    except Exception as e:
        print(f"\n❌ MIGRATION FAILED: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def verify_schema():
    """Verify the schema is correct after migration"""
    print("\n" + "=" * 60)
    print("🔍 VERIFYING SCHEMA")
    print("=" * 60)
    
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    
    # Check session_states columns
    if 'session_states' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('session_states')]
        print(f"\nsession_states columns ({len(columns)}):")
        for col in columns:
            print(f"  - {col}")
    
    # Check task_checkpoints columns
    if 'task_checkpoints' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('task_checkpoints')]
        print(f"\ntask_checkpoints columns ({len(columns)}):")
        for col in columns:
            print(f"  - {col}")


if __name__ == "__main__":
    success = run_migration()
    
    if success:
        verify_schema()
        print("\n✅ Migration and verification completed!")
        sys.exit(0)
    else:
        print("\n❌ Migration failed. Please check the error above.")
        sys.exit(1)
