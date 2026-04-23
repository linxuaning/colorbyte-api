#!/usr/bin/env python3
"""Webhook health monitor for the Dodo payment webhook endpoint.

Pulls Render logs for the last hour and alerts via Resend when it sees
signature-verification failures or any other 401/5xx coming from the
`/api/payment/dodo-webhook` route. The 4/15-4/19 incident (16 paid orders
silently blocked because `dodopayments[webhooks]` extra was missing) lasted
4 days because nothing watched the webhook delivery rate. This is the
watcher.

Designed for hourly invocation from a GitHub Actions workflow. Reads:
    RENDER_API_KEY, RENDER_OWNER_ID, RENDER_SERVICE_ID, RESEND_API_KEY
from env. Exits 0 on success even when an alert fires (so the workflow
does not flap red); exits non-zero only on missing config or unrecoverable
errors.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ALERT_TO = "linxuaning98@gmail.com"
ALERT_FROM = "support@artimagehub.com"  # support@ is verified in Resend; alerts@ is not (was causing 403)
WEBHOOK_PATH = "/api/payment/dodo-webhook"

# Patterns that indicate something is wrong with webhook delivery.
ERROR_PATTERNS = (
    "Dodo webhook signature verification failed",
    "Dodo webhook processing error",
    "PAYMENT_WEBHOOK_PARSE_FAILED",
    "PAYMENT_WEBHOOK_INVALID_SIGNATURE",
)


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"[fatal] missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return val


def fetch_recent_logs(api_key: str, owner: str, service: str, lookback_minutes: int) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback_minutes)
    qs = urllib.parse.urlencode([
        ("ownerId", owner),
        ("resource", service),
        ("limit", "500"),
        ("startTime", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("endTime", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("text", "dodo-webhook"),
    ])
    url = f"https://api.render.com/v1/logs?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    # Render sometimes returns {"logs": null} when no lines match the filter
    # in the window. Default None → [] to survive empty windows.
    return body.get("logs") or []


def classify(logs: list[dict]) -> dict:
    """Bucket log lines into success / 401 / 5xx / error-text."""
    counts = {"success_200": 0, "unauthorized_401": 0, "server_5xx": 0, "error_text": 0}
    samples = {"unauthorized_401": [], "server_5xx": [], "error_text": []}
    for entry in logs:
        msg = entry.get("message", "")
        ts = entry.get("timestamp", "")[:19]
        if WEBHOOK_PATH in msg:
            if " 200 OK" in msg:
                counts["success_200"] += 1
            elif " 401 Unauthorized" in msg:
                counts["unauthorized_401"] += 1
                if len(samples["unauthorized_401"]) < 5:
                    samples["unauthorized_401"].append(f"{ts}  {msg[:200]}")
            elif any(f" {code} " in msg for code in ("500", "502", "503", "504")):
                counts["server_5xx"] += 1
                if len(samples["server_5xx"]) < 5:
                    samples["server_5xx"].append(f"{ts}  {msg[:200]}")
        if any(p in msg for p in ERROR_PATTERNS):
            counts["error_text"] += 1
            if len(samples["error_text"]) < 5:
                samples["error_text"].append(f"{ts}  {msg[:300]}")
    return {"counts": counts, "samples": samples}


def should_alert(report: dict) -> bool:
    c = report["counts"]
    # Alert only when we observe a definite failure signal in the window.
    return c["unauthorized_401"] > 0 or c["server_5xx"] > 0 or c["error_text"] > 0


def send_alert(api_key: str, report: dict, lookback_minutes: int) -> None:
    c = report["counts"]
    body_lines = [
        f"Window: last {lookback_minutes} minutes (UTC)",
        f"Endpoint: {WEBHOOK_PATH}",
        "",
        f"  200 OK:           {c['success_200']}",
        f"  401 Unauthorized: {c['unauthorized_401']}",
        f"  5xx:              {c['server_5xx']}",
        f"  error log lines:  {c['error_text']}",
        "",
    ]
    for kind in ("error_text", "unauthorized_401", "server_5xx"):
        rows = report["samples"][kind]
        if rows:
            body_lines.append(f"-- {kind} samples --")
            body_lines.extend(rows)
            body_lines.append("")
    body_lines.append(
        "Action: open Render logs for colorbyte-api and Dodo dashboard "
        "Webhooks page. The 4/15 incident pattern was 'dodopayments[webhooks]' "
        "extra missing -> all signatures fail. Other patterns: rotated signing "
        "secret, processing error in _handle_dodo_payment_succeeded."
    )
    body = "\n".join(body_lines)
    payload = {
        "from": ALERT_FROM,
        "to": [ALERT_TO],
        "subject": f"[ALERT] Dodo webhook failures — {c['unauthorized_401']} 401, {c['server_5xx']} 5xx, {c['error_text']} errors in last {lookback_minutes}min",
        "text": body,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Cloudflare (in front of api.resend.com) 1010-blocks the default
            # Python-urllib/x.y User-Agent. Branded UA unblocks it.
            "User-Agent": "artimagehub-monitor/1.0",
        },
        method="POST",
    )
    # Surface alert body BEFORE POST so transient Resend timeouts (~10% rate observed 4/22-4/23)
    # still leave the alert content (counts + sample log lines) visible in GH Actions log for triage.
    print(f"[alert body] subject={payload['subject']!r}\n{body}\n[/alert body]", flush=True)
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    print(f"[alert] sent — id={out.get('id')}")


def main() -> int:
    render_key = _env("RENDER_API_KEY")
    owner = _env("RENDER_OWNER_ID")
    service = _env("RENDER_SERVICE_ID")
    resend_key = _env("RESEND_API_KEY")

    lookback = int(os.environ.get("LOOKBACK_MINUTES", "65"))
    logs = fetch_recent_logs(render_key, owner, service, lookback)
    print(f"fetched {len(logs)} log lines (lookback={lookback}min)")
    report = classify(logs)
    c = report["counts"]
    print(
        f"counts: 200={c['success_200']} 401={c['unauthorized_401']} "
        f"5xx={c['server_5xx']} errors={c['error_text']}"
    )

    if should_alert(report):
        send_alert(resend_key, report, lookback)
    else:
        print("ok — no alert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
