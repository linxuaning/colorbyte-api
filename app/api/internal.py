"""
Internal endpoints for trusted automation (GitHub Actions cron, ops scripts).

Auth: Bearer ADMIN_SECRET (mirror admin.py pattern). The shared secret is
already deployed on Render; cron callers reference it as a GitHub Actions
repo secret. No new env var needed.
"""
import logging

from fastapi import APIRouter, Header, HTTPException

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
