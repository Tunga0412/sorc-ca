"""Independent smoke test for the two public SORCTracks pages.

This check is intentionally separate from the updater. It can run from a
second computer or hosted scheduler and should alert when a page is stale,
incomplete, or visibly showing a known failure state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


DEFAULT_HISTORICAL_URL = "https://sorc.ca/sorctracks_tool.html"
DEFAULT_LIVE_URL = "https://sorc.ca/sorctracks_live.html"
DEFAULT_MAX_LIVE_AGE_HOURS = 48
ERROR_MARKERS = (
    "api key required",
    "error loading map",
    "uncaught typeerror",
    "sample fixture data",
    "mapbox",
    "tiles.stadiamaps.com",
    "api_key=",
    "apikey=",
    "access_token=",
)
MAP_TILE_MARKERS = (
    "basemaps.cartocdn.com",
    "tile.openstreetmap.org",
)
LIVE_TILE_HARDENING_MARKERS = (
    "tile.openstreetmap.org",
    "basemaps.cartocdn.com",
    "backupTiles",
    "tileerror",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_page(target: str) -> tuple[str, int | None]:
    if not target.lower().startswith(("http://", "https://")):
        path = Path(target)
        if path.exists():
            return path.read_text(encoding="utf-8"), None
    response = requests.get(
        target,
        timeout=(15, 45),
        headers={"User-Agent": "SORCTracks health monitor/1.0"},
    )
    response.raise_for_status()
    return response.text, response.status_code


def _constant(html: str, name: str):
    marker = f"const {name} = "
    start = html.find(marker)
    if start < 0:
        raise ValueError(f"missing JavaScript constant: {name}")
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(html[start + len(marker):])
    return value


def _check_common(name: str, html: str, status: int | None) -> list[str]:
    failures: list[str] = []
    if status is not None and status != 200:
        failures.append(f"{name} returned HTTP {status}")
    if len(html) < 1000:
        failures.append(f"{name} is unexpectedly small ({len(html)} bytes)")
    lowered = html.lower()
    for marker in ERROR_MARKERS:
        if marker in lowered:
            failures.append(f"{name} contains failure marker: {marker}")
    return failures


def check_pages(historical_url: str, live_url: str, max_live_age_hours: float) -> dict:
    checked_at = _now_utc()
    failures: list[str] = []
    observations: dict[str, object] = {}

    try:
        historical_html, historical_status = _read_page(historical_url)
        failures.extend(_check_common("Historical page", historical_html, historical_status))
        historical_data = _constant(historical_html, "DATA")
        if not isinstance(historical_data, dict) or not historical_data.get("sites"):
            failures.append("Historical page DATA has no sites")
        observations["historical_bytes"] = len(historical_html.encode("utf-8"))
        observations["historical_sites"] = len(historical_data.get("sites", [])) if isinstance(historical_data, dict) else 0
    except Exception as exc:
        failures.append(f"Historical page check failed: {exc}")

    try:
        live_html, live_status = _read_page(live_url)
        failures.extend(_check_common("Live page", live_html, live_status))
        live_data = _constant(live_html, "LIVE_DATA")
        if not isinstance(live_data, dict):
            failures.append("Live page LIVE_DATA is not an object")
        else:
            observations["live_bytes"] = len(live_html.encode("utf-8"))
            observations["live_sites"] = live_data.get("active_site_count", len(live_data.get("sites", [])))
            observations["live_as_of"] = live_data.get("as_of")
            observations["live_generated_at"] = live_data.get("generated_at")
            if live_data.get("demo_fixture"):
                failures.append("Live page is using fixture data")
            generated_at = live_data.get("generated_at")
            if not generated_at:
                failures.append("Live page has no generated_at timestamp")
            else:
                age_hours = (checked_at - _parse_datetime(str(generated_at))).total_seconds() / 3600
                observations["live_age_hours"] = round(age_hours, 2)
                if age_hours < -1:
                    failures.append("Live page generated_at is in the future")
                elif age_hours > max_live_age_hours:
                    failures.append(
                        f"Live page is {age_hours:.1f} hours old, exceeding {max_live_age_hours:.1f} hours"
                    )
        if not any(marker in live_html for marker in MAP_TILE_MARKERS):
            failures.append("Live page does not contain a supported map tile provider")
        for marker in LIVE_TILE_HARDENING_MARKERS:
            if marker not in live_html:
                failures.append(f"Live page is missing keyless map fallback marker: {marker}")
    except Exception as exc:
        failures.append(f"Live page check failed: {exc}")

    return {
        "status": "fail" if failures else "pass",
        "checked_at": checked_at.isoformat(),
        "historical_url": historical_url,
        "live_url": live_url,
        "max_live_age_hours": max_live_age_hours,
        "failures": failures,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-url", default=DEFAULT_HISTORICAL_URL)
    parser.add_argument("--live-url", default=DEFAULT_LIVE_URL)
    parser.add_argument("--max-live-age-hours", type=float, default=DEFAULT_MAX_LIVE_AGE_HOURS)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    result = check_pages(args.historical_url, args.live_url, args.max_live_age_hours)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

