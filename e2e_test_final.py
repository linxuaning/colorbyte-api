#!/usr/bin/env python3
"""Final E2E test after webhook fix deployment."""
import requests
import json

BACKEND_URL = "https://colorbyte-api.onrender.com"
TEST_EMAIL = "webhook-fix-test@artimagehub.com"

print("=" * 60)
print("🧪 E2E Test - Webhook Fix Verification")
print("=" * 60)
print(f"Test Email: {TEST_EMAIL}")
print()

# Step 1: Create checkout
print("1️⃣  Creating checkout session...")
response = requests.post(
    f"{BACKEND_URL}/api/payment/start-trial",
    json={"email": TEST_EMAIL}
)

if response.status_code == 200:
    data = response.json()
    checkout_url = data.get("checkout_url")
    session_id = data.get("session_id")
    print(f"✅ Checkout created!")
    print(f"   Session ID: {session_id}")
    print(f"   Checkout URL: {checkout_url[:80]}...")
    print()
    print("🔗 Please complete payment at:")
    print(f"   {checkout_url}")
    print()
    print("📌 After payment, press ENTER to check subscription status...")
    input()
else:
    print(f"❌ Failed: {response.status_code}")
    print(f"   {response.text}")
    exit(1)

# Step 2: Check subscription status
print("\n2️⃣  Checking subscription status...")
response = requests.get(
    f"{BACKEND_URL}/api/payment/subscription/{TEST_EMAIL}"
)

if response.status_code == 200:
    sub = response.json()
    print(f"✅ Subscription data retrieved")
    print()
    print("📊 Subscription Details:")
    print(f"   Email: {sub.get('email')}")
    print(f"   Status: {sub.get('status')}")
    print(f"   Active: {sub.get('is_active')}")
    print(f"   Trial End: {sub.get('trial_end')}")
    print(f"   Period End: {sub.get('current_period_end')}")
    print()

    if sub.get("status") in ("on_trial", "active") and sub.get("is_active"):
        print("✅✅✅ SUCCESS! Webhook fix verified!")
        print("   - Checkout completed ✓")
        print("   - Webhook processed ✓")
        print("   - Subscription synced ✓")
    else:
        print("❌ FAILED! Webhook still not working")
        print(f"   Expected: status in ('on_trial', 'active'), is_active=True")
        print(f"   Got: status='{sub.get('status')}', is_active={sub.get('is_active')}")
else:
    print(f"❌ Failed: {response.status_code}")
    print(f"   {response.text}")
