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
_ATTRIBUTION_COLUMNS = {
    "landing_page": "TEXT",
    "cta_slot": "TEXT",
    "entry_variant": "TEXT",
    "checkout_source": "TEXT",
}


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = get_settings().database_path
        Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
    return _db_path


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]):
    existing = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


def _get_metrics_database_url() -> str:
    return get_settings().metrics_database_url.strip()


def _use_metrics_postgres() -> bool:
    return bool(_get_metrics_database_url())


def get_payment_metrics_storage_backend() -> str:
    return "postgres" if _use_metrics_postgres() else "sqlite"


def _connect_metrics_postgres():
    from psycopg import connect
    from psycopg.rows import dict_row

    return connect(
        _get_metrics_database_url(),
        connect_timeout=5,
        row_factory=dict_row,
    )


def _init_metrics_postgres():
    with _connect_metrics_postgres() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_initiations (
                    order_id TEXT PRIMARY KEY,
                    payment_provider TEXT NOT NULL,
                    email TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_payment_initiations_created_at
                    ON payment_initiations(created_at);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_payment_initiations_provider_created_at
                    ON payment_initiations(payment_provider, created_at);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_successes (
                    success_key TEXT PRIMARY KEY,
                    capture_id TEXT,
                    order_id TEXT,
                    payment_provider TEXT NOT NULL,
                    email TEXT NOT NULL,
                    completed_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_payment_successes_completed_at
                    ON payment_successes(completed_at);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_payment_successes_provider_completed_at
                    ON payment_successes(payment_provider, completed_at);
                """
            )
        conn.commit()
    logger.info("Metrics Postgres initialized")


def _seed_owner_access():
    """Grant the configured owner email paid access if not already active."""
    from app.config import get_settings
    owner_email = get_settings().alert_email_to
    if not owner_email:
        return
    sub = get_subscription(owner_email)
    if sub is None or sub["status"] not in ("active", "trialing", "on_trial"):
        upsert_subscription(owner_email, payment_provider="seed", status="active")
        logger.info("Owner access seeded: %s", owner_email)


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
                landing_page TEXT,
                cta_slot TEXT,
                entry_variant TEXT,
                checkout_source TEXT,
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

            CREATE TABLE IF NOT EXISTS payment_initiations (
                order_id TEXT PRIMARY KEY,
                payment_provider TEXT NOT NULL,
                email TEXT NOT NULL,
                landing_page TEXT,
                cta_slot TEXT,
                entry_variant TEXT,
                checkout_source TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payment_initiations_created_at
                ON payment_initiations(created_at);
            CREATE INDEX IF NOT EXISTS idx_payment_initiations_provider_created_at
                ON payment_initiations(payment_provider, created_at);

            CREATE TABLE IF NOT EXISTS payment_successes (
                success_key TEXT PRIMARY KEY,
                capture_id TEXT,
                order_id TEXT,
                payment_provider TEXT NOT NULL,
                email TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payment_successes_completed_at
                ON payment_successes(completed_at);
            CREATE INDEX IF NOT EXISTS idx_payment_successes_provider_completed_at
                ON payment_successes(payment_provider, completed_at);
        """)
        _ensure_columns(conn, "processing_events", _ATTRIBUTION_COLUMNS)
        _ensure_columns(conn, "payment_initiations", _ATTRIBUTION_COLUMNS)
    logger.info("Database initialized at %s", path)

    if _use_metrics_postgres():
        try:
            _init_metrics_postgres()
        except Exception:
            logger.warning("Metrics Postgres initialization failed", exc_info=True)

    _seed_owner_access()


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
    return sub["status"] in ("trialing", "active", "on_trial")


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


def _normalize_attr(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _metrics_window(hours: int) -> tuple[datetime, str]:
    window_hours = max(1, hours)
    now = datetime.now(timezone.utc)
    start = now.timestamp() - window_hours * 3600
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
    return now, start_iso


def record_processing_complete(
    task_id: str,
    mode: str,
    landing_page: str | None = None,
    cta_slot: str | None = None,
    entry_variant: str | None = None,
    checkout_source: str | None = None,
):
    """Persist a processing completion event for 24h metric aggregation."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO processing_events
            (task_id, mode, landing_page, cta_slot, entry_variant, checkout_source, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                mode,
                _normalize_attr(landing_page),
                _normalize_attr(cta_slot),
                _normalize_attr(entry_variant),
                _normalize_attr(checkout_source),
                now,
            ),
        )


def get_processing_complete_metrics(hours: int = 24) -> dict:
    """Return completion count and mode split in the trailing N hours."""
    now, start_iso = _metrics_window(hours)

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


def record_payment_initiation(
    order_id: str,
    email: str,
    payment_provider: str = "paypal",
    landing_page: str | None = None,
    cta_slot: str | None = None,
    entry_variant: str | None = None,
    checkout_source: str | None = None,
):
    """Persist a server-side payment initiation event for exact-window counting."""
    now = datetime.now(timezone.utc).isoformat()
    if _use_metrics_postgres():
        with _connect_metrics_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payment_initiations
                    (order_id, payment_provider, email, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (order_id) DO NOTHING
                    """,
                    (order_id, payment_provider, email.lower().strip(), now),
                )
            conn.commit()
        return

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO payment_initiations
            (
                order_id,
                payment_provider,
                email,
                landing_page,
                cta_slot,
                entry_variant,
                checkout_source,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                payment_provider,
                email.lower().strip(),
                _normalize_attr(landing_page),
                _normalize_attr(cta_slot),
                _normalize_attr(entry_variant),
                _normalize_attr(checkout_source),
                now,
            ),
        )


