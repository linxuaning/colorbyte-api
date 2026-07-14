#!/usr/bin/env python3
"""Synthetic canary v0 (T241, founder direct instruction, 2026-07-10/11).

Two independent checks, run against the real live site/API:

1. Attribution check: a real Playwright browser session simulates the exact
   visitor journey T231 was built to get right (visit a blog post, click
   through to a tool page, start checkout) and asserts the request that
   would go to /api/payment/dodo-create-checkout carries landing_page =
   the blog post, not null and not the tool page. The checkout request is
   intercepted and aborted before it reaches the real backend -- no real
   session/order is ever created (same technique used to verify T231/T234/
   T210 by hand all week; this just runs it on a schedule instead of only
   when an engineer happens to test it by hand -- that gap, "correctness
   nobody exercises," is exactly what let both bugs live undetected for
   months).

2. Restoration-pipeline sanity check, TWO variants -- uploads a test photo to
   /api/upload and polls for completion, then fetches the watermarked
   preview (/api/result-preview/{id}) and asserts it isn't corrupted or
   near-black. This is the T223 incident class (a real customer photo came
   back solid black) -- this check would have caught it automatically
   instead of waiting for a customer complaint or someone spot-checking.
     a. no_face: the original synthetic gradient (zero faces) -- exercises
        the DiffBIR fallback path only, never touches T248's gentle-routing
        or face-existence-check code.
     b. face (added 2026-07-14, T248 follow-up, founder-approved): a real
        2-face vintage photo -- exercises gentle-routing + existence-check
        on every run, doubling as a live regression test for that new code.
        Image source: Internet Archive item "kaczynskaja-fota-16-002"
        (https://archive.org/details/kaczynskaja-fota-16-002), licensed
        Public Domain Mark 1.0 (https://creativecommons.org/publicdomain/
        mark/1.0/) -- explicitly not a customer photo; customer images are
        never used as test/CI assets, diagnostic one-off use excepted (see
        T244/T248 incident notes). Its content-hash is fixed and known --
        exclude it when analyzing production face/identity metrics.

   Both endpoints are pay-first gated (T220 per-feature entitlement, no
   free path exists) -- this uses CANARY_EMAIL, the same self-test address
   already excluded from revenue metrics elsewhere, which already carries
   real entitlement to "restoration" from historical test purchases. No
   payment/entitlement code is touched or modified by this script; it only
   authenticates as an already-entitled test account, same as any real
   returning customer would.

v0 scope: run + alert. No dashboard, no historical trend -- just "did the
last run pass," surfaced via a Resend email on failure (mirrors
webhook-monitor.py / hf-spaces-audit.py's existing alerting convention).
Exits 0 on a clean pass, 1 if either check fails (also fires the alert).

Env:
  RESEND_API_KEY     -- if set, send alert email on failure
  ALERT_EMAIL_TO     -- recipient (default: linxuaning98@gmail.com)
  ALERT_EMAIL_FROM   -- sender (default: support@artimagehub.com)
  CANARY_SITE_URL    -- default https://artimagehub.com
  CANARY_API_URL     -- default https://api.artimagehub.com
  CANARY_INTERNAL_KEY -- optional; if set, passed as upload.py's internal_key
                         entitlement-bypass field as a redundant safety net
                         (result-preview has no such bypass, so CANARY_EMAIL's
                         real entitlement is what actually matters either way)
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITE_URL = os.environ.get("CANARY_SITE_URL", "https://artimagehub.com").rstrip("/")
API_URL = os.environ.get("CANARY_API_URL", "https://api.artimagehub.com").rstrip("/")
BLOG_PATH = "/blog/restore-old-photos-online-free/"
TOOL_PATH = "/old-photo-restoration/"
CANARY_EMAIL = "linxuaning98@gmail.com"  # self-test address, excluded from revenue metrics
POLL_TIMEOUT_S = 300  # T248 follow-up (2026-07-14): a real run hit 183.6s and
# flapped the canary's own budget -- see this file's own commit message for
# why 300s, not the 160s->184s story it was initially attributed to (that
# story doesn't hold: this script's own test image has zero faces, confirmed
# against ab_server.log, so it never reaches gentle-routing or existence-check
# code at all). 300s leaves real margin while staying far under the real
# 1200s upstream timeout.
POLL_INTERVAL_S = 5
MIN_MEAN_BRIGHTNESS = 10  # 0-255; a real photo's restored preview should never average this dark


def check_attribution() -> dict:
    """Real browser: blog -> tool page -> start checkout. Intercepts and
    aborts the actual checkout call so nothing is ever created upstream --
    only inspects what WOULD have been sent."""
    from playwright.sync_api import sync_playwright

    captured: dict = {}

    def handle_route(route, request):
        if "dodo-create-checkout" in request.url and request.method == "POST":
            try:
                captured["body"] = json.loads(request.post_data or "{}")
            except Exception:
                captured["body"] = request.post_data
            route.abort()
            return
        route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/*", handle_route)

        page.goto(f"{SITE_URL}{BLOG_PATH}", timeout=30000)
        page.wait_for_timeout(800)

        page.goto(f"{SITE_URL}{TOOL_PATH}", timeout=30000)
        page.wait_for_timeout(1500)

        link = page.locator("a", has_text="Unlock Access").first
        href = link.get_attribute("href", timeout=15000)
        if not href:
            browser.close()
            return {"ok": False, "reason": "no 'Unlock Access' link found on tool page"}

        page.goto(f"{SITE_URL}{href}", timeout=30000)
        page.wait_for_timeout(1000)
        page.locator("input#checkout-email, input[type=email]").first.fill(
            "canary-v0-noop@example.com"
        )
        page.wait_for_timeout(300)
        page.get_by_role("button", name="Pay $4.99 securely").click()
        page.wait_for_timeout(1500)
        browser.close()

    body = captured.get("body")
    if not isinstance(body, dict):
        return {"ok": False, "reason": f"checkout request never captured (body={body!r})"}

    landing_page = body.get("landing_page")
    ok = landing_page == BLOG_PATH
    return {
        "ok": ok,
        "reason": None if ok else f"expected landing_page={BLOG_PATH!r}, got {landing_page!r}",
        "landing_page": landing_page,
    }


def _make_test_image() -> bytes:
    """Self-contained synthetic old-photo-ish test image -- no external
    asset to keep in the repo. A soft gradient with light noise; not meant
    to be a realistic damage benchmark (that's a separate, human-reviewed
    concern per T210), just something the pipeline should reliably produce
    a non-black, non-corrupted result from."""
    from PIL import Image
    import random

    w, h = 640, 480
    img = Image.new("RGB", (w, h))
    px = img.load()
    random.seed(42)  # deterministic -- a flaky canary is worse than no canary
    for y in range(h):
        base = 90 + int(120 * (y / h))
        for x in range(w):
            n = random.randint(-12, 12)
            v = max(0, min(255, base + n))
            px[x, y] = (v, v, min(255, v + 15))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# T248 follow-up (2026-07-14, founder-approved): a real 2-face test image so
# the canary also regression-tests gentle-routing + face-existence-check on
# every run, not just the no-face DiffBIR path. Source: Internet Archive item
# "kaczynskaja-fota-16-002" (https://archive.org/details/kaczynskaja-fota-16-002),
# a 1925 studio portrait from the Vitebsk Belarusian documents collection,
# licensed Public Domain Mark 1.0 (https://creativecommons.org/publicdomain/
# mark/1.0/) -- not a customer photo. See this commit's message for the full
# provenance note; customer images are never used as test/CI assets.
FACE_TEST_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "canary-face-test.jpg")


def _load_face_test_image() -> bytes:
    with open(FACE_TEST_IMAGE_PATH, "rb") as f:
        return f.read()


def _post_multipart(url: str, fields: dict, file_field: str, filename: str, file_bytes: bytes) -> dict:
    boundary = "----canaryv0boundary"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: image/jpeg\r\n\r\n"
    )
    body = "".join(parts).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def check_restoration_pipeline(image_bytes: bytes, label: str) -> dict:
    """Upload -> poll -> fetch preview -> assert not corrupted/black.

    Both /api/upload and /api/result-preview/{id} are pay-first gated
    (T220 per-feature entitlement) -- there is no free/ungated path. This
    uses CANARY_EMAIL (the same self-test address already excluded from
    revenue metrics elsewhere in the codebase), which already carries real
    entitlement to "restoration" from historical test purchases, so no
    internal_key bypass or fresh grant is needed. internal_key is passed on
    the upload anyway (belt-and-suspenders -- upload.py accepts it as an
    entitlement bypass per its own docstring) so this keeps working even if
    that email's entitlement ever lapses; result-preview has no such bypass
    parameter, so it relies on the email's real entitlement either way.

    `label` is cosmetic (surfaces in filename/logs only) -- which code path
    actually runs is determined entirely by whether image_bytes has a
    detectable face, same as any real customer upload.
    """
    try:
        upload_resp = _post_multipart(
            f"{API_URL}/api/upload",
            {
                "colorize": "false",
                "email": CANARY_EMAIL,
                "feature_key": "restoration",
                "internal_key": os.environ.get("CANARY_INTERNAL_KEY", ""),
            },
            "file",
            f"canary-v0-test-{label}.jpg",
            image_bytes,
        )
    except Exception as e:
        return {"ok": False, "reason": f"upload request failed: {e}"}

    task_id = upload_resp.get("task_id")
    if not task_id:
        return {"ok": False, "reason": f"upload response missing task_id: {upload_resp}"}

    deadline = time.time() + POLL_TIMEOUT_S
    status = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{API_URL}/api/tasks/{task_id}", timeout=15) as r:
                status = json.loads(r.read())
        except Exception as e:
            return {"ok": False, "reason": f"status poll failed: {e}", "task_id": task_id}
        if status.get("status") in ("completed", "failed"):
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        return {"ok": False, "reason": f"task did not finish within {POLL_TIMEOUT_S}s", "task_id": task_id, "last_status": status}

    if status.get("status") != "completed":
        return {"ok": False, "reason": f"task failed: {status.get('error')}", "task_id": task_id}

    preview_qs = urllib.parse.urlencode({"email": CANARY_EMAIL})
    try:
        with urllib.request.urlopen(f"{API_URL}/api/result-preview/{task_id}?{preview_qs}", timeout=30) as r:
            preview_bytes = r.read()
    except Exception as e:
        return {"ok": False, "reason": f"could not fetch result preview: {e}", "task_id": task_id}

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(preview_bytes)).convert("L")
        w, h = img.size
        if w < 10 or h < 10:
            return {"ok": False, "reason": f"result preview is tiny ({w}x{h}) -- likely corrupted", "task_id": task_id}
        mean_brightness = sum(img.getdata()) / (w * h)
    except Exception as e:
        return {"ok": False, "reason": f"result preview is not a valid image: {e}", "task_id": task_id}

    ok = mean_brightness >= MIN_MEAN_BRIGHTNESS
    return {
        "ok": ok,
        "reason": None if ok else f"result preview mean brightness {mean_brightness:.1f} < {MIN_MEAN_BRIGHTNESS} (near-black output, T223 incident class)",
        "task_id": task_id,
        "mean_brightness": round(mean_brightness, 1),
        "provider_used": status.get("provider_used"),
        "provider_backend": status.get("provider_backend"),
    }


def send_alert(results: dict) -> None:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("(RESEND_API_KEY not set -- skipping email)")
        return

    # 2026-07-11 incident: the workflow set ALERT_EMAIL_TO from a GH secret
    # that was never created, so the env var was PRESENT but "" -- os.environ
    # .get(..., default) only falls back when the key is *absent*, so the
    # canary silently failed to alert on a real outage. `or` treats an empty
    # string the same as unset.
    to = os.environ.get("ALERT_EMAIL_TO") or "linxuaning98@gmail.com"
    sender = os.environ.get("ALERT_EMAIL_FROM") or "support@artimagehub.com"

    lines = ["<h2>Canary v0 -- failure</h2>"]
    for name, r in results.items():
        if r.get("ok"):
            continue
        lines.append(f"<h3>{name}</h3><p>{r.get('reason')}</p><pre>{json.dumps(r, indent=2)}</pre>")
    lines.append("<p>Source: scripts/canary-v0.py (T241)</p>")

    body = json.dumps({
        "from": sender,
        "to": [to],
        "subject": "[artimagehub] Canary v0 -- failure",
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
        print(f"Alert email FAILED: HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}")
    except Exception as e:
        print(f"Alert email FAILED: {e}")


def main() -> int:
    results = {}

    print("== Attribution check (blog -> tool -> checkout) ==")
    try:
        results["attribution"] = check_attribution()
    except Exception as e:
        results["attribution"] = {"ok": False, "reason": f"check crashed: {e}"}
    print(f"  {'OK' if results['attribution']['ok'] else 'FAIL'}: {results['attribution']}")

    print("\n== Restoration pipeline sanity check (no_face -- DiffBIR path) ==")
    try:
        results["restoration_pipeline_no_face"] = check_restoration_pipeline(_make_test_image(), "no-face")
    except Exception as e:
        results["restoration_pipeline_no_face"] = {"ok": False, "reason": f"check crashed: {e}"}
    print(f"  {'OK' if results['restoration_pipeline_no_face']['ok'] else 'FAIL'}: {results['restoration_pipeline_no_face']}")

    print("\n== Restoration pipeline sanity check (face -- gentle-routing + existence-check path) ==")
    try:
        results["restoration_pipeline_face"] = check_restoration_pipeline(_load_face_test_image(), "face")
    except Exception as e:
        results["restoration_pipeline_face"] = {"ok": False, "reason": f"check crashed: {e}"}
    print(f"  {'OK' if results['restoration_pipeline_face']['ok'] else 'FAIL'}: {results['restoration_pipeline_face']}")

    any_failed = any(not r.get("ok") for r in results.values())
    if any_failed:
        print("\nCanary check(s) FAILED. Sending alert if configured...")
        send_alert(results)
        return 1

    print("\nAll canary checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
