#!/usr/bin/env python3
"""
Database migration: Add BMC fields to subscriptions table.

This script updates the existing subscriptions table to support both
LemonSqueezy and Buy Me a Coffee payment providers.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = "data/artimagehub.db"


def migrate():
    """Add BMC columns to subscriptions table."""
    if not Path(DB_PATH).exists():
        print(f"❌ Database not found: {DB_PATH}")
        print("   This is expected if it's a fresh install.")
        return

    print(f"🔄 Migrating database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Check if migration is needed
        cursor.execute("PRAGMA table_info(subscriptions)")
        columns = [row[1] for row in cursor.fetchall()]

        if "payment_provider" in columns:
            print("✅ Database already migrated!")
            return

        print("📝 Adding BMC columns...")

        # Add new columns
        cursor.execute("""
            ALTER TABLE subscriptions
            ADD COLUMN payment_provider TEXT DEFAULT 'lemonsqueezy'
        """)

        cursor.execute("""
            ALTER TABLE subscriptions
            ADD COLUMN bmc_supporter_id TEXT
        """)

        cursor.execute("""
            ALTER TABLE subscriptions
            ADD COLUMN bmc_membership_id TEXT
        """)

        conn.commit()
        print("✅ Migration completed successfully!")

        # Show stats
        cursor.execute("SELECT COUNT(*) FROM subscriptions")
        count = cursor.fetchone()[0]
        print(f"   Total subscriptions: {count}")

    except sqlite3.OperationalError as e:
        print(f"❌ Migration failed: {e}")
        conn.rollback()
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
