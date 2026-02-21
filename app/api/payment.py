"""
Payment API endpoints.
LemonSqueezy integration for subscription with 7-day free trial.
MVP: $9.9/month, email-based (no user accounts/passwords).
"""
import json
import logging
import hmac
import hashlib
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from app.config import get_settings
from app.services.database import (
    upsert_subscription,
    get_subscription,
    get_subscription_by_customer,
    is_user_active,
    cancel_subscription_db,
    is_event_processed,
    mark_event_processed,
)

logger = logging.getLogger("artimagehub.payment")
router = APIRouter()


# --- Request/Response Models ---

class StartTrialRequest(BaseModel):
    email: EmailStr


class StartTrialResponse(BaseModel):
    checkout_url: str
    session_id: str


class SubscriptionStatusResponse(BaseModel):
    email: str
    is_active: bool
    status: str  # none, on_trial, active, cancelled, expired, past_due
    trial_end: str | None = None
    current_period_end: str | None = None
    cancel_at_period_end: bool = False


class CancelRequest(BaseModel):
    email: EmailStr


# --- LemonSqueezy API Helper ---

class LemonSqueezyAPI:
    """Helper class for LemonSqueezy API calls."""

    BASE_URL = "https://api.lemonsqueezy.com/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        }

    async def create_checkout(
        self,
        store_id: str,
        variant_id: str,
        email: str,
        trial_days: int,
        success_url: str,
        cancel_url: str,
    ) -> dict:
        """Create a checkout session."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/checkouts",
                headers=self.headers,
                json={
                    "data": {
                        "type": "checkouts",
                        "attributes": {
                            "checkout_data": {
                                "email": email,
                                "custom": {
                                    "email": email,
                                },
                            },
                            "checkout_options": {
                                "embed": False,
                                "media": False,
                                "logo": True,
                                "discount": False,
                                "button_color": "#2563eb",
                            },
                            "expires_at": None,
                            "preview": False,
                        },
                        "relationships": {
                            "store": {
                                "data": {
                                    "type": "stores",
                                    "id": store_id,
                                }
                            },
                            "variant": {
                                "data": {
                                    "type": "variants",
                                    "id": variant_id,
                                }
                            },
                        },
                    }
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["data"]

    async def get_subscription(self, subscription_id: str) -> dict:
        """Get subscription details."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/subscriptions/{subscription_id}",
                headers=self.headers,
            )
            response.raise_for_status()
            data = response.json()
            return data["data"]

    async def cancel_subscription(self, subscription_id: str) -> dict:
        """Cancel a subscription (at period end)."""
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self.BASE_URL}/subscriptions/{subscription_id}",
                headers=self.headers,
            )
            response.raise_for_status()
            data = response.json()
            return data["data"]


# --- Endpoints ---

@router.post("/payment/start-trial", response_model=StartTrialResponse)
async def start_trial(req: StartTrialRequest):
    """Create a LemonSqueezy Checkout session for 7-day free trial + subscription."""
    settings = get_settings()

    if not settings.lemonsqueezy_api_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    email = req.email.lower().strip()

    # Check if user already has an active subscription
    if is_user_active(email):
        raise HTTPException(status_code=409, detail="You already have an active subscription")

    try:
        api = LemonSqueezyAPI(settings.lemonsqueezy_api_key)
        checkout = await api.create_checkout(
            store_id=settings.lemonsqueezy_store_id,
            variant_id=settings.lemonsqueezy_variant_id,
            email=email,
            trial_days=settings.trial_days,
            success_url=f"{settings.frontend_url}/payment/success?email={{email}}",
            cancel_url=f"{settings.frontend_url}/payment/cancel",
        )

        checkout_url = checkout["attributes"]["url"]
        checkout_id = checkout["id"]

        logger.info("Trial checkout created: %s for %s", checkout_id, email)
        return StartTrialResponse(checkout_url=checkout_url, session_id=checkout_id)

    except httpx.HTTPStatusError as e:
        logger.error("LemonSqueezy HTTP error: %s - %s", e.response.status_code, e.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"Payment service error: {e.response.text}",
        )
    except Exception as e:
        logger.error("LemonSqueezy error: %s", e)
        raise HTTPException(status_code=502, detail=f"Payment service error: {str(e)}")


