#!/usr/bin/env python3
"""
Migration script to add instance tracking columns to existing tables.
This handles the case where the database already exists without the new columns.
"""

import sys
import sqlite3
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import engine
from app.models import Base, Session as SessionModel, LogEntry


def migrate_database():
    """Migrate existing database to add instance tracking columns"""

    print("Starting database migration...")

    # Get connection
    with engine.connect() as conn:
        # Check if columns already exist
        inspector = inspect(conn)

        # Add instance_id column to sessions table if it doesn't exist
        if "instance_id" not in [
            col["name"] for col in inspector.get_columns("sessions")
        ]:
            print("Adding instance_id column to sessions table...")
            conn.execute(
                text(
                    """
                ALTER TABLE sessions 
                ADD COLUMN instance_id VARCHAR(36)
            """
                )
            )
            print("✓ Added instance_id to sessions")

        # Add deleted_at column to sessions table if it doesn't exist
        if "deleted_at" not in [
            col["name"] for col in inspector.get_columns("sessions")
        ]:
            print("Adding deleted_at column to sessions table...")
            conn.execute(
                text(
                    """
                ALTER TABLE sessions 
                ADD COLUMN deleted_at DATETIME
            """
                )
            )
            print("✓ Added deleted_at to sessions")

        # Add session_instance_id column to log_entries table if it doesn't exist
        if "session_instance_id" not in [
            col["name"] for col in inspector.get_columns("log_entries")
        ]:
            print("Adding session_instance_id column to log_entries table...")
            conn.execute(
                text(
                    """
                ALTER TABLE log_entries 
                ADD COLUMN session_instance_id VARCHAR(36)
            """
                )
            )
            print("✓ Added session_instance_id to log_entries")

    print("\nMigration completed successfully!")
    print("\nNext steps:")
    print("1. Restart the backend service to pick up model changes")
    print("2. New sessions will be created with unique instance IDs")
    print("3. Existing sessions will get instance IDs when they're recreated")


if __name__ == "__main__":
    from sqlalchemy import inspect, text

    migrate_database()
