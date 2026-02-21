#!/usr/bin/env python3
"""Check recent webhook delivery attempts."""
import requests
import json

API_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiI5NGQ1OWNlZi1kYmI4LTRlYTUtYjE3OC1kMjU0MGZjZDY5MTkiLCJqdGkiOiJjMmJlM2NhNzE3YjJmMDcxYzliNjk2ZGNmMTZmMWNiZjk1OTRlMzgxMWUxMjAyYWIzNmFiNGFlMzcwNjUyZjJhYjk2NzczNzUzMzhjYzZkOSIsImlhdCI6MTc3MTMzODk2OC4zOTQ0NDYsIm5iZiI6MTc3MTMzODk2OC4zOTQ0NDksImV4cCI6MTc4NjkyNDgwMC4wNDAxNzUsInN1YiI6IjY1MzM4MjYiLCJzY29wZXMiOltdfQ.4oapMxg4r7ORZuAKxKFO9Q3V_G9MGVWlZbV7yPSAsI98zM_f3T0xmyiMy7bk9ysxhoxSoU6PDxxHehU-22VU9HTbk0qe_2cmysF0O4oFKwg-FejGI5QZ3qWMs8Fk5oD2clUxUTML9-2ZnejZBIwnq6r15VV7OZ7tWF-LeGMhMz8RqUu4VIWI3eK2KOhy9iCdKeKrxqkhYiS-u8OxE8MZUaZ1QfBBTsdeWi99IJpsca60hhUZ-luKncl6iYhBs906-Q4A-qnmlHm-VmmGX0vESZPfiAqN2eht-BUlo2Y4qUhjTjtQXmPxlZCpd9ML9gPk6Ilhb-ay8AEFh1RKUE212H_Z89UKn8ZzZOW4kNlbiLhMKWBDozYwLly7iNS4QliyAmwTIVcAM9uYNMzqJeyWSFZ6TWuUp-uneSCKUTPAS_UP2bIbO-VF0qrDHiEkAn3Q-uu23hopX8SXWFHoxti6YYL1aEnQEpijTzIHRg2qshBE3uaUil0aKlHMr3dmQuDssxAiS8C5wTFzxu35-JsdqKkfxPUNofLSvMna3n3DKTDCEbh-hY--Yv_xuiIqtb_IPgAXWBTa8aiAr8SpVYl_P1wVoGiOSKhIlyi1EKEnkwDCTPEnkZJK8W0bEcsRz8fK7FYNkjODTUW_AkWnYfDofm2Us6ZGDG0DQlK7cYXgM2A"

headers = {
    "Accept": "application/vnd.api+json",
    "Authorization": f"Bearer {API_KEY}"
}

WEBHOOK_ID = "74720"  # The correct webhook

print("📡 Checking Recent Webhook Deliveries")
print("=" * 60)

# Get webhook deliveries (this endpoint might not be available in the API)
# Let's try the webhook details endpoint
response = requests.get(
    f"https://api.lemonsqueezy.com/v1/webhooks/{WEBHOOK_ID}",
    headers=headers
)

if response.status_code == 200:
    data = response.json()
    webhook = data.get("data", {})
    attrs = webhook.get("attributes", {})

    print("Webhook Details:")
    print(f"  ID: {webhook.get('id')}")
    print(f"  URL: {attrs.get('url')}")
    print(f"  Events: {attrs.get('events')}")
    print(f"  Last Sent: {attrs.get('last_sent_at')}")
    print(f"  Test Mode: {attrs.get('test_mode')}")
    print()

    # Print full attributes to see what else is available
    print("Full attributes:")
    print(json.dumps(attrs, indent=2))
else:
    print(f"❌ Error: {response.status_code}")
    print(response.text)

print("\n" + "=" * 60)
print("📝 Note: LemonSqueezy API might not expose delivery logs.")
print("Please check the LemonSqueezy Dashboard → Settings → Webhooks")
print("to see detailed delivery attempts and error messages.")
