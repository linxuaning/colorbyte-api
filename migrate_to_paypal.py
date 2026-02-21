#!/usr/bin/env python3
"""
Database migration: Add PayPal support to subscriptions table
"""
import sqlite3
from pathlib import Path


def migrate():
    """Add PayPal fields to subscriptions table."""
    db_path = "data/artimagehub.db"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        # Check if paypal columns already exist
        cursor = conn.execute("PRAGMA table_info(subscriptions)")
        columns = {row[1] for row in cursor.fetchall()}

        if "paypal_order_id" not in columns:
            print("Adding paypal_order_id column...")
            conn.execute("ALTER TABLE subscriptions ADD COLUMN paypal_order_id TEXT")
            print("✅ Added paypal_order_id")
        else:
            print("⏭️  paypal_order_id already exists")

        if "paypal_payer_id" not in columns:
            print("Adding paypal_payer_id column...")
            conn.execute("ALTER TABLE subscriptions ADD COLUMN paypal_payer_id TEXT")
            print("✅ Added paypal_payer_id")
        else:
            print("⏭️  paypal_payer_id already exists")

        conn.commit()
        print("\n✅ Migration complete!")


if __name__ == "__main__":
    migrate()
