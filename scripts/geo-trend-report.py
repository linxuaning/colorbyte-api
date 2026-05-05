#!/usr/bin/env python3
"""GEO/AI referrer daily trend report for artimagehub.com.

Queries GA4 for the past N days and outputs a day-by-day table of:
  - Total sessions / PV / UV
  - GEO (AI/LLM referrer) sessions + share

Usage:
    python3 scripts/geo-trend-report.py [--days 30] [--csv]

Env:
    ARTIMAGEHUB_GA4_SA_KEY   — SA JSON string (falls back to ~/.config/artimagehub/gcp-sa.json)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from collections import defaultdict

PROPERTY_ID = "525510036"
GA4_URL = f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY_ID}:runReport"

GEO_SOURCES = {
    # Tier 1 — confirmed paid conversions (90d GA4 data)
    "chatgpt.com",          # 108 sessions, 2 confirmed paid users
    "copilot.com",          # 15 sessions, 2 payment_clicks (Microsoft Copilot)
    # Tier 2 — confirmed sessions
    "perplexity", "perplexity.ai",
    "chat.qwen.ai",         # Alibaba Qwen
    "doubao.com",           # ByteDance Doubao
    "search.brave.com",
    "toolpilot.ai",
    "gemini.google.com", "bard.google.com",
    "x.com",
    # Tier 3 — watchlist
    "claude.ai", "openai.com",
    "copilot.microsoft.com", "you.com", "phind.com",
    "poe.com", "pi.ai", "grok.com", "meta.ai",
    "kagi.com", "character.ai", "mistral.ai", "groq.com",
    "inflection.ai",
}


def _get_token() -> str:
    sa_json = os.environ.get("ARTIMAGEHUB_GA4_SA_KEY", "").strip()
    if sa_json:
        sa_info = json.loads(sa_json)
    else:
        sa_path = os.path.expanduser("~/.config/artimagehub/gcp-sa.json")
        with open(sa_path) as f:
            sa_info = json.load(f)

    from google.oauth2 import service_account  # type: ignore
    from google.auth.transport.requests import Request as GReq  # type: ignore

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    creds.refresh(GReq())
    return creds.token


def _ga4_post(token: str, body: dict) -> dict:
    req = urllib.request.Request(
        GA4_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_trend(days: int = 30) -> list[dict]:
    token = _get_token()
    end = date.today() - timedelta(days=1)  # T-1 (finalized)
    start = end - timedelta(days=days - 1)

    date_range = {"startDate": start.strftime("%Y-%m-%d"), "endDate": end.strftime("%Y-%m-%d")}

    # 1. Daily totals: PV + UV + sessions
    total_resp = _ga4_post(token, {
        "dateRanges": [date_range],
        "dimensions": [{"name": "date"}],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "activeUsers"},
            {"name": "sessions"},
        ],
        "orderBys": [{"dimension": {"dimensionName": "date"}}],
        "limit": days + 5,
    })

    totals: dict[str, dict] = {}
    for row in (total_resp.get("rows") or []):
        d = row["dimensionValues"][0]["value"]  # YYYYMMDD
        v = row["metricValues"]
        totals[d] = {"pv": int(v[0]["value"]), "uv": int(v[1]["value"]), "sessions": int(v[2]["value"])}

    # 2. GEO sessions by date + source
    geo_resp = _ga4_post(token, {
        "dateRanges": [date_range],
        "dimensions": [{"name": "date"}, {"name": "sessionSource"}],
        "metrics": [{"name": "sessions"}],
        "orderBys": [{"dimension": {"dimensionName": "date"}}],
        "limit": 5000,
    })

    geo_by_date: dict[str, int] = defaultdict(int)
    geo_source_totals: dict[str, int] = defaultdict(int)
    for row in (geo_resp.get("rows") or []):
        d = row["dimensionValues"][0]["value"]
        src = row["dimensionValues"][1]["value"]
        n = int(row["metricValues"][0]["value"])
        if src in GEO_SOURCES:
            geo_by_date[d] += n
            geo_source_totals[src] += n

    # Build sorted day list
    all_dates = sorted(totals.keys())
    rows = []
    for d in all_dates:
        t = totals[d]
        geo = geo_by_date.get(d, 0)
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "sessions": t["sessions"],
            "pv": t["pv"],
            "uv": t["uv"],
            "geo_sessions": geo,
            "geo_pct": (geo / t["sessions"] * 100) if t["sessions"] > 0 else 0.0,
        })

    return rows, dict(geo_source_totals)


def print_table(rows: list[dict], geo_sources: dict, csv_mode: bool = False) -> None:
    if csv_mode:
        print("date,sessions,pv,uv,geo_sessions,geo_pct")
        for r in rows:
            print(f"{r['date']},{r['sessions']},{r['pv']},{r['uv']},{r['geo_sessions']},{r['geo_pct']:.2f}")
        return

    print(f"\n{'='*72}")
    print(f"  artimagehub.com — GEO/AI 流量日别趋势 (T-1, Asia/Shanghai)")
    print(f"{'='*72}")
    header = f"{'日期':12} {'Sessions':>9} {'PV':>7} {'UV':>7} {'GEO会话':>9} {'GEO占比':>8}"
    print(header)
    print("-" * 72)
    for r in rows:
        geo_flag = " ●" if r["geo_sessions"] > 0 else ""
        print(
            f"{r['date']:12} {r['sessions']:>9} {r['pv']:>7} {r['uv']:>7} "
            f"{r['geo_sessions']:>9} {r['geo_pct']:>7.1f}%{geo_flag}"
        )
    print("-" * 72)

    total_sessions = sum(r["sessions"] for r in rows)
    total_pv = sum(r["pv"] for r in rows)
    total_uv_avg = sum(r["uv"] for r in rows) // len(rows) if rows else 0
    total_geo = sum(r["geo_sessions"] for r in rows)
    avg_geo_pct = (total_geo / total_sessions * 100) if total_sessions > 0 else 0

    print(
        f"{'合计/均值':12} {total_sessions:>9} {total_pv:>7} {total_uv_avg:>7}* "
        f"{total_geo:>9} {avg_geo_pct:>7.1f}%"
    )
    print(f"  * UV 列为日均")

    if geo_sources:
        print(f"\n  GEO 来源明细 (period total):")
        for src, n in sorted(geo_sources.items(), key=lambda x: -x[1]):
            print(f"    {src:<30} {n:>4} sessions")
    else:
        print(f"\n  GEO 来源: 期间内未检测到 AI referrer 流量")

    print(f"{'='*72}\n")


def main() -> int:
    days = 30
    csv_mode = "--csv" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg == "--days" and sys.argv.index(arg) + 1 < len(sys.argv):
            days = int(sys.argv[sys.argv.index(arg) + 1])

    print(f"Fetching {days} days of GA4 data...", file=sys.stderr)
    rows, geo_sources = fetch_trend(days)
    print_table(rows, geo_sources, csv_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