def get_payment_initiation_metrics(hours: int = 24) -> dict:
    """Return payment initiation count and provider split in trailing N hours."""
    now, start_iso = _metrics_window(hours)

    if _use_metrics_postgres():
        with _connect_metrics_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM payment_initiations WHERE created_at >= %s",
                    (start_iso,),
                )
                total_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT payment_provider, COUNT(*) AS cnt
                    FROM payment_initiations
                    WHERE created_at >= %s
                    GROUP BY payment_provider
                    ORDER BY cnt DESC
                    """,
                    (start_iso,),
                )
                provider_rows = cur.fetchall()

        return {
            "count": int(total_row["cnt"]) if total_row else 0,
            "by_provider": {
                row["payment_provider"]: int(row["cnt"]) for row in provider_rows
            },
            "storage_backend": get_payment_metrics_storage_backend(),
            "window_hours": max(1, hours),
            "generated_at": now.isoformat(),
        }

    with get_db() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM payment_initiations WHERE created_at >= ?",
            (start_iso,),
        ).fetchone()
        provider_rows = conn.execute(
            """
            SELECT payment_provider, COUNT(*) AS cnt
            FROM payment_initiations
            WHERE created_at >= ?
            GROUP BY payment_provider
            ORDER BY cnt DESC
            """,
            (start_iso,),
        ).fetchall()

    return {
        "count": int(total_row["cnt"]) if total_row else 0,
        "by_provider": {
            row["payment_provider"]: int(row["cnt"]) for row in provider_rows
        },
        "storage_backend": get_payment_metrics_storage_backend(),
        "window_hours": max(1, hours),
        "generated_at": now.isoformat(),
    }


def get_exact_funnel_tuple_metrics(
    landing_page: str,
    cta_slot: str,
    entry_variant: str,
    checkout_source: str,
    hours: int = 24,
) -> dict:
    """Return exact payment/processing counts for one funnel tuple."""
    if _use_metrics_postgres():
        raise RuntimeError(
            "Exact funnel tuple metrics currently require sqlite metrics backend"
        )

    now, start_iso = _metrics_window(hours)
    normalized_tuple = {
        "landing_page": _normalize_attr(landing_page),
        "cta_slot": _normalize_attr(cta_slot),
        "entry_variant": _normalize_attr(entry_variant),
        "checkout_source": _normalize_attr(checkout_source),
    }

    if not all(normalized_tuple.values()):
        raise ValueError("Exact funnel tuple metrics require all 4 attribution fields")

    tuple_params = (
        start_iso,
        normalized_tuple["landing_page"],
        normalized_tuple["cta_slot"],
        normalized_tuple["entry_variant"],
        normalized_tuple["checkout_source"],
    )

    with get_db() as conn:
        payment_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM payment_initiations
            WHERE created_at >= ?
              AND landing_page = ?
              AND cta_slot = ?
              AND entry_variant = ?
              AND checkout_source = ?
            """,
            tuple_params,
        ).fetchone()
        payment_provider_rows = conn.execute(
            """
            SELECT payment_provider, COUNT(*) AS cnt
            FROM payment_initiations
            WHERE created_at >= ?
              AND landing_page = ?
              AND cta_slot = ?
              AND entry_variant = ?
              AND checkout_source = ?
            GROUP BY payment_provider
            ORDER BY cnt DESC
            """,
            tuple_params,
        ).fetchall()
        processing_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM processing_events
            WHERE completed_at >= ?
              AND landing_page = ?
              AND cta_slot = ?
              AND entry_variant = ?
              AND checkout_source = ?
            """,
            tuple_params,
        ).fetchone()
        processing_mode_rows = conn.execute(
            """
            SELECT mode, COUNT(*) AS cnt
            FROM processing_events
            WHERE completed_at >= ?
              AND landing_page = ?
              AND cta_slot = ?
              AND entry_variant = ?
              AND checkout_source = ?
            GROUP BY mode
            ORDER BY cnt DESC
            """,
            tuple_params,
        ).fetchall()

    return {
        **normalized_tuple,
        "payment_initiations": int(payment_row["cnt"]) if payment_row else 0,
        "payment_by_provider": {
            row["payment_provider"]: int(row["cnt"])
            for row in payment_provider_rows
        },
        "processing_completions": int(processing_row["cnt"]) if processing_row else 0,
        "processing_by_mode": {
            row["mode"]: int(row["cnt"])
            for row in processing_mode_rows
        },
        "storage_backend": get_payment_metrics_storage_backend(),
        "window_hours": max(1, hours),
        "generated_at": now.isoformat(),
    }


def record_payment_success(
    *,
    order_id: str | None = None,
    capture_id: str | None = None,
    email: str,
    payment_provider: str = "paypal",
    completed_at: str | None = None,
):
    """Persist a server-side payment success event for exact-window counting."""
    success_key = capture_id or (f"order:{order_id}" if order_id else None)
    if success_key is None:
        raise ValueError("record_payment_success requires capture_id or order_id")

    occurred_at = completed_at or datetime.now(timezone.utc).isoformat()
    if _use_metrics_postgres():
        with _connect_metrics_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payment_successes
                    (success_key, capture_id, order_id, payment_provider, email, completed_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (success_key) DO NOTHING
                    """,
                    (
                        success_key,
                        capture_id,
                        order_id,
                        payment_provider,
                        email.lower().strip(),
                        occurred_at,
                    ),
                )
            conn.commit()
        return

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO payment_successes
            (success_key, capture_id, order_id, payment_provider, email, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                success_key,
                capture_id,
                order_id,
                payment_provider,
                email.lower().strip(),
                occurred_at,
            ),
        )


