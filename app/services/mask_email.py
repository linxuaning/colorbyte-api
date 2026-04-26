"""
Mask post-purchase thank-you email service.

Founder-driven feature (2026-04-26 GO): every Dodo paid order triggers a
sincere founder-voice email 5 minutes after purchase, inviting the customer
to reply with feedback or a redo request.

Pipeline:
  webhook (payment.succeeded) → enqueue_mask_email() → mask_email_queue (PG)
  GitHub Actions cron (every 5 min) → POST /api/internal/mask-email-poll
  → process_due_emails() → Resend API (User-Agent set; CF 1010 mitigated)
  → UPDATE sent_at / status

Hard guards (per feedback_no_fake_emails_hard_rule + payment_flow_freeze):
  - PG mode only (silent skip in sqlite-fallback rollback mode).
  - Dodo provider only (no PayPal / LemonSqueezy / seed).
  - Subscription status must be active.
  - Owner email (alert_email_to) is excluded — we don't email ourselves.
  - Feature kill switch: settings.mask_email_enabled = False stops all sends.
  - DB-level UNIQUE (email, payment_id) prevents webhook-retry double-fire.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services.database import _connect_postgres, _use_postgres

logger = logging.getLogger("artimagehub.mask_email")

# --- Constants ---

MASK_DELAY_SECONDS = 5 * 60  # 5 minutes after purchase
MASK_SENDER = "Mask <support@artimagehub.com>"
MASK_REPLY_TO = "support@artimagehub.com"
MASK_USER_AGENT = "artimagehub-backend/1.0"  # CF 1010 mitigation per 649a6a4
MASK_SUBJECT = "Thanks for trying artimagehub — Mask here"
MASK_MAX_ATTEMPTS = 5
MASK_POLL_BATCH_LIMIT = 50

MASK_BODY_TEMPLATE = """Hi {first_name_or_email},

I'm Mask, the founder of artimagehub. I noticed you just tried our {tool_name} tool — thank you for the support. For a small team like ours, that genuinely matters; it's how we keep going.

If you're not happy with the result, just reply to this email with the original photo and a quick note about what you'd like instead. I'll personally take a look and have our team redo it (free of charge), with a response within 24 hours.

Your feedback directly shapes how we improve.

— Mask
artimagehub.com
"""

# Map landing_page → human-readable tool name for the email body.
_TOOL_NAME_MAP = {
    "old-photo-restoration": "old photo restoration",
    "photo-colorizer": "photo colorization",
    "photo-enhancer": "photo enhancement",
    "restore-old-photos-free": "old photo restoration",
    "photo-restoration-service": "photo restoration",
    "best-photo-restoration-software": "photo restoration",
    "vs-remini": "photo restoration",
    "vs-photoshop-restoration": "photo restoration",
}
_TOOL_NAME_DEFAULT = "photo restoration"


# --- Public API ---


def landing_to_tool_name(landing_page: str | None) -> str:
    """Derive a human-readable tool name from a landing-page path.

    `/old-photo-restoration` and `/es/old-photo-restoration` both map to
    "old photo restoration". Unknown paths fall back to a generic phrase.
    """
    if not landing_page:
        return _TOOL_NAME_DEFAULT
    # Strip leading slash and locale prefix (e.g., "es/old-photo-restoration").
    parts = [p for p in landing_page.strip("/").split("/") if p]
    if not parts:
        return _TOOL_NAME_DEFAULT
    # If first segment looks like a 2-3 char locale (es, fr, ja, pt-BR),
    # take the next segment as the slug.
    candidate = parts[-1]
    return _TOOL_NAME_MAP.get(candidate, _TOOL_NAME_DEFAULT)


def extract_first_name(email: str, customer_name: str | None = None) -> str:
    """Pick a first-name token for greeting.

    Prefer an explicit customer_name (first whitespace-separated word).
    Fallback to the email local-part (before @).
    """
    if customer_name:
        first = customer_name.strip().split()[0] if customer_name.strip() else ""
        if first:
            return first
    if email and "@" in email:
        local = email.split("@", 1)[0].strip()
        if local:
            return local
    return "there"


def should_enqueue_mask_email(
    *,
    email: str,
    payment_provider: str,
    subscription_status: str,
) -> tuple[bool, str | None]:
    """Apply all four hard guards. Returns (allowed, reason_if_blocked)."""
    settings = get_settings()

    if not settings.mask_email_enabled:
        return False, "kill switch off (MASK_EMAIL_ENABLED=false)"

    if payment_provider != "dodo":
        return False, f"provider not dodo (got '{payment_provider}')"

    if subscription_status != "active":
        return False, f"status not active (got '{subscription_status}')"

    owner_email = (settings.alert_email_to or "").strip().lower()
    if owner_email and email.strip().lower() == owner_email:
        return False, "owner email excluded"

    return True, None


def enqueue_mask_email(
    *,
    email: str,
    payment_id: str,
    payment_provider: str,
    subscription_status: str,
    landing_page: str | None = None,
    customer_name: str | None = None,
) -> str:
    """Insert a row into mask_email_queue; idempotent on (email, payment_id).

    Returns a short status code: "enqueued", "skipped:<reason>", or
    "skipped:not_postgres" (rollback-mode safety: queue table only exists in PG).
    Never raises — failures are logged and treated as best-effort.
    """
    if not _use_postgres():
        logger.info("mask_email enqueue skipped: sqlite mode (rollback safe)")
        return "skipped:not_postgres"

    if not email or not payment_id:
        logger.warning("mask_email enqueue skipped: missing email or payment_id")
        return "skipped:missing_id"

    allowed, reason = should_enqueue_mask_email(
        email=email,
        payment_provider=payment_provider,
        subscription_status=subscription_status,
    )
    if not allowed:
        logger.info("mask_email enqueue skipped: %s (email=%s payment_id=%s)", reason, email, payment_id)
        return f"skipped:{reason}"

    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=MASK_DELAY_SECONDS)
    first_name = extract_first_name(email, customer_name)
    tool_name = landing_to_tool_name(landing_page)

    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mask_email_queue
                        (email, payment_id, first_name, tool_name, scheduled_at, status)
                    VALUES (%s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (email, payment_id) DO NOTHING
                    """,
                    (email.strip().lower(), payment_id, first_name, tool_name, scheduled_at),
                )
                inserted = cur.rowcount
            conn.commit()
        if inserted:
            logger.info(
                "mask_email enqueued: email=%s payment_id=%s tool=%s scheduled_at=%s",
                email, payment_id, tool_name, scheduled_at.isoformat(),
            )
            return "enqueued"
        logger.info("mask_email enqueue dedup hit: email=%s payment_id=%s", email, payment_id)
        return "skipped:duplicate"
    except Exception:
        logger.exception("mask_email enqueue failed: email=%s payment_id=%s", email, payment_id)
        return "skipped:db_error"


