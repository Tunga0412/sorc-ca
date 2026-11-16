"""Monthly SORCShortages refresh from the Health Canada source."""

from __future__ import annotations

import argparse
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
import csv
import gzip
import hashlib
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
    read_export_zip,
)
from sorcshortages.cli import write_browser_dataset
from sorcshortages.pipeline import build_consolidated
from sorcshortages.schema import infer_report_type


def snapshot_today():
    return datetime.now(ZoneInfo("America/Edmonton")).date().isoformat()


def month_ranges(start_date: date, end_date: date):
    cursor = start_date
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
    if row.get("draft") is True:
        raise ShortagesApiError("A draft report is outside the public source census")
    identifier = report_id(row)
    if not identifier:
        raise ShortagesApiError(f"{kind} source row has no report ID")
    kind = kind.lower()
    if kind not in {"shortage", "discontinuation"}:
        raise ShortagesApiError(f"Unknown report type: {kind}")
    key = (kind, identifier)
    if key in rows:
        raise ShortagesApiError(f"Repeated source report: {kind}:{identifier}")
    rows[key] = row


def load_existing_dataset(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    match = re.search(r"window\.SORC_DATA\s*=\s*(\{.*\})\s*;?\s*$", text, re.S)
    if not match:
        raise ShortagesApiError(f"Could not parse the existing dataset at {path}")
    return json.loads(match.group(1))


def validate_candidate(result: dict, source_count: int, existing: dict, minimum_records: int, minimum_retention: float, expected_snapshot_date: str | None = None) -> None:
    records = result.get("records") or []
    if len(records) != source_count:
        raise ShortagesApiError(f"Source count {source_count} differs from derived count {len(records)}")
    if source_count < minimum_records or len(records) < minimum_records:
        raise ShortagesApiError(
            f"Candidate contains only {len(records)} records from {source_count} source rows, below the safety floor of {minimum_records}"
        )
    if result.get("latest_snapshot") != (expected_snapshot_date or snapshot_today()):
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
            add_source_row(rows, row, "shortage")
        for row in export["discontinuations"]:
            add_source_row(rows, row, "discontinuation")
        print(
            f"{window_start.isoformat()} to {window_end.isoformat()}: "
            f"{total} reports, {len(rows)} unique so far",
            flush=True,
        )
    if not rows or len(rows) != expected_total:
        raise ShortagesApiError(
            f"Public export returned {len(rows)} unique reports from {expected_total} reported rows"
        )
    return rows, "Health Product Shortages Canada public CSV export"


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the SORCShortages published dataset")
    parser.add_argument("--source", choices=("api", "public-export", "snapshot", "csv-zip"), default="api")
    parser.add_argument("--input-export", type=Path, help="original provider CSV ZIP downloaded in a regular browser")
    parser.add_argument("--input-snapshot", type=Path, help="replay a retained original source snapshot without network access")
    parser.add_argument("--start-date", default="2016-01-01", help="first date-created window for public-export mode")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="last date-created window for public-export mode")
    parser.add_argument("--output", type=Path, default=Path("data/consolidated.json"))
    parser.add_argument("--minimum-records", type=int, default=20000)
    parser.add_argument("--minimum-retention", type=float, default=0.99)
    args = parser.parse_args()
    capture_started = datetime.now(timezone.utc).isoformat()
    replay_date = None
    replay_completed = None

    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if start_date > end_date:
        raise SystemExit("--start-date cannot be later than --end-date")

    if args.source == "api":
        source_rows, source_label = collect_api_rows()
    elif args.source == "public-export":
        source_rows, source_label = collect_public_export_rows(start_date, end_date)
    elif args.source == "csv-zip":
        if not args.input_export: raise ShortagesApiError("CSV source needs --input-export")
        exported = read_export_zip(args.input_export)
        source_rows = {}
        for kind,collection in [("shortage","shortages"),("discontinuation","discontinuations")]:
            for row in exported[collection]: add_source_row(source_rows,row,kind)
        source_label = "Health Product Shortages Canada original public CSV ZIP"
    else:
        if not args.input_snapshot: raise ShortagesApiError("Snapshot source needs --input-snapshot")
        packed = args.input_snapshot.read_bytes()
        payload = json.loads(gzip.decompress(packed) if packed[:2] == b"\x1f\x8b" else packed)
        source_rows = {}
        if "reports" in payload:
            for row in payload["reports"]: add_source_row(source_rows,row,infer_report_type(row))
        else:
            for kind,key in [("shortage","shortages"),("discontinuation","discontinuations")]:
                for row in payload.get(key,[]): add_source_row(source_rows,row,kind)
        source_label = payload.get("source", "Health Product Shortages Canada retained original API capture")
        capture_started = payload.get("capture_started_at", capture_started)
        replay_completed = payload.get("capture_completed_at")
        replay_date = payload.get("snapshot_date") or (datetime.fromisoformat(replay_completed.replace("Z","+00:00")).astimezone(ZoneInfo("America/Edmonton")).date().isoformat() if replay_completed else None)

    if len(source_rows) < args.minimum_records:
        raise ShortagesApiError(f"Source returned only {len(source_rows)} unique reports")

    capture_completed = replay_completed or datetime.now(timezone.utc).isoformat()
    snapshot_date = replay_date or snapshot_today()
    with tempfile.TemporaryDirectory(prefix="sorcshortages-") as temp_dir:
        temp_root = Path(temp_dir)
        snapshots = temp_root / "snapshots"
        raw_path = snapshots / f"{snapshot_date}.json"
        write_snapshot(
            raw_path,
            snapshot_date=snapshot_date,
            shortages=[row for (kind, _), row in source_rows.items() if kind == "shortage"],
            discontinuations=[row for (kind, _), row in source_rows.items() if kind == "discontinuation"],
            source=source_label,
        )
        original = json.loads(raw_path.read_text())
        original.update({"capture_started_at":capture_started,"capture_completed_at":capture_completed,"source_url":"https://healthproductshortages.ca/"})
        raw_path.write_text(json.dumps(original,ensure_ascii=False,indent=2)+"\n")
        raw_bytes = raw_path.read_bytes()
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        archive_name = f"snapshots/{snapshot_date}-{raw_hash[:12]}.json.gz"
        candidate_json = temp_root / "consolidated.json"
        result = build_consolidated(snapshots, candidate_json)
        existing = load_existing_dataset(args.output.with_suffix(".js"))
        validate_candidate(result, len(source_rows), existing, args.minimum_records, args.minimum_retention, snapshot_date)
        result["provenance"] = {"raw_snapshot_path":archive_name,"raw_snapshot_sha256":raw_hash,"source_report_count":len(source_rows),"capture_started_at":capture_started,"capture_completed_at":capture_completed,"source_url":"https://healthproductshortages.ca/","snapshot_timezone":"America/Edmonton","calculation_version":"1.1","code_revision":os.getenv("GITHUB_SHA") or "local-review"}
        result["provenance"]["producer_code_sha256"] = {name:hashlib.sha256((Path(__file__).resolve().parents[1]/name).read_bytes()).hexdigest() for name in ["sorcshortages/schema.py","sorcshortages/pipeline.py","sorcshortages/api.py","sorcshortages/cli.py","sorcshortages/audit.py","automation/shortages_monthly.py"]}
        old_keys = {r["record_key"] for r in existing.get("records",[])}
        new_keys = {r["record_key"] for r in result["records"]}
        from sorcshortages.audit import audit_dataset
        audit = audit_dataset(original,result)
        audit["previous_snapshot"] = existing.get("latest_snapshot")
        audit["added_report_keys"] = sorted(new_keys-old_keys)
        audit["removed_report_keys"] = sorted(old_keys-new_keys)
        result["source_audit"] = audit
        candidate_json.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
        candidate_js = write_browser_dataset(result, candidate_json)
        csv_path = temp_root / "records.csv"
        fields = [key for key in result["records"][0] if key != "history"]
        with csv_path.open("w",encoding="utf-8-sig",newline="") as handle:
            writer = csv.DictWriter(handle,fieldnames=fields)
            writer.writeheader()
            for row in result["records"]:
                writer.writerow({key:(";".join(row[key]) if isinstance(row[key],list) else row[key]) for key in fields})

        args.output.parent.mkdir(parents=True, exist_ok=True)
        archive = args.output.parent / archive_name
        archive.parent.mkdir(parents=True,exist_ok=True)
        archive.write_bytes(gzip.compress(raw_bytes,mtime=0))
        (temp_root/"source-audit.json").write_text(json.dumps(audit,indent=2)+"\n")
        os.replace(csv_path,args.output.parent/"records.csv")
        os.replace(temp_root/"source-audit.json",args.output.parent/"source-audit.json")
        os.replace(candidate_json, args.output)
        os.replace(candidate_js, args.output.with_suffix(".js"))

    print(f"Source audit passed: {audit['source_records']} reports, {audit['source_fields_checked']} source fields, {audit['months_checked']} months, {audit['field_and_rule_comparisons']} field/rule comparisons",flush=True)
    print(
        f"Validated candidate: {len(result['records'])} reports, "
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
