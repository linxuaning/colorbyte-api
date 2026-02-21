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


# --- Buy Me a Coffee Integration ---

@router.post("/payment/bmc-webhook")
async def buymeacoffee_webhook(request: Request):
    """
    Handle Buy Me a Coffee webhook events.

    Webhook events from BMC:
    - supporter.new_donation: One-time donation
    - supporter.new_membership: New membership subscription
    - membership.updated: Membership status changed
    - membership.cancelled: Membership cancelled

    BMC webhook payload structure:
    {
        "event": "supporter.new_membership",
        "data": {
            "supporter_id": "abc123",
            "supporter_name": "John Doe",
            "supporter_email": "john@example.com",
            "support_coffee_count": 5,
            "support_message": "Thanks!",
            "membership_id": "mem_xyz789",
            "membership_level_id": "level_123",
            "membership_level_name": "Premium",
            "is_monthly": true,
            "created_at": "2026-02-17T12:00:00Z"
        }
    }
    """
    settings = get_settings()

    # Get raw payload for signature verification
    payload = await request.body()
    payload_str = payload.decode()

    # Verify webhook signature (Bearer token or HMAC)
    # BMC uses Bearer token in Authorization header
    auth_header = request.headers.get("authorization", "")

    if settings.bmc_webhook_secret:
        expected_auth = f"Bearer {settings.bmc_webhook_secret}"
        if not auth_header or auth_header != expected_auth:
            logger.error("BMC webhook auth failed: invalid token")
            raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        logger.warning("BMC webhook secret not configured, skipping auth verification")

    # Parse webhook payload
    try:
        event_data = json.loads(payload_str)
    except json.JSONDecodeError:
        logger.error("BMC webhook: invalid JSON payload")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event_data.get("event", "")
    data = event_data.get("data", {})

    # Generate event ID for idempotency
    # BMC doesn't provide event IDs, so we create one from event type + supporter ID + timestamp
    supporter_id = data.get("supporter_id", "")
    membership_id = data.get("membership_id", "")
    created_at = data.get("created_at", "")
    event_id = f"bmc_{event_type}_{supporter_id}_{membership_id}_{created_at}"

    # Idempotency check
    if is_event_processed(event_id):
        logger.info("BMC webhook already processed: %s", event_id)
        return {"status": "ok", "already_processed": True}

    logger.info("Processing BMC webhook: %s (%s)", event_type, event_id)

    # Handle different event types
    if event_type == "supporter.new_membership":
        _handle_bmc_new_membership(data)

    elif event_type == "membership.updated":
        _handle_bmc_membership_updated(data)

    elif event_type == "membership.cancelled":
        _handle_bmc_membership_cancelled(data)

    elif event_type == "supporter.new_donation":
        _handle_bmc_donation(data)

    else:
        logger.warning("Unhandled BMC webhook event: %s", event_type)

    # Mark as processed
    mark_event_processed(event_id, event_type)

    return {"status": "ok"}


# --- BMC Webhook Handlers ---

def _handle_bmc_new_membership(data: dict):
    """Handle new BMC membership subscription."""
    email = data.get("supporter_email", "").lower().strip()
    supporter_id = data.get("supporter_id", "")
    membership_id = data.get("membership_id", "")
    membership_level_name = data.get("membership_level_name", "")
    is_monthly = data.get("is_monthly", True)
    created_at = data.get("created_at", "")

    if not email:
        logger.error("BMC new_membership without email: %s", membership_id)
        return

    # BMC doesn't have trials - memberships are active immediately after payment
    # We'll set status to "active" and calculate period end based on monthly/annual
    # For now, assume monthly subscriptions renew every 30 days
    from datetime import datetime, timedelta, timezone

    if created_at:
        try:
            period_start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            # Assume monthly subscription (30 days)
            period_end = period_start + timedelta(days=30)
        except ValueError:
            period_start = datetime.now(timezone.utc)
            period_end = period_start + timedelta(days=30)
    else:
        period_start = datetime.now(timezone.utc)
        period_end = period_start + timedelta(days=30)

    upsert_subscription(
        email=email,
        payment_provider="bmc",
        bmc_supporter_id=supporter_id,
        bmc_membership_id=membership_id,
        status="active",
        current_period_start=period_start.isoformat(),
        current_period_end=period_end.isoformat(),
    )
    logger.info("BMC membership created: %s (level=%s)", email, membership_level_name)


