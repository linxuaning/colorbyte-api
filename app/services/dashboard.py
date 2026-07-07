"""
T224: operational dashboard aggregation (founder direct instruction, 2026-07-07).

Three panels, MVP scope only:
  1. orders   — real Dodo payment truth (payment_successes is broken, see
                reference_backend_metrics_broken_use_dodo.md; NEVER use it)
  2. task health — per feature_key task volume/success rate from persistent_tasks
  3. customers — dedup + repeat-purchase rate, derived from the same Dodo pull

Self-test emails and non-artimagehub (shared Dodo merchant account) payments
are excluded from orders/customers. Task health intentionally includes all
tasks (including self-test) since it's a system-reliability signal, not a
revenue signal.
"""
from __future__ import annotations

import logging
import httpx
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services.database import _connect_postgres, _use_postgres
from app.services.abandoned_cart import SELF_TEST_EMAILS

logger = logging.getLogger("artimagehub.dashboard")

DODO_PAGE_SIZE = 100


def _dodo_get(path: str, api_key: str) -> dict:
    r = httpx.get(
        f"https://live.dodopayments.com{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


def _fetch_dodo_payments_since(cutoff: datetime) -> list[dict]:
    """Paginate Dodo's /payments (newest-first) until we're past the cutoff.
    Raises on any API error -- callers should surface that, not silently show
    an empty/zero dashboard (a broken order panel must look broken, not calm)."""
    settings = get_settings()
    if not settings.dodo_payments_api_key:
        raise RuntimeError("DODO_PAYMENTS_API_KEY not configured")
    out: list[dict] = []
    page = 0  # Dodo's /payments page_number is 0-indexed (verified empirically:
              # page_number=0 returns the newest page; page_number=1 skips it)
    while True:
        data = _dodo_get(f"/payments?page_size={DODO_PAGE_SIZE}&page_number={page}", settings.dodo_payments_api_key)
        items = data.get("items", [])
        if not items:
            break
        stop = False
        for item in items:
            created = item.get("created_at")
            if not created:
                continue
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt < cutoff:
                stop = True
                continue
            out.append(item)
        if stop or len(items) < DODO_PAGE_SIZE:
            break
        page += 1
        if page > 50:  # hard circuit breaker, not an expected real case
            logger.warning("dodo payments pagination exceeded 50 pages, stopping")
            break
    return out


def _is_real_artimagehub_payment(item: dict) -> bool:
    """The Dodo account is shared across sibling products (test-you/mbti/
    artimagehub) — filter by product_id, the only reliable discriminator
    (see reference_dodo_shared_account_contamination.md). metadata.feature_key
    is only ever set by artimagehub's checkout, which is a second signal."""
    settings = get_settings()
    md = item.get("metadata") or {}
    product_id = item.get("product_id") or md.get("product_id")
    if product_id:
        return product_id == settings.dodo_payments_product_id
    return "feature_key" in md


def _period_key(dt: datetime, granularity: str) -> str:
    if granularity == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "month":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


def get_orders_panel(days: int = 30, granularity: str = "day") -> dict:
    if granularity not in ("day", "week", "month"):
        granularity = "day"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    raw = _fetch_dodo_payments_since(cutoff)

    buckets: dict[str, dict] = defaultdict(lambda: {"orders": 0, "revenue_usd": 0.0})
    excluded_self_test = 0
    excluded_other_product = 0
    included = 0
    for item in raw:
        if item.get("status") != "succeeded":
            continue
        email = ((item.get("customer") or {}).get("email") or "").strip().lower()
        if email in SELF_TEST_EMAILS:
            excluded_self_test += 1
            continue
        if not _is_real_artimagehub_payment(item):
            excluded_other_product += 1
            continue
        created_dt = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        key = _period_key(created_dt, granularity)
        buckets[key]["orders"] += 1
        buckets[key]["revenue_usd"] += (item.get("total_amount") or 0) / 100.0
        included += 1

    series = [
        {"period": k, "orders": v["orders"], "revenue_usd": round(v["revenue_usd"], 2)}
        for k, v in sorted(buckets.items())
    ]
    return {
        "granularity": granularity,
        "days": days,
        "series": series,
        "totals": {
            "orders": included,
            "revenue_usd": round(sum(v["revenue_usd"] for v in buckets.values()), 2),
        },
        "excluded_self_test": excluded_self_test,
        "excluded_other_product": excluded_other_product,
        "source": "dodo_live_api",
    }


def get_customers_panel(days: int = 30) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    raw = _fetch_dodo_payments_since(cutoff)

    per_email: dict[str, int] = defaultdict(int)
    for item in raw:
        if item.get("status") != "succeeded":
            continue
        email = ((item.get("customer") or {}).get("email") or "").strip().lower()
        if not email or email in SELF_TEST_EMAILS:
            continue
        if not _is_real_artimagehub_payment(item):
            continue
        per_email[email] += 1

    total_customers = len(per_email)
    repeat_customers = sum(1 for c in per_email.values() if c > 1)
    return {
        "days": days,
        "unique_customers": total_customers,
        "repeat_customers": repeat_customers,
        "repeat_rate": round(repeat_customers / total_customers, 4) if total_customers else 0.0,
        "source": "dodo_live_api",
    }


_FEATURE_KEYS = ("restoration", "denoising", "deblurring", "jpeg-fix")


def get_task_health_panel(days: int = 30) -> dict:
    """Per feature_key task volume/success rate. persistent_tasks is the
    source of truth for ALL task outcomes (completed/failed/processing),
    unlike processing_events which only records completions."""
    if not _use_postgres():
        return {"days": days, "error": "postgres not configured", "features": {}}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result: dict[str, dict] = {}
    with _connect_postgres() as conn:
        for feature_key in _FEATURE_KEYS:
            rows = conn.execute(
                """
                SELECT task_json->>'status' AS status, count(*) AS n
                FROM persistent_tasks
                WHERE task_json->>'feature_key' = %s AND created_at >= %s
                GROUP BY 1
                """,
                (feature_key, cutoff),
            ).fetchall()
            by_status = {r["status"]: r["n"] for r in rows}
            total = sum(by_status.values())
            completed = by_status.get("completed", 0)
            failed = by_status.get("failed", 0)
            processing = by_status.get("processing", 0)

            mode_rows = conn.execute(
                """
                SELECT pe.mode AS mode, count(*) AS n
                FROM processing_events pe
                JOIN persistent_tasks pt ON pt.task_id = pe.task_id
                WHERE pt.task_json->>'feature_key' = %s AND pt.created_at >= %s
                GROUP BY 1
                """,
                (feature_key, cutoff),
            ).fetchall()
            fallback_count = sum(r["n"] for r in mode_rows if "fallback" in (r["mode"] or ""))

            result[feature_key] = {
                "total": total,
                "completed": completed,
                "failed": failed,
                "processing": processing,
                "success_rate": round(completed / total, 4) if total else None,
                "fallback_count": fallback_count,
                "fallback_rate": round(fallback_count / completed, 4) if completed else None,
            }
    return {"days": days, "features": result}


def get_dashboard_snapshot(days: int = 30, granularity: str = "day") -> dict:
    """All three panels in one call, each independently fault-isolated --
    a Dodo API hiccup must not blank out the (locally-sourced) task-health
    panel, and vice versa."""
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        out["orders"] = get_orders_panel(days=days, granularity=granularity)
    except Exception as exc:
        logger.exception("orders panel failed")
        out["orders"] = {"error": str(exc)}
    try:
        out["customers"] = get_customers_panel(days=days)
    except Exception as exc:
        logger.exception("customers panel failed")
        out["customers"] = {"error": str(exc)}
    try:
        out["task_health"] = get_task_health_panel(days=days)
    except Exception as exc:
        logger.exception("task_health panel failed")
        out["task_health"] = {"error": str(exc)}
    return out
