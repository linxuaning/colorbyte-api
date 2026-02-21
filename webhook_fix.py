"""
WEBHOOK EMAIL EXTRACTION FIX

The problem: LemonSqueezy subscription_created webhooks don't include user_email in attributes.
They only have customer_id. But if subscription_created arrives BEFORE order_created,
we don't have a customer_id -> email mapping yet.

SOLUTION: Extract email from multiple possible locations:
1. attributes.user_email (for orders)
2. attributes.customer_email (alternative field)
3. meta.custom_data.email (if we pass it during checkout)
4. Look up by customer_id (fallback if record exists)
5. Store pending subscriptions and link them when order arrives

This improved handler should be added to payment.py
"""

def _handle_subscription_update_improved(subscription: dict, meta: dict = None):
    """Handle subscription created/updated events with robust email extraction."""
    attrs = subscription.get("attributes", {})
    sub_id = subscription.get("id")
    customer_id = attrs.get("customer_id")
    status = attrs.get("status")

    trial_ends_at = attrs.get("trial_ends_at")
    renews_at = attrs.get("renews_at")
    ends_at = attrs.get("ends_at")

    # TRY MULTIPLE EMAIL SOURCES (in order of reliability)
    email = None

    # 1. Direct from subscription attributes
    email = attrs.get("user_email") or attrs.get("customer_email")

    # 2. From webhook meta custom_data (if we passed it during checkout)
    if not email and meta:
        custom_data = meta.get("custom_data", {})
        email = custom_data.get("email")

    # 3. From related customer/order objects (if included)
    if not email:
        relationships = subscription.get("relationships", {})
        customer = relationships.get("customer", {}).get("data", {})
        # Note: This won't have email either, but we can try customer.attributes if loaded

    # 4. Look up by customer_id in existing records
    if not email and customer_id:
        sub_record = get_subscription_by_customer(str(customer_id))
        if sub_record:
            email = sub_record["email"]

    # 5. If still no email, store as pending and wait for order_created
    if not email:
        logger.warning(
            "No email found for subscription %s (customer %s). "
            "This is likely because subscription_created arrived before order_created. "
            "Storing pending subscription.",
            sub_id, customer_id
        )

        # Store in a pending_subscriptions table or cache
        # When order_created arrives, process all pending subscriptions for that customer
        _store_pending_subscription(
            customer_id=str(customer_id),
            subscription_id=str(sub_id),
            status=status,
            trial_ends_at=trial_ends_at,
            renews_at=renews_at,
            ends_at=ends_at
        )
        return

    # Process subscription with email
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


def _handle_order_created_improved(order: dict):
    """Handle order_created with pending subscription processing."""
    attrs = order.get("attributes", {})
    email = attrs.get("user_email") or attrs.get("customer_email", "")
    customer_id = attrs.get("customer_id")
    first_subscription_item = attrs.get("first_subscription_item")

    if not email or not customer_id:
        logger.error("Order created without email or customer_id: %s", order.get("id"))
        return

    subscription_id = None
    if first_subscription_item:
        subscription_id = first_subscription_item.get("subscription_id")

    # Create/update subscription record
    upsert_subscription(
        email=email,
        lemonsqueezy_customer_id=str(customer_id),
        lemonsqueezy_subscription_id=str(subscription_id) if subscription_id else None,
        status="on_trial",
    )
    logger.info("Order created: %s → customer=%s sub=%s", email, customer_id, subscription_id)

    # Check if there are pending subscriptions for this customer
    # Process them now that we have the email
    pending = _get_pending_subscriptions(customer_id=str(customer_id))
    if pending:
        logger.info("Processing %d pending subscription(s) for customer %s", len(pending), customer_id)
        for pending_sub in pending:
            # Update with the actual subscription data
            upsert_subscription(
                email=email,
                lemonsqueezy_customer_id=str(customer_id),
                lemonsqueezy_subscription_id=pending_sub["subscription_id"],
                status=pending_sub["status"],
                trial_end=pending_sub.get("trial_ends_at"),
                current_period_end=pending_sub.get("renews_at") or pending_sub.get("ends_at"),
            )
        _clear_pending_subscriptions(customer_id=str(customer_id))


print("""
RECOMMENDED FIX:

1. Add pending_subscriptions table to database schema
2. Update webhook handler to pass meta dict to _handle_subscription_update
3. Implement pending subscription storage/retrieval functions
4. Replace current handlers with improved versions above

OR SIMPLER FIX:

Pass email in custom_data during checkout creation:
- In start-trial endpoint, add: "checkout_data": {"custom": {"email": email}}
- In webhook handler, extract: meta.get("custom_data", {}).get("email")
""")