def _handle_bmc_membership_updated(data: dict):
    """Handle BMC membership update (renewal, etc.)."""
    email = data.get("supporter_email", "").lower().strip()
    membership_id = data.get("membership_id", "")

    if not email:
        logger.warning("BMC membership_updated without email: %s", membership_id)
        return

    # Update period end (assume renewal for 30 days from now)
    from datetime import datetime, timedelta, timezone
    period_start = datetime.now(timezone.utc)
    period_end = period_start + timedelta(days=30)

    upsert_subscription(
        email=email,
        payment_provider="bmc",
        bmc_membership_id=membership_id,
        status="active",
        current_period_start=period_start.isoformat(),
        current_period_end=period_end.isoformat(),
    )
    logger.info("BMC membership updated: %s", email)


def _handle_bmc_membership_cancelled(data: dict):
    """Handle BMC membership cancellation."""
    email = data.get("supporter_email", "").lower().strip()
    membership_id = data.get("membership_id", "")

    if not email:
        logger.warning("BMC membership_cancelled without email: %s", membership_id)
        return

    # Mark as cancelled (BMC cancellations are immediate, no "cancel at period end")
    upsert_subscription(
        email=email,
        payment_provider="bmc",
        status="cancelled",
        cancel_at_period_end=True,
    )
    logger.info("BMC membership cancelled: %s", email)


def _handle_bmc_donation(data: dict):
    """Handle BMC one-time donation (used for Lifetime Pro access)."""
    email = data.get("supporter_email", "").lower().strip()
    supporter_id = data.get("supporter_id", "")
    supporter_name = data.get("supporter_name", "")
    support_coffees = data.get("support_coffees", 0)  # Number of coffees
    support_message = data.get("support_message", "")
    created_at = data.get("created_at", "")

    # BMC sends coffee count, typically 1 coffee = $5
    # For $29.9, that would be ~6 coffees
    # We'll check if >= 6 coffees (>= $29.9)
    REQUIRED_COFFEES = 6

    if not email:
        logger.warning("BMC donation without email: %s", supporter_name)
        return

    logger.info(
        "BMC donation received: %s coffees from %s (%s)",
        support_coffees,
        email,
        supporter_name,
    )

    # Only activate Pro for donations >= $29.9 (6 coffees)
    if support_coffees >= REQUIRED_COFFEES:
        # Grant lifetime Pro access
        from datetime import datetime, timedelta, timezone

        # Set period_end far in the future (100 years) for lifetime access
        if created_at:
            try:
                period_start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                period_start = datetime.now(timezone.utc)
        else:
            period_start = datetime.now(timezone.utc)

        # Lifetime = 100 years from now
        period_end = period_start + timedelta(days=36500)

        upsert_subscription(
            email=email,
            payment_provider="bmc",
            bmc_supporter_id=supporter_id,
            status="active",
            current_period_start=period_start.isoformat(),
            current_period_end=period_end.isoformat(),
        )
        logger.info(
            "BMC donation activated Pro Lifetime: %s (%d coffees >= %d required)",
            email,
            support_coffees,
            REQUIRED_COFFEES,
        )
    else:
        # Thank them but don't activate Pro
        logger.info(
            "BMC donation appreciated but insufficient: %s (%d coffees < %d required)",
            email,
            support_coffees,
            REQUIRED_COFFEES,
        )


# ============================================================================
# PayPal Integration
# ============================================================================


class PayPalCreateOrderRequest(BaseModel):
    """Request to create PayPal order."""
    email: EmailStr


class PayPalCreateOrderResponse(BaseModel):
    """Response from creating PayPal order."""
    order_id: str
    approval_url: str | None


class PayPalCapturePaymentRequest(BaseModel):
    """Request to capture PayPal payment."""
    order_id: str


class PayPalCapturePaymentResponse(BaseModel):
    """Response from capturing PayPal payment."""
    success: bool
    email: str | None
    status: str


@router.post("/payment/paypal-create-order", response_model=PayPalCreateOrderResponse)
async def create_paypal_order(request: PayPalCreateOrderRequest):
    """
    Create a PayPal order for $29.9 Lifetime Pro access.

    Frontend will call this, then redirect user to PayPal for approval.
    """
    from app.services.paypal import create_order

    try:
        result = create_order(
            amount="29.90",
            currency="USD",
            description=f"ArtImageHub Pro Lifetime - {request.email}",
        )

        logger.info(
            "PayPal order created: order_id=%s email=%s",
            result["order_id"],
            request.email,
        )

        return PayPalCreateOrderResponse(
            order_id=result["order_id"],
            approval_url=result.get("approval_url"),
        )

    except Exception as e:
        logger.error("Failed to create PayPal order: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create PayPal order")


