"""
Subscription / metrics persistence.

Two modes:
  - PG configured (DATABASE_URL set): reads go to Postgres only; writes
    fan out to sqlite (best-effort backup) + Postgres (authoritative for reads).
    Rollback path: unset DATABASE_URL + redeploy → falls back to sqlite-only.
  - PG not configured: pure sqlite (original local-dev mode).
"""
import sqlite3
import logging
import time
import threading
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

# --- Dual-write observability ---
# Tracks recent dual-write outcomes for /health success-ratio reporting.
# Single-worker free tier; lock keeps trim+append atomic under the GIL.
_DUAL_WRITE_OBS_WINDOW_SECONDS = 60
_dual_write_lock = threading.Lock()
_dual_write_events: list[tuple[float, str, bool, bool]] = []  # (monotonic_ts, op, sqlite_ok, pg_ok)


def _record_obs(op: str, sqlite_ok: bool, pg_ok: bool) -> None:
    """Log a dual-write outcome and append to the rolling window for /health."""
    now = time.monotonic()
    cutoff = now - _DUAL_WRITE_OBS_WINDOW_SECONDS
    with _dual_write_lock:
        _dual_write_events[:] = [e for e in _dual_write_events if e[0] >= cutoff]
        _dual_write_events.append((now, op, sqlite_ok, pg_ok))
    logger.info("dual_write op=%s sqlite_ok=%s pg_ok=%s", op, sqlite_ok, pg_ok)


def get_dual_write_health() -> dict:
    """Return last-60s dual-write success ratios for /health."""
    now = time.monotonic()
    cutoff = now - _DUAL_WRITE_OBS_WINDOW_SECONDS
    with _dual_write_lock:
        recent = [e for e in _dual_write_events if e[0] >= cutoff]
    total = len(recent)
    if total == 0:
        return {
            "window_seconds": _DUAL_WRITE_OBS_WINDOW_SECONDS,
            "samples": 0,
            "sqlite_ok_rate": None,
            "pg_ok_rate": None,
        }
    sqlite_ok = sum(1 for _, _, s, _ in recent if s)
    pg_ok = sum(1 for _, _, _, p in recent if p)
    return {
        "window_seconds": _DUAL_WRITE_OBS_WINDOW_SECONDS,
        "samples": total,
        "sqlite_ok_rate": round(sqlite_ok / total, 3),
        "pg_ok_rate": round(pg_ok / total, 3),
    }


def _row_to_dict(row) -> dict:
    """Normalize psycopg dict_row / sqlite Row to a plain dict with ISO-8601 strings.

    PG returns native datetime / date / bool; sqlite returns TEXT/INTEGER. Callers
    expect string timestamps + 0/1 booleans (legacy sqlite shape), so coerce here.
    """
    if row is None:
        return None
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat") and not isinstance(v, (int, float, str, bytes)):
            # date objects, etc.
            out[k] = v.isoformat()
        elif isinstance(v, bool):
            out[k] = 1 if v else 0
    return out


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


def _get_database_url() -> str:
    """Unified Postgres URL. Prefers `database_url`; falls back to legacy `metrics_database_url`."""
    settings = get_settings()
    primary = settings.database_url.strip()
    if primary:
        return primary
    return settings.metrics_database_url.strip()


def _use_postgres() -> bool:
    return bool(_get_database_url())


def get_database_backend() -> str:
    return "postgres" if _use_postgres() else "sqlite"


def _connect_postgres():
    from psycopg import connect
    from psycopg.rows import dict_row

    return connect(
        _get_database_url(),
        connect_timeout=5,
        row_factory=dict_row,
    )


# --- Deprecated metrics-only aliases (kept so app-code keeps working until Commit B) ---

def _get_metrics_database_url() -> str:
    return _get_database_url()


def _use_metrics_postgres() -> bool:
    return _use_postgres()


def get_payment_metrics_storage_backend() -> str:
    return get_database_backend()


def _connect_metrics_postgres():
    return _connect_postgres()


