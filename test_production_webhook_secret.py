#!/usr/bin/env python3
"""Test if production webhook secret matches our local one."""
import hmac
import hashlib
import json
import requests

BACKEND_URL = "https://colorbyte-api.onrender.com"
LOCAL_SECRET = "ZWY0ODVmZjVkNTIwM"  # From .env

# Simple test payload
test_payload = {
    "meta": {"event_name": "subscription_created"},
    "data": {
        "type": "subscriptions",
        "id": "test-123",
        "attributes": {
            "customer_id": 999,
            "user_email": "secret-test@test.com",
            "status": "on_trial",
            "trial_ends_at": "2026-03-01T00:00:00Z",
            "renews_at": "2026-03-01T00:00:00Z",
        }
    }
}

payload_str = json.dumps(test_payload)
payload_bytes = payload_str.encode()

# Generate signature with local secret
signature = hmac.new(
    LOCAL_SECRET.encode(),
    payload_bytes,
    hashlib.sha256
).hexdigest()

print("🧪 Testing Webhook Secret Match")
print("=" * 60)
print(f"Local Secret: {LOCAL_SECRET}")
print(f"Signature: {signature[:30]}...")
print()

# Test with correct signature
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

if response.status_code == 200:
    print("✅ **SECRET MATCHES!**")
    print("   Webhook secret in Render matches local .env")
    print("   The issue must be something else.")
elif response.status_code == 400 and "Invalid signature" in response.text:
    print("❌ **SECRET MISMATCH!**")
    print("   Webhook secret in Render is DIFFERENT from local .env")
    print("   This is why webhooks are failing!")
    print()
    print("   🔧 FIX: Update LEMONSQUEEZY_WEBHOOK_SECRET in Render to:")
    print(f"       {LOCAL_SECRET}")
else:
    print(f"⚠️  Unexpected response: {response.status_code}")
    print("   Manual investigation needed.")

# Also test with wrong signature
print("\n" + "=" * 60)
print("🧪 Testing with WRONG signature (sanity check)...")
wrong_sig = "0" * 64
response2 = requests.post(
    f"{BACKEND_URL}/api/payment/webhook",
    headers={
        "Content-Type": "application/json",
        "x-signature": wrong_sig,
    },
    data=payload_str,
)
print(f"Response: {response2.status_code} - {response2.text[:100]}")
if response2.status_code == 400:
    print("✅ Signature verification is working correctly")
