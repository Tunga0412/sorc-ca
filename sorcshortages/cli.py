"""Command line entry point for monthly ingestion and consolidation."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path

from .api import ShortagesApiClient, ShortagesApiError, read_export_zip, write_snapshot
from .pipeline import build_consolidated
from .schema import infer_report_type


def write_browser_dataset(result: dict, output_path: Path) -> Path:
    """Write a browser-loadable copy alongside the canonical JSON dataset."""
    browser_path = output_path.with_suffix(".js")
    browser_path.write_text(
        "window.SORC_DATA = "
        + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    return browser_path


def ingest(args: argparse.Namespace) -> Path:
    snapshot_date = args.snapshot_date or date.today().isoformat()
    if args.csv_zip:
        export = read_export_zip(args.csv_zip)
        return write_snapshot(args.snapshots / f"{snapshot_date}.json", snapshot_date=snapshot_date, shortages=export["shortages"], discontinuations=export["discontinuations"], source="public CSV export")

    email = args.email or os.getenv("SORCSHORTAGES_API_EMAIL")
    password = args.password or os.getenv("SORCSHORTAGES_API_PASSWORD")
    if not email or not password:
        raise SystemExit("API credentials are required. Set SORCSHORTAGES_API_EMAIL and SORCSHORTAGES_API_PASSWORD, or pass --email/--password. Use --csv-zip for the public export fallback.")
    client = ShortagesApiClient(email, password)
    all_rows = client.fetch_all()
    shortages = [row for row in all_rows if infer_report_type(row) == "shortage"]
    discontinuations = [row for row in all_rows if infer_report_type(row) == "discontinuation"]
    return write_snapshot(args.snapshots / f"{snapshot_date}.json", snapshot_date=snapshot_date, shortages=shortages, discontinuations=discontinuations, source="public API")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the SORCShortages monthly data product")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest", help="download one raw dated snapshot")
    ingest_parser.add_argument("--snapshots", type=Path, default=Path("snapshots"))
    ingest_parser.add_argument("--snapshot-date")
    ingest_parser.add_argument("--csv-zip", type=Path, help="use a downloaded public export instead of the API")
    ingest_parser.add_argument("--email")
    ingest_parser.add_argument("--password")
    consolidate_parser = sub.add_parser("consolidate", help="rebuild the derived front-end dataset")
    consolidate_parser.add_argument("--snapshots", type=Path, default=Path("snapshots"))
    consolidate_parser.add_argument("--output", type=Path, default=Path("data/consolidated.json"))
    all_parser = sub.add_parser("all", help="ingest then consolidate")
    all_parser.add_argument("--snapshots", type=Path, default=Path("snapshots"))
    all_parser.add_argument("--output", type=Path, default=Path("data/consolidated.json"))
    all_parser.add_argument("--snapshot-date")
    all_parser.add_argument("--csv-zip", type=Path)
    all_parser.add_argument("--email")
    all_parser.add_argument("--password")
    args = parser.parse_args()
    if args.command == "ingest":
        print(ingest(args))
    elif args.command == "consolidate":
        result = build_consolidated(args.snapshots, args.output)
        browser_path = write_browser_dataset(result, args.output)
        print(f"{args.output}: {len(result['records'])} reports from {result['snapshot_count']} snapshots; {browser_path}")
    else:
        print(ingest(args))
        result = build_consolidated(args.snapshots, args.output)
        browser_path = write_browser_dataset(result, args.output)
        print(f"{args.output}: {len(result['records'])} reports from {result['snapshot_count']} snapshots; {browser_path}")


if __name__ == "__main__":
    main()

