#!/usr/bin/env python3
"""Check LemonSqueezy webhook configuration."""
import requests
import json

API_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiI5NGQ1OWNlZi1kYmI4LTRlYTUtYjE3OC1kMjU0MGZjZDY5MTkiLCJqdGkiOiJjMmJlM2NhNzE3YjJmMDcxYzliNjk2ZGNmMTZmMWNiZjk1OTRlMzgxMWUxMjAyYWIzNmFiNGFlMzcwNjUyZjJhYjk2NzczNzUzMzhjYzZkOSIsImlhdCI6MTc3MTMzODk2OC4zOTQ0NDYsIm5iZiI6MTc3MTMzODk2OC4zOTQ0NDksImV4cCI6MTc4NjkyNDgwMC4wNDAxNzUsInN1YiI6IjY1MzM4MjYiLCJzY29wZXMiOltdfQ.4oapMxg4r7ORZuAKxKFO9Q3V_G9MGVWlZbV7yPSAsI98zM_f3T0xmyiMy7bk9ysxhoxSoU6PDxxHehU-22VU9HTbk0qe_2cmysF0O4oFKwg-FejGI5QZ3qWMs8Fk5oD2clUxUTML9-2ZnejZBIwnq6r15VV7OZ7tWF-LeGMhMz8RqUu4VIWI3eK2KOhy9iCdKeKrxqkhYiS-u8OxE8MZUaZ1QfBBTsdeWi99IJpsca60hhUZ-luKncl6iYhBs906-Q4A-qnmlHm-VmmGX0vESZPfiAqN2eht-BUlo2Y4qUhjTjtQXmPxlZCpd9ML9gPk6Ilhb-ay8AEFh1RKUE212H_Z89UKn8ZzZOW4kNlbiLhMKWBDozYwLly7iNS4QliyAmwTIVcAM9uYNMzqJeyWSFZ6TWuUp-uneSCKUTPAS_UP2bIbO-VF0qrDHiEkAn3Q-uu23hopX8SXWFHoxti6YYL1aEnQEpijTzIHRg2qshBE3uaUil0aKlHMr3dmQuDssxAiS8C5wTFzxu35-JsdqKkfxPUNofLSvMna3n3DKTDCEbh-hY--Yv_xuiIqtb_IPgAXWBTa8aiAr8SpVYl_P1wVoGiOSKhIlyi1EKEnkwDCTPEnkZJK8W0bEcsRz8fK7FYNkjODTUW_AkWnYfDofm2Us6ZGDG0DQlK7cYXgM2A"
STORE_ID = "295039"

headers = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "Authorization": f"Bearer {API_KEY}"
}

print("🔍 Checking LemonSqueezy Webhook Configuration")
print("=" * 60)

# List all webhooks
response = requests.get(
    f"https://api.lemonsqueezy.com/v1/webhooks?filter[store_id]={STORE_ID}",
    headers=headers
)

if response.status_code == 200:
    data = response.json()
    webhooks = data.get("data", [])

    print(f"Found {len(webhooks)} webhook(s) configured:\n")

    for webhook in webhooks:
        attrs = webhook.get("attributes", {})
        events = attrs.get("events", [])
        if isinstance(events, list):
            events_str = ', '.join(events)
        else:
            events_str = str(events)

        print(f"Webhook ID: {webhook.get('id')}")
        print(f"  URL: {attrs.get('url')}")
        print(f"  Events: {events_str}")
        print(f"  Test Mode: {attrs.get('test_mode')}")
        print(f"  Last Sent: {attrs.get('last_sent_at')}")
        print(f"  Created: {attrs.get('created_at')}")
        print()

    if not webhooks:
        print("❌ No webhooks configured!")
        print("\n⚠️  ACTION REQUIRED:")
        print("   1. Go to LemonSqueezy Dashboard → Settings → Webhooks")
        print("   2. Create a new webhook with:")
        print("      URL: https://colorbyte-api.onrender.com/api/payment/webhook")
        print("      Events: subscription_created, subscription_updated, subscription_cancelled, order_created")
        print("      Secret: ZWY0ODVmZjVkNTIwM")
else:
    print(f"❌ API Error: {response.status_code}")
    print(response.text)
