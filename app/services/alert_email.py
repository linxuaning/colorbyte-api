"""
Lightweight payment-failure alert emailer.
Sends alerts to the configured address when payment processing fails.
Dedup: same alert_key fires at most once per 60 minutes (in-process window).
Primary: Resend HTTP API (if resend_api_key is set).
Fallback: SMTP (if alert_smtp_user + alert_smtp_password are set).
"""
from __future__ import annotations

import logging
import smtplib
import threading
import time
import urllib.request
import urllib.error
import json as _json
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger("artimagehub.alert")

_dedup_lock = threading.Lock()
_dedup_store: dict[str, float] = {}
_DEDUP_WINDOW_SECS = 3600  # 60 minutes


def _is_duplicate(alert_key: str) -> bool:
    with _dedup_lock:
        now = time.monotonic()
        last_sent = _dedup_store.get(alert_key)
        if last_sent is not None and (now - last_sent) < _DEDUP_WINDOW_SECS:
            return True
        _dedup_store[alert_key] = now
        return False


def send_payment_failure_alert(
    *,
    alert_type: str,
    payment_id: str | None = None,
    customer_email: str | None = None,
    error_msg: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Send a payment failure alert email (best-effort, never raises).

    alert_type examples:
      - "checkout_create_failed"
      - "payment_failed_webhook"
      - "webhook_sig_failed"

    Dedup key: alert_type + payment_id (falls back to customer_email or "unknown").
    Same key suppressed for 60 minutes.
    """
    from app.config import get_settings

    settings = get_settings()

    has_resend = bool(settings.resend_api_key)
    has_smtp = bool(settings.alert_smtp_user and settings.alert_smtp_password)

    if not has_resend and not has_smtp:
        logger.warning(
            "Alert email skipped: neither RESEND_API_KEY nor SMTP credentials configured "
            "(alert_type=%s payment_id=%s)",
            alert_type,
            payment_id or customer_email or "unknown",
        )
        return

    dedup_key = f"{alert_type}:{payment_id or customer_email or 'unknown'}"
    if _is_duplicate(dedup_key):
        logger.info("Alert suppressed by dedup (60 min window): %s", dedup_key)
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = (
        f"[Alert] Payment Failure: {alert_type} | "
        f"{payment_id or customer_email or 'unknown'}"
    )

    lines = [
        "Payment failure alert from artimagehub.com",
        "",
        f"Time:           {now_str}",
        f"Alert Type:     {alert_type}",
        f"Payment ID:     {payment_id or '-'}",
        f"Customer Email: {customer_email or '-'}",
        f"Error:          {error_msg or '-'}",
    ]
    if extra:
        lines.append("")
        lines.append("Details:")
        for k, v in extra.items():
            lines.append(f"  {k}: {v}")

    body = "\n".join(lines)
    to_addr = settings.alert_email_to

    # --- Primary: Resend HTTP API ---
    if has_resend:
        _send_via_resend(
            api_key=settings.resend_api_key,
            from_addr=settings.resend_from_email,
            to_addr=to_addr,
            subject=subject,
            body=body,
            alert_type=alert_type,
            dedup_key=dedup_key,
        )
        return

    # --- Fallback: SMTP ---
    _send_via_smtp(
        settings=settings,
        to_addr=to_addr,
        subject=subject,
        body=body,
        alert_type=alert_type,
        dedup_key=dedup_key,
    )


def _send_via_resend(
    *,
    api_key: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    alert_type: str,
    dedup_key: str,
) -> None:
    payload = _json.dumps({
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": body,
    }).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(
                "Alert email sent via Resend: alert_type=%s dedup_key=%s to=%s status=%s",
                alert_type,
                dedup_key,
                to_addr,
                resp.status,
            )
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        logger.error(
            "Resend alert failed HTTP %s: %s (alert_type=%s dedup_key=%s)",
            exc.code,
            err_body,
            alert_type,
            dedup_key,
        )
    except Exception as exc:
        logger.error(
            "Resend alert exception: %s (alert_type=%s dedup_key=%s)",
            exc,
            alert_type,
            dedup_key,
        )


def _send_via_smtp(
    *,
    settings: Any,
    to_addr: str,
    subject: str,
    body: str,
    alert_type: str,
    dedup_key: str,
) -> None:
    from_addr = settings.alert_smtp_user
    try:
        msg = MIMEMultipart()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(settings.alert_smtp_host, settings.alert_smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.alert_smtp_user, settings.alert_smtp_password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())

        logger.info(
            "Alert email sent via SMTP: alert_type=%s dedup_key=%s to=%s",
            alert_type,
            dedup_key,
            to_addr,
        )
    except Exception as exc:
        logger.error(
            "SMTP alert failed: %s (alert_type=%s dedup_key=%s)",
            exc,
            alert_type,
            dedup_key,
        )
