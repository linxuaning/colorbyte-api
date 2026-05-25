#!/usr/bin/env python3
"""ArtImageHub 每日增长/收入指标邮件。

订单数只读取 ArtImageHub 后端 `/api/metrics/payment-successes`，不要使用
GA4 purchase、Dodo 全账号总数、mbtiusa/test you 项目的订单。

脚本拉取最近 24h 与前一 24h 的指标做环比，并在配置
ARTIMAGEHUB_GA4_SA_KEY 时附带 GA4 流量/GEO 趋势。GitHub Actions 每天
00:00 UTC（北京时间 08:00）运行一次。

Required env:
    RESEND_API_KEY           — for outbound email
    METRICS_BASE             — backend base URL (default https://colorbyte-api.onrender.com)
    ALERT_TO                 — recipient (default linxuaning98@gmail.com)
Optional env:
    ARTIMAGEHUB_GA4_SA_KEY   — GA4 service account JSON string; enables PV/UV/GEO section
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
        return f"+{now}（新增）"
    diff = now - prev
    pct = (diff / prev) * 100
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff} ({sign}{pct:.0f}%)"


GEO_SOURCES = {
    # Tier 1 — confirmed paid conversions (90d data)
    "chatgpt.com",          # 108 sessions, 2 confirmed paid users
    "copilot.com",          # 15 sessions, 2 payment_clicks
    # Tier 2 — confirmed sessions (no paid yet)
    "perplexity", "perplexity.ai",
    "chat.qwen.ai",         # Alibaba Qwen (Chinese AI)
    "doubao.com",           # ByteDance (Chinese AI)
    "search.brave.com",
    "toolpilot.ai",
    "gemini.google.com", "bard.google.com",
    "x.com",
    # Tier 3 — watchlist (0 sessions but industry standard)
    "claude.ai", "openai.com",
    "copilot.microsoft.com", "you.com", "phind.com",
    "poe.com", "pi.ai", "grok.com", "meta.ai",
    "kagi.com", "character.ai", "mistral.ai", "groq.com",
}

INTERNAL_FUNNEL_MARKERS = (
    "probe",
    "monitor",
    "debug",
    "codex",
    "foreman",
    "local",
    "cors",
    "alias",
    "incident",
)


def is_internal_funnel_row(row: dict) -> bool:
    joined = " ".join(
        str(row.get(k) or "").lower()
        for k in ("landing_page", "cta_slot", "entry_variant", "checkout_source")
    )
    return any(marker in joined for marker in INTERNAL_FUNNEL_MARKERS)


def filtered_initiation_count(breakdown: dict) -> int | None:
    rows = breakdown.get("breakdown")
    if not isinstance(rows, list):
        return None
    return sum(
        int(row.get("payment_initiations") or 0)
        for row in rows
        if isinstance(row, dict) and not is_internal_funnel_row(row)
    )


def _ga4_token() -> tuple:
    """Return (token, url, headers) or raise."""
    sa_json = os.environ.get("ARTIMAGEHUB_GA4_SA_KEY", "").strip()
    if not sa_json:
        raise RuntimeError("ARTIMAGEHUB_GA4_SA_KEY not set")
    from google.oauth2 import service_account  # type: ignore
    from google.auth.transport.requests import Request as GRequest  # type: ignore
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    creds.refresh(GRequest())
    url = "https://analyticsdata.googleapis.com/v1beta/properties/525510036:runReport"
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    return url, headers


def _ga4_post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ascii_bar(value: int, max_val: int, width: int = 18) -> str:
    if max_val == 0:
        return "░" * width
    filled = round(value / max_val * width)
    return "█" * filled + "░" * (width - filled)


def fetch_ga4_metrics() -> dict:
    """Fetch yesterday's PV, UV, sessions, GEO breakdown from GA4 Data API.
    Returns {'available': False} when SA key is missing or import fails."""
    sa_json = os.environ.get("ARTIMAGEHUB_GA4_SA_KEY", "").strip()
    if not sa_json:
        return {"available": False, "reason": "ARTIMAGEHUB_GA4_SA_KEY not set"}
    try:
        from google.oauth2 import service_account  # type: ignore  # noqa: F401
        from google.auth.transport.requests import Request as GRequest  # type: ignore  # noqa: F401
    except ImportError:
        return {"available": False, "reason": "google-auth not installed"}
    try:
        from datetime import date, timedelta

        url, headers = _ga4_token()
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

        total = _ga4_post(url, headers, {
            "dateRanges": [{"startDate": yesterday, "endDate": yesterday}],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "activeUsers"},
                {"name": "sessions"},
            ],
        })

        pv = uv = sessions = 0
        if total.get("rows"):
            vals = total["rows"][0]["metricValues"]
            pv, uv, sessions = int(vals[0]["value"]), int(vals[1]["value"]), int(vals[2]["value"])

        by_src = _ga4_post(url, headers, {
            "dateRanges": [{"startDate": yesterday, "endDate": yesterday}],
            "dimensions": [{"name": "sessionSource"}],
            "metrics": [{"name": "sessions"}],
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            "limit": 100,
        })

        geo_sessions = 0
        geo_breakdown: dict[str, int] = {}
        for row in (by_src.get("rows") or []):
            src = row["dimensionValues"][0]["value"]
            n = int(row["metricValues"][0]["value"])
            if src in GEO_SOURCES:
                geo_sessions += n
                geo_breakdown[src] = n

        return {
            "available": True,
            "date": yesterday,
            "pv": pv,
            "uv": uv,
            "sessions": sessions,
            "geo_sessions": geo_sessions,
            "geo_pct": (geo_sessions / sessions * 100) if sessions > 0 else 0.0,
            "geo_breakdown": geo_breakdown,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def fetch_ga4_7day_trend() -> dict:
    """Fetch 7-day daily PV/UV/GEO trend for sparkline charts in the email."""
    sa_json = os.environ.get("ARTIMAGEHUB_GA4_SA_KEY", "").strip()
    if not sa_json:
        return {"available": False}
    try:
        from google.oauth2 import service_account  # type: ignore  # noqa: F401
        from google.auth.transport.requests import Request as GRequest  # type: ignore  # noqa: F401
    except ImportError:
        return {"available": False}
    try:
        url, headers = _ga4_token()

        # Daily PV/UV/sessions for last 7 complete days
        daily = _ga4_post(url, headers, {
            "dateRanges": [{"startDate": "7daysAgo", "endDate": "yesterday"}],
            "dimensions": [{"name": "date"}],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "activeUsers"},
                {"name": "sessions"},
            ],
            "orderBys": [{"dimension": {"dimensionName": "date"}}],
        })

        # Daily GEO sessions (date + source)
        geo_daily = _ga4_post(url, headers, {
            "dateRanges": [{"startDate": "7daysAgo", "endDate": "yesterday"}],
            "dimensions": [{"name": "date"}, {"name": "sessionSource"}],
            "metrics": [{"name": "sessions"}],
            "limit": 500,
        })

        # Build geo_by_date lookup
        geo_by_date: dict[str, int] = {}
        for row in (geo_daily.get("rows") or []):
            d = row["dimensionValues"][0]["value"]  # YYYYMMDD
            src = row["dimensionValues"][1]["value"]
            n = int(row["metricValues"][0]["value"])
            if src in GEO_SOURCES:
                geo_by_date[d] = geo_by_date.get(d, 0) + n

        rows = []
        for row in (daily.get("rows") or []):
            d = row["dimensionValues"][0]["value"]  # YYYYMMDD
            vals = row["metricValues"]
            pv = int(vals[0]["value"])
            uv = int(vals[1]["value"])
            sess = int(vals[2]["value"])
            geo = geo_by_date.get(d, 0)
            label = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            rows.append({"date": label, "pv": pv, "uv": uv, "sessions": sess, "geo": geo})

        return {"available": True, "rows": rows}
    except Exception as e:
        return {"available": False, "reason": str(e)}


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
    breakdown24 = fetch("/api/metrics/payment-funnel-breakdown", 24)
    breakdown48 = fetch("/api/metrics/payment-funnel-breakdown", 48)
    succ24 = fetch("/api/metrics/payment-successes", 24)
    succ48 = fetch("/api/metrics/payment-successes", 48)
    proc24 = fetch("/api/metrics/processing-complete", 24)
    proc48 = fetch("/api/metrics/processing-complete", 48)

    init_now = filtered_initiation_count(breakdown24)
    init48_filtered = filtered_initiation_count(breakdown48)
    if init_now is None:
        init_now = init24.get("count", 0)
    if init48_filtered is None:
        init_prev = max(0, init48.get("count", 0) - init_now)
    else:
        init_prev = max(0, init48_filtered - init_now)
    succ_now = succ24.get("count", 0)
    succ_prev = max(0, succ48.get("count", 0) - succ_now)
    proc_now = proc24.get("count", 0)
    proc_prev = max(0, proc48.get("count", 0) - proc_now)

    revenue = succ_now * 4.99
    conv = (succ_now / init_now * 100) if init_now > 0 else 0

    wh = fetch_webhook_health()
    wh_line = (
        f"Webhook 健康（24h）: 200={wh.get('ok_200',0)} | 401={wh.get('fail_401',0)} | 5xx={wh.get('fail_5xx',0)}"
        if wh.get("available")
        else "Webhook 健康:（Render API 未配置）"
    )

    ga4 = fetch_ga4_metrics()
    trend = fetch_ga4_7day_trend()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subj = f"[ArtImageHub 日报] {today} — 订单={succ_now} 收入=${revenue:.2f} | checkout={init_now} | 转化={conv:.1f}%"

    lines = [
        f"ArtImageHub 每日快照 — {today}（窗口：滚动 24 小时，UTC）",
        "",
        f"  付费订单:        {succ_now:4}    较前 24h: {delta(succ_now, succ_prev)}",
        f"  收入:            ${revenue:7.2f}",
        f"  Checkout 发起:   {init_now:4}    较前 24h: {delta(init_now, init_prev)}",
        f"  修复完成:        {proc_now:4}    较前 24h: {delta(proc_now, proc_prev)}",
        f"  Checkout→付款率: {conv:5.1f}%   （样本 <50 次时只看方向，不做结论）",
        "",
        wh_line,
        "",
        "支付渠道（付费订单 24h）:",
    ]
    for prov, n in (succ24.get("by_provider") or {}).items():
        lines.append(f"  - {prov}: {n}")
    if not (succ24.get("by_provider") or {}):
        lines.append("  （无）")

    lines.append("")
    if ga4.get("available"):
        geo_pct = ga4["geo_pct"]
        lines += [
            f"流量 — {ga4['date']}（GA4，T-1，北京时间）:",
            f"  PV（页面浏览）:       {ga4['pv']:6}",
            f"  UV（活跃用户）:       {ga4['uv']:6}",
            f"  Sessions:           {ga4['sessions']:6}",
            f"  GEO/AI 会话:         {ga4['geo_sessions']:6}    （占 sessions {geo_pct:.1f}%）",
        ]
        if ga4["geo_breakdown"]:
            lines.append("  GEO 来源拆分:")
            for src, n in sorted(ga4["geo_breakdown"].items(), key=lambda x: -x[1]):
                lines.append(f"    {src}: {n}")
        else:
            lines.append("  GEO 来源拆分:（未检测到）")
    else:
        lines.append(f"流量:（GA4 不可用 — {ga4.get('reason', 'unknown')}）")

    # 7-day trend charts
    if trend.get("available") and trend.get("rows"):
        trows = trend["rows"]
        max_uv  = max((r["uv"]  for r in trows), default=1) or 1
        max_pv  = max((r["pv"]  for r in trows), default=1) or 1
        max_geo = max((r["geo"] for r in trows), default=1) or 1

        lines += ["", "─" * 50, "7 日趋势（UV · PV · GEO/AI sessions）", "─" * 50]
        lines.append(f"{'日期':<12}  {'UV':>4}  {'趋势':<18}  {'PV':>5}  {'GEO':>4}")
        lines.append(f"{'────────────':<12}  {'────':>4}  {'──────────────────':<18}  {'─────':>5}  {'────':>4}")
        for r in trows:
            bar = ascii_bar(r["uv"], max_uv)
            lines.append(f"{r['date']:<12}  {r['uv']:>4}  {bar:<18}  {r['pv']:>5}  {r['geo']:>4}")

        lines.append("")
        lines.append("GEO/AI 趋势（sessions/day）:")
        for r in trows:
            bar = ascii_bar(r["geo"], max_geo)
            pct = f"{r['geo']/r['sessions']*100:.0f}%" if r["sessions"] else "0%"
            lines.append(f"  {r['date']}  {bar}  {r['geo']:>3} ({pct})")

        total_uv  = sum(r["uv"]  for r in trows)
        total_geo = sum(r["geo"] for r in trows)
        lines += [
            "",
            f"7 日合计: UV={total_uv}  GEO sessions={total_geo}",
            "─" * 50,
        ]
    elif not trend.get("available"):
        lines += ["", f"7 日趋势:（不可用 — {trend.get('reason', 'GA4 key not set')}）"]

    lines += [
        "",
        "口径说明",
        "- 订单/收入/checkout：只来自 ArtImageHub live backend `/api/metrics/payment-*` 的滚动 24h 数据。",
        "- 不使用 GA4 `purchase`、Dodo 全账号订单数、mbtiusa/test you 项目订单作为本日报订单数。",
        "- 流量/GEO：GA4 T-1 日（北京时间昨天）。GEO 来源包含 chatgpt/perplexity/claude/gemini/copilot/you/phind 等。",
        f"- 后台入口: {DASHBOARD_HINT}",
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
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Cloudflare (in front of api.resend.com) returns 1010 to the default
            # Python-urllib/x.y User-Agent. Sending a branded UA unblocks it.
            "User-Agent": "artimagehub-monitor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
        print(f"sent — id={out.get('id')}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        print(f"[fatal] Resend returned HTTP {e.code}: {body_text}", file=sys.stderr)
        raise


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
