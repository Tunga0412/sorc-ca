"""Monthly SORCShortages refresh from the Health Canada source."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import tempfile

from sorcshortages.api import (
    PublicExportClient,
    ShortagesApiClient,
    ShortagesApiError,
    write_snapshot,
)
from sorcshortages.cli import write_browser_dataset
from sorcshortages.pipeline import build_consolidated
from sorcshortages.schema import infer_report_type


def month_ranges(start_date: date, end_date: date):
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        yield cursor, min(next_month - timedelta(days=1), end_date)
        cursor = next_month


def report_id(row: dict[str, str]) -> str | None:
    for key in ("Report ID", "report.id", "report_id", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def add_source_row(rows: dict[tuple[str, str], dict], row: dict, kind: str) -> None:
    identifier = report_id(row)
    if not identifier:
        raise ShortagesApiError(f"{kind} source row has no report ID")
    rows[(kind, identifier)] = row


def load_existing_dataset(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    match = re.search(r"window\.SORC_DATA\s*=\s*(\{.*\})\s*;?\s*$", text, re.S)
    if not match:
        raise ShortagesApiError(f"Could not parse the existing dataset at {path}")
    return json.loads(match.group(1))


def validate_candidate(result: dict, source_count: int, existing: dict, minimum_records: int, minimum_retention: float) -> None:
    records = result.get("records") or []
    if source_count < minimum_records or len(records) < minimum_records:
        raise ShortagesApiError(
            f"Candidate contains only {len(records)} records from {source_count} source rows, below the safety floor of {minimum_records}"
        )
    if result.get("latest_snapshot") != date.today().isoformat():
        raise ShortagesApiError("Candidate snapshot date is not today's date")
    keys = [record.get("record_key") for record in records]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ShortagesApiError("Candidate contains missing or duplicate record keys")
    types = {record.get("type") for record in records}
    if not {"shortage", "discontinuation"}.issubset(types):
        raise ShortagesApiError(f"Candidate is missing one report type: {sorted(types)}")
    old_summary = existing.get("summary") or {}
    old_total = int(old_summary.get("total_reports") or 0)
    if old_total and len(records) < round(old_total * minimum_retention):
        raise ShortagesApiError(
            f"Candidate dropped from {old_total} to {len(records)} records, below the {minimum_retention:.0%} retention floor"
        )
    summary = result.get("summary") or {}
    if summary.get("shortages", 0) <= 0 or summary.get("discontinuations", 0) <= 0:
        raise ShortagesApiError("Candidate summary does not contain both report types")
    for label in ("shortages", "discontinuations"):
        old_count = int(old_summary.get(label) or 0)
        new_count = int(summary.get(label) or 0)
        if old_count and new_count < round(old_count * minimum_retention):
            raise ShortagesApiError(
                f"Candidate {label} count dropped from {old_count} to {new_count}, "
                f"below the {minimum_retention:.0%} retention floor"
            )
    series = result.get("series") or {}
    if not series.get("active_shortages") or not series.get("new_shortages"):
        raise ShortagesApiError("Candidate is missing monthly shortage series")


def collect_api_rows() -> tuple[dict[tuple[str, str], dict], str]:
    email = os.getenv("SORCSHORTAGES_API_EMAIL")
    password = os.getenv("SORCSHORTAGES_API_PASSWORD")
    if not email or not password:
        raise ShortagesApiError(
            "API source selected but SORCSHORTAGES_API_EMAIL and SORCSHORTAGES_API_PASSWORD are not set"
        )
    all_rows = ShortagesApiClient(email, password).fetch_all()
    rows: dict[tuple[str, str], dict] = {}
    for row in all_rows:
        kind = infer_report_type(row)
        add_source_row(rows, row, kind)
    print(f"API source returned {len(rows)} unique reports", flush=True)
    return rows, "Health Product Shortages Canada public API"


def collect_public_export_rows(start_date: date, end_date: date) -> tuple[dict[tuple[str, str], dict], str]:
    client = PublicExportClient()
    rows: dict[tuple[str, str], dict] = {}
    expected_total = 0
    for window_start, window_end in month_ranges(start_date, end_date):
        total, export = client.fetch_range(window_start.isoformat(), window_end.isoformat())
        expected_total += total
        window_rows = export["shortages"] + export["discontinuations"]
        if len(window_rows) != total:
            raise ShortagesApiError(
                f"Export window {window_start} to {window_end} reported {total} rows but returned {len(window_rows)}"
            )
        for row in export["shortages"]:
            add_source_row(rows, row, "Shortage")
        for row in export["discontinuations"]:
            add_source_row(rows, row, "Discontinuation")
        print(
            f"{window_start.isoformat()} to {window_end.isoformat()}: "
            f"{total} reports, {len(rows)} unique so far",
            flush=True,
        )
    if not rows or len(rows) < round(expected_total * 0.995):
        raise ShortagesApiError(
            f"Public export returned {len(rows)} unique reports from {expected_total} reported rows"
        )
    return rows, "Health Product Shortages Canada public CSV export"


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the SORCShortages published dataset")
    parser.add_argument("--source", choices=("api", "public-export"), default="api")
    parser.add_argument("--start-date", default="2016-01-01", help="first date-created window for public-export mode")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="last date-created window for public-export mode")
    parser.add_argument("--output", type=Path, default=Path("data/consolidated.json"))
    parser.add_argument("--minimum-records", type=int, default=20000)
    parser.add_argument("--minimum-retention", type=float, default=0.75)
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if start_date > end_date:
        raise SystemExit("--start-date cannot be later than --end-date")

    if args.source == "api":
        source_rows, source_label = collect_api_rows()
    else:
        source_rows, source_label = collect_public_export_rows(start_date, end_date)

    if len(source_rows) < args.minimum_records:
        raise ShortagesApiError(f"Source returned only {len(source_rows)} unique reports")

    snapshot_date = date.today().isoformat()
    with tempfile.TemporaryDirectory(prefix="sorcshortages-") as temp_dir:
        temp_root = Path(temp_dir)
        snapshots = temp_root / "snapshots"
        write_snapshot(
            snapshots / f"{snapshot_date}.json",
            snapshot_date=snapshot_date,
            shortages=[row for (kind, _), row in source_rows.items() if kind == "shortage"],
            discontinuations=[row for (kind, _), row in source_rows.items() if kind == "discontinuation"],
            source=source_label,
        )
        candidate_json = temp_root / "consolidated.json"
        result = build_consolidated(snapshots, candidate_json)
        existing = load_existing_dataset(args.output.with_suffix(".js"))
        validate_candidate(result, len(source_rows), existing, args.minimum_records, args.minimum_retention)
        candidate_js = write_browser_dataset(result, candidate_json)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate_json, args.output)
        os.replace(candidate_js, args.output.with_suffix(".js"))

    print(
        f"Published candidate: {len(result['records'])} reports, "
        f"{result['summary']['shortages']} shortages, "
        f"{result['summary']['discontinuations']} discontinuations, "
        f"snapshot {result['latest_snapshot']}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except ShortagesApiError as exc:
        raise SystemExit(f"SORCShortages refresh failed: {exc}") from exc
