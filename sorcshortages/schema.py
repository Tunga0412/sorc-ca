"""Normalize Health Product Shortages Canada API/CSV rows.

The public site currently exposes the same concepts through a few naming
surfaces (API JSON, CSV export, and human-readable report pages).  This module
keeps the rest of the pipeline independent of those surface-specific names.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Iterable


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _find(record: dict[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    """Find a value using exact, dotted, and normalized keys."""
    for alias in aliases:
        if alias in record and record[alias] not in (None, ""):
            return record[alias]
    normalized = {_norm_key(str(k)): v for k, v in record.items()}
    for alias in aliases:
        value = normalized.get(_norm_key(alias))
        if value not in (None, ""):
            return value
    # A few API responses group fields under report/drug/company objects.
    for parent in ("report", "drug", "company", "shortage", "discontinuation"):
        child = record.get(parent)
        if isinstance(child, dict):
            found = _find(child, aliases, None)
            if found not in (None, ""):
                return found
    return default


def parse_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Keep dates stable even if the API returns ISO timestamps.
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        candidate = match.group(0)
        if is_implausible_date(candidate):
            return None
        return candidate
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            from datetime import datetime

            parsed = datetime.strptime(text, fmt).date().isoformat()
            return None if is_implausible_date(parsed) else parsed
        except ValueError:
            pass
    return None


def is_implausible_date(value: Any) -> bool:
    """Whether a date is ISO-shaped but outside a plausible reporting range."""
    if value is None:
        return False
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value).strip())
    if not match:
        return False
    year = int(match.group(0)[:4])
    return year < 1900 or year > 2100


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "t", "tier 3", "actual tier 3"}


def _text_value(value: Any, default: str = "") -> str:
    """Extract the English display value from API lookup objects."""
    if value is None or value == "":
        return default
    if isinstance(value, dict):
        for key in ("en_name", "en_reason", "label", "name", "value"):
            if value.get(key) not in (None, ""):
                return str(value[key]).strip()
        return default
    return str(value).strip()


def infer_report_type(record: dict[str, Any]) -> str:
    """Infer report type from the API's nested type label or CSV fields."""
    value = _find(record, ("report.type", "report_type", "type"), "")
    if isinstance(value, dict):
        value = value.get("label") or value.get("name") or value.get("value") or ""
    text = str(value).lower()
    if text:
        return "discontinuation" if "discontinu" in text else "shortage"
    if any("discontinu" in _norm_key(str(k)) for k in record):
        return "discontinuation"
    return "shortage"


