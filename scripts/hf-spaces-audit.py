#!/usr/bin/env python3
"""Audit HuggingFace Spaces used by the AI pipeline.

Checks each Space's runtime state and API endpoints. Intended to run on a
schedule (GitHub Actions cron) so we catch upstream breakage before a paying
user hits "Something went wrong".

Exit codes:
  0 = all critical spaces healthy
  1 = at least one critical space is broken (will fire alert email if configured)

A "critical" space is one in RESTORE_SPACES[0] (the primary face-restorer).
Secondary spaces + colorization spaces are monitored but non-critical.

Env:
  RESEND_API_KEY    — if set, send alert email on critical failure
  ALERT_EMAIL_TO    — recipient (default: creator)
  ALERT_EMAIL_FROM  — sender (default: onboarding@resend.dev)

Run:
  python3 photofix/backend/scripts/hf-spaces-audit.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "photofix" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

STATE_PATH = BACKEND_ROOT / "docs" / "hf-spaces-audit.json"


def probe_space(space_id: str) -> dict:
    """Connect to a Space and capture endpoints + health."""
    from gradio_client import Client
    try:
        client = Client(space_id, verbose=False)
        try:
            info = client.view_api(return_format="dict")
        except Exception:
            info = {}
        endpoints = list(info.get("named_endpoints", {}).keys()) if isinstance(info, dict) else []
        return {"status": "ok", "endpoints": endpoints}
    except Exception as e:
        return {"status": "fail", "error_type": type(e).__name__, "error": str(e)[:400]}


def main() -> int:
    # Discover the list of Spaces from the running code so the audit stays in
    # sync with whatever the provider imports today.
    from app.services.ai_service import HuggingFaceProvider

    restore = list(HuggingFaceProvider.RESTORE_SPACES)
    colorize = list(HuggingFaceProvider.DEOLDIFY_SPACES)
    esrgan = ["doevent/Face-Real-ESRGAN"]  # hardcoded in _call_esrgan

    report: dict = {
        "restore": [],
        "colorize": [],
        "esrgan": [],
        "critical_failed": False,
    }

    print("== RESTORE ==")
    for i, entry in enumerate(restore):
        sid = entry[0] if isinstance(entry, tuple) else str(entry)
        r = probe_space(sid)
        r["space_id"] = sid
        r["critical"] = i == 0
        report["restore"].append(r)
        marker = "✓" if r["status"] == "ok" else "✗"
        print(f"  {marker} {sid:<80} {r.get('endpoints') or r.get('error_type')}")
        if i == 0 and r["status"] != "ok":
            report["critical_failed"] = True

    print("\n== COLORIZE ==")
    for entry in colorize:
        sid = entry[0] if isinstance(entry, tuple) else str(entry)
        r = probe_space(sid)
        r["space_id"] = sid
        report["colorize"].append(r)
        marker = "✓" if r["status"] == "ok" else "✗"
        print(f"  {marker} {sid:<80} {r.get('endpoints') or r.get('error_type')}")

    print("\n== ESRGAN ==")
    for sid in esrgan:
        r = probe_space(sid)
        r["space_id"] = sid
        report["esrgan"].append(r)
        marker = "✓" if r["status"] == "ok" else "✗"
        print(f"  {marker} {sid:<80} {r.get('endpoints') or r.get('error_type')}")

    # Write state
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nState → {STATE_PATH.relative_to(REPO_ROOT)}")

    if report["critical_failed"]:
        print("\n⚠ CRITICAL Space failed. Sending alert if configured…")
        send_alert(report)
        return 1
    return 0


def send_alert(report: dict) -> None:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("(RESEND_API_KEY not set — skipping email)")
        return

    to = os.environ.get("ALERT_EMAIL_TO", "linxuaning98@gmail.com")
    sender = os.environ.get("ALERT_EMAIL_FROM", "onboarding@resend.dev")

    failed_restore = [r for r in report["restore"] if r["status"] != "ok"]
    failed_colorize = [r for r in report["colorize"] if r["status"] != "ok"]

    lines = [
        "<h2>HF Spaces audit — critical failure</h2>",
        "<p>Primary face-restoration Space is down. Users will hit slow/failed processing.</p>",
        "<h3>Failed restore Spaces</h3><ul>",
        *[f"<li>{r['space_id']} — {r.get('error_type', 'unknown')}: "
          f"{(r.get('error') or '')[:200]}</li>" for r in failed_restore],
        "</ul>",
    ]
    if failed_colorize:
        lines += ["<h3>Failed colorize Spaces</h3><ul>"]
        lines += [
            f"<li>{r['space_id']} — {r.get('error_type', 'unknown')}: "
            f"{(r.get('error') or '')[:200]}</li>"
            for r in failed_colorize
        ]
        lines.append("</ul>")

    lines.append(
        "<p>Next steps: update signatures in "
        "<code>photofix/backend/app/services/ai_service.py</code> "
        "or switch AI_PROVIDER to Replicate.</p>"
    )

    import urllib.request
    body = json.dumps({
        "from": sender,
        "to": [to],
        "subject": "[artimagehub] HF Spaces audit — critical failure",
        "html": "".join(lines),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"Alert email sent: HTTP {r.status}")
    except Exception as e:
        print(f"Alert email FAILED: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