def process_due_emails() -> dict:
    """Send all due, unsent mask emails. Called by /api/internal/mask-email-poll.

    Returns a summary dict for the endpoint response. Never raises; per-row
    failures are recorded in last_error and do not abort the batch.
    """
    if not _use_postgres():
        return {"backend": "sqlite", "due": 0, "sent": 0, "failed": 0, "skipped": 0}

    settings = get_settings()
    if not settings.mask_email_enabled:
        return {"backend": "postgres", "due": 0, "sent": 0, "failed": 0, "skipped": 0, "kill_switch": "off"}

    if not settings.resend_api_key:
        logger.warning("mask_email poll: RESEND_API_KEY not configured; nothing sent")
        return {"backend": "postgres", "due": 0, "sent": 0, "failed": 0, "skipped": 0, "resend": "missing"}

    sent = 0
    failed = 0
    skipped = 0
    due_rows: list[dict] = []

    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, payment_id, first_name, tool_name, attempt_count
                    FROM mask_email_queue
                    WHERE sent_at IS NULL
                      AND status = 'pending'
                      AND scheduled_at <= NOW()
                    ORDER BY scheduled_at ASC
                    LIMIT %s
                    """,
                    (MASK_POLL_BATCH_LIMIT,),
                )
                due_rows = list(cur.fetchall())
    except Exception:
        logger.exception("mask_email poll: failed to fetch due rows")
        return {"backend": "postgres", "due": 0, "sent": 0, "failed": 0, "skipped": 0, "error": "fetch_failed"}

    for row in due_rows:
        row_id = row["id"]
        email = row["email"]
        first_name = row.get("first_name") or extract_first_name(email)
        tool_name = row.get("tool_name") or _TOOL_NAME_DEFAULT
        attempts = int(row.get("attempt_count") or 0)

        if attempts >= MASK_MAX_ATTEMPTS:
            _mark_failed(row_id, "max_attempts_exceeded")
            skipped += 1
            continue

        body = MASK_BODY_TEMPLATE.format(
            first_name_or_email=first_name,
            tool_name=tool_name,
        )
        try:
            _send_via_resend(
                api_key=settings.resend_api_key,
                to_addr=email,
                subject=MASK_SUBJECT,
                body=body,
            )
            _mark_sent(row_id)
            sent += 1
        except Exception as exc:
            _mark_attempt_failed(row_id, attempts + 1, str(exc)[:500])
            logger.exception("mask_email send failed: id=%s email=%s", row_id, email)
            failed += 1

    return {
        "backend": "postgres",
        "due": len(due_rows),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }


# --- Internal helpers ---


def _send_via_resend(*, api_key: str, to_addr: str, subject: str, body: str) -> None:
    """POST to Resend with explicit User-Agent (CF 1010 mitigation)."""
    payload = json.dumps({
        "from": MASK_SENDER,
        "to": [to_addr],
        "reply_to": MASK_REPLY_TO,
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": MASK_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Resend HTTP {resp.status}")


def _mark_sent(row_id: int) -> None:
    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mask_email_queue
                    SET sent_at = NOW(),
                        status = 'sent',
                        attempt_count = attempt_count + 1,
                        last_error = NULL
                    WHERE id = %s
                    """,
                    (row_id,),
                )
            conn.commit()
    except Exception:
        logger.exception("mask_email mark_sent failed: id=%s", row_id)


def _mark_attempt_failed(row_id: int, new_attempts: int, error: str) -> None:
    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mask_email_queue
                    SET attempt_count = %s,
                        last_error = %s
                    WHERE id = %s
                    """,
                    (new_attempts, error, row_id),
                )
            conn.commit()
    except Exception:
        logger.exception("mask_email mark_attempt_failed update failed: id=%s", row_id)


def _mark_failed(row_id: int, error: str) -> None:
    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mask_email_queue
                    SET status = 'failed',
                        last_error = %s
                    WHERE id = %s
                    """,
                    (error, row_id),
                )
            conn.commit()
    except Exception:
        logger.exception("mask_email mark_failed update failed: id=%s", row_id)
