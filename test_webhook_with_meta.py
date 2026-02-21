#!/usr/bin/env python3
"""Test webhook with meta.custom_data email extraction."""
import hmac
import hashlib
import json
import requests

BACKEND_URL = "http://localhost:8000"
WEBHOOK_SECRET = "ZWY0ODVmZjVkNTIwM"
TEST_EMAIL = "meta-test@artimagehub.com"

# Webhook payload with meta.custom_data
webhook_payload = {
    "meta": {
        "event_name": "subscription_created",
        "custom_data": {
            "email": TEST_EMAIL  # This is what we pass during checkout
        }
    },
    "data": {
        "type": "subscriptions",
        "id": "meta-sub-12345",
        "attributes": {
            "store_id": 295039,
            "customer_id": 88888,  # New customer, not in DB
            "product_id": 123456,
            "variant_id": 1317124,
            # NOTE: No user_email in subscription attributes!
            "status": "on_trial",
            "trial_ends_at": "2026-02-24T12:00:00.000000Z",
            "renews_at": "2026-02-24T12:00:00.000000Z",
            "ends_at": None,
            "created_at": "2026-02-17T12:00:00.000000Z",
            "updated_at": "2026-02-17T12:00:00.000000Z",
        }
    }
}

payload_str = json.dumps(webhook_payload)
payload_bytes = payload_str.encode()

signature = hmac.new(
    WEBHOOK_SECRET.encode(),
    payload_bytes,
    hashlib.sha256
).hexdigest()

print("🧪 Testing Webhook with meta.custom_data Email")
print("=" * 60)
print(f"Email in meta.custom_data: {TEST_EMAIL}")
print(f"Customer ID: 88888 (not in database)")
print(f"user_email in attributes: NOT PRESENT")
print()

response = requests.post(
    f"{BACKEND_URL}/api/payment/webhook",
    headers={
        "Content-Type": "application/json",
        "x-signature": signature,
    },
    data=payload_str,
)

print(f"Webhook Response: {response.status_code}")
print(f"Response Body: {response.text}")
print()

# Check if subscription was created
check_response = requests.get(
    f"{BACKEND_URL}/api/payment/subscription/{TEST_EMAIL}"
)
subscription = check_response.json()

print("Subscription Status:")
print(f"  Email: {subscription.get('email')}")
print(f"  Active: {subscription.get('is_active')}")
print(f"  Status: {subscription.get('status')}")
print()

if subscription.get("status") == "on_trial":
    print("✅ SUCCESS! Email extracted from meta.custom_data")
else:
    print("❌ FAILED! Subscription not created correctly")
    print(f"   Expected: status='on_trial', is_active=True")
    print(f"   Got: status='{subscription.get('status')}', is_active={subscription.get('is_active')}")
