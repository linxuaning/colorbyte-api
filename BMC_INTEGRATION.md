# Buy Me a Coffee Integration Guide

## Overview

Simple subscription system using Buy Me a Coffee (BMC) for payment processing.

**Implementation Status**: ✅ Infrastructure ready, ⏳ waiting for BMC page link

---

## What's Been Prepared

### 1. Database Schema ✅
- Added `payment_provider` field to support both LemonSqueezy and BMC
- Added `bmc_supporter_id` and `bmc_membership_id` fields
- Migration script ready: `migrate_to_bmc.py`

### 2. Backend Webhook ✅
- BMC webhook endpoint: `POST /api/payment/bmc-webhook`
- Handles events:
  - `supporter.new_membership` → Create subscription
  - `membership.updated` → Renew subscription
  - `membership.cancelled` → Cancel subscription
- Bearer token authentication
- Idempotency protection

### 3. Configuration ✅
- Environment variables template: `.env.bmc.template`
- Config fields added to `app/config.py`:
  - `BMC_API_TOKEN`
  - `BMC_WEBHOOK_SECRET`
  - `BMC_PAGE_URL`

### 4. Test Script ✅
- Local test: `test_bmc_webhook.py`
- Simulates BMC webhook payload
- Verifies subscription creation

---

## What's Still Needed (20 min after BMC link)

### Step 1: Get BMC Credentials (User Action)
1. User creates/provides BMC page URL
2. User gets API token from BMC dashboard (if available)
3. User sets webhook secret in BMC settings

### Step 2: Frontend Integration (10 min)
Update `photofix/frontend/src/app/pricing-section.tsx`:

```tsx
// Replace LemonSqueezy checkout button with BMC link
<a
  href={BMC_PAGE_URL}  // From user
  target="_blank"
  rel="noopener noreferrer"
  className="btn-primary"
>
  Subscribe via Buy Me a Coffee
</a>
```

### Step 3: Environment Configuration (5 min)
Update `.env` on Render:
```bash
BMC_API_TOKEN=your_token
BMC_WEBHOOK_SECRET=your_secret
BMC_PAGE_URL=https://buymeacoffee.com/username
```

### Step 4: Configure BMC Webhook (5 min)
In BMC dashboard:
- Webhook URL: `https://colorbyte-api.onrender.com/api/payment/bmc-webhook`
- Secret: `{same as BMC_WEBHOOK_SECRET in .env}`
- Events: ✅ All membership events

---

## Testing Checklist

### Local Testing
```bash
# 1. Run migration
python migrate_to_bmc.py

# 2. Update .env with test values
BMC_WEBHOOK_SECRET=test_secret_123

# 3. Start backend
uvicorn app.main:app --reload

# 4. Run webhook test
python test_bmc_webhook.py
```

Expected output:
```
✅✅✅ SUCCESS! BMC webhook integration working!
   - Webhook processed ✓
   - Subscription created ✓
   - Status = active ✓
```

### Production Testing
1. User subscribes via BMC page
2. BMC sends webhook to backend
3. Check subscription status: `GET /api/payment/subscription/{email}`
4. Verify status = "active", is_active = true

---

## BMC Webhook Payload Reference

### New Membership
```json
{
  "event": "supporter.new_membership",
  "data": {
    "supporter_id": "sup_xxx",
    "supporter_email": "user@example.com",
    "membership_id": "mem_xxx",
    "membership_level_name": "Premium",
    "is_monthly": true,
    "created_at": "2026-02-17T12:00:00Z"
  }
}
```

### Membership Cancelled
```json
{
  "event": "membership.cancelled",
  "data": {
    "supporter_email": "user@example.com",
    "membership_id": "mem_xxx"
  }
}
```

---

## Comparison: LemonSqueezy vs BMC

| Feature | LemonSqueezy | Buy Me a Coffee |
|---------|--------------|-----------------|
| API Complexity | High (500 lines) | Low (100 lines) |
| Trial Period | ✅ 7 days | ❌ No trials |
| Programmatic Cancel | ✅ Yes | ❌ Manual only |
| Webhook Events | 10+ events | 4 events |
| Implementation Time | 3-4 hours | 1 hour |
| Conversion Rate | Highest | Medium (-10-20%) |

---

## Timeline

- ✅ **Done (40 min)**: Database, webhook, config, tests
- ⏳ **Waiting**: User's BMC page link + credentials
- 🔜 **Final (20 min)**: Frontend update + deployment + testing

**Total**: ~1 hour from BMC link received to production ready

---

## Notes

- BMC doesn't support free trials (immediate payment required)
- No programmatic subscription management (users cancel via BMC)
- Period end calculated as +30 days from start (monthly assumption)
- Simple flat-rate pricing (no complex plans/tiers)
