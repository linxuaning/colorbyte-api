#!/usr/bin/env python3
"""Test Buy Me a Coffee webhook integration."""
import requests
import json

BACKEND_URL = "http://localhost:8000"
BMC_WEBHOOK_SECRET = "test_secret_123"  # Match with .env
TEST_EMAIL = "bmc-test@artimagehub.com"

# Simulated BMC webhook payload for new membership
webhook_payload = {
    "event": "supporter.new_membership",
    "data": {
        "supporter_id": "sup_bmc12345",
        "supporter_name": "Test User",
        "supporter_email": TEST_EMAIL,
        "support_coffee_count": 5,
        "support_message": "Thanks for the great tool!",
        "membership_id": "mem_xyz789",
        "membership_level_id": "level_premium",
        "membership_level_name": "Premium Member",
        "is_monthly": True,
        "created_at": "2026-02-17T12:00:00Z"
    }
}

print("=" * 60)
print("🧪 Testing BMC Webhook Integration")
print("=" * 60)
print(f"Test Email: {TEST_EMAIL}")
print()

print("1️⃣  Sending webhook event: supporter.new_membership")
response = requests.post(
    f"{BACKEND_URL}/api/payment/bmc-webhook",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {BMC_WEBHOOK_SECRET}",
    },
    json=webhook_payload,
)

print(f"Webhook Response: {response.status_code}")
print(f"Response Body: {response.text}")
print()

if response.status_code != 200:
    print("❌ Webhook failed!")
    exit(1)

# Check subscription status
print("2️⃣  Checking subscription status...")
check_response = requests.get(
    f"{BACKEND_URL}/api/payment/subscription/{TEST_EMAIL}"
)

if check_response.status_code == 200:
    sub = check_response.json()
    print("✅ Subscription retrieved")
    print()
    print("📊 Subscription Details:")
    print(f"   Email: {sub.get('email')}")
    print(f"   Status: {sub.get('status')}")
    print(f"   Active: {sub.get('is_active')}")
    print(f"   Period End: {sub.get('current_period_end')}")
    print()

    if sub.get("status") == "active" and sub.get("is_active"):
        print("✅✅✅ SUCCESS! BMC webhook integration working!")
        print("   - Webhook processed ✓")
        print("   - Subscription created ✓")
        print("   - Status = active ✓")
    else:
        print("❌ FAILED! Subscription not active")
        print(f"   Expected: status='active', is_active=True")
        print(f"   Got: status='{sub.get('status')}', is_active={sub.get('is_active')}")
else:
    print(f"❌ Failed to retrieve subscription: {check_response.status_code}")
    print(f"   {check_response.text}")
