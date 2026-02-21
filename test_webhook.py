#!/usr/bin/env python3
"""Test LemonSqueezy webhook locally."""
import hmac
import hashlib
import json
import requests

# Test configuration
BACKEND_URL = "https://colorbyte-api.onrender.com"
WEBHOOK_SECRET = "ZWY0ODVmZjVkNTIwM"  # From .env
TEST_EMAIL = "e2e-test@artimagehub.com"

# Sample subscription_created webhook payload
# Based on LemonSqueezy API documentation
webhook_payload = {
    "meta": {
        "event_name": "subscription_created",
        "custom_data": {}
    },
    "data": {
        "type": "subscriptions",
        "id": "12345",
        "attributes": {
            "store_id": 295039,
            "customer_id": 67890,
            "order_id": 11111,
            "product_id": 123456,
            "variant_id": 1317124,
            "user_email": TEST_EMAIL,
            "user_name": "E2E Test",
            "status": "on_trial",
            "status_formatted": "On trial",
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

# Generate signature
signature = hmac.new(
    WEBHOOK_SECRET.encode(),
    payload_bytes,
    hashlib.sha256
).hexdigest()

print("🧪 Testing LemonSqueezy Webhook")
print("=" * 60)
print(f"Endpoint: {BACKEND_URL}/api/payment/webhook")
print(f"Event: subscription_created")
print(f"Email: {TEST_EMAIL}")
print(f"Signature: {signature[:20]}...")
print()

# Send webhook request
response = requests.post(
    f"{BACKEND_URL}/api/payment/webhook",
    headers={
        "Content-Type": "application/json",
        "x-signature": signature,
    },
    data=payload_str,
)

print(f"Response Status: {response.status_code}")
print(f"Response Body: {response.text}")
print()

# Check subscription status
print("🔍 Checking subscription status...")
check_response = requests.get(
    f"{BACKEND_URL}/api/payment/subscription/{TEST_EMAIL}"
)
print(f"Status: {check_response.status_code}")
print(f"Subscription Data: {check_response.json()}")
