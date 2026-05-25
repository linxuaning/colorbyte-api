#!/usr/bin/env python3
"""Audit Singapore traffic quality for artimagehub.com in GA4.

Checks sessions, engagement, source/medium, landing pages, devices, and hostnames
for country=Singapore over the last N complete days.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from typing import Any

PROPERTY_ID = os.environ.get("ARTIMAGEHUB_GA4_PROPERTY_ID", "525510036").strip()
GA4_URL = f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY_ID}:runReport"


def _get_token() -> str:
    sa_json = os.environ.get("ARTIMAGEHUB_GA4_SA_KEY", "").strip()
    if sa_json:
        sa_info = json.loads(sa_json)
    else:
        with open(os.path.expanduser("~/.config/artimagehub/gcp-sa.json")) as f:
            sa_info = json.load(f)

    from google.oauth2 import service_account  # type: ignore
    from google.auth.transport.requests import Request as GReq  # type: ignore

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    creds.refresh(GReq())
    return creds.token


def _ga4_post(token: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        GA4_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def _date_range(days: int) -> dict[str, str]:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return {"startDate": start.isoformat(), "endDate": end.isoformat()}


def _sg_filter() -> dict[str, Any]:
    return {
        "filter": {
            "fieldName": "country",
            "stringFilter": {"value": "Singapore", "matchType": "EXACT"},
        }
    }


def _rows(resp: dict[str, Any], dims: list[str], metrics: list[str]) -> list[dict[str, Any]]:
    out = []
    for row in resp.get("rows") or []:
        item: dict[str, Any] = {}
        for i, dim in enumerate(dims):
            item[dim] = row["dimensionValues"][i]["value"]
        for i, metric in enumerate(metrics):
            raw = row["metricValues"][i]["value"]
            item[metric] = float(raw) if "." in raw else int(raw)
        out.append(item)
    return out


def run(days: int) -> dict[str, Any]:
    token = _get_token()
    date_range = _date_range(days)
    metric_set = [
        "sessions",
        "activeUsers",
        "screenPageViews",
        "engagedSessions",
        "engagementRate",
        "averageSessionDuration",
        "eventCount",
        "conversions",
    ]

    total = _ga4_post(token, {
        "dateRanges": [date_range],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": m} for m in metric_set],
        "dimensionFilter": _sg_filter(),
        "orderBys": [{"dimension": {"dimensionName": "date"}}],
        "limit": days + 5,
    })

    def breakdown(dimensions: list[str], limit: int = 25) -> list[dict[str, Any]]:
        resp = _ga4_post(token, {
            "dateRanges": [date_range],
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": "sessions"}, {"name": "activeUsers"}, {"name": "engagedSessions"}, {"name": "averageSessionDuration"}],
            "dimensionFilter": _sg_filter(),
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            "limit": limit,
        })
        return _rows(resp, dimensions, ["sessions", "activeUsers", "engagedSessions", "averageSessionDuration"])

    return {
        "property_id": PROPERTY_ID,
        "date_range": date_range,
        "daily": _rows(total, ["date"], metric_set),
        "source_medium": breakdown(["sessionSource", "sessionMedium"], 50),
        "landing_pages": breakdown(["landingPage"], 50),
        "device_browser": breakdown(["deviceCategory", "browser"], 50),
        "hostnames": breakdown(["hostName"], 25),
    }


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def print_report(data: dict[str, Any]) -> None:
    print(f"ArtImageHub 新加坡流量诊断 — GA4 property {data['property_id']}")
    print(f"日期范围: {data['date_range']['startDate']} 至 {data['date_range']['endDate']}")
    print()
    print("每日趋势:")
    print(f"{'日期':<12} {'sessions':>8} {'users':>6} {'PV':>6} {'engaged':>7} {'eng_rate':>9} {'avg_sec':>8} {'conv':>5}")
    for r in data["daily"]:
        print(
            f"{_fmt_date(str(r['date'])):<12} {r['sessions']:>8} {r['activeUsers']:>6} "
            f"{r['screenPageViews']:>6} {r['engagedSessions']:>7} "
            f"{float(r['engagementRate']) * 100:>8.1f}% {float(r['averageSessionDuration']):>8.1f} {r['conversions']:>5}"
        )

    sections = [
        ("来源/媒介", "source_medium", ["sessionSource", "sessionMedium"]),
        ("Landing page", "landing_pages", ["landingPage"]),
        ("设备/浏览器", "device_browser", ["deviceCategory", "browser"]),
        ("Host", "hostnames", ["hostName"]),
    ]
    for title, key, dims in sections:
        print()
        print(title + ":")
        for r in data[key][:20]:
            label = " / ".join(str(r[d]) for d in dims)
            print(
                f"  {label:<70} sessions={r['sessions']:>4} users={r['activeUsers']:>4} "
                f"engaged={r['engagedSessions']:>4} avg_sec={float(r['averageSessionDuration']):.1f}"
            )


def main() -> int:
    days = 14
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        days = int(sys.argv[idx + 1])
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=", 1)[1])
    data = run(days)
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_report(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
