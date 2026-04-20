#!/usr/bin/env python3
"""Daily growth/revenue metrics email for artimagehub.com.

Pulls 24h numbers from the backend `/api/metrics/*` endpoints and the prior
24h window for delta context, then sends a clean text email via Resend.
Runs once a day from a GitHub Actions cron at 00:00 UTC (08:00 Beijing).

Required env:
    RESEND_API_KEY      — for outbound email
    METRICS_BASE        — backend base URL (default https://colorbyte-api.onrender.com)
    ALERT_TO            — recipient (default linxuaning98@gmail.com)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


METRICS_BASE = os.environ.get("METRICS_BASE", "https://colorbyte-api.onrender.com").rstrip("/")
ALERT_TO = os.environ.get("ALERT_TO", "linxuaning98@gmail.com")
ALERT_FROM = os.environ.get("ALERT_FROM", "support@artimagehub.com")  # alerts@ isn't verified in Resend → 403
DASHBOARD_HINT = "https://artimagehub.com"


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"[fatal] missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return val


def fetch(path: str, hours: int) -> dict:
    url = f"{METRICS_BASE}{path}?hours={hours}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e), "count": 0, "by_provider": {}, "by_mode": {}}


def delta(now: int, prev: int) -> str:
    if prev == 0 and now == 0:
        return "—"
    if prev == 0:
        return f"+{now} (new)"
    diff = now - prev
    pct = (diff / prev) * 100
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff} ({sign}{pct:.0f}%)"


def fetch_webhook_health() -> dict:
    """Quick webhook health snapshot using Render API. Returns counts in last 24h."""
    render_key = os.environ.get("RENDER_API_KEY", "").strip()
    owner = os.environ.get("RENDER_OWNER_ID", "").strip()
    service = os.environ.get("RENDER_SERVICE_ID", "").strip()
    if not (render_key and owner and service):
        return {"available": False}
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    import urllib.parse
    qs = urllib.parse.urlencode([
        ("ownerId", owner),
        ("resource", service),
        ("limit", "500"),
        ("startTime", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("endTime", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("text", "dodo-webhook"),
    ])
    req = urllib.request.Request(
        f"https://api.render.com/v1/logs?{qs}",
        headers={"Authorization": f"Bearer {render_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
    except Exception as e:
        return {"available": False, "error": str(e)}
    counts = {"ok_200": 0, "fail_401": 0, "fail_5xx": 0}
    for entry in (body.get("logs") or []):
        msg = entry.get("message", "")
        if "/api/payment/dodo-webhook" in msg:
            if " 200 OK" in msg:
                counts["ok_200"] += 1
            elif " 401 " in msg:
                counts["fail_401"] += 1
            elif any(f" {c} " in msg for c in ("500", "502", "503", "504")):
                counts["fail_5xx"] += 1
    counts["available"] = True
    return counts


def build_email_body() -> tuple[str, str]:
    init24 = fetch("/api/metrics/payment-initiations", 24)
    init48 = fetch("/api/metrics/payment-initiations", 48)
    succ24 = fetch("/api/metrics/payment-successes", 24)
    succ48 = fetch("/api/metrics/payment-successes", 48)
    proc24 = fetch("/api/metrics/processing-complete", 24)
    proc48 = fetch("/api/metrics/processing-complete", 48)

    init_now = init24.get("count", 0)
    init_prev = max(0, init48.get("count", 0) - init_now)
    succ_now = succ24.get("count", 0)
    succ_prev = max(0, succ48.get("count", 0) - succ_now)
    proc_now = proc24.get("count", 0)
    proc_prev = max(0, proc48.get("count", 0) - proc_now)

    revenue = succ_now * 4.99
    conv = (succ_now / init_now * 100) if init_now > 0 else 0

    wh = fetch_webhook_health()
    wh_line = (
        f"Webhook health (24h): 200={wh.get('ok_200',0)} | 401={wh.get('fail_401',0)} | 5xx={wh.get('fail_5xx',0)}"
        if wh.get("available")
        else "Webhook health: (Render API not configured)"
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subj = f"[artimagehub daily] {today} — paid={succ_now} ${revenue:.2f} | checkout={init_now} | conv={conv:.1f}%"

    lines = [
        f"Daily snapshot — {today} (window: rolling 24h, all UTC)",
        "",
        f"  Paid orders:        {succ_now:4}    Δ vs prior 24h: {delta(succ_now, succ_prev)}",
        f"  Revenue:            ${revenue:7.2f}",
        f"  Checkout attempts:  {init_now:4}    Δ vs prior 24h: {delta(init_now, init_prev)}",
        f"  Restorations done:  {proc_now:4}    Δ vs prior 24h: {delta(proc_now, proc_prev)}",
        f"  Checkout→pay rate:  {conv:5.1f}%   (need >= 50 attempts before reading too much into this)",
        "",
        wh_line,
        "",
        "By provider (paid 24h):",
    ]
    for prov, n in (succ24.get("by_provider") or {}).items():
        lines.append(f"  - {prov}: {n}")
    if not (succ24.get("by_provider") or {}):
        lines.append("  (none)")
    lines += [
        "",
        "Notes",
        "- Numbers above are from /api/metrics/payment-* on the live backend.",
        "- LLM referrer + GSC impressions are not in this report yet (need Vercel Analytics or GSC API wired). Ping foreman to add when wanted.",
        f"- Full dashboard: {DASHBOARD_HINT}",
    ]
    return subj, "\n".join(lines)


def send_email(api_key: str, subject: str, body: str) -> None:
    payload = {
        "from": ALERT_FROM,
        "to": [ALERT_TO],
        "subject": subject,
        "text": body,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    print(f"sent — id={out.get('id')}")


def main() -> int:
    resend_key = _env("RESEND_API_KEY")
    subj, body = build_email_body()
    print("=== preview ===")
    print(subj)
    print("---")
    print(body)
    print("=== /preview ===")
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1 — not sending")
        return 0
    send_email(resend_key, subj, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
