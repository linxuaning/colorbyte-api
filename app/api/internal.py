"""
Internal endpoints for trusted automation (GitHub Actions cron, ops scripts).

Auth: Bearer ADMIN_SECRET (mirror admin.py pattern). The shared secret is
already deployed on Render; cron callers reference it as a GitHub Actions
repo secret. No new env var needed.
"""
import json
import logging
import urllib.request

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.services.mask_email import process_due_emails
from app.services.abandoned_cart import discover_abandoned_carts, process_due_reminders

logger = logging.getLogger("artimagehub.internal")
router = APIRouter()


def _require_admin(authorization: str | None) -> None:
    settings = get_settings()
    if not settings.admin_secret:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    expected = f"Bearer {settings.admin_secret}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


class _EmailAttachment(BaseModel):
    filename: str
    content_base64: str


class _SendEmailRequest(BaseModel):
    to: str
    subject: str
    text: str
    attachments: list[_EmailAttachment] = []


# T223 (2026-07-07): one-off utility to deliver the manually-reprocessed
# restoration images for the 3 real customers affected by the old_chain
# black-image bug (dev-environment sandbox has no DNS route to resend.com,
# only Render's egress does — this endpoint is the bridge). Same sender/
# transactional-tone pattern as mask_email.py / abandoned_cart.py.
@router.post("/internal/send-email")
async def send_email(req: _SendEmailRequest, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    settings = get_settings()
    if not settings.resend_api_key:
        raise HTTPException(status_code=503, detail="Resend not configured")
    payload = json.dumps({
        "from": "artimagehub <support@artimagehub.com>",
        "to": [req.to],
        "reply_to": "support@artimagehub.com",
        "subject": req.subject,
        "text": req.text,
        "attachments": [
            {"filename": a.filename, "content": a.content_base64} for a in req.attachments
        ],
    }).encode("utf-8")
    r = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "artimagehub-backend/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            body = resp.read().decode()
            logger.info("send-email: to=%s status=%s", req.to, resp.status)
            return {"ok": True, "status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        logger.error("send-email failed: to=%s status=%s body=%s", req.to, exc.code, detail)
        raise HTTPException(status_code=502, detail=f"Resend error {exc.code}: {detail}")


@router.get("/internal/email-status/{email_id}")
async def email_status(email_id: str, authorization: str | None = Header(default=None)):
    """T223 follow-up: independent delivery-status check for the send-email
    endpoint above (founder verification, not just trusting the send-time 200).
    Proxies Resend's GET /emails/{id} using the backend's own resend_api_key
    (the only environment in this stack with real DNS/network to resend.com)."""
    _require_admin(authorization)
    settings = get_settings()
    if not settings.resend_api_key:
        raise HTTPException(status_code=503, detail="Resend not configured")
    r = urllib.request.Request(
        f"https://api.resend.com/emails/{email_id}",
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "User-Agent": "artimagehub-backend/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return {"ok": True, "status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise HTTPException(status_code=502, detail=f"Resend error {exc.code}: {detail}")


@router.post("/internal/mask-email-poll")
async def mask_email_poll(authorization: str | None = Header(default=None)):
    """Send all due Mask emails. Called every 5 min by GitHub Actions cron."""
    _require_admin(authorization)
    summary = process_due_emails()
    logger.info("mask_email poll summary: %s", summary)
    return summary


@router.post("/internal/abandoned-cart-poll")
async def abandoned_cart_poll(authorization: str | None = Header(default=None)):
    """Discover new Dodo requires_payment_method checkouts and send any due
    reminder emails. Called once daily by GitHub Actions cron (T209)."""
    _require_admin(authorization)
    discovery = discover_abandoned_carts()
    send_summary = process_due_reminders()
    logger.info("abandoned_cart poll: discovery=%s send=%s", discovery, send_summary)
    return {"discovery": discovery, "send": send_summary}
