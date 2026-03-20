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
                -- Payment provider fields (provider can be 'lemonsqueezy', 'bmc', or 'paypal')
                payment_provider TEXT DEFAULT 'lemonsqueezy',
                lemonsqueezy_customer_id TEXT,
                lemonsqueezy_subscription_id TEXT,
                bmc_supporter_id TEXT,
                bmc_membership_id TEXT,
                paypal_order_id TEXT,
                paypal_payer_id TEXT,
                -- Subscription status
                status TEXT NOT NULL DEFAULT 'none',
                trial_start TEXT,
                trial_end TEXT,
                current_period_start TEXT,
                current_period_end TEXT,
                cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sub_lemonsqueezy_customer
                ON subscriptions(lemonsqueezy_customer_id);
            CREATE INDEX IF NOT EXISTS idx_sub_lemonsqueezy_sub
                ON subscriptions(lemonsqueezy_subscription_id);

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

            CREATE TABLE IF NOT EXISTS processing_events (
                task_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_processing_events_completed_at
                ON processing_events(completed_at);

            CREATE TABLE IF NOT EXISTS paypal_checkout_context (
                order_id TEXT PRIMARY KEY,
                checkout_email TEXT NOT NULL,
                payer_email TEXT,
                capture_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paypal_checkout_email
                ON paypal_checkout_context(checkout_email);
            CREATE INDEX IF NOT EXISTS idx_paypal_capture_id
                ON paypal_checkout_context(capture_id);
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
    payment_provider: str = "lemonsqueezy",
    lemonsqueezy_customer_id: str | None = None,
    lemonsqueezy_subscription_id: str | None = None,
    bmc_supporter_id: str | None = None,
    bmc_membership_id: str | None = None,
    paypal_order_id: str | None = None,
    paypal_payer_id: str | None = None,
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
               (email, payment_provider, lemonsqueezy_customer_id, lemonsqueezy_subscription_id,
                bmc_supporter_id, bmc_membership_id, paypal_order_id, paypal_payer_id, status,
                trial_start, trial_end, current_period_start, current_period_end,
                cancel_at_period_end, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                   payment_provider = ?,
                   lemonsqueezy_customer_id = COALESCE(?, lemonsqueezy_customer_id),
                   lemonsqueezy_subscription_id = COALESCE(?, lemonsqueezy_subscription_id),
                   bmc_supporter_id = COALESCE(?, bmc_supporter_id),
                   bmc_membership_id = COALESCE(?, bmc_membership_id),
                   paypal_order_id = COALESCE(?, paypal_order_id),
                   paypal_payer_id = COALESCE(?, paypal_payer_id),
                   status = ?,
                   trial_start = COALESCE(?, trial_start),
                   trial_end = COALESCE(?, trial_end),
                   current_period_start = COALESCE(?, current_period_start),
                   current_period_end = COALESCE(?, current_period_end),
                   cancel_at_period_end = ?,
                   updated_at = ?""",
            (
                email, payment_provider, lemonsqueezy_customer_id, lemonsqueezy_subscription_id,
                bmc_supporter_id, bmc_membership_id, paypal_order_id, paypal_payer_id, status,
                trial_start, trial_end, current_period_start, current_period_end,
                1 if cancel_at_period_end else 0, now, now,
                # ON CONFLICT params:
                payment_provider,
                lemonsqueezy_customer_id, lemonsqueezy_subscription_id,
                bmc_supporter_id, bmc_membership_id, paypal_order_id, paypal_payer_id, status,
                trial_start, trial_end, current_period_start, current_period_end,
                1 if cancel_at_period_end else 0, now,
            ),
        )
    logger.info("Subscription upserted: %s provider=%s status=%s", email, payment_provider, status)


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


def get_subscription_by_customer(lemonsqueezy_customer_id: str) -> dict | None:
    """Look up subscription by LemonSqueezy customer ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE lemonsqueezy_customer_id = ?",
            (lemonsqueezy_customer_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def save_paypal_checkout_email(order_id: str, checkout_email: str):
    """Persist the checkout email chosen before PayPal approval."""
    now = datetime.now(timezone.utc).isoformat()
    normalized_email = checkout_email.lower().strip()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO paypal_checkout_context
               (order_id, checkout_email, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                   checkout_email = excluded.checkout_email,
                   updated_at = excluded.updated_at""",
            (order_id, normalized_email, now, now),
        )

    logger.info(
        "Saved PayPal checkout email: order_id=%s checkout_email=%s",
        order_id,
        normalized_email,
    )


def get_paypal_checkout_email(order_id: str) -> str | None:
    """Return the saved checkout email for a PayPal order, if present."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT checkout_email FROM paypal_checkout_context WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return row["checkout_email"]


def record_paypal_capture(
    order_id: str,
    capture_id: str | None = None,
    payer_email: str | None = None,
):
    """Attach capture audit data to a stored PayPal checkout context."""
    now = datetime.now(timezone.utc).isoformat()
    normalized_payer_email = payer_email.lower().strip() if payer_email else None

    with get_db() as conn:
        conn.execute(
            """UPDATE paypal_checkout_context
               SET payer_email = COALESCE(?, payer_email),
                   capture_id = COALESCE(?, capture_id),
                   updated_at = ?
               WHERE order_id = ?""",
            (normalized_payer_email, capture_id, now, order_id),
        )

        if conn.total_changes == 0 and normalized_payer_email:
            conn.execute(
                """INSERT OR IGNORE INTO paypal_checkout_context
                   (order_id, checkout_email, payer_email, capture_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    normalized_payer_email,
                    normalized_payer_email,
                    capture_id,
                    now,
                    now,
                ),
            )

    logger.info(
        "Recorded PayPal capture audit: order_id=%s capture_id=%s payer_email=%s",
        order_id,
        capture_id,
        normalized_payer_email,
    )


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


def record_processing_complete(task_id: str, mode: str):
    """Persist a processing completion event for 24h metric aggregation."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processing_events (task_id, mode, completed_at) VALUES (?, ?, ?)",
            (task_id, mode, now),
        )


def get_processing_complete_metrics(hours: int = 24) -> dict:
    """Return completion count and mode split in the trailing N hours."""
    now = datetime.now(timezone.utc)
    start = now.timestamp() - max(1, hours) * 3600
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()

    with get_db() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM processing_events WHERE completed_at >= ?",
            (start_iso,),
        ).fetchone()
        mode_rows = conn.execute(
            """
            SELECT mode, COUNT(*) AS cnt
            FROM processing_events
            WHERE completed_at >= ?
            GROUP BY mode
            ORDER BY cnt DESC
            """,
            (start_iso,),
        ).fetchall()

    return {
        "count": int(total_row["cnt"]) if total_row else 0,
        "by_mode": {row["mode"]: int(row["cnt"]) for row in mode_rows},
        "window_hours": max(1, hours),
        "generated_at": now.isoformat(),
    }
