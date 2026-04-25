# Manual Grant Access (Support Workflow)

**Use case:** A user emails saying "I paid but the site says I need to pay again."
This typically happens when their subscription record was lost during the pre-PG sqlite ephemeral era. Restore their access with one curl.

## Prerequisites

- `ADMIN_SECRET` env var available locally (same value as Render `ADMIN_SECRET`).
- Get the user's email from their support message.

## Command

```bash
# Set ADMIN_SECRET in your shell once per session — DO NOT paste the value into this command directly:
export ADMIN_SECRET='<value from Render dashboard>'

# Then run:
curl -X POST https://backend.artimagehub.com/api/admin/grant-access \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "payment_provider": "manual",
    "note": "grandfathered from pre-PG sqlite cutover"
  }'
```

## Expected response (success)

```json
{
  "ok": true,
  "email": "user@example.com",
  "subscription": {
    "status": "active",
    "payment_provider": "manual",
    ...
  }
}
```

## Verify the grant landed

```bash
curl "https://backend.artimagehub.com/api/check-limit?email=user@example.com"
# Should show: "is_subscriber": true
```

## After granting

Reply to the user: "Access restored — please refresh the page and try again. Sorry for the trouble."

## Notes

- The endpoint is idempotent: running it twice for the same email is safe.
- `note` is a free-form audit string stored on the subscription row.
- If the response is `401 Unauthorized`, double-check `ADMIN_SECRET` matches the value in Render's environment.
- Once Postgres is the active backend, grants persist across Render redeploys (no longer wiped on cold start).