def _init_postgres():
    """Create all Postgres tables idempotently (subscriptions + auxiliary + metrics)."""
    url = _get_database_url()
    if "-pooler" not in url:
        logger.warning(
            "DATABASE_URL does not contain '-pooler' — direct Neon endpoint detected. "
            "Consider switching to the pooler endpoint for connection scaling."
        )

    with _connect_postgres() as conn:
        with conn.cursor() as cur:
            # subscriptions (P0 entitlement)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    email TEXT PRIMARY KEY,
                    payment_provider TEXT DEFAULT 'lemonsqueezy',
                    lemonsqueezy_customer_id TEXT,
                    lemonsqueezy_subscription_id TEXT,
                    bmc_supporter_id TEXT,
                    bmc_membership_id TEXT,
                    paypal_order_id TEXT,
                    paypal_payer_id TEXT,
                    status TEXT NOT NULL DEFAULT 'none',
                    trial_start TIMESTAMPTZ,
                    trial_end TIMESTAMPTZ,
                    current_period_start TIMESTAMPTZ,
                    current_period_end TIMESTAMPTZ,
                    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_lemonsqueezy_customer ON subscriptions(lemonsqueezy_customer_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_lemonsqueezy_sub ON subscriptions(lemonsqueezy_subscription_id);")

            # webhook_events (P1 idempotency)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    processed_at TIMESTAMPTZ NOT NULL
                );
                """
            )

            # paypal_checkout_context (P1 webhook→email mapping)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS paypal_checkout_context (
                    order_id TEXT PRIMARY KEY,
                    checkout_email TEXT NOT NULL,
                    payer_email TEXT,
                    capture_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_paypal_checkout_email ON paypal_checkout_context(checkout_email);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_paypal_capture_id ON paypal_checkout_context(capture_id);")

            # downloads (P2 free-tier rate limit)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id BIGSERIAL PRIMARY KEY,
                    ip TEXT NOT NULL,
                    download_date DATE NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_downloads_ip_date ON downloads(ip, download_date);")

            # processing_events (P2 attribution metrics)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS processing_events (
                    task_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    landing_page TEXT,
                    cta_slot TEXT,
                    entry_variant TEXT,
                    checkout_source TEXT,
                    completed_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_processing_events_completed_at ON processing_events(completed_at);")

            # payment_initiations (existing) + attribution columns
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_initiations (
                    order_id TEXT PRIMARY KEY,
                    payment_provider TEXT NOT NULL,
                    email TEXT NOT NULL,
                    landing_page TEXT,
                    cta_slot TEXT,
                    entry_variant TEXT,
                    checkout_source TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            # Backfill attribution columns for pre-existing PG installations
            cur.execute("ALTER TABLE payment_initiations ADD COLUMN IF NOT EXISTS landing_page TEXT;")
            cur.execute("ALTER TABLE payment_initiations ADD COLUMN IF NOT EXISTS cta_slot TEXT;")
            cur.execute("ALTER TABLE payment_initiations ADD COLUMN IF NOT EXISTS entry_variant TEXT;")
            cur.execute("ALTER TABLE payment_initiations ADD COLUMN IF NOT EXISTS checkout_source TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payment_initiations_created_at ON payment_initiations(created_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payment_initiations_provider_created_at ON payment_initiations(payment_provider, created_at);")

            # payment_successes
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payment_successes_completed_at ON payment_successes(completed_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payment_successes_provider_completed_at ON payment_successes(payment_provider, completed_at);")

            # mask_email_queue (post-purchase Mask thank-you email; founder-driven feature)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mask_email_queue (
                    id BIGSERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    payment_id TEXT NOT NULL,
                    first_name TEXT,
                    tool_name TEXT,
                    scheduled_at TIMESTAMPTZ NOT NULL,
                    sent_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (email, payment_id)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mask_email_due ON mask_email_queue(scheduled_at) WHERE sent_at IS NULL;"
            )
        conn.commit()
    logger.info("PostgreSQL schema initialized (subscriptions + auxiliary + metrics + mask_email_queue)")


# Deprecated alias retained for callers in current app-code; rewritten in Commit B.
def _init_metrics_postgres():
    _init_postgres()


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

    if _use_postgres():
        try:
            _init_postgres()
        except Exception:
            logger.warning("PostgreSQL initialization failed", exc_info=True)

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
    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM webhook_events WHERE event_id = %s", (event_id,))
                return cur.fetchone() is not None

    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM webhook_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None


def mark_event_processed(event_id: str, event_type: str):
    """Record that a webhook event has been processed (dual-write)."""
    now = datetime.now(timezone.utc).isoformat()
    sqlite_ok = False
    pg_ok = not _use_postgres()  # if PG not configured, treat as N/A

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO webhook_events (event_id, event_type, processed_at) VALUES (?, ?, ?)",
                (event_id, event_type, now),
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write mark_event_processed sqlite failed event_id=%s", event_id)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO webhook_events (event_id, event_type, processed_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (event_id, event_type, now),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write mark_event_processed pg failed event_id=%s", event_id)

    _record_obs("mark_event_processed", sqlite_ok, pg_ok)
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for mark_event_processed event_id={event_id}")


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
    """Create or update a subscription record (dual-write)."""
    now = datetime.now(timezone.utc).isoformat()
    email = email.lower().strip()
    sqlite_ok = False
    pg_ok = not _use_postgres()

    sqlite_params = (
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
    )

    try:
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
                sqlite_params,
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write upsert_subscription sqlite failed email=%s", email)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO subscriptions
                            (email, payment_provider, lemonsqueezy_customer_id, lemonsqueezy_subscription_id,
                             bmc_supporter_id, bmc_membership_id, paypal_order_id, paypal_payer_id, status,
                             trial_start, trial_end, current_period_start, current_period_end,
                             cancel_at_period_end, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                            payment_provider = EXCLUDED.payment_provider,
                            lemonsqueezy_customer_id = COALESCE(EXCLUDED.lemonsqueezy_customer_id, subscriptions.lemonsqueezy_customer_id),
                            lemonsqueezy_subscription_id = COALESCE(EXCLUDED.lemonsqueezy_subscription_id, subscriptions.lemonsqueezy_subscription_id),
                            bmc_supporter_id = COALESCE(EXCLUDED.bmc_supporter_id, subscriptions.bmc_supporter_id),
                            bmc_membership_id = COALESCE(EXCLUDED.bmc_membership_id, subscriptions.bmc_membership_id),
                            paypal_order_id = COALESCE(EXCLUDED.paypal_order_id, subscriptions.paypal_order_id),
                            paypal_payer_id = COALESCE(EXCLUDED.paypal_payer_id, subscriptions.paypal_payer_id),
                            status = EXCLUDED.status,
                            trial_start = COALESCE(EXCLUDED.trial_start, subscriptions.trial_start),
                            trial_end = COALESCE(EXCLUDED.trial_end, subscriptions.trial_end),
                            current_period_start = COALESCE(EXCLUDED.current_period_start, subscriptions.current_period_start),
                            current_period_end = COALESCE(EXCLUDED.current_period_end, subscriptions.current_period_end),
                            cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (
                            email, payment_provider, lemonsqueezy_customer_id, lemonsqueezy_subscription_id,
                            bmc_supporter_id, bmc_membership_id, paypal_order_id, paypal_payer_id, status,
                            trial_start, trial_end, current_period_start, current_period_end,
                            bool(cancel_at_period_end), now, now,
                        ),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write upsert_subscription pg failed email=%s", email)

    _record_obs("upsert_subscription", sqlite_ok, pg_ok)
    logger.info(
        "Subscription upserted: %s provider=%s status=%s sqlite_ok=%s pg_ok=%s",
        email, payment_provider, status, sqlite_ok, pg_ok,
    )
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for upsert_subscription email={email}")


def get_subscription(email: str) -> dict | None:
    """Get subscription info for an email. PG-only when configured; sqlite fallback for dev."""
    normalized = email.lower().strip()
    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM subscriptions WHERE email = %s", (normalized,))
                row = cur.fetchone()
        return _row_to_dict(row)

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE email = ?", (normalized,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def get_subscription_by_customer(lemonsqueezy_customer_id: str) -> dict | None:
    """Look up subscription by LemonSqueezy customer ID."""
    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE lemonsqueezy_customer_id = %s",
                    (lemonsqueezy_customer_id,),
                )
                row = cur.fetchone()
        return _row_to_dict(row)

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE lemonsqueezy_customer_id = ?",
            (lemonsqueezy_customer_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def save_paypal_checkout_email(order_id: str, checkout_email: str):
    """Persist the checkout email chosen before PayPal approval (dual-write)."""
    now = datetime.now(timezone.utc).isoformat()
    normalized_email = checkout_email.lower().strip()
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
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
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write save_paypal_checkout_email sqlite failed order_id=%s", order_id)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO paypal_checkout_context
                            (order_id, checkout_email, created_at, updated_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (order_id) DO UPDATE SET
                            checkout_email = EXCLUDED.checkout_email,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (order_id, normalized_email, now, now),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write save_paypal_checkout_email pg failed order_id=%s", order_id)

    _record_obs("save_paypal_checkout_email", sqlite_ok, pg_ok)
    logger.info(
        "Saved PayPal checkout email: order_id=%s checkout_email=%s sqlite_ok=%s pg_ok=%s",
        order_id, normalized_email, sqlite_ok, pg_ok,
    )
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for save_paypal_checkout_email order_id={order_id}")


def get_paypal_checkout_email(order_id: str) -> str | None:
    """Return the saved checkout email for a PayPal order, if present."""
    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT checkout_email FROM paypal_checkout_context WHERE order_id = %s",
                    (order_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return row["checkout_email"]

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
    """Attach capture audit data to a stored PayPal checkout context (dual-write)."""
    now = datetime.now(timezone.utc).isoformat()
    normalized_payer_email = payer_email.lower().strip() if payer_email else None
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
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
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write record_paypal_capture sqlite failed order_id=%s", order_id)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    # Try UPDATE first; if 0 rows affected and we have an email, INSERT.
                    cur.execute(
                        """
                        UPDATE paypal_checkout_context
                        SET payer_email = COALESCE(%s, payer_email),
                            capture_id = COALESCE(%s, capture_id),
                            updated_at = %s
                        WHERE order_id = %s
                        """,
                        (normalized_payer_email, capture_id, now, order_id),
                    )
                    rows_updated = cur.rowcount

                    if rows_updated == 0 and normalized_payer_email:
                        cur.execute(
                            """
                            INSERT INTO paypal_checkout_context
                                (order_id, checkout_email, payer_email, capture_id, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (order_id) DO NOTHING
                            """,
                            (
                                order_id,
                                normalized_payer_email,
                                normalized_payer_email,
                                capture_id,
                                now,
                                now,
                            ),
                        )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write record_paypal_capture pg failed order_id=%s", order_id)

    _record_obs("record_paypal_capture", sqlite_ok, pg_ok)
    logger.info(
        "Recorded PayPal capture audit: order_id=%s capture_id=%s payer_email=%s sqlite_ok=%s pg_ok=%s",
        order_id, capture_id, normalized_payer_email, sqlite_ok, pg_ok,
    )
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for record_paypal_capture order_id={order_id}")


def is_user_active(email: str) -> bool:
    """Check if a user has an active subscription or is in trial."""
    sub = get_subscription(email)
    if sub is None:
        return False
    return sub["status"] in ("trialing", "active", "on_trial")


def cancel_subscription_db(email: str):
    """Mark subscription as pending cancellation (cancel at period end). Dual-write."""
    now = datetime.now(timezone.utc).isoformat()
    email = email.lower().strip()
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE subscriptions SET cancel_at_period_end = 1, updated_at = ? WHERE email = ?",
                (now, email),
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write cancel_subscription_db sqlite failed email=%s", email)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE subscriptions SET cancel_at_period_end = TRUE, updated_at = %s WHERE email = %s",
                        (now, email),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write cancel_subscription_db pg failed email=%s", email)

    _record_obs("cancel_subscription_db", sqlite_ok, pg_ok)
    logger.info(
        "Subscription cancel requested: %s sqlite_ok=%s pg_ok=%s",
        email, sqlite_ok, pg_ok,
    )
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for cancel_subscription_db email={email}")


# --- Download tracking ---

FREE_DAILY_LIMIT = 3


def get_download_count(ip: str, date_str: str) -> int:
    """Get number of downloads for an IP on a given date."""
    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM downloads WHERE ip = %s AND download_date = %s",
                    (ip, date_str),
                )
                row = cur.fetchone()
        return int(row["cnt"]) if row and row.get("cnt") is not None else 0

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM downloads WHERE ip = ? AND download_date = ?",
            (ip, date_str),
        ).fetchone()
        return row["cnt"] if row else 0


def record_download(ip: str, task_id: str):
    """Record a download event (dual-write)."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    iso = now.isoformat()
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO downloads (ip, download_date, task_id, created_at) VALUES (?, ?, ?, ?)",
                (ip, date_str, task_id, iso),
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write record_download sqlite failed ip=%s task_id=%s", ip, task_id)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO downloads (ip, download_date, task_id, created_at) VALUES (%s, %s, %s, %s)",
                        (ip, date_str, task_id, iso),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write record_download pg failed ip=%s task_id=%s", ip, task_id)

    _record_obs("record_download", sqlite_ok, pg_ok)
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for record_download ip={ip}")


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
    """Persist a processing completion event (dual-write)."""
    now = datetime.now(timezone.utc).isoformat()
    lp = _normalize_attr(landing_page)
    cs = _normalize_attr(cta_slot)
    ev = _normalize_attr(entry_variant)
    src = _normalize_attr(checkout_source)
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processing_events
                (task_id, mode, landing_page, cta_slot, entry_variant, checkout_source, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, mode, lp, cs, ev, src, now),
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write record_processing_complete sqlite failed task_id=%s", task_id)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO processing_events
                            (task_id, mode, landing_page, cta_slot, entry_variant, checkout_source, completed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (task_id) DO UPDATE SET
                            mode = EXCLUDED.mode,
                            landing_page = EXCLUDED.landing_page,
                            cta_slot = EXCLUDED.cta_slot,
                            entry_variant = EXCLUDED.entry_variant,
                            checkout_source = EXCLUDED.checkout_source,
                            completed_at = EXCLUDED.completed_at
                        """,
                        (task_id, mode, lp, cs, ev, src, now),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write record_processing_complete pg failed task_id=%s", task_id)

    _record_obs("record_processing_complete", sqlite_ok, pg_ok)
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for record_processing_complete task_id={task_id}")


def get_processing_complete_metrics(hours: int = 24) -> dict:
    """Return completion count and mode split in the trailing N hours."""
    now, start_iso = _metrics_window(hours)

    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM processing_events WHERE completed_at >= %s",
                    (start_iso,),
                )
                total_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT mode, COUNT(*) AS cnt
                    FROM processing_events
                    WHERE completed_at >= %s
                    GROUP BY mode
                    ORDER BY cnt DESC
                    """,
                    (start_iso,),
                )
                mode_rows = cur.fetchall()
        return {
            "count": int(total_row["cnt"]) if total_row else 0,
            "by_mode": {row["mode"]: int(row["cnt"]) for row in mode_rows},
            "storage_backend": get_database_backend(),
            "window_hours": max(1, hours),
            "generated_at": now.isoformat(),
        }

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
        "storage_backend": get_database_backend(),
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
    """Persist a server-side payment initiation event (dual-write with attribution)."""
    now = datetime.now(timezone.utc).isoformat()
    normalized_email = email.lower().strip()
    lp = _normalize_attr(landing_page)
    cs = _normalize_attr(cta_slot)
    ev = _normalize_attr(entry_variant)
    src = _normalize_attr(checkout_source)
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO payment_initiations
                (order_id, payment_provider, email, landing_page, cta_slot, entry_variant, checkout_source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, payment_provider, normalized_email, lp, cs, ev, src, now),
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write record_payment_initiation sqlite failed order_id=%s", order_id)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO payment_initiations
                            (order_id, payment_provider, email, landing_page, cta_slot, entry_variant, checkout_source, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (order_id) DO NOTHING
                        """,
                        (order_id, payment_provider, normalized_email, lp, cs, ev, src, now),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write record_payment_initiation pg failed order_id=%s", order_id)

    _record_obs("record_payment_initiation", sqlite_ok, pg_ok)
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for record_payment_initiation order_id={order_id}")


def get_payment_initiation_metrics(hours: int = 24) -> dict:
    """Return payment initiation count and provider split in trailing N hours. PG-only when configured."""
    now, start_iso = _metrics_window(hours)

    if _use_postgres():
        with _connect_postgres() as conn:
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
            "storage_backend": get_database_backend(),
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
        "storage_backend": get_database_backend(),
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
    """Return exact payment/processing counts for one funnel tuple. PG-only when configured."""
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

    if _use_postgres():
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM payment_initiations
                    WHERE created_at >= %s
                      AND landing_page = %s
                      AND cta_slot = %s
                      AND entry_variant = %s
                      AND checkout_source = %s
                    """,
                    tuple_params,
                )
                payment_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT payment_provider, COUNT(*) AS cnt
                    FROM payment_initiations
                    WHERE created_at >= %s
                      AND landing_page = %s
                      AND cta_slot = %s
                      AND entry_variant = %s
                      AND checkout_source = %s
                    GROUP BY payment_provider
                    ORDER BY cnt DESC
                    """,
                    tuple_params,
                )
                payment_provider_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM processing_events
                    WHERE completed_at >= %s
                      AND landing_page = %s
                      AND cta_slot = %s
                      AND entry_variant = %s
                      AND checkout_source = %s
                    """,
                    tuple_params,
                )
                processing_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT mode, COUNT(*) AS cnt
                    FROM processing_events
                    WHERE completed_at >= %s
                      AND landing_page = %s
                      AND cta_slot = %s
                      AND entry_variant = %s
                      AND checkout_source = %s
                    GROUP BY mode
                    ORDER BY cnt DESC
                    """,
                    tuple_params,
                )
                processing_mode_rows = cur.fetchall()

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
            "storage_backend": get_database_backend(),
            "window_hours": max(1, hours),
            "generated_at": now.isoformat(),
        }

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
        "storage_backend": get_database_backend(),
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
    """Persist a server-side payment success event (dual-write)."""
    success_key = capture_id or (f"order:{order_id}" if order_id else None)
    if success_key is None:
        raise ValueError("record_payment_success requires capture_id or order_id")

    occurred_at = completed_at or datetime.now(timezone.utc).isoformat()
    normalized_email = email.lower().strip()
    sqlite_ok = False
    pg_ok = not _use_postgres()

    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO payment_successes
                (success_key, capture_id, order_id, payment_provider, email, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (success_key, capture_id, order_id, payment_provider, normalized_email, occurred_at),
            )
        sqlite_ok = True
    except Exception:
        logger.exception("dual_write record_payment_success sqlite failed key=%s", success_key)

    if _use_postgres():
        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO payment_successes
                            (success_key, capture_id, order_id, payment_provider, email, completed_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (success_key) DO NOTHING
                        """,
                        (success_key, capture_id, order_id, payment_provider, normalized_email, occurred_at),
                    )
                conn.commit()
            pg_ok = True
        except Exception:
            logger.exception("dual_write record_payment_success pg failed key=%s", success_key)

    _record_obs("record_payment_success", sqlite_ok, pg_ok)
    if not sqlite_ok:
        raise RuntimeError(f"sqlite write failed for record_payment_success key={success_key}")


def get_payment_success_metrics(hours: int = 24) -> dict:
    """Return payment success count and provider split in trailing N hours. PG-only when configured."""
    now = datetime.now(timezone.utc)
    start = now.timestamp() - max(1, hours) * 3600
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()

    if _use_postgres():
        with _connect_postgres() as conn:
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
            "storage_backend": get_database_backend(),
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
        "storage_backend": get_database_backend(),
        "window_hours": max(1, hours),
        "generated_at": now.isoformat(),
    }
