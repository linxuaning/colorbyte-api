"""
Admin API endpoints — protected by ADMIN_SECRET env var.
Only enabled when admin_secret is non-empty.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel, EmailStr

from app.config import get_settings
from app.services.database import (
    upsert_subscription, get_subscription,
    grant_feature_entitlement, is_feature_entitled,
    FEATURE_RESTORATION, FEATURE_DENOISING, FEATURE_DEBLURRING, FEATURE_JPEG_FIX,
)
from app.services.dashboard import get_recall_candidates

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


class GrantFeatureRequest(BaseModel):
    email: EmailStr
    features: list[str] = [FEATURE_RESTORATION, FEATURE_DENOISING, FEATURE_DEBLURRING, FEATURE_JPEG_FIX]
    note: str = ""


@router.post("/admin/grant-all-features")
async def grant_all_features(
    body: GrantFeatureRequest,
    authorization: str | None = Header(default=None),
):
    """Grant all (or specified) feature entitlements to an email. Idempotent."""
    _require_admin(authorization)

    results = {}
    for feature_key in body.features:
        try:
            grant_feature_entitlement(body.email, feature_key, payment_id="admin-seed")
            results[feature_key] = "granted"
        except Exception as exc:
            results[feature_key] = f"error: {exc}"

    # Also ensure subscriptions table is active (covers legacy restoration check)
    upsert_subscription(body.email, payment_provider="admin", status="active")

    logger.info("Admin granted all features: %s features=%s note=%r", body.email, list(results), body.note)
    return {"ok": True, "email": body.email, "features": results}


@router.get("/admin/recall-candidates")
async def recall_candidates(
    days: int = Query(default=14, ge=1, le=90),
    authorization: str | None = Header(default=None),
):
    """T244 (创始人 dispatch, 2026-07-12, founder direct authorization on the
    recall action): read-only lookup of real paying customers whose most
    recent task in the window failed with no success since. Sends nothing;
    the list is for review before any recall action is taken."""
    _require_admin(authorization)
    return get_recall_candidates(days=days)
