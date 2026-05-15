"""GA4 Measurement Protocol helpers for server-side paid conversions."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("artimagehub.ga4")

GA4_MP_ENDPOINT = "https://www.google-analytics.com/mp/collect"


def send_purchase_event(
    *,
    client_id: str | None,
    transaction_id: str,
    value: float,
    currency: str,
    payment_provider: str,
    feature_key: str | None = None,
    landing_page: str | None = None,
    cta_slot: str | None = None,
    entry_variant: str | None = None,
    checkout_source: str | None = None,
) -> bool:
    """Send a real paid conversion to GA4 from the payment webhook.

    Returns False when configuration is absent or GA4 rejects the request.
    Never raises; webhook success must not depend on analytics delivery.
    """
    settings = get_settings()
    measurement_id = settings.ga4_measurement_id.strip()
    api_secret = settings.ga4_measurement_api_secret.strip()
    normalized_client_id = (client_id or "").strip()

    if not measurement_id or not api_secret:
        logger.info("GA4 purchase skipped: measurement protocol env is not configured")
        return False
    if not normalized_client_id:
        logger.info("GA4 purchase skipped: missing client_id transaction_id=%s", transaction_id)
        return False

    params: dict[str, Any] = {
        "transaction_id": transaction_id,
        "currency": currency,
        "value": value,
        "payment_provider": payment_provider,
    }
    if feature_key:
        params["feature_key"] = feature_key
    if landing_page:
        params["landing_page"] = landing_page
    if cta_slot:
        params["cta_slot"] = cta_slot
    if entry_variant:
        params["entry_variant"] = entry_variant
    if checkout_source:
        params["checkout_source"] = checkout_source

    purchase_params = {
        **params,
        "items": [
            {
                "item_id": feature_key or "restoration",
                "item_name": "ArtImageHub one-time unlock",
                "price": value,
                "quantity": 1,
            }
        ],
    }

    payload = {
        "client_id": normalized_client_id,
        "events": [
            {"name": "purchase", "params": purchase_params},
            {"name": "payment_success", "params": params},
        ],
    }

    try:
        response = httpx.post(
            GA4_MP_ENDPOINT,
            params={"measurement_id": measurement_id, "api_secret": api_secret},
            json=payload,
            timeout=5,
        )
        response.raise_for_status()
    except Exception:
        logger.exception("GA4 purchase send failed transaction_id=%s", transaction_id)
        return False

    logger.info("GA4 purchase sent transaction_id=%s", transaction_id)
    return True
