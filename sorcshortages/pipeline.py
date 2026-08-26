"""Build a consolidated, front-end-ready dataset from permanent snapshots."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime
import json
from pathlib import Path
from typing import Any

from .schema import normalize_record, parse_date


REPORTING_ERA_START = "2017-03"


def _days_between(start: str | None, end: str | None, censor_date: str) -> int | None:
    if not start:
        return None
    finish = end or censor_date
    try:
        days = (date.fromisoformat(finish) - date.fromisoformat(start)).days
        return days if days >= 0 else None
    except ValueError:
        return None


def _implausible_date(value: str | None) -> bool:
    """Flag syntactically valid dates outside a plausible reporting range."""
    if not value:
        return False
    try:
        year = date.fromisoformat(value).year
    except ValueError:
        return False
    return year < 1900 or year > 2100


def _month(value: str | None) -> str | None:
    return value[:7] if value and len(value) >= 7 else None


def _active_at(row: dict[str, Any], snapshot_date: str) -> bool:
    """Whether a shortage episode is active on a snapshot date.

    Closed statuses are excluded even when a source row has a missing end
    date. This prevents unresolved source fields on resolved/avoided reports
    from becoming active episodes.
    """
    if row.get("type") != "shortage" or not row.get("start_date") or row["start_date"] > snapshot_date:
        return False
    status = str(row.get("status") or "").lower()
    if any(term in status for term in ("resolved", "avoided", "discontinued", "reversed")):
        return False
    return not row.get("end_date") or row["end_date"] > snapshot_date


def _month_range(start_month: str, end_month: str) -> list[str]:
    start = date.fromisoformat(f"{start_month}-01")
    end = date.fromisoformat(f"{end_month}-01")
    months: list[str] = []
    cursor = start
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return months


def _episode_overlaps_month(row: dict[str, Any], month: str) -> bool:
    """Count a reported shortage when its episode overlaps a calendar month."""
    if row.get("type") != "shortage" or not row.get("start_date"):
        return False
    start = date.fromisoformat(row["start_date"])
    end = date.fromisoformat(row["end_date"]) if row.get("end_date") else None
    month_start = date.fromisoformat(f"{month}-01")
    next_month = date(month_start.year + (month_start.month == 12), 1 if month_start.month == 12 else month_start.month + 1, 1)
    month_end = next_month.fromordinal(next_month.toordinal() - 1)
    if end and end < start:
        return False
    return start <= month_end and (end is None or end >= month_start)


def _load_snapshot(path: Path) -> tuple[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot_date = str(payload.get("snapshot_date") or path.stem)
    return snapshot_date, payload


def build_consolidated(snapshot_dir: str | Path, output_path: str | Path) -> dict[str, Any]:
    snapshot_paths = sorted(Path(snapshot_dir).glob("*.json"))
    if not snapshot_paths:
        raise FileNotFoundError(f"No snapshot JSON files found in {snapshot_dir}")

    snapshots: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    for path in snapshot_paths:
        snapshot_date, payload = _load_snapshot(path)
        shortages = [normalize_record(row, "shortage") for row in payload.get("shortages", [])]
        discontinuations = [normalize_record(row, "discontinuation") for row in payload.get("discontinuations", [])]
        snapshots.append((snapshot_date, shortages, discontinuations))
    snapshots.sort(key=lambda x: x[0])

    latest: dict[str, dict[str, Any]] = {}
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot_date, shortages, discontinuations in snapshots:
        for row in shortages + discontinuations:
            key = row.get("record_key") or f"{row['type']}:{row.get('din')}:{row.get('brand_name')}"
            latest[key] = row
            histories[key].append({
                "snapshot_date": snapshot_date,
                "status": row["status"],
                "tier_3": row["tier_3"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "estimated_end_date": row["estimated_end_date"],
                "updated_date": row["updated_date"],
            })

    censor_date = snapshots[-1][0]
    records: list[dict[str, Any]] = []
    for key, row in latest.items():
        clean = {k: v for k, v in row.items() if k != "raw"}
        source_order_issue = bool(row.get("start_date") and row.get("end_date") and row["end_date"] < row["start_date"])
        clean["avoided_before_start"] = bool(source_order_issue and str(row.get("status") or "").lower() == "avoided_shortage")
        clean["date_order_issue"] = bool(source_order_issue and not clean["avoided_before_start"])
        clean["date_plausibility_issue"] = bool(row.get("date_plausibility_issue"))
        clean["future_start"] = bool(row["type"] == "shortage" and row.get("start_date") and row["start_date"] > censor_date)
        clean["active_at_snapshot"] = _active_at(row, censor_date)
        clean["duration_days"] = _days_between(row.get("start_date"), row.get("end_date"), censor_date)
        clean["censored"] = bool(clean["active_at_snapshot"] and not row.get("end_date"))
        clean["history"] = histories[key]
        records.append(clean)
    records.sort(key=lambda r: (r["type"], r.get("start_date") or "9999-99-99", r.get("report_id") or ""))

    shortage_records = [r for r in records if r["type"] == "shortage"]
    first_month = min((_month(r.get("start_date")) for r in shortage_records if r.get("start_date") and r["start_date"] <= censor_date), default=_month(censor_date))
    first_month = max(first_month, REPORTING_ERA_START)
    last_month = _month(censor_date)
    reconstructed_months = _month_range(first_month, last_month)
    monthly_active = Counter({month: sum(_episode_overlaps_month(row, month) for row in shortage_records) for month in reconstructed_months})
    monthly_new = Counter(
        _month(r.get("start_date"))
        for r in shortage_records
        if _month(r.get("start_date")) and r.get("start_date") <= censor_date
    )
    duration_buckets = Counter()
    for row in records:
        if row["type"] != "shortage" or row.get("duration_days") is None:
            continue
        days = row["duration_days"]
        bucket = "0–30 days" if days <= 30 else "31–90 days" if days <= 90 else "91–180 days" if days <= 180 else "181–365 days" if days <= 365 else "366+ days"
        duration_buckets[bucket] += 1

    type_counts = Counter(r["type"] for r in records)
    status_counts = Counter(r["status"] for r in records)
    tier_counts = Counter("Tier 3" if r["tier_3"] else "Other" for r in records if r["type"] == "shortage")
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "latest_snapshot": snapshots[-1][0],
        "snapshot_count": len(snapshots),
        "source": "Health Product Shortages Canada",
        "records": records,
        "summary": {
            "total_reports": len(records),
            "shortages": type_counts.get("shortage", 0),
            "discontinuations": type_counts.get("discontinuation", 0),
            "active_shortages": sum(1 for r in records if r.get("active_at_snapshot")),
            "tier_3_shortages": tier_counts.get("Tier 3", 0),
            "median_duration_days": _median([r["duration_days"] for r in records if r["type"] == "shortage" and r.get("duration_days") is not None]),
            "duration_records": sum(1 for r in records if r["type"] == "shortage" and r.get("duration_days") is not None),
            "date_order_issues": sum(1 for r in records if r.get("date_order_issue")),
            "date_plausibility_issues": sum(1 for r in records if r.get("date_plausibility_issue")),
            "avoided_before_start_records": sum(1 for r in records if r.get("avoided_before_start")),
            "future_start_records": sum(1 for r in records if r.get("future_start")),
        },
        "series": {
            "active_shortages": [{"month": k, "value": monthly_active[k]} for k in sorted(monthly_active)],
            "new_shortages": [{"month": k, "value": monthly_new.get(k, 0)} for k in reconstructed_months],
            "duration_distribution": [{"label": k, "value": duration_buckets[k]} for k in ("0–30 days", "31–90 days", "91–180 days", "181–365 days", "366+ days")],
            "tier_3": [{"label": k, "value": tier_counts.get(k, 0)} for k in ("Tier 3", "Other")],
            "active_basis": "episode_interval_reconstruction",
            "series_start": REPORTING_ERA_START,
        },
        "status_counts": [{"label": k, "value": v} for k, v in status_counts.most_common()],
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else round((values[middle - 1] + values[middle]) / 2)

