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

import base64
import json
import logging
import subprocess
import tempfile
import time
import httpx
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services.database import _connect_postgres, _use_postgres
from app.services.abandoned_cart import SELF_TEST_EMAILS

logger = logging.getLogger("artimagehub.dashboard")

DODO_PAGE_SIZE = 100

# T227: same exclusion list as scripts/artimagehub-clean-growth-report.py --
# confirmed 2026-07-07 as bot-crawler/founder-VPN noise, not real external
# traffic (China: 1.7% engagement / 1.5s avg session).
INTERNAL_COUNTRIES = {"Singapore", "Japan", "China"}
# GA4 event marking the moment a visitor enters the payment flow -- the
# closest available proxy for "attempted to start using the product" on a
# pay-first, no-login product (see T227 dispatch: this is 创始人's own
# interpretation, explicitly flagged as unconfirmed with founder).
FUNNEL_START_EVENT = "payment_click"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _ga4_access_token(sa_key_json: str) -> str:
    """Mints a GA4 Data API OAuth2 token from a service-account JSON, via the
    same openssl-signed JWT approach as artimagehub-clean-growth-report.py
    (no new crypto/google-auth dependency needed -- openssl is a standard
    system binary, unlike an added Python package)."""
    sa = json.loads(sa_key_json)
    now = int(time.time())
    token_uri = sa.get("token_uri", "https://oauth2.googleapis.com/token")
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "iss": sa["client_email"],
                "scope": "https://www.googleapis.com/auth/analytics.readonly",
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode()
    )
    unsigned = f"{header}.{payload}".encode("ascii")
    with tempfile.NamedTemporaryFile("w", delete=True) as key_file:
        key_file.write(sa["private_key"])
        key_file.flush()
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file.name],
            input=unsigned,
            capture_output=True,
            check=True,
        ).stdout
    assertion = f"{unsigned.decode('ascii')}.{_b64url(sig)}"
    resp = httpx.post(
        token_uri,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _ga4_run_report(body: dict) -> dict:
    settings = get_settings()
    if not settings.artimagehub_ga4_sa_key:
        raise RuntimeError("ARTIMAGEHUB_GA4_SA_KEY not configured")
    token = _ga4_access_token(settings.artimagehub_ga4_sa_key)
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{settings.ga4_property_id}:runReport"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _ga4_rows(raw: dict) -> list[tuple[list[str], list[int]]]:
    rows = []
    for row in raw.get("rows") or []:
        dims = [v.get("value", "") for v in row.get("dimensionValues") or []]
        metrics = [int(float(v.get("value", "0"))) for v in row.get("metricValues") or []]
        rows.append((dims, metrics))
    return rows


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


def get_funnel_panel(days: int = 30) -> dict:
    """T227 (2026-07-07, founder direct instruction): daily top-of-funnel —
    traffic / payment attempts / distinct visitors starting the flow.

    ① traffic: GA4 sessions/users, external-clean (INTERNAL_COUNTRIES excluded)
       -- same filter as artimagehub-clean-growth-report.py, deliberately
       reused rather than re-derived so this panel can't silently drift from
       the already-corrected methodology.
    ② payment attempts: every Dodo payment record (any status) for
       artimagehub's product_id, self-test emails excluded -- an "attempt"
       includes abandoned/failed/requires_payment_method, not just success
       (that's the orders panel).
    ③ "registration" proxy: distinct GA4 users firing payment_click per day,
       external-clean. This product is pay-first with no account system, so
       there is no real registration event -- 创始人's own interpretation
       (NOT founder-confirmed yet), labeled as such in the response so it
       can't be mistaken for a real signup metric.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)

    traffic_by_day: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "users": 0})
    funnel_start_by_day: dict[str, dict] = defaultdict(lambda: {"events": 0, "users": 0})
    ga4_error = None
    try:
        traffic_raw = _ga4_run_report({
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "date"}, {"name": "country"}],
            "metrics": [{"name": "sessions"}, {"name": "totalUsers"}],
            "limit": 5000,
        })
        for dims, metrics in _ga4_rows(traffic_raw):
            date_raw, country = dims
            if country in INTERNAL_COUNTRIES:
                continue
            sessions, users = metrics
            day = f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
            traffic_by_day[day]["sessions"] += sessions
            traffic_by_day[day]["users"] += users

        funnel_raw = _ga4_run_report({
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "date"}, {"name": "country"}],
            "metrics": [{"name": "eventCount"}, {"name": "totalUsers"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "eventName",
                    "stringFilter": {"matchType": "EXACT", "value": FUNNEL_START_EVENT},
                }
            },
            "limit": 5000,
        })
        for dims, metrics in _ga4_rows(funnel_raw):
            date_raw, country = dims
            if country in INTERNAL_COUNTRIES:
                continue
            events, users = metrics
            day = f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
            funnel_start_by_day[day]["events"] += events
            funnel_start_by_day[day]["users"] += users
    except Exception as exc:
        logger.exception("GA4 funnel fetch failed")
        ga4_error = str(exc)

    payments_by_day: dict[str, int] = defaultdict(int)
    dodo_error = None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        raw = _fetch_dodo_payments_since(cutoff)
        for item in raw:
            email = ((item.get("customer") or {}).get("email") or "").strip().lower()
            if email in SELF_TEST_EMAILS:
                continue
            if not _is_real_artimagehub_payment(item):
                continue
            created_dt = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            payments_by_day[created_dt.strftime("%Y-%m-%d")] += 1
    except Exception as exc:
        logger.exception("Dodo payment-attempts fetch failed")
        dodo_error = str(exc)

    all_days = sorted(set(traffic_by_day) | set(funnel_start_by_day) | set(payments_by_day))
    series = [
        {
            "date": day,
            "sessions_external": traffic_by_day[day]["sessions"],
            "users_external": traffic_by_day[day]["users"],
            "payment_attempts": payments_by_day.get(day, 0),
            "funnel_start_users_external": funnel_start_by_day[day]["users"],
            "funnel_start_events_external": funnel_start_by_day[day]["events"],
        }
        for day in all_days
    ]
    return {
        "days": days,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "series": series,
        "totals": {
            "sessions_external": sum(r["sessions_external"] for r in series),
            "users_external": sum(r["users_external"] for r in series),
            "payment_attempts": sum(r["payment_attempts"] for r in series),
            "funnel_start_users_external": sum(r["funnel_start_users_external"] for r in series),
        },
        "notes": {
            "traffic_filter": "GA4 sessions/users with INTERNAL_COUNTRIES (Singapore/Japan/China) excluded, matching scripts/artimagehub-clean-growth-report.py",
            "payment_attempts_definition": "any Dodo payment record (succeeded/failed/requires_payment_method/etc) for artimagehub's product_id, self-test emails excluded -- not just successful orders",
            "registration_caveat": "\"registration\" has no real meaning on this pay-first, no-login product. funnel_start_users_external = distinct external visitors who fired the payment_click event (start of the payment flow), used as a proxy. This is 创始人's own interpretation, NOT yet confirmed with founder -- do not read this as a real signup count.",
        },
        "errors": {"ga4": ga4_error, "dodo": dodo_error},
    }


def get_dashboard_snapshot(days: int = 30, granularity: str = "day") -> dict:
    """All four panels in one call, each independently fault-isolated --
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
    try:
        out["funnel"] = get_funnel_panel(days=days)
    except Exception as exc:
        logger.exception("funnel panel failed")
        out["funnel"] = {"error": str(exc)}
    return out
