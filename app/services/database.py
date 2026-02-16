"""
SQLite database for subscriptions.
MVP: email-based subscription system, no user accounts/passwords.
"""
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import get_settings

logger = logging.getLogger("artimagehub.db")

_db_path: str | None = None


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = get_settings().database_path
        Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
    return _db_path


def init_db():
    """Create tables if they don't exist."""
    path = _get_db_path()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                email TEXT PRIMARY KEY,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                status TEXT NOT NULL DEFAULT 'none',
                trial_start TEXT,
                trial_end TEXT,
                current_period_start TEXT,
                current_period_end TEXT,
                cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sub_stripe_customer
                ON subscriptions(stripe_customer_id);
            CREATE INDEX IF NOT EXISTS idx_sub_stripe_sub
                ON subscriptions(stripe_subscription_id);

            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                download_date TEXT NOT NULL,
                task_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_downloads_ip_date
                ON downloads(ip, download_date);
        """)
    logger.info("Database initialized at %s", path)


@contextmanager
def get_db():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_event_processed(event_id: str) -> bool:
    """Check if a webhook event has already been processed (idempotency)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM webhook_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None


def mark_event_processed(event_id: str, event_type: str):
    """Record that a webhook event has been processed."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO webhook_events (event_id, event_type, processed_at) VALUES (?, ?, ?)",
            (event_id, event_type, now),
        )


def upsert_subscription(
    email: str,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    status: str = "none",
    trial_start: str | None = None,
    trial_end: str | None = None,
    current_period_start: str | None = None,
    current_period_end: str | None = None,
    cancel_at_period_end: bool = False,
):
    """Create or update a subscription record."""
    now = datetime.now(timezone.utc).isoformat()
    email = email.lower().strip()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO subscriptions
               (email, stripe_customer_id, stripe_subscription_id, status,
                trial_start, trial_end, current_period_start, current_period_end,
                cancel_at_period_end, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                   stripe_customer_id = COALESCE(?, stripe_customer_id),
                   stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                   status = ?,
                   trial_start = COALESCE(?, trial_start),
                   trial_end = COALESCE(?, trial_end),
                   current_period_start = COALESCE(?, current_period_start),
                   current_period_end = COALESCE(?, current_period_end),
                   cancel_at_period_end = ?,
                   updated_at = ?""",
            (
                email, stripe_customer_id, stripe_subscription_id, status,
                trial_start, trial_end, current_period_start, current_period_end,
                1 if cancel_at_period_end else 0, now, now,
                # ON CONFLICT params:
                stripe_customer_id, stripe_subscription_id, status,
                trial_start, trial_end, current_period_start, current_period_end,
                1 if cancel_at_period_end else 0, now,
            ),
        )
    logger.info("Subscription upserted: %s status=%s", email, status)


def get_subscription(email: str) -> dict | None:
    """Get subscription info for an email. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def get_subscription_by_customer(stripe_customer_id: str) -> dict | None:
    """Look up subscription by Stripe customer ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def is_user_active(email: str) -> bool:
    """Check if a user has an active subscription or is in trial."""
    sub = get_subscription(email)
    if sub is None:
        return False
    return sub["status"] in ("trialing", "active")


def cancel_subscription_db(email: str):
    """Mark subscription as pending cancellation (cancel at period end)."""
    now = datetime.now(timezone.utc).isoformat()
    email = email.lower().strip()
    with get_db() as conn:
        conn.execute(
            "UPDATE subscriptions SET cancel_at_period_end = 1, updated_at = ? WHERE email = ?",
            (now, email),
        )
    logger.info("Subscription cancel requested: %s", email)


# --- Download tracking ---

FREE_DAILY_LIMIT = 3


def get_download_count(ip: str, date_str: str) -> int:
    """Get number of downloads for an IP on a given date."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM downloads WHERE ip = ? AND download_date = ?",
            (ip, date_str),
        ).fetchone()
        return row["cnt"] if row else 0


def record_download(ip: str, task_id: str):
    """Record a download event."""
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO downloads (ip, download_date, task_id, created_at) VALUES (?, ?, ?, ?)",
            (ip, now.strftime("%Y-%m-%d"), task_id, now.isoformat()),
        )


def check_download_limit(ip: str, email: str | None = None) -> dict:
    """Check if a download is allowed. Subscribers get unlimited access."""
    if email and is_user_active(email):
        return {"allowed": True, "remaining": -1, "is_subscriber": True}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = get_download_count(ip, today)
    remaining = max(0, FREE_DAILY_LIMIT - count)
    return {"allowed": remaining > 0, "remaining": remaining, "is_subscriber": False}
