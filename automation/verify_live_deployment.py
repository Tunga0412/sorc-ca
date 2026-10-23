"""Require the hosted Live page to equal the exact locally accepted bytes."""
from __future__ import annotations
import argparse
import hashlib
import time
from pathlib import Path
import requests
from public_data_reader import public_constant
URL = "https://sorc.ca/sorctracks_live.html"
def check(expected: bytes, actual: bytes):
    if hashlib.sha256(expected).digest() != hashlib.sha256(actual).digest():
        raise ValueError("Deployed Live bytes differ from accepted commit")
    text = actual.decode("utf-8")
    data = public_constant(text, "LIVE_DATA")
    if data.get("demo_fixture") or not data.get("generated_at"):
        raise ValueError("Deployed Live data is invalid")
    if type(data.get("schema_version")) is not int or data["schema_version"] < 8 or not data.get("source_fetched_at"):
        raise ValueError("Live publisher regressed below the received v8 source-capture contract")
    if type(data.get("source_page_count")) is not int or not 1 <= data["source_page_count"] <= 20:
        raise ValueError("Live source page census is missing or invalid")
    required = ("tile.openstreetmap.org", "basemaps.cartocdn.com", "backupTiles", "tileerror")
    if any(x not in text for x in required):
        raise ValueError("Deployed Live map lacks fallback")
    return hashlib.sha256(actual).hexdigest()
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=90)
    parser.add_argument("--delay-seconds", type=int, default=10)
    args = parser.parse_args()
    expected = args.expected.read_bytes()
    digest = hashlib.sha256(expected).hexdigest()
    for attempt in range(args.attempts):
        try:
            response = requests.get(URL, params={"deployment_health_check": str(time.time_ns())}, timeout=(15,30), headers={"User-Agent":"SORCTracks exact deployment monitor/1.5", "Cache-Control":"no-cache"})
            response.raise_for_status()
            if response.status_code != 200 or response.url.split("?")[0] != URL:
                raise ValueError("Unexpected deployed response")
            observed = check(expected, response.content)
            print("Exact accepted Live bytes deployed: " + observed)
            return 0
        except (requests.RequestException, ValueError):
            print("Waiting for exact Live deployment, attempt " + str(attempt + 1))
            if attempt + 1 < args.attempts:
                time.sleep(args.delay_seconds)
    raise SystemExit("GitHub Pages did not deploy the exact accepted Live bytes: " + digest)
if __name__ == "__main__":
    raise SystemExit(main())

