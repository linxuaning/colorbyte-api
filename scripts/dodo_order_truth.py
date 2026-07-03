"""Ground-truth paid-order source for the daily metrics report (T213).

`/api/metrics/payment-successes` (and the `payment_successes` field inside
`/api/metrics/payment-funnel-breakdown`) reads a Postgres table whose webhook
write path has been silent since the 6/11 Render migration — it reports
count=0 while real orders keep happening. This module replaces that data
source with Dodo's own API, which is authoritative for what was actually
charged.

Design notes:
  - Filtered to the artimagehub product (`product_cart[].product_id`), not
    `total_amount`, since amount varies with tax/currency (see memory
    reference_dodo_shared_account_contamination.md). This Dodo account is
    shared with sibling products (mbtiusa, test-you) at different price
    points — product_id is the only reliable boundary.
  - Attribution comes directly from each payment's own `metadata` (the same
    dict the backend passes into `create_checkout_session`, which Dodo
    stores and returns verbatim on the payment record) — no join against the
    local `payment_initiations` table is needed for this. Older payments
    (pre-dating the attribution-metadata feature, observed for records
    before ~2026-05) lack these fields entirely; those are labeled
    "unknown" rather than guessed at.
  - Self-test emails are excluded per the locked filter in memory
    (reference_artimagehub_self_test_filter.md): linxuaning98@gmail.com,
    181420491@qq.com, linxuaning@qq.com.
  - Dedup is natural: Dodo's payment_id is the charge-level primary key, so
    there is no double-counting risk the way there can be with local
    initiation rows (a customer can create multiple checkout sessions but
    each successful charge is its own payment_id).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DODO_API_BASE = "https://live.dodopayments.com"
DODO_ARTIMAGEHUB_PRODUCT_ID = "pdt_0NcPHNyTthqNlXt3sjLjk"
DODO_SELF_TEST_EMAILS = {"linxuaning98@gmail.com", "181420491@qq.com", "linxuaning@qq.com"}

# Real order volume is roughly 0.5-2/day across this Dodo account's several
# products combined; this page cap is generous headroom for the widest
# report window (168h) without risking an unbounded loop against a live API.
_MAX_LIST_PAGES = 25
_LIST_PAGE_SIZE = 100


def _dodo_key() -> str:
    return os.environ.get("DODO_PAYMENTS_API_KEY", "").strip()


def _dodo_get(path: str, key: str) -> dict:
    req = urllib.request.Request(
        f"{DODO_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {key}",
            # Cloudflare in front of live.dodopayments.com 403s the default
            # Python-urllib UA (same class of block hit elsewhere in this repo).
            "User-Agent": "artimagehub-daily-metrics/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _parse_created_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_dodo_real_orders(hours: int) -> dict:
    """Real artimagehub paid orders in the trailing `hours`, sourced from Dodo.

    Returns a dict shaped for daily-metrics.py's report:
      available, count, revenue_usd, non_usd_payment_ids,
      excluded_self_test, excluded_foreign_product, unmatched_attribution,
      breakdown (list of {landing_page, cta_slot, entry_variant,
      checkout_source, count}, sorted by count desc).
    On any fetch error, `available` is False and callers should fall back to
    treating the window as unknown (not zero — a fetch failure must never be
    read as "no orders").
    """
    key = _dodo_key()
    if not key:
        return {"available": False, "reason": "DODO_PAYMENTS_API_KEY not set", "count": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    succeeded_candidates = []

    try:
        for page in range(_MAX_LIST_PAGES):
            data = _dodo_get(f"/payments?page_size={_LIST_PAGE_SIZE}&page_number={page}", key)
            items = data.get("items") or []
            if not items:
                break
            reached_cutoff = False
            for p in items:
                created = _parse_created_at(p.get("created_at", ""))
                if created is None:
                    continue
                if created < cutoff:
                    reached_cutoff = True
                    break
                if str(p.get("status", "")).lower() == "succeeded":
                    succeeded_candidates.append(p)
            if reached_cutoff or len(items) < _LIST_PAGE_SIZE:
                break
    except Exception as e:
        return {"available": False, "reason": f"list fetch failed: {e}", "count": 0}

    real_orders = []
    excluded_self_test = 0
    excluded_foreign_product = 0

    for p in succeeded_candidates:
        email = (p.get("customer", {}).get("email") or "").strip().lower()
        if email in DODO_SELF_TEST_EMAILS:
            excluded_self_test += 1
            continue
        try:
            detail = _dodo_get(f"/payments/{p['payment_id']}", key)
        except Exception:
            # Detail fetch failure on one payment must not silently drop it
            # from the count as if it were a foreign-product exclusion.
            continue
        product_ids = {item.get("product_id") for item in (detail.get("product_cart") or [])}
        if DODO_ARTIMAGEHUB_PRODUCT_ID not in product_ids:
            excluded_foreign_product += 1
            continue
        real_orders.append(detail)

    total_revenue_cents = sum(
        (o.get("settlement_amount") if o.get("settlement_amount") is not None else o.get("total_amount")) or 0
        for o in real_orders
    )
    non_usd_payment_ids = [
        o["payment_id"] for o in real_orders
        if (o.get("settlement_currency") or o.get("currency")) not in (None, "USD")
    ]

    by_attr: dict[tuple, int] = {}
    unmatched_attribution = 0
    for o in real_orders:
        md = o.get("metadata") or {}
        lp = (md.get("landing_page") or "").strip()
        cs = (md.get("cta_slot") or "").strip()
        ev = (md.get("entry_variant") or "").strip()
        src = (md.get("checkout_source") or "").strip()
        if not any((lp, cs, ev, src)):
            unmatched_attribution += 1
        tup = (lp or "unknown", cs or "unknown", ev or "unknown", src or "unknown")
        by_attr[tup] = by_attr.get(tup, 0) + 1

    breakdown = [
        {
            "landing_page": lp, "cta_slot": cs, "entry_variant": ev,
            "checkout_source": src, "count": n,
        }
        for (lp, cs, ev, src), n in sorted(by_attr.items(), key=lambda kv: -kv[1])
    ]

    return {
        "available": True,
        "count": len(real_orders),
        "revenue_usd": total_revenue_cents / 100.0,
        "non_usd_payment_ids": non_usd_payment_ids,
        "excluded_self_test": excluded_self_test,
        "excluded_foreign_product": excluded_foreign_product,
        "unmatched_attribution": unmatched_attribution,
        "breakdown": breakdown,
    }
