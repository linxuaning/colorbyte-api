"""
Abandoned-cart recovery email service (T209, founder-authorized 2026-07-05).

Pipeline (mirrors mask_email.py's queue/poll shape):
  GitHub Actions cron (daily) -> POST /api/internal/abandoned-cart-poll
  -> discover_abandoned_carts(): scan Dodo requires_payment_method payments,
     filter to the artimagehub product + real customers, INSERT one row per
     email (dedup: UNIQUE(email), so a customer is enqueued at most once ever
     no matter how many times they abandon checkout)
  -> process_due_reminders(): send exactly one email per un-sent row via
     Resend, mark sent_at.

Hard guards (per T209 task-card authorized scope + feedback_no_fake_emails_hard_rule):
  - Feature kill switch: settings.abandoned_cart_email_enabled = False stops
    all SENDING (discovery/enqueue still runs so the backlog is ready the
    moment the switch flips on after copy review).
  - Filtered to product_id == settings.dodo_payments_product_id (not amount —
    amount varies with tax/currency across this shared Dodo account).
  - Self-test emails excluded (locked list + linxuaning9@gmail.com, a
    suspected variant flagged for founder confirmation — see ABANDONED_CART_SELF_TEST_EMAILS).
  - DB-level UNIQUE(email) prevents ever emailing the same customer twice,
    even across multiple abandoned attempts or repeated cron runs.
  - Email content states only real facts (their checkout, their real link) —
    no fabricated urgency/scarcity/claims.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services.database import _connect_postgres, _use_postgres

logger = logging.getLogger("artimagehub.abandoned_cart")

DODO_API_BASE = "https://live.dodopayments.com"

# Locked self-test list (reference_artimagehub_self_test_filter.md) plus
# linxuaning9@gmail.com — appears 3x in requires_payment_method scans over
# 5 weeks, never converts, one character off the known founder test email
# linxuaning98@gmail.com. Excluded pending founder confirmation; safer to
# skip a possible test address than send a confusing internal email.
SELF_TEST_EMAILS = {
    "linxuaning98@gmail.com",
    "181420491@qq.com",
    "linxuaning@qq.com",
    "linxuaning9@gmail.com",
}

# Discovery window: how far back to scan for requires_payment_method records.
# Bounded (not "all history") so a first-run/backlog does not suddenly email
# customers about a checkout they abandoned months ago, which would read as
# stale/confusing rather than a timely reminder. Real volume is ~14/quarter
# (per T209 task background), so 7 days is generous headroom for daily cron.
DISCOVERY_WINDOW_DAYS = 7
DISCOVERY_MAX_PAGES = 10
DISCOVERY_PAGE_SIZE = 100

ABANDONED_CART_SENDER = "artimagehub <support@artimagehub.com>"
ABANDONED_CART_USER_AGENT = "artimagehub-backend/1.0"
ABANDONED_CART_MAX_ATTEMPTS = 5
ABANDONED_CART_POLL_BATCH_LIMIT = 50

# Plain, honest, transactional copy — no fabricated urgency, no fake scarcity.
# States only real facts: they started checkout, it didn't complete, here is
# the real link if they still want to finish it.
ABANDONED_CART_SUBJECT = "Your photo restoration checkout didn't go through"
ABANDONED_CART_BODY_TEMPLATE = """Hi,

We noticed you started checking out for {tool_name} on artimagehub but the payment didn't go through. Your photo hasn't been charged.

If you'd still like to complete it, here's your checkout link:
{checkout_url}

If this wasn't you, or you've changed your mind, no action is needed — you won't hear from us about this again.

— artimagehub
support@artimagehub.com
"""

_TOOL_NAME_MAP = {
    "restoration": "old photo restoration",
    "denoising": "photo denoising",
    "deblurring": "photo deblurring",
    "jpeg-fix": "JPEG artifact removal",
}
_TOOL_NAME_DEFAULT = "photo restoration"


def _feature_key_to_tool_name(feature_key: str | None) -> str:
    return _TOOL_NAME_MAP.get((feature_key or "").strip().lower(), _TOOL_NAME_DEFAULT)


def _dodo_get(path: str, key: str) -> dict:
    req = urllib.request.Request(
        f"{DODO_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": ABANDONED_CART_USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def discover_abandoned_carts() -> dict:
    """Scan Dodo for requires_payment_method artimagehub checkouts and enqueue
    one reminder row per new (never-before-seen) customer email.

    Returns a summary dict. Never raises — failures are logged and returned
    as an error field so the caller (the poll endpoint) always gets a
    well-formed response.
    """
    settings = get_settings()
    if not _use_postgres():
        return {"scanned": 0, "candidates": 0, "enqueued": 0, "backend": "sqlite"}

    key = settings.dodo_payments_api_key
    product_id = settings.dodo_payments_product_id
    if not key or not product_id:
        logger.warning("abandoned_cart discovery skipped: Dodo key or product_id not configured")
        return {"scanned": 0, "candidates": 0, "enqueued": 0, "error": "dodo_not_configured"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=DISCOVERY_WINDOW_DAYS)
    candidates = []
    try:
        for page in range(DISCOVERY_MAX_PAGES):
            data = _dodo_get(f"/payments?page_size={DISCOVERY_PAGE_SIZE}&page_number={page}", key)
            items = data.get("items") or []
            if not items:
                break
            reached_cutoff = False
            for p in items:
                created = _parse_dt(p.get("created_at", ""))
                if created is None:
                    continue
                if created < cutoff:
                    reached_cutoff = True
                    break
                if str(p.get("status", "")).lower() == "requires_payment_method":
                    candidates.append(p)
            if reached_cutoff or len(items) < DISCOVERY_PAGE_SIZE:
                break
    except Exception as e:
        logger.exception("abandoned_cart discovery: Dodo list fetch failed")
        return {"scanned": 0, "candidates": 0, "enqueued": 0, "error": f"dodo_fetch_failed: {e}"}

    enqueued = 0
    excluded_self_test = 0
    excluded_foreign_product = 0

    for p in candidates:
        email = (p.get("customer", {}).get("email") or "").strip().lower()
        if not email:
            continue
        if email in SELF_TEST_EMAILS:
            excluded_self_test += 1
            continue
        try:
            detail = _dodo_get(f"/payments/{p['payment_id']}", key)
        except Exception:
            continue
        product_ids = {item.get("product_id") for item in (detail.get("product_cart") or [])}
        if product_id not in product_ids:
            excluded_foreign_product += 1
            continue

        md = detail.get("metadata") or {}
        checkout_url = detail.get("payment_link") or ""
        abandoned_at = _parse_dt(detail.get("created_at", "")) or datetime.now(timezone.utc)

        try:
            with _connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO abandoned_cart_reminders
                            (email, dodo_payment_id, checkout_url, landing_page, feature_key, abandoned_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO NOTHING
                        """,
                        (
                            email, detail.get("payment_id"), checkout_url,
                            md.get("landing_page"), md.get("feature_key"), abandoned_at,
                        ),
                    )
                    if cur.rowcount:
                        enqueued += 1
                conn.commit()
        except Exception:
            logger.exception("abandoned_cart enqueue failed: email=%s", email)

    return {
        "scanned": len(candidates),
        "candidates": len(candidates),
        "enqueued": enqueued,
        "excluded_self_test": excluded_self_test,
        "excluded_foreign_product": excluded_foreign_product,
        "backend": "postgres",
    }