@router.post("/payment/paypal-capture-payment", response_model=PayPalCapturePaymentResponse)
async def capture_paypal_payment(request: PayPalCapturePaymentRequest):
    """
    Capture PayPal payment after user approves.

    Frontend calls this after user returns from PayPal.
    This activates Pro Lifetime access.
    """
    from app.services.paypal import capture_order
    from datetime import timedelta

    try:
        result = capture_order(request.order_id)

        if result["status"] == "COMPLETED":
            payer_email = result.get("payer_email")

            if payer_email:
                # Activate Pro Lifetime access
                now = datetime.now(timezone.utc)
                period_end = now + timedelta(days=36500)  # 100 years

                upsert_subscription(
                    email=payer_email,
                    payment_provider="paypal",
                    paypal_order_id=request.order_id,
                    paypal_payer_id=result.get("payer_id"),
                    status="active",
                    current_period_start=now.isoformat(),
                    current_period_end=period_end.isoformat(),
                )

                logger.info(
                    "PayPal payment captured & Pro activated: order_id=%s email=%s",
                    request.order_id,
                    payer_email,
                )

                return PayPalCapturePaymentResponse(
                    success=True,
                    email=payer_email,
                    status="active",
                )
            else:
                logger.error(
                    "PayPal capture succeeded but no payer email: order_id=%s",
                    request.order_id,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Payment succeeded but could not extract payer email",
                )
        else:
            logger.warning(
                "PayPal capture incomplete: order_id=%s status=%s",
                request.order_id,
                result["status"],
            )
            return PayPalCapturePaymentResponse(
                success=False,
                email=None,
                status=result["status"],
            )

    except Exception as e:
        logger.error(
            "Failed to capture PayPal payment: order_id=%s error=%s",
            request.order_id,
            str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to capture payment")


@router.post("/payment/paypal-webhook")
async def paypal_webhook(request: Request):
    """
    Handle PayPal webhook events.

    Events we care about:
    - PAYMENT.CAPTURE.COMPLETED
    - PAYMENT.CAPTURE.REFUNDED
    """
    from app.services.paypal import verify_webhook_signature

    settings = get_settings()
    payload = await request.body()
    payload_str = payload.decode()

    # Verify webhook signature
    headers = dict(request.headers)
    if not verify_webhook_signature(
        webhook_id=settings.paypal_webhook_id,
        headers=headers,
        body=payload_str,
    ):
        logger.error("PayPal webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        event_data = json.loads(payload_str)
        event_type = event_data.get("event_type", "")
        event_id = event_data.get("id", "")

        logger.info("PayPal webhook received: event_type=%s event_id=%s", event_type, event_id)

        # Idempotency check
        if is_event_processed(event_id):
            logger.info("PayPal webhook already processed: %s", event_id)
            return {"status": "ok", "message": "already processed"}

        # Handle different event types
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            _handle_paypal_capture_completed(event_data)
        elif event_type == "PAYMENT.CAPTURE.REFUNDED":
            _handle_paypal_refund(event_data)
        else:
            logger.info("PayPal webhook ignored: %s", event_type)

        # Mark as processed
        mark_event_processed(event_id, event_type)

        return {"status": "ok"}

    except Exception as e:
        logger.error("PayPal webhook processing error: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Webhook processing failed")


def _handle_paypal_capture_completed(event_data: dict):
    """Handle PAYMENT.CAPTURE.COMPLETED webhook event."""
    from datetime import timedelta

    resource = event_data.get("resource", {})
    payer_email = resource.get("payer", {}).get("email_address")
    payer_id = resource.get("payer", {}).get("payer_id")
    capture_id = resource.get("id")

    if not payer_email:
        logger.warning("PayPal capture completed but no payer email: %s", capture_id)
        return

    # Activate Pro Lifetime
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=36500)  # 100 years

    upsert_subscription(
        email=payer_email,
        payment_provider="paypal",
        paypal_order_id=capture_id,
        paypal_payer_id=payer_id,
        status="active",
        current_period_start=now.isoformat(),
        current_period_end=period_end.isoformat(),
    )

    logger.info("PayPal webhook activated Pro: email=%s capture_id=%s", payer_email, capture_id)


def _handle_paypal_refund(event_data: dict):
    """Handle PAYMENT.CAPTURE.REFUNDED webhook event."""
    # For MVP, we might not implement refund handling
    # Just log it for now
    resource = event_data.get("resource", {})
    refund_id = resource.get("id")

    logger.warning("PayPal refund received (not handled): refund_id=%s", refund_id)
    # TODO: Implement refund handling if needed
    # - Deactivate user's Pro access
    # - Update subscription status to 'refunded'
