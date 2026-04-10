"""
Dodo Payments service helpers for checkout creation and webhook verification.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger("artimagehub.dodo")


try:
    from dodopayments import DodoPayments
except Exception as exc:  # pragma: no cover - handled at runtime in API layer
    DodoPayments = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _assert_sdk_available() -> None:
    if DodoPayments is None:
        raise RuntimeError(
            "DodoPayments SDK is not available. Install dependency `dodopayments` first."
        ) from _IMPORT_ERROR


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()  # pydantic-style
    if hasattr(value, "to_dict"):
        return value.to_dict()  # SDK-style
    if hasattr(value, "__dict__"):
        return {
            k: v
            for k, v in vars(value).items()
            if not k.startswith("_") and not callable(v)
        }
    return {}


def _build_client(require_webhook_key: bool = False):
    _assert_sdk_available()
    settings = get_settings()

    if not settings.dodo_payments_api_key:
        raise RuntimeError("DodoPayments API key is missing (DODO_PAYMENTS_API_KEY).")

    webhook_key = settings.dodo_payments_webhook_key.strip()
    if require_webhook_key and not webhook_key:
        raise RuntimeError(
            "DodoPayments webhook key is missing (DODO_PAYMENTS_WEBHOOK_KEY)."
        )

    kwargs: dict[str, Any] = {
        "bearer_token": settings.dodo_payments_api_key,
        "environment": settings.dodo_payments_environment,
    }
    if webhook_key:
        kwargs["webhook_key"] = webhook_key

    return DodoPayments(**kwargs)


def create_checkout_session(
    *,
    email: str,
    return_url: str,
    cancel_url: str | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a Dodo hosted checkout session and return normalized fields."""
    settings = get_settings()
    if not settings.dodo_payments_product_id:
        raise RuntimeError(
            "DodoPayments product id is missing (DODO_PAYMENTS_PRODUCT_ID)."
        )

    client = _build_client()
    payload: dict[str, Any] = {
        "product_cart": [
            {
                "product_id": settings.dodo_payments_product_id,
                "quantity": 1,
            }
        ],
        "customer": {"email": email},
        "billing_currency": settings.dodo_payments_currency,
        "return_url": return_url,
    }
    if cancel_url:
        payload["cancel_url"] = cancel_url
    if metadata:
        payload["metadata"] = metadata

    try:
        response = client.checkout_sessions.create(**payload)
    except TypeError as exc:
        # Some SDK versions may not expose cancel_url yet.
        if "cancel_url" in payload:
            payload.pop("cancel_url", None)
            logger.warning(
                "Dodo SDK rejected cancel_url, retrying checkout session without cancel_url"
            )
            response = client.checkout_sessions.create(**payload)
        else:
            raise exc

    data = _as_dict(response)
    session_id = data.get("session_id") or data.get("id")
    checkout_url = data.get("checkout_url") or data.get("url")

    if not session_id or not checkout_url:
        raise RuntimeError(
            "Dodo checkout session returned incomplete data (missing session_id/checkout_url)."
        )

    return {
        "session_id": str(session_id),
        "checkout_url": str(checkout_url),
        "raw": data,
    }


def unwrap_webhook_event(payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Verify and decode a webhook payload using Dodo SDK helper."""
    client = _build_client(require_webhook_key=True)

    required = {
        "webhook-id": headers.get("webhook-id", ""),
        "webhook-signature": headers.get("webhook-signature", ""),
        "webhook-timestamp": headers.get("webhook-timestamp", ""),
    }

    if not all(required.values()):
        raise RuntimeError("Missing Dodo webhook signature headers.")

    unwrapped = client.webhooks.unwrap(payload, headers=required)
    event = _as_dict(unwrapped)
    if not event:
        raise RuntimeError("Dodo webhook payload could not be decoded.")
    return event
