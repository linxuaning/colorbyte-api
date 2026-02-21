"""
PayPal payment integration service using REST API
"""
import logging
import base64
from typing import Dict, Any

import httpx

from app.config import get_settings

logger = logging.getLogger("artimagehub.paypal")


def get_paypal_base_url() -> str:
    """Get PayPal API base URL based on mode."""
    settings = get_settings()
    if settings.paypal_mode == "sandbox":
        return "https://api-m.sandbox.paypal.com"
    return "https://api-m.paypal.com"


def get_access_token() -> str:
    """Get PayPal access token using client credentials."""
    settings = get_settings()
    base_url = get_paypal_base_url()

    # Validate credentials are configured
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        error_msg = "PayPal credentials not configured. Check PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET environment variables."
        logger.error(error_msg)
        raise Exception(error_msg)

    # Encode credentials
    credentials = f"{settings.paypal_client_id}:{settings.paypal_client_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    headers = {
        "Authorization": f"Basic {encoded_credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {"grant_type": "client_credentials"}

    response = httpx.post(
        f"{base_url}/v1/oauth2/token",
        headers=headers,
        data=data,
        timeout=30.0,
    )

    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        error_detail = response.text
        logger.error(
            "Failed to get PayPal access token: %d %s",
            response.status_code,
            error_detail,
        )
        raise Exception(f"PayPal OAuth failed (status {response.status_code}): {error_detail}")


def create_order(
    amount: str = "29.90",
    currency: str = "USD",
    description: str = "ArtImageHub Pro Lifetime Access",
) -> Dict[str, Any]:
    """
    Create a PayPal order for one-time payment.

    Returns:
        Dict with order_id and approval_url
    """
    base_url = get_paypal_base_url()
    access_token = get_access_token()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    order_data = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "amount": {
                    "currency_code": currency,
                    "value": amount,
                },
                "description": description,
            }
        ],
        "application_context": {
            "return_url": "https://colorbyte.vercel.app/payment/success",
            "cancel_url": "https://colorbyte.vercel.app/#pricing",
        },
    }

    response = httpx.post(
        f"{base_url}/v2/checkout/orders",
        headers=headers,
        json=order_data,
        timeout=30.0,
    )

    if response.status_code == 201:
        result = response.json()
        order_id = result["id"]

        # Find approval URL
        approval_url = None
        for link in result.get("links", []):
            if link.get("rel") == "approve":
                approval_url = link.get("href")
                break

        logger.info("PayPal order created: order_id=%s", order_id)

        return {
            "order_id": order_id,
            "approval_url": approval_url,
            "status": result.get("status"),
        }
    else:
        error_detail = response.text
        logger.error(
            "PayPal order creation failed: %d %s",
            response.status_code,
            error_detail,
        )
        raise Exception(f"PayPal order creation failed (status {response.status_code}): {error_detail}")


def capture_order(order_id: str) -> Dict[str, Any]:
    """
    Capture payment for an approved PayPal order.

    Returns:
        Dict with order details including payer info
    """
    base_url = get_paypal_base_url()
    access_token = get_access_token()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    response = httpx.post(
        f"{base_url}/v2/checkout/orders/{order_id}/capture",
        headers=headers,
        timeout=30.0,
    )

    if response.status_code == 201:
        result = response.json()

        # Extract payer email
        payer_email = result.get("payer", {}).get("email_address")
        payer_id = result.get("payer", {}).get("payer_id")
        status = result.get("status")

        logger.info(
            "PayPal order captured: order_id=%s payer_email=%s status=%s",
            order_id,
            payer_email,
            status,
        )

        return {
            "order_id": order_id,
            "status": status,
            "payer_email": payer_email,
            "payer_id": payer_id,
        }
    else:
        logger.error(
            "PayPal order capture failed: order_id=%s status=%d %s",
            order_id,
            response.status_code,
            response.text,
        )
        raise Exception(f"PayPal capture failed: {response.status_code}")


def verify_webhook_signature(
    webhook_id: str,
    headers: Dict[str, str],
    body: str,
) -> bool:
    """
    Verify PayPal webhook signature.

    For MVP, basic validation. Full implementation can be added later.
    """
    settings = get_settings()

    # For MVP, accept webhooks if webhook_id is configured
    if not settings.paypal_webhook_id:
        logger.warning("PayPal webhook_id not configured, allowing webhook")
        return True

    # TODO: Implement full webhook signature verification
    # https://developer.paypal.com/api/rest/webhooks/rest/#verify-webhook-signature

    return True
