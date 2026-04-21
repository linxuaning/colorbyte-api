#!/usr/bin/env python3
"""Audit HuggingFace Spaces used by the AI pipeline.

Runs on a schedule (GitHub Actions cron) to catch upstream breakage before a
paying user hits "Something went wrong".

Spaces tracked here are a hand-maintained mirror of the list in
`app/services/ai_service.py` (HuggingFaceProvider.RESTORE_SPACES / DEOLDIFY_SPACES
/ ESRGAN_SPACES). Keeping them in sync is deliberate — the audit must be
runnable in minimal environments (CI, ops box) with ONLY gradio_client
installed, no backend Python deps.

If the production code changes its Space list, update SPACES below.

Exit codes:
  0 = primary restore Space healthy
  1 = primary restore Space is broken (fires alert email if RESEND_API_KEY set)

Env:
  RESEND_API_KEY    — if set, send alert email on critical failure
  ALERT_EMAIL_TO    — recipient (default: linxuaning98@gmail.com)
  ALERT_EMAIL_FROM  — sender (default: support@artimagehub.com, the only
                      artimagehub.com address verified in Resend)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Mirror of app.services.ai_service.HuggingFaceProvider.*_SPACES (audited 2026-04-17).
# When ai_service.py changes, update these tuples.
RESTORE_SPACES = [
    # (space_id, space_type, api_endpoint)
    ("sczhou/CodeFormer", "codeformer_v2", "/inference"),
    ("PERCY001/CodeFormer", "codeformer_v2", "/predict"),
    ("avans06/Image_Face_Upscale_Restoration-GFPGAN-RestoreFormer-CodeFormer-GPEN", "multimodel_v2", "/inference"),
    ("titanito/Image_Face_Upscale_Restoration-GFPGAN-RestoreFormer-CodeFormer-GPEN", "multimodel_v2", "/inference"),
]

COLORIZE_SPACES = [
    ("ialhashim/Colorizer", "single_arg"),
]

ESRGAN_SPACES = [
    ("Fabrice-TIERCELIN/RealESRGAN", "size_modifier"),
    ("guetLzy/Real-ESRGAN-Demo", "enhance_full"),
    ("doevent/Face-Real-ESRGAN", "single_arg"),
]

# State file — written to CI artifact dir when run in GH Actions, else local.
STATE_PATH = Path(os.environ.get("HF_AUDIT_STATE_PATH", "hf-spaces-audit.json"))


def probe(space_id: str) -> dict:
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
    report: dict = {"restore": [], "colorize": [], "esrgan": [], "critical_failed": False}

    print("== RESTORE ==")
    for i, entry in enumerate(RESTORE_SPACES):
        sid = entry[0]
        r = probe(sid)
        r["space_id"] = sid
        r["critical"] = i == 0
        report["restore"].append(r)
        marker = "OK" if r["status"] == "ok" else "FAIL"
        print(f"  [{marker}] {sid:<70} {r.get('endpoints') or r.get('error_type')}")
        if i == 0 and r["status"] != "ok":
            report["critical_failed"] = True

    print("\n== COLORIZE ==")
    for entry in COLORIZE_SPACES:
        sid = entry[0]
        r = probe(sid)
        r["space_id"] = sid
        report["colorize"].append(r)
        marker = "OK" if r["status"] == "ok" else "FAIL"
        print(f"  [{marker}] {sid:<70} {r.get('endpoints') or r.get('error_type')}")

    print("\n== ESRGAN ==")
    for entry in ESRGAN_SPACES:
        sid = entry[0]
        r = probe(sid)
        r["space_id"] = sid
        report["esrgan"].append(r)
        marker = "OK" if r["status"] == "ok" else "FAIL"
        print(f"  [{marker}] {sid:<70} {r.get('endpoints') or r.get('error_type')}")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nState -> {STATE_PATH}")

    if report["critical_failed"]:
        print("\nCRITICAL Space failed. Sending alert if configured...")
        send_alert(report)
        return 1
    return 0


def send_alert(report: dict) -> None:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("(RESEND_API_KEY not set — skipping email)")
        return

    to = os.environ.get("ALERT_EMAIL_TO", "linxuaning98@gmail.com")
    sender = os.environ.get("ALERT_EMAIL_FROM", "support@artimagehub.com")

    failed_restore = [r for r in report["restore"] if r["status"] != "ok"]
    failed_colorize = [r for r in report["colorize"] if r["status"] != "ok"]

    lines = [
        "<h2>HF Spaces audit — critical failure</h2>",
        "<p>Primary face-restoration Space is down. Users will hit slow/failed processing.</p>",
        "<h3>Failed restore Spaces</h3><ul>",
    ]
    for r in failed_restore:
        lines.append(
            f"<li>{r['space_id']} — {r.get('error_type', 'unknown')}: "
            f"{(r.get('error') or '')[:200]}</li>"
        )
    lines.append("</ul>")
    if failed_colorize:
        lines.append("<h3>Failed colorize Spaces</h3><ul>")
        for r in failed_colorize:
            lines.append(
                f"<li>{r['space_id']} — {r.get('error_type', 'unknown')}: "
                f"{(r.get('error') or '')[:200]}</li>"
            )
        lines.append("</ul>")
    lines.append(
        "<p>Next steps: update signatures in "
        "<code>photofix/backend/app/services/ai_service.py</code> and the "
        "mirror list in <code>scripts/hf-spaces-audit.py</code>.</p>"
    )

    import urllib.request
    import urllib.error
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
            "User-Agent": "artimagehub-monitor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"Alert email sent: HTTP {r.status}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        print(f"Alert email FAILED: HTTP {e.code}: {body_text}")
    except Exception as e:
        print(f"Alert email FAILED: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
