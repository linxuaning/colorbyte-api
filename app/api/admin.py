"""
Admin API endpoints — protected by ADMIN_SECRET env var.
Only enabled when admin_secret is non-empty.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, EmailStr

from app.config import get_settings
from app.services.database import upsert_subscription, get_subscription

logger = logging.getLogger("artimagehub.admin")
router = APIRouter()


def _require_admin(authorization: str | None):
    settings = get_settings()
    if not settings.admin_secret:
        raise HTTPException(status_code=404, detail="Not found")
    expected = f"Bearer {settings.admin_secret}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


class GrantAccessRequest(BaseModel):
    email: EmailStr
    payment_provider: str = "manual"
    note: str = ""


@router.post("/admin/grant-access")
async def grant_access(
    body: GrantAccessRequest,
    authorization: str | None = Header(default=None),
):
    """Manually grant paid access to an email (admin only)."""
    _require_admin(authorization)

    now = datetime.now(timezone.utc).isoformat()
    upsert_subscription(
        email=body.email,
        payment_provider=body.payment_provider,
        status="active",
        current_period_start=now,
    )

    sub = get_subscription(body.email)
    logger.info("Admin granted access: %s provider=%s note=%r", body.email, body.payment_provider, body.note)
    return {"ok": True, "email": body.email, "status": sub["status"] if sub else "active"}