def process_due_reminders() -> dict:
    """Send all pending, unsent abandoned-cart reminders. Called by
    /api/internal/abandoned-cart-poll. Never raises; per-row failures are
    recorded and do not abort the batch."""
    settings = get_settings()
    if not _use_postgres():
        return {"due": 0, "sent": 0, "failed": 0, "skipped": 0, "backend": "sqlite"}

    if not settings.abandoned_cart_email_enabled:
        return {"due": 0, "sent": 0, "failed": 0, "skipped": 0, "backend": "postgres", "kill_switch": "off"}

    if not settings.resend_api_key:
        logger.warning("abandoned_cart poll: RESEND_API_KEY not configured; nothing sent")
        return {"due": 0, "sent": 0, "failed": 0, "skipped": 0, "backend": "postgres", "resend": "missing"}

    sent = 0
    failed = 0
    skipped = 0
    due_rows: list[dict] = []

    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, checkout_url, feature_key, attempt_count
                    FROM abandoned_cart_reminders
                    WHERE sent_at IS NULL AND status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (ABANDONED_CART_POLL_BATCH_LIMIT,),
                )
                due_rows = list(cur.fetchall())
    except Exception:
        logger.exception("abandoned_cart poll: failed to fetch due rows")
        return {"due": 0, "sent": 0, "failed": 0, "skipped": 0, "backend": "postgres", "error": "fetch_failed"}

    for row in due_rows:
        row_id = row["id"]
        email = row["email"]
        checkout_url = row.get("checkout_url") or ""
        tool_name = _feature_key_to_tool_name(row.get("feature_key"))
        attempts = int(row.get("attempt_count") or 0)

        if not checkout_url:
            _mark_failed(row_id, "missing_checkout_url")
            skipped += 1
            continue
        if attempts >= ABANDONED_CART_MAX_ATTEMPTS:
            _mark_failed(row_id, "max_attempts_exceeded")
            skipped += 1
            continue

        body = ABANDONED_CART_BODY_TEMPLATE.format(tool_name=tool_name, checkout_url=checkout_url)
        try:
            _send_via_resend(
                api_key=settings.resend_api_key,
                to_addr=email,
                subject=ABANDONED_CART_SUBJECT,
                body=body,
            )
            _mark_sent(row_id)
            sent += 1
        except Exception as exc:
            _mark_attempt_failed(row_id, attempts + 1, str(exc)[:500])
            logger.exception("abandoned_cart send failed: id=%s email=%s", row_id, email)
            failed += 1

    return {"due": len(due_rows), "sent": sent, "failed": failed, "skipped": skipped, "backend": "postgres"}


def _send_via_resend(*, api_key: str, to_addr: str, subject: str, body: str) -> None:
    payload = json.dumps({
        "from": ABANDONED_CART_SENDER,
        "to": [to_addr],
        "reply_to": "support@artimagehub.com",
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": ABANDONED_CART_USER_AGENT,
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
                    UPDATE abandoned_cart_reminders
                    SET sent_at = NOW(), status = 'sent',
                        attempt_count = attempt_count + 1, last_error = NULL
                    WHERE id = %s
                    """,
                    (row_id,),
                )
            conn.commit()
    except Exception:
        logger.exception("abandoned_cart mark_sent failed: id=%s", row_id)


def _mark_attempt_failed(row_id: int, new_attempts: int, error: str) -> None:
    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE abandoned_cart_reminders SET attempt_count = %s, last_error = %s WHERE id = %s",
                    (new_attempts, error, row_id),
                )
            conn.commit()
    except Exception:
        logger.exception("abandoned_cart mark_attempt_failed update failed: id=%s", row_id)


def _mark_failed(row_id: int, error: str) -> None:
    try:
        with _connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE abandoned_cart_reminders SET status = 'failed', last_error = %s WHERE id = %s",
                    (error, row_id),
                )
            conn.commit()
    except Exception:
        logger.exception("abandoned_cart mark_failed update failed: id=%s", row_id)
