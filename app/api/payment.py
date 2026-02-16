"""
Payment API endpoints.
Stripe Checkout integration for subscription with 7-day free trial.
MVP: $9.9/month, email-based (no user accounts/passwords).
"""
import json
import logging
from datetime import datetime, timezone

import stripe
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
    status: str  # none, trialing, active, canceled, past_due
    trial_end: str | None = None
    current_period_end: str | None = None
    cancel_at_period_end: bool = False


class CancelRequest(BaseModel):
    email: EmailStr


# --- Endpoints ---

@router.post("/payment/start-trial", response_model=StartTrialResponse)
async def start_trial(req: StartTrialRequest):
    """Create a Stripe Checkout session for 7-day free trial + subscription."""
    settings = get_settings()

    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    stripe.api_key = settings.stripe_secret_key
    email = req.email.lower().strip()

    # Check if user already has an active subscription
    if is_user_active(email):
        raise HTTPException(status_code=409, detail="You already have an active subscription")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            customer_email=email,
            line_items=[
                {
                    "price": settings.stripe_price_id,
                    "quantity": 1,
                }
            ],
            mode="subscription",
            subscription_data={
                "trial_period_days": settings.trial_days,
                "metadata": {"email": email},
            },
            success_url=f"{settings.frontend_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.frontend_url}/payment/cancel",
            metadata={"email": email},
        )

        logger.info("Trial checkout session created: %s for %s", session.id, email)
        return StartTrialResponse(checkout_url=session.url, session_id=session.id)

    except stripe.StripeError as e:
        logger.error("Stripe error: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"Payment service error: {getattr(e, 'user_message', None) or str(e)}",
        )


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
        is_active=sub["status"] in ("trialing", "active"),
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

    if sub is None or sub["status"] not in ("trialing", "active"):
        raise HTTPException(status_code=404, detail="No active subscription found")

    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    stripe.api_key = settings.stripe_secret_key

    try:
        # Cancel at period end (user keeps access until end of billing period)
        stripe.Subscription.modify(
            sub["stripe_subscription_id"],
            cancel_at_period_end=True,
        )
        cancel_subscription_db(email)

        logger.info("Subscription cancellation requested: %s", email)
        return {
            "status": "canceling",
            "message": "Your subscription will be canceled at the end of the current period.",
            "cancel_at": sub.get("current_period_end") or sub.get("trial_end"),
        }

    except stripe.StripeError as e:
        logger.error("Cancel error: %s", e)
        raise HTTPException(status_code=502, detail="Could not cancel subscription")


@router.post("/payment/create-portal-session")
async def create_portal_session(req: CancelRequest):
    """Create a Stripe Customer Portal session for self-serve management."""
    settings = get_settings()
    email = req.email.lower().strip()
    sub = get_subscription(email)

    if sub is None or not sub.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No subscription found for this email")

    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    stripe.api_key = settings.stripe_secret_key

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=sub["stripe_customer_id"],
            return_url=f"{settings.frontend_url}/subscription",
        )
        return {"url": portal_session.url}

    except stripe.StripeError as e:
        logger.error("Portal session error: %s", e)
        raise HTTPException(status_code=502, detail="Could not create portal session")


@router.get("/payment/verify-session/{session_id}")
async def verify_session(session_id: str):
    """Verify a checkout session status (called from success page)."""
    settings = get_settings()

    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    stripe.api_key = settings.stripe_secret_key

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        email = session.customer_email or session.metadata.get("email", "")
        sub = get_subscription(email)

        return {
            "status": "success" if session.status == "complete" else session.status,
            "email": email,
            "subscription_status": sub["status"] if sub else "pending",
            "trial_end": sub.get("trial_end") if sub else None,
        }

    except stripe.StripeError as e:
        logger.error("Session verification error: %s", e)
        raise HTTPException(status_code=502, detail="Could not verify session")


@router.post("/payment/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events for subscription lifecycle."""
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify webhook signature
    if settings.stripe_webhook_secret:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.stripe_webhook_secret
            )
        except stripe.SignatureVerificationError:
            logger.error("Webhook signature verification failed")
            raise HTTPException(status_code=400, detail="Invalid signature")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payload")
    else:
        logger.warning("Webhook secret not configured, skipping signature verification")
        event = json.loads(payload)

    event_id = event.get("id", "")
    event_type = event["type"]

    # Idempotency check
    if is_event_processed(event_id):
        logger.info("Webhook event already processed: %s", event_id)
        return {"status": "ok", "already_processed": True}

    logger.info("Processing webhook: %s (%s)", event_type, event_id)

    # Handle subscription events
    if event_type == "customer.subscription.created":
        _handle_subscription_update(event["data"]["object"])

    elif event_type == "customer.subscription.updated":
        _handle_subscription_update(event["data"]["object"])

    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(event["data"]["object"])

    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(event["data"]["object"])

    elif event_type == "checkout.session.completed":
        _handle_checkout_completed(event["data"]["object"])

    # Mark as processed
    mark_event_processed(event_id, event_type)

    return {"status": "ok"}


# --- Webhook Handlers ---

def _handle_checkout_completed(session: dict):
    """Handle checkout.session.completed - link customer to email."""
    email = session.get("customer_email") or session.get("metadata", {}).get("email", "")
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")

    if email and customer_id:
        upsert_subscription(
            email=email,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            status="trialing",  # Will be updated by subscription.created event
        )
        logger.info("Checkout completed: %s → customer=%s sub=%s", email, customer_id, subscription_id)


def _handle_subscription_update(subscription: dict):
    """Handle subscription created/updated events."""
    customer_id = subscription.get("customer")
    sub_id = subscription.get("id")
    status = subscription.get("status")  # trialing, active, past_due, canceled, etc.

    # Extract dates
    trial_start = _ts_to_iso(subscription.get("trial_start"))
    trial_end = _ts_to_iso(subscription.get("trial_end"))
    period_start = _ts_to_iso(subscription.get("current_period_start"))
    period_end = _ts_to_iso(subscription.get("current_period_end"))
    cancel_at_period_end = subscription.get("cancel_at_period_end", False)

    # Find email by customer ID
    email = _get_email_for_customer(customer_id)
    if not email:
        logger.warning("No email found for customer %s, skipping", customer_id)
        return

    upsert_subscription(
        email=email,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status=status,
        trial_start=trial_start,
        trial_end=trial_end,
        current_period_start=period_start,
        current_period_end=period_end,
        cancel_at_period_end=cancel_at_period_end,
    )
    logger.info("Subscription updated: %s status=%s", email, status)


def _handle_subscription_deleted(subscription: dict):
    """Handle subscription canceled/deleted."""
    customer_id = subscription.get("customer")
    email = _get_email_for_customer(customer_id)
    if not email:
        return

    upsert_subscription(
        email=email,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription.get("id"),
        status="canceled",
    )
    logger.info("Subscription canceled: %s", email)


def _handle_payment_failed(invoice: dict):
    """Handle failed payment (after trial ends and card is charged)."""
    customer_id = invoice.get("customer")
    email = _get_email_for_customer(customer_id)
    if not email:
        return

    upsert_subscription(email=email, status="past_due")
    logger.warning("Payment failed for %s", email)


# --- Helpers ---

def _get_email_for_customer(customer_id: str) -> str | None:
    """Look up email by Stripe customer ID from our DB."""
    sub = get_subscription_by_customer(customer_id)
    return sub["email"] if sub else None


def _ts_to_iso(ts: int | None) -> str | None:
    """Convert Unix timestamp to ISO string."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