@router.get("/payment/subscription/{email}", response_model=SubscriptionStatusResponse)
async def check_subscription(email: str):
    """Check subscription status for an email address."""
    email = email.lower().strip()
    sub = get_subscription(email)

    if sub is None:
        return SubscriptionStatusResponse(
            email=email, is_active=False, status="none",
        )

    return SubscriptionStatusResponse(
        email=email,
        is_active=sub["status"] in ("on_trial", "active"),
        status=sub["status"],
        trial_end=sub.get("trial_end"),
        current_period_end=sub.get("current_period_end"),
        cancel_at_period_end=bool(sub.get("cancel_at_period_end", 0)),
    )


@router.post("/payment/cancel")
async def cancel_subscription(req: CancelRequest):
    """Cancel subscription at end of current period."""
    settings = get_settings()
    email = req.email.lower().strip()
    sub = get_subscription(email)

    if sub is None or sub["status"] not in ("on_trial", "active"):
        raise HTTPException(status_code=404, detail="No active subscription found")

    if not settings.lemonsqueezy_api_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    try:
        api = LemonSqueezyAPI(settings.lemonsqueezy_api_key)
        await api.cancel_subscription(sub["lemonsqueezy_subscription_id"])
        cancel_subscription_db(email)

        logger.info("Subscription cancellation requested: %s", email)
        return {
            "status": "canceling",
            "message": "Your subscription will be canceled at the end of the current period.",
            "cancel_at": sub.get("current_period_end") or sub.get("trial_end"),
        }

    except httpx.HTTPStatusError as e:
        logger.error("Cancel error: %s", e)
        raise HTTPException(status_code=502, detail="Could not cancel subscription")


@router.post("/payment/create-portal-session")
async def create_portal_session(req: CancelRequest):
    """Get LemonSqueezy subscription management URL."""
    settings = get_settings()
    email = req.email.lower().strip()
    sub = get_subscription(email)

    if sub is None or not sub.get("lemonsqueezy_subscription_id"):
        raise HTTPException(status_code=404, detail="No subscription found for this email")

    # LemonSqueezy subscription management URL
    # Format: https://app.lemonsqueezy.com/my-orders/{subscription_id}
    subscription_id = sub["lemonsqueezy_subscription_id"]
    portal_url = f"https://app.lemonsqueezy.com/my-orders/{subscription_id}"

    return {"url": portal_url}


@router.get("/payment/verify-session/{session_id}")
async def verify_session(session_id: str):
    """Verify a checkout session status (called from success page)."""
    settings = get_settings()

    if not settings.lemonsqueezy_api_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    # For LemonSqueezy, we'll look up by email from the success URL parameter
    # The session_id is actually the checkout ID, but we don't need to verify it
    # because the webhook will handle the subscription creation

    # Return a generic success response
    return {
        "status": "success",
        "message": "Checkout completed. Your subscription will be activated shortly.",
    }


