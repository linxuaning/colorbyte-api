#!/usr/bin/env python3
"""Read GA4 campaign-level metrics for a single campaign.

Read-only ops helper for T139 acquisition measurement. It does not touch
checkout, processing, or production state.

Auth:
  ARTIMAGEHUB_GA4_SA_KEY  Service account JSON string. If absent, falls back to
                          ~/.config/artimagehub/gcp-sa.json when present.

Examples:
  python scripts/ga4-campaign-read.py \
    --campaign 2026-05-15_day1_users924to9240 \
    --days 1 \
    --dimension sessionCampaignName

  python scripts/ga4-campaign-read.py \
    --campaign __negative_no_such_campaign__ \
    --expect-zero
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib import parse
from pathlib import Path
from typing import Any

PROPERTY_ID = "525510036"
GA4_URL = f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY_ID}:runReport"
DEFAULT_DIMENSIONS = ("sessionCampaignName", "firstUserCampaignName")
METRICS = ("sessions", "totalUsers", "eventCount", "conversions")
REQUEST_TIMEOUT_SECONDS = 15


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise RuntimeError("script timed out before GA4 read completed")


def _load_service_account_json() -> dict[str, Any]:
    raw = os.environ.get("ARTIMAGEHUB_GA4_SA_KEY", "").strip()
    if not raw:
        fallback = Path.home() / ".config" / "artimagehub" / "gcp-sa.json"
        if fallback.exists():
            raw = fallback.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError(
            "GA4 credentials missing: set ARTIMAGEHUB_GA4_SA_KEY or provide ~/.config/artimagehub/gcp-sa.json"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GA4 service account JSON is invalid") from exc


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _signed_jwt(service_account_info: dict[str, Any]) -> str:
    client_email = service_account_info.get("client_email")
    private_key = service_account_info.get("private_key")
    token_uri = service_account_info.get("token_uri", "https://oauth2.googleapis.com/token")
    if not client_email or not private_key:
        raise RuntimeError("GA4 service account JSON must include client_email and private_key")

    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/analytics.readonly",
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }, separators=(",", ":")).encode())
    unsigned = f"{header}.{payload}".encode("ascii")

    with tempfile.NamedTemporaryFile("w", delete=True) as key_file:
        key_file.write(private_key)
        key_file.flush()
        try:
            signature = subprocess.check_output(
                ["openssl", "dgst", "-sha256", "-sign", key_file.name],
                input=unsigned,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("openssl failed to sign the GA4 service-account JWT") from exc

    return f"{unsigned.decode('ascii')}.{_b64url(signature)}"


def _curl_json(url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("wb", delete=True) as body_file, tempfile.NamedTemporaryFile(
        "w", delete=True
    ) as config_file:
        body_file.write(body)
        body_file.flush()
        config_file.write("silent\n")
        config_file.write("show-error\n")
        config_file.write("fail-with-body\n")
        config_file.write(f"max-time = {REQUEST_TIMEOUT_SECONDS}\n")
        config_file.write(f"url = {json.dumps(url)}\n")
        config_file.write("request = POST\n")
        for key, value in headers.items():
            config_file.write(f"header = {json.dumps(f'{key}: {value}')}\n")
        config_file.write(f"data-binary = @{body_file.name}\n")
        config_file.flush()
        try:
            completed = subprocess.run(
                ["curl", "--config", config_file.name],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("curl is required for this ops script") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "curl request failed").strip()
            raise RuntimeError(message[:500]) from exc
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("HTTP response was not valid JSON") from exc


def _access_token() -> str:
    service_account_info = _load_service_account_json()
    token_uri = service_account_info.get("token_uri", "https://oauth2.googleapis.com/token")
    jwt = _signed_jwt(service_account_info)
    body = parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }).encode("utf-8")
    token_data = _curl_json(token_uri, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("Google token exchange response did not include access_token")
    return str(access_token)


def _post_ga4(token: str, body: dict[str, Any]) -> dict[str, Any]:
    return _curl_json(
        GA4_URL,
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json.dumps(body).encode("utf-8"),
    )


def _campaign_filter(dimension: str, campaign: str, match_type: str) -> dict[str, Any]:
    return {
        "filter": {
            "fieldName": dimension,
            "stringFilter": {
                "matchType": match_type,
                "value": campaign,
                "caseSensitive": False,
            },
        }
    }


def _build_body(campaign: str, days: int, dimension: str, match_type: str, limit: int) -> dict[str, Any]:
    return {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "dimensions": [
            {"name": "date"},
            {"name": dimension},
            {"name": "sessionSourceMedium"},
            {"name": "landingPagePlusQueryString"},
        ],
        "metrics": [{"name": metric} for metric in METRICS],
        "dimensionFilter": _campaign_filter(dimension, campaign, match_type),
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": limit,
    }


def _parse_rows(raw: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in raw.get("rows") or []:
        dimensions = row.get("dimensionValues") or []
        metrics = row.get("metricValues") or []
        rows.append({
            "date": dimensions[0].get("value", "") if len(dimensions) > 0 else "",
            dimension: dimensions[1].get("value", "") if len(dimensions) > 1 else "",
            "sessionSourceMedium": dimensions[2].get("value", "") if len(dimensions) > 2 else "",
            "landingPagePlusQueryString": dimensions[3].get("value", "") if len(dimensions) > 3 else "",
            "sessions": int(metrics[0].get("value", "0")) if len(metrics) > 0 else 0,
            "users": int(metrics[1].get("value", "0")) if len(metrics) > 1 else 0,
            "events": int(metrics[2].get("value", "0")) if len(metrics) > 2 else 0,
            "conversions": int(metrics[3].get("value", "0")) if len(metrics) > 3 else 0,
        })
    return rows


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "sessions": sum(int(row["sessions"]) for row in rows),
        "users": sum(int(row["users"]) for row in rows),
        "events": sum(int(row["events"]) for row in rows),
        "conversions": sum(int(row["conversions"]) for row in rows),
    }


def _dimension_list(value: str) -> list[str]:
    if value == "both":
        return list(DEFAULT_DIMENSIONS)
    return [value]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read GA4 metrics for one campaign.")
    parser.add_argument("--campaign", required=True, help="Campaign value to match, e.g. utm_campaign")
    parser.add_argument("--days", type=int, default=1, help="Trailing days window, default 1")
    parser.add_argument(
        "--dimension",
        default="sessionCampaignName",
        choices=("sessionCampaignName", "firstUserCampaignName", "both"),
        help="GA4 campaign dimension to filter. Use 'both' to query both common口径.",
    )
    parser.add_argument(
        "--match-type",
        default="EXACT",
        choices=("EXACT", "CONTAINS", "BEGINS_WITH", "ENDS_WITH", "FULL_REGEXP", "PARTIAL_REGEXP"),
        help="GA4 stringFilter matchType, default EXACT",
    )
    parser.add_argument("--limit", type=int, default=100, help="Max GA4 rows per dimension, default 100")
    parser.add_argument(
        "--expect-zero",
        action="store_true",
        help="Exit non-zero if any queried dimension returns sessions/users/events/conversions.",
    )
    parser.add_argument("--timeout", type=int, default=45, help="Whole-script timeout seconds, default 45")
    args = parser.parse_args()

    if args.days < 1 or args.days > 90:
        raise SystemExit("--days must be between 1 and 90")
    if args.timeout < 5 or args.timeout > 300:
        raise SystemExit("--timeout must be between 5 and 300")
    global REQUEST_TIMEOUT_SECONDS
    REQUEST_TIMEOUT_SECONDS = max(5, min(30, args.timeout))
    socket.setdefaulttimeout(REQUEST_TIMEOUT_SECONDS)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(args.timeout)
    if args.limit < 1 or args.limit > 10000:
        raise SystemExit("--limit must be between 1 and 10000")

    token = _access_token()
    results: dict[str, Any] = {
        "property_id": PROPERTY_ID,
        "campaign": args.campaign,
        "days": args.days,
        "match_type": args.match_type,
        "dimensions": {},
    }

    any_non_zero = False
    for dimension in _dimension_list(args.dimension):
        body = _build_body(args.campaign, args.days, dimension, args.match_type, args.limit)
        raw = _post_ga4(token, body)
        rows = _parse_rows(raw, dimension)
        totals = _totals(rows)
        any_non_zero = any_non_zero or any(totals.values())
        results["dimensions"][dimension] = {
            "totals": totals,
            "rows": rows,
            "raw_rows": raw.get("rows") or [],
            "row_count": len(rows),
        }

    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))

    if args.expect_zero and any_non_zero:
        print("Expected zero rows/metrics, but campaign returned non-zero metrics", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