def normalize_record(record: dict[str, Any], report_type: str | None = None) -> dict[str, Any]:
    """Return a stable, analysis-ready representation of one source row."""
    inferred_type = report_type or infer_report_type(record)
    inferred_type = str(inferred_type).lower()
    if "discontinu" in inferred_type:
        inferred_type = "discontinuation"
    else:
        inferred_type = "shortage"

    report_id = _find(record, ("report.id", "report_id", "id", "reportId"))
    actual_start_raw = _find(record, ("actual_start_date", "actual start date", "actualStartDate"))
    anticipated_start_raw = _find(record, ("anticipated_start_date", "anticipated start date", "anticipatedStartDate"))
    actual_end_raw = _find(record, ("actual_end_date", "actual end date", "actualEndDate", "resolved_date"))
    anticipated_end_raw = _find(record, ("estimated_end_date", "estimated end date", "estimatedEndDate"))
    actual_discontinuation_raw = _find(record, ("discontinuation.date", "actual_discontinuation_date", "actual discontinuation date", "discontinuation_date"))
    anticipated_discontinuation_raw = _find(record, ("anticipated_discontinuation_date", "anticipated discontinuation date"))
    actual_start = parse_date(actual_start_raw)
    anticipated_start = parse_date(anticipated_start_raw)
    actual_end = parse_date(actual_end_raw)
    anticipated_end = parse_date(anticipated_end_raw)
    actual_discontinuation = parse_date(actual_discontinuation_raw)
    anticipated_discontinuation = parse_date(anticipated_discontinuation_raw)

    if inferred_type == "shortage":
        start_date = actual_start or anticipated_start
        start_date_source = "actual_start_date" if actual_start else ("anticipated_start_date" if anticipated_start else None)
        end_date = actual_end
        status = _find(record, ("shortage_status", "shortage status", "status"))
    else:
        start_date = actual_discontinuation or anticipated_discontinuation
        start_date_source = "actual_discontinuation_date" if actual_discontinuation else ("anticipated_discontinuation_date" if anticipated_discontinuation else None)
        end_date = actual_discontinuation
        status = _find(record, ("discontinuation_status", "discontinuation status", "status"))

    status_text = str(status or "Unknown").strip()
    status_lower = status_text.lower()
    closed = any(term in status_lower for term in ("resolved", "avoided", "discontinued", "reversed"))
    ongoing = inferred_type == "shortage" and not end_date and not closed
    selected_start_raw = actual_start_raw if actual_start else anticipated_start_raw
    selected_end_raw = actual_end_raw if inferred_type == "shortage" else actual_discontinuation_raw
    selected_date_plausibility_issue = is_implausible_date(selected_start_raw) or is_implausible_date(selected_end_raw)

    return {
        "report_id": str(report_id) if report_id is not None else None,
        "record_key": f"{inferred_type}:{report_id}" if report_id is not None else None,
        "type": inferred_type,
        "din": str(_find(record, ("din", "drug_identification_number", "drug identification number"), "")),
        "brand_name": _text_value(_find(record, ("brand.name", "brand_name", "brand name", "en_drug_brand_name"), "")),
        "company_name": str(_find(record, ("company.name", "company_name", "company name"), "")),
        "common_name": _text_value(_find(record, ("common.name", "common_name", "common or proper name", "en_drug_common_name"), "")),
        "active_ingredient": _text_value(_find(record, ("ingredients", "active_ingredient", "active_ingredients", "active ingredient(s)", "en_ingredients"), "")),
        "strength": str(_find(record, ("drug_strength", "strength", "strength(s)"), "")),
        "dosage_form": str(_find(record, ("drug_dosage_form", "dosage_form", "dosage form(s)"), "")),
        "route": str(_find(record, ("drug_route", "route", "route of administration"), "")),
        "packaging": str(_find(record, ("drug_package_quantity", "packaging", "packaging size"), "")),
        "atc_code": str(_find(record, ("atc.code", "atc_code", "atc code", "atc_number"), "")),
        "atc_description": str(_find(record, ("atc_description", "atc description"), "")),
        "status": status_text,
        "tier_3": as_bool(_find(record, ("tier_3", "tier3", "tier 3 status", "tier_3_status"), False)),
        "anticipated_start_date": anticipated_start,
        "actual_start_date": actual_start,
        "estimated_end_date": anticipated_end,
        "actual_end_date": actual_end,
        "anticipated_discontinuation_date": anticipated_discontinuation,
        "actual_discontinuation_date": actual_discontinuation,
        "remaining_supply_date": parse_date(_find(record, ("remaining_supply_date", "remaining supply date"))),
        "start_date": start_date,
        "start_date_source": start_date_source,
        "end_date": end_date,
        "date_plausibility_issue": selected_date_plausibility_issue,
        "ongoing": ongoing,
        "updated_date": parse_date(_find(record, ("last.updated", "last_updated", "updated_date", "updated date"))),
        "created_date": parse_date(_find(record, ("date.created", "created_date", "date created"))),
        "reason": _text_value(_find(record, ("reason", "reason for shortage", "reason for discontinuation", "shortage_reason", "discontinuance_reason"), "")),
        "english_description": _text_value(_find(record, ("description_en", "english_description", "description english", "en_comments", "hc_en_comments", "en_discontinuation_comments"), "")),
        "french_description": _text_value(_find(record, ("description_fr", "french_description", "description french", "fr_comments", "hc_fr_comments", "fr_discontinuation_comments"), "")),
        "raw": record,
    }