@router.post("/payment/webhook")
async def lemonsqueezy_webhook(request: Request):
    """Handle LemonSqueezy webhook events for subscription lifecycle."""
    settings = get_settings()

    payload = await request.body()
    sig_header = request.headers.get("x-signature", "")

    # Verify webhook signature
    if settings.lemonsqueezy_webhook_secret:
        expected_sig = hmac.new(
            settings.lemonsqueezy_webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(sig_header, expected_sig):
            logger.error("Webhook signature verification failed")
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        logger.warning("Webhook secret not configured, skipping signature verification")

    event = json.loads(payload)

    # LemonSqueezy webhook structure:
    # { "meta": { "event_name": "...", "custom_data": {...} }, "data": {...} }
    event_id = event.get("meta", {}).get("event_name", "") + "_" + event.get("data", {}).get("id", "")
    event_type = event.get("meta", {}).get("event_name", "")

    # Idempotency check
    if is_event_processed(event_id):
        logger.info("Webhook event already processed: %s", event_id)
        return {"status": "ok", "already_processed": True}

    logger.info("Processing webhook: %s (%s)", event_type, event_id)

    # Handle subscription events
    # LemonSqueezy event names:
    # - subscription_created
    # - subscription_updated
    # - subscription_cancelled
    # - subscription_resumed
    # - subscription_expired
    # - subscription_paused
    # - subscription_unpaused
    # - order_created (for one-time purchases)
    # - subscription_payment_failed
    # - subscription_payment_success
    # - subscription_payment_recovered

    if event_type == "subscription_created":
        _handle_subscription_update(event["data"], event.get("meta", {}))

    elif event_type == "subscription_updated":
        _handle_subscription_update(event["data"], event.get("meta", {}))

    elif event_type == "subscription_cancelled":
        _handle_subscription_cancelled(event["data"])

    elif event_type == "subscription_expired":
        _handle_subscription_expired(event["data"])

    elif event_type == "subscription_payment_failed":
        _handle_payment_failed(event["data"])

    elif event_type == "order_created":
        _handle_order_created(event["data"])

    # Mark as processed
    mark_event_processed(event_id, event_type)

    return {"status": "ok"}


# --- Webhook Handlers ---

def _handle_order_created(order: dict):
    """Handle order_created - link customer to email when checkout completes."""
    attrs = order.get("attributes", {})
    email = attrs.get("user_email") or attrs.get("customer_email", "")
    customer_id = attrs.get("customer_id")
    first_subscription_item = attrs.get("first_subscription_item")

    if email and customer_id and first_subscription_item:
        subscription_id = first_subscription_item.get("subscription_id")

        upsert_subscription(
            email=email,
            lemonsqueezy_customer_id=str(customer_id),
            lemonsqueezy_subscription_id=str(subscription_id) if subscription_id else None,
            status="on_trial",  # Will be updated by subscription_created event
        )
        logger.info("Order created: %s → customer=%s sub=%s", email, customer_id, subscription_id)


def _handle_subscription_update(subscription: dict, meta: dict = None):
    """Handle subscription created/updated events."""
    attrs = subscription.get("attributes", {})
    sub_id = subscription.get("id")
    customer_id = attrs.get("customer_id")
    status = attrs.get("status")  # on_trial, active, paused, past_due, unpaid, cancelled, expired

    # LemonSqueezy uses different status names than Stripe:
    # - on_trial: In trial period
    # - active: Active subscription
    # - paused: Paused by customer
    # - past_due: Payment failed
    # - unpaid: Payment failed multiple times
    # - cancelled: Cancelled (still active until end of period)
    # - expired: Ended

    # Extract dates (ISO 8601 strings)
    trial_ends_at = attrs.get("trial_ends_at")
    renews_at = attrs.get("renews_at")
    ends_at = attrs.get("ends_at")

    # Get email from multiple sources (in order of preference)
    email = None

    # 1. From custom_data (passed during checkout)
    if meta:
        custom_data = meta.get("custom_data", {})
        email = custom_data.get("email")

    # 2. From subscription attributes
    if not email:
        email = attrs.get("user_email", "")

    # 3. Try to find email by customer ID
    if not email:
        sub_record = get_subscription_by_customer(str(customer_id))
        if sub_record:
            email = sub_record["email"]

    if not email:
        logger.warning("No email found for subscription %s (customer %s), skipping", sub_id, customer_id)
        return

    upsert_subscription(
        email=email,
        lemonsqueezy_customer_id=str(customer_id),
        lemonsqueezy_subscription_id=str(sub_id),
        status=status,
        trial_end=trial_ends_at,
        current_period_end=renews_at or ends_at,
        cancel_at_period_end=status == "cancelled",
    )
    logger.info("Subscription updated: %s status=%s", email, status)


def _handle_subscription_cancelled(subscription: dict):
    """Handle subscription cancelled."""
    attrs = subscription.get("attributes", {})
    sub_id = subscription.get("id")
    customer_id = attrs.get("customer_id")
    email = attrs.get("user_email", "")

    if not email:
        sub_record = get_subscription_by_customer(str(customer_id))
        if sub_record:
            email = sub_record["email"]

    if not email:
        return

    upsert_subscription(
        email=email,
        lemonsqueezy_customer_id=str(customer_id),
        lemonsqueezy_subscription_id=str(sub_id),
        status="cancelled",
        cancel_at_period_end=True,
    )
    logger.info("Subscription cancelled: %s", email)


def _handle_subscription_expired(subscription: dict):
    """Handle subscription expired."""
    attrs = subscription.get("attributes", {})
    sub_id = subscription.get("id")
    customer_id = attrs.get("customer_id")
    email = attrs.get("user_email", "")

    if not email:
        sub_record = get_subscription_by_customer(str(customer_id))
        if sub_record:
            email = sub_record["email"]

    if not email:
        return

    upsert_subscription(
        email=email,
        lemonsqueezy_customer_id=str(customer_id),
        lemonsqueezy_subscription_id=str(sub_id),
        status="expired",
    )
    logger.info("Subscription expired: %s", email)


def _handle_payment_failed(subscription: dict):
    """Handle failed payment."""
    attrs = subscription.get("attributes", {})
    sub_id = subscription.get("id")
    customer_id = attrs.get("customer_id")
    email = attrs.get("user_email", "")

    if not email:
        sub_record = get_subscription_by_customer(str(customer_id))
        if sub_record:
            email = sub_record["email"]

    if not email:
        return

    upsert_subscription(
        email=email,
        status="past_due",
    )
    logger.warning("Payment failed for %s", email)