def get_payment_success_metrics(hours: int = 24) -> dict:
    """Return payment success count and provider split in trailing N hours."""
    now = datetime.now(timezone.utc)
    start = now.timestamp() - max(1, hours) * 3600
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()

    if _use_metrics_postgres():
        with _connect_metrics_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM payment_successes WHERE completed_at >= %s",
                    (start_iso,),
                )
                total_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT payment_provider, COUNT(*) AS cnt
                    FROM payment_successes
                    WHERE completed_at >= %s
                    GROUP BY payment_provider
                    ORDER BY cnt DESC
                    """,
                    (start_iso,),
                )
                provider_rows = cur.fetchall()

        return {
            "count": int(total_row["cnt"]) if total_row else 0,
            "by_provider": {
                row["payment_provider"]: int(row["cnt"]) for row in provider_rows
            },
            "storage_backend": get_payment_metrics_storage_backend(),
            "window_hours": max(1, hours),
            "generated_at": now.isoformat(),
        }

    with get_db() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM payment_successes WHERE completed_at >= ?",
            (start_iso,),
        ).fetchone()
        provider_rows = conn.execute(
            """
            SELECT payment_provider, COUNT(*) AS cnt
            FROM payment_successes
            WHERE completed_at >= ?
            GROUP BY payment_provider
            ORDER BY cnt DESC
            """,
            (start_iso,),
        ).fetchall()

    return {
        "count": int(total_row["cnt"]) if total_row else 0,
        "by_provider": {
            row["payment_provider"]: int(row["cnt"]) for row in provider_rows
        },
        "storage_backend": get_payment_metrics_storage_backend(),
        "window_hours": max(1, hours),
        "generated_at": now.isoformat(),
    }
