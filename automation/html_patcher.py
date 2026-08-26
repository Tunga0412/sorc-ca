"""
HTML patcher.
- Parses the existing const DATA = {...}; block in sorctracks_tool.html
  (the JSON spans multiple lines, so we use a brace-depth scan, not a regex).
- Builds new sites by merging static metadata from existing HTML with fresh CSV numbers.
- Rewrites both DATA and YEAR_SUMMARY blocks, and the footer "Updated" date.
- Preserves everything else in the file byte-for-byte.
"""

import json
import math
import re
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd


DATA_PREFIX = "const DATA = "
YEAR_SUMMARY_PREFIX = "const YEAR_SUMMARY = "
SERVICE_YEAR_SUMMARY_PREFIX = "const SERVICE_YEAR_SUMMARY = "
SERVICE_LAYER_META_PREFIX = "const SERVICE_LAYER_META = "
OB_SUBSECTION_META_PREFIX = "const OB_SUBSECTION_META = "
SERVICE_TYPE_META_PREFIX = "const SERVICE_TYPE_META = "
UPDATED_PATTERN = re.compile(r"(<div>)Updated [^&]+( &middot;)")

SERVICE_LAYER_META_DEFAULT = {
    "all": {
        "label": "All analyzed services",
        "note": "All analyzed services reports additive service-hours across completed service layers.",
        "color": "#5A6A85",
        "sort_order": 0,
    },
    "ed": {
        "label": "Emergency",
        "note": "Emergency Department layer selected.",
        "color": "#C23B3B",
        "sort_order": 10,
    },
    "ob": {
        "label": "Obstetrics",
        "note": "Obstetrics layer selected. Use the Type filter for broad disruption views; hover over monthly bars to see contributing service categories.",
        "color": "#2F8C83",
        "sort_order": 20,
    },
    "acute": {
        "label": "Acute Care",
        "note": "Acute Care layer selected. Source-verified acute/inpatient disruption intervals are included; hover over monthly bars to see the affected disruption type.",
        "color": "#7A5C9E",
        "sort_order": 30,
    },
    "surgery": {
        "label": "Surgery",
        "note": "Surgery/operative capability layer selected. Source-verified surgery, operating-room, anesthesia, and operative-backup disruption intervals are included; hover over monthly bars to see the affected disruption type.",
        "color": "#B06A2C",
        "sort_order": 40,
    },
    "other": {
        "label": "Other Services",
        "note": "Other Services layer selected. This reports documented disruption/access-reduction interval-hours for validated non-ED, non-obstetrics, non-acute-care, and non-surgery service categories; these are not equivalent to full service unavailability.",
        "color": "#577C6F",
        "sort_order": 50,
    },
}

ACUTE_TYPE_META = {
    "default": "all_acute_care_disruptions",
    "order": [
        "all_acute_care_disruptions",
        "partial_bed_reduction",
        "service_or_unit_unavailable",
        "support_service_disruption",
        "other_mixed_acute_care",
    ],
    "options": {
        "all_acute_care_disruptions": {
            "label": "All types",
            "note": "All source-verified acute/inpatient disruption intervals.",
        },
        "partial_bed_reduction": {
            "label": "Partial bed reduction",
            "note": "Posted reductions in available acute/inpatient bed capacity.",
        },
        "service_or_unit_unavailable": {
            "label": "Service/unit unavailable",
            "note": "Acute/inpatient service, unit, admissions, transfer, or all-bed unavailability.",
        },
        "support_service_disruption": {
            "label": "Support-service disruption",
            "note": "Internal medicine, hospitalist, physician, or related acute-care support disruption.",
        },
        "other_mixed_acute_care": {
            "label": "Other/mixed acute care",
            "note": "Mixed-service or less-specific acute/inpatient disruption wording.",
        },
    },
}

ACUTE_SUBTYPE_TO_TYPE = {
    "partial_acute_inpatient_bed_reduction": "partial_bed_reduction",
    "all_acute_inpatient_beds_unavailable": "service_or_unit_unavailable",
    "acute_inpatient_service_or_unit_unavailable": "service_or_unit_unavailable",
    "acute_admissions_transfer_disruption": "service_or_unit_unavailable",
    "acute_support_service_disruption": "support_service_disruption",
    "mixed_acute_emergency_department": "other_mixed_acute_care",
    "mixed_acute_obstetrics": "other_mixed_acute_care",
    "mixed_acute_detox": "other_mixed_acute_care",
    "mixed_acute_rehabilitation": "other_mixed_acute_care",
    "mixed_acute_other": "other_mixed_acute_care",
    "acute_inpatient_disruption_unspecified": "other_mixed_acute_care",
    "acute_inpatient_wording_signal_unspecified": "other_mixed_acute_care",
}

SURGERY_TYPE_META = {
    "default": "all_surgery_operative_capability",
    "order": [
        "all_surgery_operative_capability",
        "anesthesia_operative_backup_unavailable",
        "surgery_unavailable_or_reduced",
        "or_slate_bed_capacity_reduction",
        "mixed_operative_service_disruption",
        "other_surgery_operative_disruption",
    ],
    "options": {
        "all_surgery_operative_capability": {
            "label": "All types",
            "note": "All source-verified surgery/operative capability disruption intervals.",
        },
        "anesthesia_operative_backup_unavailable": {
            "label": "Anesthesia/operative backup unavailable",
            "note": "Posted anesthesia, OR backup, or operative backup unavailability.",
        },
        "surgery_unavailable_or_reduced": {
            "label": "Surgery unavailable or reduced",
            "note": "Posted surgery, scheduled surgery, emergent surgery, or orthopedic surgery unavailability or reduction.",
        },
        "or_slate_bed_capacity_reduction": {
            "label": "OR/slate/bed capacity reduction",
            "note": "Posted operating-room, slate, or surgical-bed capacity reductions.",
        },
        "mixed_operative_service_disruption": {
            "label": "Mixed operative-service disruption",
            "note": "Mixed surgery/operative capability disruptions involving obstetrics, endoscopy, or emergency department services.",
        },
        "other_surgery_operative_disruption": {
            "label": "Other",
            "note": "Other source-verified surgery/operative capability disruption wording.",
        },
    },
}

SURGERY_SUBTYPE_TO_TYPE = {
    "anesthesia_or_or_backup_unavailable": "anesthesia_operative_backup_unavailable",
    "emergent_unscheduled_surgery_unavailable": "surgery_unavailable_or_reduced",
    "scheduled_surgery_reduced_or_unavailable": "surgery_unavailable_or_reduced",
    "orthopedic_surgery_disruption": "surgery_unavailable_or_reduced",
    "partial_operating_room_reduction": "or_slate_bed_capacity_reduction",
    "mixed_surgery_obstetrics": "mixed_operative_service_disruption",
    "mixed_surgery_endoscopy": "mixed_operative_service_disruption",
    "mixed_surgery_emergency_department": "mixed_operative_service_disruption",
    "surgery_disruption_other": "other_surgery_operative_disruption",
}

OTHER_TYPE_META = {
    "default": "all_other_services",
    "order": [
        "all_other_services",
        "ambulatory_urgent_care",
        "long_term_care",
        "home_care",
        "rehabilitation",
        "ophthalmology_on_call",
        "cardiology_on_call",
    ],
    "options": {
        "all_other_services": {
            "label": "All types",
            "note": "All validated non-ED, non-obstetrics, non-acute-care, non-surgery service disruption/access-reduction intervals.",
        },
        "ambulatory_urgent_care": {
            "label": "Ambulatory/urgent care",
            "note": "Ambulatory care, advanced ambulatory care, or urgent-care service disruption/access-reduction intervals.",
        },
        "long_term_care": {
            "label": "Long-term care",
            "note": "Long-term care bed or service disruptions.",
        },
        "home_care": {
            "label": "Home care/community",
            "note": "Home care or community-care service disruptions.",
        },
        "rehabilitation": {
            "label": "Rehabilitation",
            "note": "Inpatient rehabilitation or rehabilitation-service disruptions.",
        },
        "ophthalmology_on_call": {
            "label": "Ophthalmology on-call",
            "note": "Ophthalmology on-call service disruption.",
        },
        "cardiology_on_call": {
            "label": "Cardiology on-call",
            "note": "Cardiology on-call service disruption.",
        },
    },
}

OB_EXTRA_SITE_METADATA = {
    "fort saskatchewan": {
        "name": "fort saskatchewan",
        "display_name": "Fort Saskatchewan Community Hospital",
        "facility_name": "Fort Saskatchewan Community Hospital",
        "community": "Fort Saskatchewan",
        "lat": 53.7168,
        "lng": -113.2137,
        "aliases": ["Fort Saskatchewan", "Fort Sask", "Fort Saskatchewan Community Hospital"],
        "metadata_note": "Added for OB/maternity layer; not present in the ED reference layer.",
    },
    "sylvan lake": {
        "name": "sylvan lake",
        "display_name": "Sylvan Lake Advanced Ambulatory Care Centre",
        "facility_name": "Sylvan Lake Advanced Ambulatory Care Centre (SLAAC)",
        "community": "Sylvan Lake",
        "lat": 52.309,
        "lng": -114.096,
        "aliases": ["Sylvan Lake", "Sylvan Lake Advanced Ambulatory Care Centre", "SLAAC"],
        "metadata_note": "Added for OB/maternity layer; not present in the ED reference layer.",
    }
}

ACUTE_EXTRA_SITE_METADATA = {
    "glenrose rehabilitation": {
        "name": "glenrose rehabilitation",
        "display_name": "Glenrose Rehabilitation Hospital",
        "facility_name": "Glenrose Rehabilitation Hospital",
        "community": "Edmonton",
        "lat": 53.5574,
        "lng": -113.5053,
        "aliases": ["Glenrose Rehabilitation", "Glenrose Rehabilitation Hospital"],
        "metadata_note": "Added for Acute Care lab layer; not present in the ED reference layer.",
    },
}

OTHER_EXTRA_SITE_METADATA = {
    "airdrie": {
        "name": "airdrie",
        "display_name": "Airdrie Community Health Centre",
        "facility_name": "Airdrie Community Health Centre",
        "community": "Airdrie",
        "lat": 51.2890,
        "lng": -114.0140,
        "aliases": ["Airdrie", "Airdrie Community Health Centre"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
    "east edmonton": {
        "name": "east edmonton",
        "display_name": "East Edmonton Health Centre",
        "facility_name": "East Edmonton Health Centre",
        "community": "Edmonton",
        "lat": 53.561335980688,
        "lng": -113.462661967363,
        "aliases": ["East Edmonton", "East Edmonton Community Health Centre", "East Edmonton Health Centre"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
    "galahad": {
        "name": "galahad",
        "display_name": "Galahad Care Centre",
        "facility_name": "Galahad Care Centre",
        "community": "Galahad",
        "lat": 52.5220,
        "lng": -111.9250,
        "aliases": ["Galahad", "Galahad Care Centre"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
    "la crete": {
        "name": "la crete",
        "display_name": "Advanced Ambulatory Care Centre / La Crete Medical Clinic",
        "facility_name": "Advanced Ambulatory Care Centre / La Crete Medical Clinic",
        "community": "La Crete",
        "lat": 58.1920,
        "lng": -116.1400,
        "aliases": ["La Crete", "La Crete Medical Clinic", "Advanced Ambulatory Care Centre"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
    "picture butte": {
        "name": "picture butte",
        "display_name": "Piyami Health Centre",
        "facility_name": "Piyami Health Centre",
        "community": "Picture Butte",
        "lat": 49.8730,
        "lng": -112.7850,
        "aliases": ["Picture Butte", "Piyami Health Centre"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
    "rainbow lake": {
        "name": "rainbow lake",
        "display_name": "Rainbow Lake Healthcare Centre",
        "facility_name": "Rainbow Lake Healthcare Centre",
        "community": "Rainbow Lake",
        "lat": 58.503311337081,
        "lng": -119.396585983957,
        "aliases": ["Rainbow Lake", "Rainbow Lake Healthcare Centre", "Rainbow Lake Health Centre", "Rainbow Lake Community Health Services"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
    "south calgary health centre": {
        "name": "south calgary health centre",
        "display_name": "South Calgary Health Centre",
        "facility_name": "South Calgary Health Centre",
        "community": "Calgary",
        "lat": 50.9040,
        "lng": -114.0580,
        "aliases": ["Calgary", "South Calgary", "South Calgary Health Centre"],
        "metadata_note": "Added for Other Services layer; not present in the ED reference layer.",
    },
}

SITE_CANONICAL_OVERRIDES = {
    "barrrhead": "barrhead",
    "brooks health centre": "brooks",
    "crowsnest pass health centre": "crowsnest pass",
    "high river general hospital": "high river",
    "provost health centre": "provost",
    "slake lake": "slave lake",
    "royal alexandra": "royal alexandra hospital",
    "taber health centre": "taber",
    "university of alberta": "university of alberta hospital",
    "whitecourt healthcare centre": "whitecourt",
    "smokey lake": "smoky lake",
}

AHS_ZERO_CANONICAL_OVERRIDES = {
    "ahs zero cardston health centre": "cardston",
    "ahs zero fort saskatchewan community hospital": "fort saskatchewan",
    "ahs zero grande prairie regional hospital": "grande prairie",
    "ahs zero hanna health centre": "hanna",
    "ahs zero medicine hat regional hospital": "medicine hat",
    "ahs zero northern lights regional health centre": "northern lights regional",
    "ahs zero northwest health centre": "high level",
    "ahs zero peter lougheed centre": "peter lougheed",
    "ahs zero red deer regional hospital centre": "red deer",
    "ahs zero rimbey hospital and care centre": "rimbey",
    "ahs zero slave lake healthcare centre": "slave lake",
    "ahs zero three hills health centre": "three hills",
    "ahs zero wetaskiwin hospital and care centre": "wetaskiwin",
}

LAYER_VALUE_FIELDS = {
    "total_hours",
    "years_active",
    "episode_count",
    "monthly",
    "has_disruption_data",
    "episodes_by_year",
    "monthly_details",
    "sublayers",
}


# These category memberships are deliberately kept in step with the OB parser's
# public Type filters.  They let the HTML builder apply the same merged-span
# episode denominator to every displayed Type, rather than exposing raw source
# record counts for the non-default filters.
OB_SUBSECTION_CATEGORY_SETS = {
    "all_ob_maternity_signals": None,
    "all_obstetrics_unavailable": {"all_obstetrics_unavailable"},
    "operative_ob_c_section_unavailable": {
        "all_obstetrics_unavailable",
        "c_section_unavailable",
        "c_section_and_epidural_unavailable",
        "c_section_unavailable_low_risk_ob_continues",
        "delivery_and_urgent_c_section_unavailable_scheduled_c_section_available",
        "surgical_ob_backup_unavailable",
    },
    "epidural_unavailable": {
        "epidural_unavailable",
        "c_section_and_epidural_unavailable",
    },
    "other_named_ob_service_disruption": {
        "ob_physician_coverage_unavailable",
        "ob_staffing_coverage_disruption",
        "conditional_ob_interruption_possible",
        "ldrp_beds_closed_repurposed",
        "non_emergent_ob_services_unavailable",
        "ob_services_disrupted_emergency_c_section_available",
    },
}


def _find_json_block(text, prefix):
    """
    Find a block like `const NAME = {...};` (possibly spanning newlines).
    Returns (start_index, end_index_exclusive, json_str) or None.
    start_index points to the start of the prefix.
    end_index_exclusive points one past the trailing `;`.
    """
    start = text.find(prefix)
    if start < 0:
        return None
    # Find the opening `{` after the prefix
    json_start = text.find("{", start + len(prefix))
    if json_start < 0:
        return None
    # Walk braces, respecting string literals
    depth = 0
    in_string = False
    string_quote = None
    escape = False
    i = json_start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == string_quote:
                in_string = False
        else:
            if ch == '"' or ch == "'":
                in_string = True
                string_quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    json_end = i + 1
                    # Skip whitespace, then expect `;`
                    j = json_end
                    while j < len(text) and text[j] in " \t\r\n":
                        j += 1
                    if j < len(text) and text[j] == ";":
                        return start, j + 1, text[json_start:json_end]
                    return start, json_end, text[json_start:json_end]
        i += 1
    return None


def parse_existing_data(html_path):
    """Return the parsed DATA object (a dict with 'sites' list)."""
    text = Path(html_path).read_text(encoding="utf-8")
    found = _find_json_block(text, DATA_PREFIX)
    if not found:
        raise ValueError(
            f"Could not find '{DATA_PREFIX}{{...}};' block in {html_path}."
        )
    _, _, json_str = found
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"DATA block in {html_path} is not valid JSON: {e}")


def parse_existing_year_summary(html_path):
    """Return the parsed YEAR_SUMMARY object, or {} if absent."""
    text = Path(html_path).read_text(encoding="utf-8")
    found = _find_json_block(text, YEAR_SUMMARY_PREFIX)
    if not found:
        return {}
    _, _, json_str = found
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"YEAR_SUMMARY block in {html_path} is not valid JSON: {e}")


def parse_service_year_summary(html_path):
    """Return SERVICE_YEAR_SUMMARY if present, otherwise {}."""
    text = Path(html_path).read_text(encoding="utf-8")
    found = _find_json_block(text, SERVICE_YEAR_SUMMARY_PREFIX)
    if not found:
        return {}
    _, _, json_str = found
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"SERVICE_YEAR_SUMMARY block in {html_path} is not valid JSON: {e}")


def _build_alias_to_canonical(existing_data):
    """
    Returns a dict mapping every lowercased alias to the canonical site `name`.
    Also includes the canonical name and display_name themselves.
    """
    mapping = {}
    for site in existing_data.get("sites", []):
        canonical = str(site.get("name", "")).lower().strip()
        if not canonical:
            continue
        mapping[canonical] = canonical
        # display_name and facility_name lowercased
        for f in ("display_name", "facility_name"):
            v = site.get(f)
            if v:
                mapping[str(v).lower().strip()] = canonical
        for alias in site.get("aliases", []) or []:
            mapping[str(alias).lower().strip()] = canonical
    return mapping


def _is_existing_alias(site_id, alias_map):
    """True when supplemental metadata resolves to an already-known site."""
    key = str(site_id or "").lower().strip()
    canonical = _preferred_canonical_site(key, alias_map)
    return bool(canonical and canonical != key)


def _preferred_canonical_site(value, alias_map):
    key = str(value or "").lower().strip()
    override = SITE_CANONICAL_OVERRIDES.get(key)
    if override:
        return override
    resolved = alias_map.get(key, key)
    resolved_key = str(resolved or "").lower().strip()
    if resolved_key in AHS_ZERO_CANONICAL_OVERRIDES:
        return AHS_ZERO_CANONICAL_OVERRIDES[resolved_key]
    if resolved_key.startswith("ahs zero "):
        stripped = resolved_key.removeprefix("ahs zero ").strip()
        return SITE_CANONICAL_OVERRIDES.get(stripped, stripped)
    return resolved


def compute_archive_episode_count(fate_ledger_df):
    """
    Compute the "1,023-style" headline episode count from v84's
    v84_all_source_ed_event_fate_ledger.csv.

    Each row in the fate ledger is one source-stream event (one episode).
    The headline count is the number of rows where source_stream is
    'snapshot_archive_episode' or 'notice_archive_event'. Rows where
    source_stream is 'manual_add' are treated as augmentations and are
    NOT counted toward the headline (matching the abstract methodology).

    Returns: (total_count, snapshot_count, notice_count, manual_count)
    Returns (None, None, None, None) if the ledger is missing or malformed.
    """
    if fate_ledger_df is None or len(fate_ledger_df) == 0:
        return None, None, None, None
    if "source_stream" not in fate_ledger_df.columns:
        return None, None, None, None
    snap = int((fate_ledger_df["source_stream"] == "snapshot_archive_episode").sum())
    notice = int((fate_ledger_df["source_stream"] == "notice_archive_event").sum())
    manual = int((fate_ledger_df["source_stream"] == "manual_add").sum())
    return snap + notice, snap, notice, manual


def compute_distinct_episode_counts(active_episodes_df, alias_map=None):
    """
    Count distinct episodes per site and per (site, year), ignoring cross-year
    duplication. v84's site_multi_year and site_year panels count each
    (episode, analysis_year) row separately, so a closure spanning Dec-Jan is
    double-counted. This dedups by (site_id, program, start_date, end_date,
    first_seen_snapshot) and assigns each distinct episode to its earliest
    analysis_year.

    Returns:
      per_site_total: {canonical_site: int}
      per_site_year:  {canonical_site: {year_str: int}}
    Or (None, None) if the input is empty or columns are missing.
    """
    if active_episodes_df is None or len(active_episodes_df) == 0:
        return None, None

    required = {"site_id", "program_or_service", "start_date_text",
                "anticipated_end_date_text", "first_seen_snapshot_date",
                "analysis_year"}
    if not required.issubset(active_episodes_df.columns):
        return None, None

    df = active_episodes_df.copy()
    df["site_id"] = df["site_id"].astype(str).str.lower().str.strip()
    if alias_map:
        df["_canonical"] = df["site_id"].map(lambda s: alias_map.get(s, s))
    else:
        df["_canonical"] = df["site_id"]

    key_cols = ["_canonical", "program_or_service", "start_date_text",
                "anticipated_end_date_text", "first_seen_snapshot_date"]
    # Fill NaN with sentinel so groupby doesn't drop rows
    for c in key_cols:
        df[c] = df[c].fillna("__NA__").astype(str)
    df["analysis_year"] = pd.to_numeric(df["analysis_year"], errors="coerce")
    df = df.dropna(subset=["analysis_year"])

    # One row per distinct episode, with its earliest analysis_year
    distinct = df.groupby(key_cols, as_index=False)["analysis_year"].min()
    distinct["analysis_year"] = distinct["analysis_year"].astype(int)

    per_site_total = distinct.groupby("_canonical").size().to_dict()
    per_site_year = {}
    for _, row in distinct.iterrows():
        site = row["_canonical"]
        year = str(int(row["analysis_year"]))
        per_site_year.setdefault(site, {})
        per_site_year[site][year] = per_site_year[site].get(year, 0) + 1

    return per_site_total, per_site_year


def compute_merged_episode_counts(
    active_episodes_df,
    alias_map=None,
    service_col="program_or_service",
    start_col="first_interval_in_window",
    end_col="last_interval_in_window",
    gap_tolerance_hours=0,
    merge_scope="site_service",
):
    """
    Merge source-derived active episode-year spans into public disruption episodes.

    Active episode files are reconstruction ledgers, not true real-world episode
    ledgers. A single continuous disruption can appear as repeated source keys
    or as separate calendar-year rows. Public "episode" counts should therefore
    use merged spans, while archive-derived row/key counts remain QA/provenance
    metrics. Most layers merge by site/service. OB top-line episodes merge by
    site only because multiple posted OB service labels/capability labels can
    describe the same continuous maternity disruption period.
    """
    if active_episodes_df is None or len(active_episodes_df) == 0:
        return {}, {}, 0, 0, {}, pd.DataFrame()

    site_col = "site_best" if "site_best" in active_episodes_df.columns else "site_id"
    required = {site_col, service_col, start_col, end_col}
    if not required.issubset(active_episodes_df.columns):
        return {}, {}, 0, 0, {}, pd.DataFrame()

    df = active_episodes_df.copy()
    alias_map = alias_map or {}
    df["_merge_site"] = df[site_col].map(lambda s: _preferred_canonical_site(s, alias_map))
    df["_merge_service"] = df[service_col].fillna("").astype(str).str.lower().str.strip()
    df["_merge_start"] = pd.to_datetime(df[start_col], errors="coerce")
    df["_merge_end"] = pd.to_datetime(df[end_col], errors="coerce")
    df = df[
        df["_merge_site"].fillna("").astype(str).str.strip().ne("")
        & df["_merge_start"].notna()
        & df["_merge_end"].notna()
        & (df["_merge_end"] > df["_merge_start"])
    ].copy()
    if df.empty:
        return {}, {}, 0, 0, {}, pd.DataFrame()

    def _normalize_episode_service(value):
        service = str(value or "").lower().strip()
        if "emergency" in service or service in {"ed", "emergency dept", "emergency department"}:
            return "emergency department"
        return service

    tolerance = pd.Timedelta(hours=float(gap_tolerance_hours or 0))
    rows = []
    merged_id = 0
    df["_merge_service_norm"] = df["_merge_service"].map(_normalize_episode_service)
    if merge_scope == "site":
        group_iter = ((site_id, "obstetrics", group) for site_id, group in df.groupby("_merge_site", dropna=False))
    else:
        group_iter = (
            (site_id, service, group)
            for (site_id, service), group in df.groupby(["_merge_site", "_merge_service_norm"], dropna=False)
        )
    for site_id, service, group in group_iter:
        spans = sorted(zip(group["_merge_start"], group["_merge_end"]))
        if not spans:
            continue
        cur_start, cur_end = spans[0]
        source_span_count = 1
        for start, end in spans[1:]:
            if start <= cur_end + tolerance:
                cur_end = max(cur_end, end)
                source_span_count += 1
            else:
                merged_id += 1
                rows.append({
                    "merged_episode_id": merged_id,
                    "site_id": site_id,
                    "service": service,
                    "merged_start": cur_start,
                    "merged_end": cur_end,
                    "source_span_count": source_span_count,
                })
                cur_start, cur_end = start, end
                source_span_count = 1
        merged_id += 1
        rows.append({
            "merged_episode_id": merged_id,
            "site_id": site_id,
            "service": service,
            "merged_start": cur_start,
            "merged_end": cur_end,
            "source_span_count": source_span_count,
        })

    merged = pd.DataFrame(rows)
    if merged.empty:
        return {}, {}, 0, 0, {}, merged

    per_site_total = merged.groupby("site_id").size().astype(int).to_dict()
    per_site_year = {}
    annual_counts = {}
    total_episode_years = 0
    for _, row in merged.iterrows():
        start = pd.Timestamp(row["merged_start"])
        end = pd.Timestamp(row["merged_end"])
        site = row["site_id"]
        for year in range(int(start.year), int(end.year) + 1):
            year_start = pd.Timestamp(f"{year}-01-01")
            year_end = pd.Timestamp(f"{year + 1}-01-01")
            if min(end, year_end) > max(start, year_start):
                year_str = str(year)
                per_site_year.setdefault(site, {})
                per_site_year[site][year_str] = per_site_year[site].get(year_str, 0) + 1
                annual_counts[year_str] = annual_counts.get(year_str, 0) + 1
                total_episode_years += 1

    return per_site_total, per_site_year, int(len(merged)), int(total_episode_years), annual_counts, merged


def restrict_episode_counts_to_parent_spans(parent_ledger, child_ledger):
    """Count child-Type activity using the all-OB merged episode spans it occupies.

    A category can have two separated source periods inside one continuous
    all-OB disruption because another OB signal bridges the gap.  Counting the
    child spans directly would then make a narrower Type appear to have more
    episodes than its parent.  This maps each child span back to its overlapping
    all-OB parent episode before counting it.
    """
    if parent_ledger is None or child_ledger is None or parent_ledger.empty or child_ledger.empty:
        return {}, {}

    parents_by_site = {}
    for _, row in parent_ledger.iterrows():
        site = str(row.get("site_id") or "")
        start = pd.to_datetime(row.get("merged_start"), errors="coerce")
        end = pd.to_datetime(row.get("merged_end"), errors="coerce")
        if site and pd.notna(start) and pd.notna(end) and end > start:
            parents_by_site.setdefault(site, []).append((int(row["merged_episode_id"]), start, end))

    matched = {}
    for _, row in child_ledger.iterrows():
        site = str(row.get("site_id") or "")
        start = pd.to_datetime(row.get("merged_start"), errors="coerce")
        end = pd.to_datetime(row.get("merged_end"), errors="coerce")
        if not site or pd.isna(start) or pd.isna(end) or end <= start:
            continue
        for parent_id, parent_start, parent_end in parents_by_site.get(site, []):
            if start < parent_end and end > parent_start:
                matched.setdefault((site, parent_id), []).append((start, end))

    per_site_total = {}
    per_site_year = {}
    for (site, _parent_id), spans in matched.items():
        per_site_total[site] = per_site_total.get(site, 0) + 1
        years = set()
        for start, end in spans:
            for year in range(int(start.year), int(end.year) + 1):
                year_start = pd.Timestamp(f"{year}-01-01")
                year_end = pd.Timestamp(f"{year + 1}-01-01")
                if min(end, year_end) > max(start, year_start):
                    years.add(str(year))
        for year in years:
            per_site_year.setdefault(site, {})
            per_site_year[site][year] = per_site_year[site].get(year, 0) + 1
    return per_site_total, per_site_year


def build_new_data(existing_data, site_multi_year, site_year_month, site_year,
                   rescued_hours_by_site=None, rescued_episode_count_by_site=None,
                   distinct_episode_count_by_site=None,
                   distinct_episode_count_by_site_year=None,
                   rescued_episode_year_by_site=None):
    """
    Build the new DATA dict.

    Extra parameters for the rescue layer:
      rescued_hours_by_site: dict {canonical_site_name: {year_month: hours}}
        Hours to ADD to the v84 monthly time series for each site.
      rescued_episode_count_by_site: dict {canonical_site_name: int}
        Number of rescued episodes to add to the per-site episode count.
      rescued_episode_year_by_site: dict {canonical_site_name: {year_str: int}}
        How rescued episodes break down by year (each rescued episode counted
        in the year of its start date).

    Cross-year-dedup parameters (preferred when available):
      distinct_episode_count_by_site: dict {canonical_site_name: int}
        Per-site distinct episode count after cross-year deduplication.
        If provided, this is used instead of v84's site_multi_year sum.
      distinct_episode_count_by_site_year: dict {canonical_site_name: {year_str: int}}
        Per-(site, year) distinct count after cross-year deduplication.
        If provided, this is used instead of v84's site_year panel.

    After applying rescued hours, total_hours is recomputed as sum(monthly) and
    years_active is recomputed as the count of distinct years with > 0 hours.
    """
    rescued_hours_by_site = rescued_hours_by_site or {}
    rescued_episode_count_by_site = rescued_episode_count_by_site or {}
    rescued_episode_year_by_site = rescued_episode_year_by_site or {}
    distinct_episode_count_by_site = distinct_episode_count_by_site or {}
    distinct_episode_count_by_site_year = distinct_episode_count_by_site_year or {}
    alias_map = _build_alias_to_canonical(existing_data)

    smy = site_multi_year.copy() if not site_multi_year.empty else pd.DataFrame()
    sym = site_year_month.copy() if not site_year_month.empty else pd.DataFrame()
    sy = site_year.copy() if not site_year.empty else pd.DataFrame()

    def _canonical(s):
        key = str(s).lower().strip()
        return alias_map.get(key, key)  # if no match, return the lowercased raw

    if not smy.empty:
        smy["site_best"] = smy["site_best"].astype(str).str.lower().str.strip()
        smy["_canonical"] = smy["site_best"].map(_canonical)
    if not sym.empty:
        sym["site_best"] = sym["site_best"].astype(str).str.lower().str.strip()
        sym["_canonical"] = sym["site_best"].map(_canonical)
        sym["year_month"] = sym["year_month"].astype(str)
    if not sy.empty:
        sy["site_best"] = sy["site_best"].astype(str).str.lower().str.strip()
        sy["_canonical"] = sy["site_best"].map(_canonical)
        sy["analysis_year"] = sy["analysis_year"].astype(int)

    # Aggregate the monthly panel by canonical site + year_month (sum hours)
    monthly_by_site = {}
    if not sym.empty:
        if "summary_variant" in sym.columns:
            sym_filt = sym[sym["summary_variant"].astype(str) == "all_methods"].copy()
            if sym_filt.empty:
                sym_filt = sym
        else:
            sym_filt = sym
        agg = sym_filt.groupby(["_canonical", "year_month"], as_index=False)["unioned_closure_hours"].sum()
        for site, group in agg.groupby("_canonical"):
            monthly_by_site[site] = {
                str(r["year_month"]): float(r["unioned_closure_hours"])
                for _, r in group.iterrows()
                if pd.notna(r.get("unioned_closure_hours")) and float(r["unioned_closure_hours"]) > 0
            }

    # Aggregate episodes_by_year by canonical (sum across aliases)
    episodes_by_year_by_site = {}
    if not sy.empty:
        agg = sy.groupby(["_canonical", "analysis_year"], as_index=False)["active_episode_count"].sum()
        for site, group in agg.groupby("_canonical"):
            episodes_by_year_by_site[site] = {
                str(int(r["analysis_year"])): int(r["active_episode_count"] or 0)
                for _, r in group.iterrows()
            }

    # Aggregate site-level totals by canonical (sum hours and episodes, max years_active)
    aggregate_by_site = {}
    if not smy.empty:
        agg = smy.groupby("_canonical", as_index=False).agg(
            unioned_closure_hours=("unioned_closure_hours", "sum"),
            active_episode_count=("active_episode_count", "sum"),
            years_active=("years_active", "max"),
        )
        for _, r in agg.iterrows():
            site = str(r["_canonical"])
            aggregate_by_site[site] = {
                "total_hours": float(r["unioned_closure_hours"] or 0),
                "years_active": int(r["years_active"] or 0),
                "episode_count": int(r["active_episode_count"] or 0),
            }

    # Identify unmapped sites: CSV site_ids whose canonical lookup did NOT hit the HTML
    html_canonical_set = {str(s.get("name", "")).lower().strip() for s in existing_data.get("sites", [])}
    csv_canonicals = set(aggregate_by_site.keys()) | set(monthly_by_site.keys()) | set(episodes_by_year_by_site.keys())
    unmapped = sorted(csv_canonicals - html_canonical_set)

    # Walk the existing HTML site list and rebuild each
    all_years = _all_years_from_episodes(episodes_by_year_by_site)
    new_sites = []
    for site in existing_data.get("sites", []):
        site_id = str(site.get("name", "")).lower().strip()
        is_reference = site.get("has_disruption_data") is False

        new_site = {
            "name": site_id,
            "display_name": site.get("display_name", site_id.title()),
        }
        if "facility_name" in site:
            new_site["facility_name"] = site["facility_name"]
        if "community" in site:
            new_site["community"] = site["community"]
        new_site["lat"] = site.get("lat")
        new_site["lng"] = site.get("lng")
        new_site["aliases"] = site.get("aliases", [])

        if is_reference:
            new_site.update({
                "total_hours": 0,
                "years_active": 0,
                "episode_count": 0,
                "monthly": {},
                "has_disruption_data": False,
                "episodes_by_year": _zero_year_map(all_years),
            })
        else:
            agg = aggregate_by_site.get(site_id, {"total_hours": 0, "years_active": 0, "episode_count": 0})
            monthly = dict(monthly_by_site.get(site_id, {}))  # copy
            eps_by_year = episodes_by_year_by_site.get(site_id, {})

            # Apply rescued hours: add to monthly dict
            rescued_for_site = rescued_hours_by_site.get(site_id, {})
            for ym, h in rescued_for_site.items():
                monthly[ym] = monthly.get(ym, 0.0) + float(h)

            # Recompute totals after rescue
            monthly_total = sum(monthly.values())
            distinct_years = len({ym[:4] for ym, h in monthly.items() if float(h) > 0})

            # For public episode counts, prefer merged site/service disruption
            # spans so repeated source postings do not inflate episode totals.
            rescued_eps = int(rescued_episode_count_by_site.get(site_id, 0))
            if site_id in distinct_episode_count_by_site:
                total_episodes = int(distinct_episode_count_by_site.get(site_id) or 0) + rescued_eps
            else:
                total_episodes = int(agg["episode_count"]) + rescued_eps

            if site_id in distinct_episode_count_by_site_year:
                year_breakdown = {
                    str(y): int((distinct_episode_count_by_site_year.get(site_id) or {}).get(str(y), 0) or 0)
                    for y in all_years
                }
            else:
                year_breakdown = dict(eps_by_year)
            for y, n in (rescued_episode_year_by_site.get(site_id) or {}).items():
                year_breakdown[str(y)] = int(year_breakdown.get(str(y), 0)) + int(n)
            full_eps = {str(y): int(year_breakdown.get(str(y), 0)) for y in all_years}

            new_site.update({
                "total_hours": _round_hours(monthly_total),
                "years_active": distinct_years if rescued_for_site else int(agg["years_active"]),
                "episode_count": total_episodes,
                "monthly": {ym: _round_hours(h) for ym, h in sorted(monthly.items())},
                "has_disruption_data": True,
                "episodes_by_year": full_eps,
            })

        # Preserve any custom fields the user may have added to the existing site dict
        # (anything we did not explicitly write above)
        known_fields = {
            "name", "display_name", "facility_name", "community", "lat", "lng",
            "aliases", "total_hours", "years_active", "episode_count", "monthly",
            "has_disruption_data", "episodes_by_year",
        }
        for k, v in site.items():
            if k not in known_fields and k not in new_site:
                new_site[k] = v

        new_sites.append(new_site)

    return {"sites": new_sites}, unmapped


def build_year_summary(new_data, distinct_total_episodes=None):
    """
    Build the YEAR_SUMMARY dict from the post-rescue new_data.

    Returns:
      {"all": {hours, episodes, affected_sites, reference_sites},
       "<year>": {hours, episodes, affected_sites, reference_sites}, ...}

    - hours per year = sum of all sites' monthly hours in that year
    - episodes per year = sum of all sites' episodes_by_year[year]
      (v84 convention: an episode active during a year counts in that year,
       so cross-year episodes contribute to multiple years; matches original
       per-site click UX)
    - affected_sites per year = count of distinct sites with > 0 hours
    - "all" rolls up hours summed monthly (no cross-year double-count) and
      uses distinct_total_episodes (cross-year-deduplicated) when provided.
      Otherwise falls back to sum of per-site episode_count (which DOES
      include cross-year duplication; only use when no distinct count is
      available).
    - reference_sites = total searchable AHS locations (active + reference)
    """
    sites = new_data.get("sites", [])
    if not sites:
        return {}

    reference_site_count = len(sites)

    # Collect all years that appear in any monthly key or episodes_by_year key
    all_years = set()
    for s in sites:
        for ym in (s.get("monthly") or {}).keys():
            all_years.add(int(ym[:4]))
        for y in (s.get("episodes_by_year") or {}).keys():
            try:
                all_years.add(int(y))
            except Exception:
                pass

    out = {}

    # "all" rollup
    all_hours = sum(float(s.get("total_hours", 0)) for s in sites)
    if distinct_total_episodes is not None:
        all_episodes = int(distinct_total_episodes)
    else:
        all_episodes = sum(int(s.get("episode_count", 0)) for s in sites)
    affected_all = sum(1 for s in sites if float(s.get("total_hours", 0)) > 0)
    out["all"] = {
        "hours": _round_hours(all_hours),
        "episodes": all_episodes,
        "affected_sites": affected_all,
        "reference_sites": reference_site_count,
    }

    # Per-year breakdown
    for year in sorted(all_years):
        year_str = str(year)
        hours_this_year = 0.0
        affected_this_year = 0
        episodes_this_year = 0
        for s in sites:
            site_year_hours = sum(
                float(h) for ym, h in (s.get("monthly") or {}).items() if ym.startswith(year_str)
            )
            hours_this_year += site_year_hours
            if site_year_hours > 0:
                affected_this_year += 1
            episodes_this_year += int((s.get("episodes_by_year") or {}).get(year_str, 0))
        out[year_str] = {
            "hours": _round_hours(hours_this_year),
            "episodes": episodes_this_year,
            "affected_sites": affected_this_year,
            "reference_sites": reference_site_count,
        }

    return out


def _canonicalize_site(value, alias_map):
    return _preferred_canonical_site(value, alias_map)


def _is_ahs_zero_label(value):
    return str(value or "").lower().strip().startswith("ahs zero ")


def _clean_public_aliases(aliases):
    out = []
    for alias in aliases or []:
        if _is_ahs_zero_label(alias):
            continue
        if alias not in out:
            out.append(alias)
    return out


def _site_metadata(site):
    out = {}
    for k, v in site.items():
        if k not in LAYER_VALUE_FIELDS and k != "layers":
            out[k] = _clean_public_aliases(v) if k == "aliases" else v
    return out


def _layer_from_site(site, layer_id=None, all_years=None):
    if layer_id and isinstance(site.get("layers"), dict) and site["layers"].get(layer_id):
        layer = dict(site["layers"][layer_id])
    else:
        layer = {k: site.get(k) for k in LAYER_VALUE_FIELDS if k in site}
    layer.setdefault("monthly", {})
    layer.setdefault("monthly_details", {})
    layer.setdefault("total_hours", _round_hours(sum(float(v) for v in layer.get("monthly", {}).values())))
    layer.setdefault("episode_count", int(layer.get("episode_count") or 0))
    layer.setdefault("years_active", len({ym[:4] for ym, h in (layer.get("monthly") or {}).items() if float(h) > 0}))
    if all_years is not None:
        eps = layer.get("episodes_by_year") or {}
        layer["episodes_by_year"] = {str(y): int(eps.get(str(y), 0) or 0) for y in all_years}
    else:
        layer.setdefault("episodes_by_year", {})
    if "has_disruption_data" not in layer:
        layer["has_disruption_data"] = bool(float(layer.get("total_hours") or 0) > 0 or int(layer.get("episode_count") or 0) > 0)
    out = {
        "total_hours": _round_hours(layer.get("total_hours", 0)),
        "years_active": int(layer.get("years_active") or 0),
        "episode_count": int(layer.get("episode_count") or 0),
        "monthly": {str(k): _round_hours(v) for k, v in sorted((layer.get("monthly") or {}).items()) if float(v) > 0},
        "monthly_details": layer.get("monthly_details") or {},
        "has_disruption_data": bool(layer.get("has_disruption_data")),
        "episodes_by_year": layer.get("episodes_by_year") or {},
    }
    if isinstance(layer.get("sublayers"), dict) and layer["sublayers"]:
        out["sublayers"] = layer["sublayers"]
    return out


def _merge_layer_blocks(left, right):
    left = dict(left or {})
    right = dict(right or {})
    monthly = dict(left.get("monthly") or {})
    for ym, hours in (right.get("monthly") or {}).items():
        monthly[str(ym)] = _round_hours(float(monthly.get(str(ym), 0) or 0) + float(hours or 0))

    monthly_details = dict(left.get("monthly_details") or {})
    for ym, detail in (right.get("monthly_details") or {}).items():
        if ym not in monthly_details:
            monthly_details[ym] = detail

    eps_by_year = dict(left.get("episodes_by_year") or {})
    for year, count in (right.get("episodes_by_year") or {}).items():
        eps_by_year[str(year)] = int(eps_by_year.get(str(year), 0) or 0) + int(count or 0)

    merged = {
        "total_hours": _round_hours(sum(float(v or 0) for v in monthly.values())),
        "years_active": len({str(ym)[:4] for ym, h in monthly.items() if float(h or 0) > 0}),
        "episode_count": int(left.get("episode_count") or 0) + int(right.get("episode_count") or 0),
        "monthly": {str(k): _round_hours(v) for k, v in sorted(monthly.items()) if float(v or 0) > 0},
        "monthly_details": monthly_details,
        "has_disruption_data": bool(left.get("has_disruption_data") or right.get("has_disruption_data") or any(float(v or 0) > 0 for v in monthly.values())),
        "episodes_by_year": eps_by_year,
    }

    sublayers = {}
    for sid, block in (left.get("sublayers") or {}).items():
        sublayers[sid] = block
    for sid, block in (right.get("sublayers") or {}).items():
        sublayers[sid] = _merge_layer_blocks(sublayers.get(sid), block) if sid in sublayers else block
    if sublayers:
        merged["sublayers"] = sublayers
    return merged


def extract_service_layer_data(data, layer_id):
    """Return a plain DATA-shaped object for one layer from layered or legacy DATA."""
    years = sorted({
        int(y)
        for site in data.get("sites", [])
        for y in (site.get("episodes_by_year") or {}).keys()
        if str(y).isdigit()
    })
    out_sites = []
    for site in data.get("sites", []):
        meta = _site_metadata(site)
        layer = _layer_from_site(site, layer_id if site.get("layers") else None, years)
        out = dict(meta)
        out.update(layer)
        out_sites.append(out)
    return {"sites": out_sites}


def build_data_from_site_panels(
    existing_data,
    site_multi_year,
    site_year_month,
    site_year,
    monthly_details=None,
    distinct_episode_count_by_site=None,
    distinct_episode_count_by_site_year=None,
):
    """
    Build a DATA-shaped object from generic site/year/month panels. This is used
    for OB/maternity and future non-ED layers while preserving the existing HTML
    metadata contract.
    """
    alias_map = _build_alias_to_canonical(existing_data)
    smy = site_multi_year.copy() if site_multi_year is not None and not site_multi_year.empty else pd.DataFrame()
    sym = site_year_month.copy() if site_year_month is not None and not site_year_month.empty else pd.DataFrame()
    sy = site_year.copy() if site_year is not None and not site_year.empty else pd.DataFrame()
    details = monthly_details.copy() if monthly_details is not None and not monthly_details.empty else pd.DataFrame()
    distinct_episode_count_by_site = distinct_episode_count_by_site or {}
    distinct_episode_count_by_site_year = distinct_episode_count_by_site_year or {}

    for df in (smy, sym, sy, details):
        if not df.empty:
            source_col = "site_best" if "site_best" in df.columns else "site_id"
            df["_canonical"] = df[source_col].map(lambda s: _canonicalize_site(s, alias_map))

    if not smy.empty:
        smy = smy.groupby("_canonical", as_index=False).agg(
            active_episode_count=("active_episode_count", "sum"),
            years_active=("years_active", "max"),
        )

    monthly_by_site = {}
    if not sym.empty:
        sym["year_month"] = sym["year_month"].astype(str)
        hours_col = "unioned_closure_hours"
        if "summary_variant" in sym.columns:
            filt = sym[sym["summary_variant"].astype(str).eq("all_methods")].copy()
            if not filt.empty:
                sym = filt
        agg = sym.groupby(["_canonical", "year_month"], as_index=False)[hours_col].sum()
        for site, group in agg.groupby("_canonical"):
            monthly_by_site[site] = {
                str(r["year_month"]): _round_hours(r[hours_col])
                for _, r in group.iterrows()
                if pd.notna(r[hours_col]) and float(r[hours_col]) > 0
            }

    eps_year_by_site = {}
    if not sy.empty:
        sy["analysis_year"] = sy["analysis_year"].astype(int)
        agg = sy.groupby(["_canonical", "analysis_year"], as_index=False)["active_episode_count"].sum()
        for site, group in agg.groupby("_canonical"):
            eps_year_by_site[site] = {
                str(int(r["analysis_year"])): int(r["active_episode_count"] or 0)
                for _, r in group.iterrows()
            }

    details_by_site = {}
    if not details.empty:
        details["year_month"] = details["year_month"].astype(str)
        for (site, ym), group in details.groupby(["_canonical", "year_month"], dropna=False):
            services = []
            categories = []
            excerpts = []
            for _, r in group.iterrows():
                row_services = [
                    x.strip()
                    for x in str(r.get("services_down") or "").split("|")
                    if x.strip() and x.strip().lower() != "nan"
                ]
                type_labels = []
                for col in [c for c in group.columns if str(c).endswith("_type_label")]:
                    val = r.get(col)
                    if pd.notna(val) and str(val).strip() and str(val).strip().lower() != "nan":
                        label = str(val).strip()
                        if label not in type_labels:
                            type_labels.append(label)
                subtype_labels = []
                for col in [c for c in group.columns if str(c).endswith("_subtype_label")]:
                    val = r.get(col)
                    if pd.notna(val) and str(val).strip() and str(val).strip().lower() != "nan":
                        label = str(val).strip()
                        if label not in subtype_labels:
                            subtype_labels.append(label)
                display_labels = type_labels or subtype_labels
                if not row_services and display_labels:
                    row_services = list(display_labels)
                if not row_services and pd.notna(r.get("program_or_service")):
                    row_services = [str(r.get("program_or_service")).strip()]
                for service in row_services:
                    if service and service not in services:
                        services.append(service)
                if pd.notna(r.get("capability_categories")):
                    for cat in str(r.get("capability_categories")).split("|"):
                        cat = cat.strip()
                        if cat and cat not in categories:
                            categories.append(cat)
                for cat in display_labels:
                    if cat and cat not in categories:
                        categories.append(cat)
                excerpt_bits = []
                for col in ("source_excerpt", "bed_or_space_reduction_text", "reason_text"):
                    val = r.get(col)
                    if pd.notna(val) and str(val).strip() and str(val).strip().lower() != "nan":
                        excerpt_bits.append(str(val).strip())
                if excerpt_bits:
                    excerpt = " | ".join(excerpt_bits)
                    if excerpt not in excerpts:
                        excerpts.append(excerpt)
            services = [
                service
                for service in services
                if service and service.lower() != "nan"
            ]
            detail = {
                "services": services,
                "services_text": "; ".join(services),
            }
            if excerpts:
                detail["source_excerpt"] = " || ".join(excerpts)[:300]
            if categories:
                detail["categories"] = categories
            details_by_site.setdefault(site, {})[ym] = detail

    all_years = set()
    for d in monthly_by_site.values():
        all_years.update(int(ym[:4]) for ym in d.keys())
    for d in eps_year_by_site.values():
        all_years.update(int(y) for y in d.keys())
    for site in existing_data.get("sites", []):
        for y in (site.get("episodes_by_year") or {}).keys():
            if str(y).isdigit():
                all_years.add(int(y))
    all_years = sorted(all_years)

    existing_lookup = {str(s.get("name", "")).lower().strip(): s for s in existing_data.get("sites", [])}
    metadata_lookup = {k: _site_metadata(v) for k, v in existing_lookup.items()}
    for site_id in list(metadata_lookup):
        canonical = _preferred_canonical_site(site_id, alias_map)
        if canonical != site_id:
            source_meta = metadata_lookup.pop(site_id, {})
            metadata_lookup.setdefault(canonical, dict(source_meta))
            metadata_lookup[canonical]["name"] = canonical
            metadata_lookup[canonical].setdefault("aliases", [])
            if not _is_ahs_zero_label(site_id) and site_id not in metadata_lookup[canonical]["aliases"]:
                metadata_lookup[canonical]["aliases"].append(site_id)
            for alias in source_meta.get("aliases", []) or []:
                if _is_ahs_zero_label(alias):
                    continue
                if alias not in metadata_lookup[canonical]["aliases"]:
                    metadata_lookup[canonical]["aliases"].append(alias)
    for site_id, meta in {**OB_EXTRA_SITE_METADATA, **ACUTE_EXTRA_SITE_METADATA, **OTHER_EXTRA_SITE_METADATA}.items():
        if _is_existing_alias(site_id, alias_map):
            continue
        metadata_lookup.setdefault(site_id, dict(meta))

    layer_sites = sorted(set(metadata_lookup) | set(monthly_by_site) | set(eps_year_by_site))
    new_sites = []
    unmapped = []
    for site_id in layer_sites:
        meta = dict(metadata_lookup.get(site_id, {"name": site_id, "display_name": site_id.title(), "aliases": [site_id]}))
        if "name" not in meta:
            meta["name"] = site_id
        if "display_name" not in meta:
            meta["display_name"] = site_id.title()
        if "aliases" not in meta:
            meta["aliases"] = [meta.get("display_name", site_id)]
        if (meta.get("lat") is None or meta.get("lng") is None) and site_id not in existing_lookup:
            unmapped.append(site_id)

        monthly = monthly_by_site.get(site_id, {})
        eps_year = eps_year_by_site.get(site_id, {})
        total_hours = _round_hours(sum(monthly.values()))
        if site_id in distinct_episode_count_by_site:
            episode_count = int(distinct_episode_count_by_site.get(site_id) or 0)
        else:
            episode_count = int(sum(eps_year.values()))
        if site_id in distinct_episode_count_by_site_year:
            eps_year = {
                str(y): int((distinct_episode_count_by_site_year.get(site_id) or {}).get(str(y), 0) or 0)
                for y in all_years
            }
        layer = {
            "total_hours": total_hours,
            "years_active": len({ym[:4] for ym, h in monthly.items() if float(h) > 0}),
            "episode_count": episode_count,
            "monthly": {ym: _round_hours(h) for ym, h in sorted(monthly.items()) if float(h) > 0},
            "monthly_details": details_by_site.get(site_id, {}),
            "has_disruption_data": bool(total_hours > 0 or episode_count > 0 or sum(eps_year.values()) > 0),
            "episodes_by_year": {str(y): int(eps_year.get(str(y), 0)) for y in all_years},
        }
        site = dict(meta)
        site.update(layer)
        new_sites.append(site)

    return {"sites": new_sites}, sorted(set(unmapped))


def _ob_subsection_meta_from_frame(meta_df):
    if meta_df is None or meta_df.empty or "ob_subsection" not in meta_df.columns:
        return None
    df = meta_df.copy()
    if "sort_order" in df.columns:
        df = df.sort_values("sort_order")
    options = {}
    order = []
    for _, r in df.iterrows():
        sid = str(r.get("ob_subsection") or "").strip()
        if not sid:
            continue
        order.append(sid)
        label = str(r.get("label") or sid.replace("_", " ").title())
        note = str(r.get("note") or "")
        label = (
            label.replace("OB/maternity", "obstetrics")
            .replace("OB/Maternity", "Obstetrics")
            .replace("Operative OB/C-section capability unavailable", "Operative/C-section capability unavailable")
            .replace("Other named OB service disruption", "Other")
        )
        note = (
            note.replace("OB/maternity", "obstetrics")
            .replace("OB/Maternity", "Obstetrics")
            .replace("Operative OB/C-section capability unavailable", "Operative/C-section capability unavailable")
            .replace("Other named OB service disruption", "Other")
        )
        options[sid] = {
            "label": label,
            "note": note,
        }
    if not order:
        return None
    return {"default": order[0], "order": order, "options": options}


def _acute_type_for_subtype(value):
    subtype = str(value or "").strip()
    return ACUTE_SUBTYPE_TO_TYPE.get(subtype, "other_mixed_acute_care")


def _acute_type_label(type_id):
    return ACUTE_TYPE_META["options"].get(type_id, {}).get("label", type_id)


def _split_interval_by_month(start, end):
    cursor = pd.Timestamp(start)
    end = pd.Timestamp(end)
    while cursor < end:
        next_month = cursor.normalize().replace(day=1) + pd.DateOffset(months=1)
        chunk_end = min(end, next_month)
        yield cursor.strftime("%Y-%m"), cursor, chunk_end
        cursor = chunk_end


def _union_interval_hours(group):
    intervals = []
    for _, row in group.iterrows():
        start = pd.to_datetime(row.get("month_interval_start"), errors="coerce")
        end = pd.to_datetime(row.get("month_interval_end"), errors="coerce")
        if pd.notna(start) and pd.notna(end) and end > start:
            intervals.append((start, end))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if not merged or start >= merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return round(sum((end - start).total_seconds() / 3600.0 for start, end in merged), 2)


def _month_rows_from_intervals(intervals, alias_map, type_col=None, type_label_fn=None):
    rows = []
    for _, row in intervals.iterrows():
        start = pd.to_datetime(row.get("interval_start_clipped"), errors="coerce")
        end = pd.to_datetime(row.get("interval_end_clipped"), errors="coerce")
        if pd.isna(start) or pd.isna(end) or end <= start:
            continue
        site_id = _canonicalize_site(row.get("site_best") if "site_best" in intervals.columns else row.get("site_id"), alias_map)
        base = {
            "site_id": site_id,
            "site_best": site_id,
            "program_or_service": row.get("program_or_service"),
            "bed_or_space_reduction_text": row.get("bed_or_space_reduction_text"),
            "reason_text": row.get("reason_text"),
            "start_date_text": row.get("start_date_text"),
            "anticipated_end_date_text": row.get("anticipated_end_date_text"),
        }
        for subtype_col in (
            "acute_inpatient_subtype",
            "acute_inpatient_subtype_label",
            "surgery_subtype",
            "surgery_subtype_label",
            "other_subtype",
            "other_subtype_label",
        ):
            if subtype_col in intervals.columns:
                base[subtype_col] = row.get(subtype_col)
        if type_col:
            type_id = row.get(type_col)
            base[type_col] = type_id
            base[f"{type_col}_label"] = type_label_fn(type_id) if type_label_fn else type_id
        for ym, chunk_start, chunk_end in _split_interval_by_month(start, end):
            out = dict(base)
            out.update(
                {
                    "year_month": ym,
                    "analysis_year": int(str(ym)[:4]),
                    "month_interval_start": chunk_start,
                    "month_interval_end": chunk_end,
                    "raw_interval_hours": round((chunk_end - chunk_start).total_seconds() / 3600.0, 2),
                }
            )
            rows.append(out)
    return pd.DataFrame(rows)


def _site_month_panel_from_month_rows(month_rows, extra_group_cols=None):
    if month_rows is None or month_rows.empty:
        cols = ["site_id", "site_best", "analysis_year", "year_month", "raw_interval_count", "raw_interval_hours", "unioned_closure_hours"]
        return pd.DataFrame(columns=[*(extra_group_cols or []), *cols])
    group_cols = ["site_id", "site_best", "analysis_year", "year_month"]
    if extra_group_cols:
        group_cols.extend(extra_group_cols)
    panel = (
        month_rows.groupby(group_cols, as_index=False, dropna=False)
        .apply(lambda g: pd.Series({
            "raw_interval_count": len(g),
            "raw_interval_hours": round(float(g["raw_interval_hours"].sum()), 2),
            "unioned_closure_hours": _union_interval_hours(g),
        }))
        .reset_index(drop=True)
    )
    return panel


def _collapse_acute_type_frames(output_dir: Path, alias_map):
    intervals_path = output_dir / "acute_inpatient_intervals_source_verified_active_all_years.csv"
    if not intervals_path.exists():
        return {}

    intervals = pd.read_csv(intervals_path).copy()
    interval_site_col = "site_best" if "site_best" in intervals.columns else "site_id"
    intervals["site_id"] = intervals[interval_site_col].map(lambda s: _canonicalize_site(s, alias_map))
    intervals["site_best"] = intervals["site_id"]
    intervals["acute_type"] = intervals["acute_inpatient_subtype"].map(_acute_type_for_subtype)
    intervals["acute_type_label"] = intervals["acute_type"].map(_acute_type_label)
    intervals["first_interval_in_window"] = intervals["interval_start_clipped"]
    intervals["last_interval_in_window"] = intervals["interval_end_clipped"]
    month_rows = _month_rows_from_intervals(intervals, alias_map, type_col="acute_type", type_label_fn=_acute_type_label)
    subtype_months = _site_month_panel_from_month_rows(month_rows, extra_group_cols=["acute_type", "acute_type_label"])
    details = month_rows.copy()

    out = {}
    for type_id in ACUTE_TYPE_META["order"]:
        if type_id == ACUTE_TYPE_META["default"]:
            continue
        months = subtype_months[subtype_months["acute_type"].eq(type_id)].copy()
        type_intervals = intervals[intervals["acute_type"].eq(type_id)].copy()
        if months.empty and type_intervals.empty:
            continue
        if not type_intervals.empty:
            (
                merged_by_site,
                merged_by_site_year,
                merged_total,
                _merged_episode_years,
                _merged_annual,
                _merged_ledger,
            ) = compute_merged_episode_counts(
                type_intervals,
                alias_map=alias_map,
                service_col="acute_type",
                start_col="first_interval_in_window",
                end_col="last_interval_in_window",
            )
        else:
            merged_by_site, merged_by_site_year, merged_total, _merged_ledger = {}, {}, 0, pd.DataFrame()
        if months.empty:
            sym = pd.DataFrame(columns=["site_id", "site_best", "analysis_year", "year_month", "raw_interval_count", "raw_interval_hours", "unioned_closure_hours"])
        else:
            sym = (
                months.groupby(["site_id", "site_best", "analysis_year", "year_month"], as_index=False)
                .agg(
                    raw_interval_count=("raw_interval_count", "sum"),
                    raw_interval_hours=("raw_interval_hours", "sum"),
                    unioned_closure_hours=("unioned_closure_hours", "sum"),
                )
            )
        years = sorted(
            set(int(y) for y in sym["analysis_year"].dropna().astype(int).tolist())
            | {int(y) for d in merged_by_site_year.values() for y in d.keys()}
        )
        sy_rows = []
        for site_id in sorted(set(sym["site_id"]) | set(merged_by_site_year)):
            site_months = sym[sym["site_id"].eq(site_id)] if not sym.empty else pd.DataFrame()
            for year in years:
                y_months = site_months[site_months["analysis_year"].astype(int).eq(int(year))] if not site_months.empty else pd.DataFrame()
                if y_months.empty and not merged_by_site_year.get(site_id, {}).get(str(year), 0):
                    continue
                sy_rows.append(
                    {
                        "site_id": site_id,
                        "site_best": site_id,
                        "analysis_year": int(year),
                        "raw_interval_count": int(y_months["raw_interval_count"].sum()) if not y_months.empty else 0,
                        "raw_interval_hours": float(y_months["raw_interval_hours"].sum()) if not y_months.empty else 0.0,
                        "unioned_closure_hours": float(y_months["unioned_closure_hours"].sum()) if not y_months.empty else 0.0,
                        "active_episode_count": int(merged_by_site_year.get(site_id, {}).get(str(year), 0)),
                    }
                )
        sy = pd.DataFrame(sy_rows)
        smy_rows = []
        for site_id in sorted(set(sym["site_id"]) | set(merged_by_site)):
            site_y = sy[sy["site_id"].eq(site_id)] if not sy.empty else pd.DataFrame()
            smy_rows.append(
                {
                    "site_id": site_id,
                    "site_best": site_id,
                    "years_active": int((site_y["unioned_closure_hours"] > 0).sum()) if not site_y.empty else 0,
                    "active_episode_count": int(merged_by_site.get(site_id, 0)),
                    "raw_interval_hours": float(site_y["raw_interval_hours"].sum()) if not site_y.empty else 0.0,
                    "unioned_closure_hours": float(site_y["unioned_closure_hours"].sum()) if not site_y.empty else 0.0,
                }
            )
        type_details = details[details["acute_type"].eq(type_id)].copy() if not details.empty else pd.DataFrame()
        out[type_id] = {
            "smy": pd.DataFrame(smy_rows),
            "sym": sym,
            "sy": sy,
            "details": type_details,
            "merged_by_site": merged_by_site,
            "merged_by_site_year": merged_by_site_year,
            "merged_total": merged_total,
            "merged_ledger": _merged_ledger,
        }
    return out


def _surgery_type_for_subtype(value):
    subtype = str(value or "").strip()
    return SURGERY_SUBTYPE_TO_TYPE.get(subtype, "other_surgery_operative_disruption")


def _surgery_type_label(type_id):
    return SURGERY_TYPE_META["options"].get(type_id, {}).get("label", type_id)


def _collapse_surgery_type_frames(output_dir: Path, alias_map):
    intervals_path = output_dir / "surgery_intervals_source_verified_active_all_years.csv"
    if not intervals_path.exists():
        return {}

    intervals = pd.read_csv(intervals_path).copy()
    interval_site_col = "site_best" if "site_best" in intervals.columns else "site_id"
    intervals["site_id"] = intervals[interval_site_col].map(lambda s: _canonicalize_site(s, alias_map))
    intervals["site_best"] = intervals["site_id"]
    intervals["surgery_type"] = intervals["surgery_subtype"].map(_surgery_type_for_subtype)
    intervals["surgery_type_label"] = intervals["surgery_type"].map(_surgery_type_label)
    intervals["first_interval_in_window"] = intervals["interval_start_clipped"]
    intervals["last_interval_in_window"] = intervals["interval_end_clipped"]
    month_rows = _month_rows_from_intervals(intervals, alias_map, type_col="surgery_type", type_label_fn=_surgery_type_label)
    subtype_months = _site_month_panel_from_month_rows(month_rows, extra_group_cols=["surgery_type", "surgery_type_label"])
    details = month_rows.copy()

    out = {}
    for type_id in SURGERY_TYPE_META["order"]:
        if type_id == SURGERY_TYPE_META["default"]:
            continue
        months = subtype_months[subtype_months["surgery_type"].eq(type_id)].copy()
        type_intervals = intervals[intervals["surgery_type"].eq(type_id)].copy()
        if months.empty and type_intervals.empty:
            continue
        if not type_intervals.empty:
            (
                merged_by_site,
                merged_by_site_year,
                merged_total,
                _merged_episode_years,
                _merged_annual,
                _merged_ledger,
            ) = compute_merged_episode_counts(
                type_intervals,
                alias_map=alias_map,
                service_col="surgery_type",
                start_col="first_interval_in_window",
                end_col="last_interval_in_window",
            )
        else:
            merged_by_site, merged_by_site_year, merged_total, _merged_ledger = {}, {}, 0, pd.DataFrame()
        if months.empty:
            sym = pd.DataFrame(columns=["site_id", "site_best", "analysis_year", "year_month", "raw_interval_count", "raw_interval_hours", "unioned_closure_hours"])
        else:
            sym = (
                months.groupby(["site_id", "site_best", "analysis_year", "year_month"], as_index=False)
                .agg(
                    raw_interval_count=("raw_interval_count", "sum"),
                    raw_interval_hours=("raw_interval_hours", "sum"),
                    unioned_closure_hours=("unioned_closure_hours", "sum"),
                )
            )
        years = sorted(
            set(int(y) for y in sym["analysis_year"].dropna().astype(int).tolist())
            | {int(y) for d in merged_by_site_year.values() for y in d.keys()}
        )
        sy_rows = []
        for site_id in sorted(set(sym["site_id"]) | set(merged_by_site_year)):
            site_months = sym[sym["site_id"].eq(site_id)] if not sym.empty else pd.DataFrame()
            for year in years:
                y_months = site_months[site_months["analysis_year"].astype(int).eq(int(year))] if not site_months.empty else pd.DataFrame()
                if y_months.empty and not merged_by_site_year.get(site_id, {}).get(str(year), 0):
                    continue
                sy_rows.append(
                    {
                        "site_id": site_id,
                        "site_best": site_id,
                        "analysis_year": int(year),
                        "raw_interval_count": int(y_months["raw_interval_count"].sum()) if not y_months.empty else 0,
                        "raw_interval_hours": float(y_months["raw_interval_hours"].sum()) if not y_months.empty else 0.0,
                        "unioned_closure_hours": float(y_months["unioned_closure_hours"].sum()) if not y_months.empty else 0.0,
                        "active_episode_count": int(merged_by_site_year.get(site_id, {}).get(str(year), 0)),
                    }
                )
        sy = pd.DataFrame(sy_rows)
        smy_rows = []
        for site_id in sorted(set(sym["site_id"]) | set(merged_by_site)):
            site_y = sy[sy["site_id"].eq(site_id)] if not sy.empty else pd.DataFrame()
            smy_rows.append(
                {
                    "site_id": site_id,
                    "site_best": site_id,
                    "years_active": int((site_y["unioned_closure_hours"] > 0).sum()) if not site_y.empty else 0,
                    "active_episode_count": int(merged_by_site.get(site_id, 0)),
                    "raw_interval_hours": float(site_y["raw_interval_hours"].sum()) if not site_y.empty else 0.0,
                    "unioned_closure_hours": float(site_y["unioned_closure_hours"].sum()) if not site_y.empty else 0.0,
                }
            )
        type_details = details[details["surgery_type"].eq(type_id)].copy() if not details.empty else pd.DataFrame()
        out[type_id] = {
            "smy": pd.DataFrame(smy_rows),
            "sym": sym,
            "sy": sy,
            "details": type_details,
            "merged_by_site": merged_by_site,
            "merged_by_site_year": merged_by_site_year,
            "merged_total": merged_total,
            "merged_ledger": _merged_ledger,
        }
    return out


def _attach_ob_sublayers(
    existing_data,
    ob_data,
    output_dir,
    all_distinct_episode_count_by_site=None,
    all_distinct_episode_count_by_site_year=None,
):
    output_dir = Path(output_dir)
    meta_path = output_dir / "ob_maternity_public_subsection_meta.csv"
    smy_path = output_dir / "ob_maternity_public_subsection_site_multi_year_summary.csv"
    sym_path = output_dir / "ob_maternity_public_subsection_site_year_month_panel.csv"
    sy_path = output_dir / "ob_maternity_public_subsection_site_year_panel.csv"
    details_path = output_dir / "ob_maternity_public_subsection_site_month_service_details.csv"
    if not (meta_path.exists() and smy_path.exists() and sym_path.exists() and sy_path.exists()):
        return ob_data, None

    meta_df = pd.read_csv(meta_path)
    subsection_meta = _ob_subsection_meta_from_frame(meta_df)
    if not subsection_meta:
        return ob_data, None

    smy_all = pd.read_csv(smy_path)
    sym_all = pd.read_csv(sym_path)
    sy_all = pd.read_csv(sy_path)
    details_all = pd.read_csv(details_path) if details_path.exists() else pd.DataFrame()

    # The original public Type panels retain active_episode_count from the
    # source episode ledger.  The main OB layer, however, already merges those
    # source rows into contiguous site-level disruption spans.  Recalculate
    # each Type with that same denominator so a narrower Type cannot appear to
    # have more episodes than the all-OB view simply because its records were
    # not coalesced.
    subsection_merged_counts = {}
    subsection_merged_year_counts = {}
    active_episode_path = output_dir / "ob_maternity_episodes_active_all_years.csv"
    if active_episode_path.exists():
        active_episodes = pd.read_csv(active_episode_path)
        if "capability_category" in active_episodes.columns:
            alias_map = _build_alias_to_canonical(existing_data)
            (
                _all_counts,
                _all_year_counts,
                _all_total,
                _all_episode_years,
                _all_annual,
                all_episode_ledger,
            ) = compute_merged_episode_counts(
                active_episodes,
                alias_map=alias_map,
                merge_scope="site",
            )
            for subsection, categories in OB_SUBSECTION_CATEGORY_SETS.items():
                if categories is None:
                    subsection_episodes = active_episodes
                else:
                    subsection_episodes = active_episodes[
                        active_episodes["capability_category"].isin(categories)
                    ].copy()
                _counts, _year_counts, _total, _episode_years, _annual, child_episode_ledger = compute_merged_episode_counts(
                    subsection_episodes,
                    alias_map=alias_map,
                    merge_scope="site",
                )
                counts, year_counts = restrict_episode_counts_to_parent_spans(
                    all_episode_ledger,
                    child_episode_ledger,
                )
                subsection_merged_counts[subsection] = counts
                subsection_merged_year_counts[subsection] = year_counts

    sublayers_by_site = {}
    subsection_labels = subsection_meta.get("options", {})
    for subsection in subsection_meta["order"]:
        smy = smy_all[smy_all["ob_subsection"].astype(str).eq(subsection)].copy() if "ob_subsection" in smy_all.columns else pd.DataFrame()
        sym = sym_all[sym_all["ob_subsection"].astype(str).eq(subsection)].copy() if "ob_subsection" in sym_all.columns else pd.DataFrame()
        sy = sy_all[sy_all["ob_subsection"].astype(str).eq(subsection)].copy() if "ob_subsection" in sy_all.columns else pd.DataFrame()
        details = (
            details_all[details_all["ob_subsection"].astype(str).eq(subsection)].copy()
            if not details_all.empty and "ob_subsection" in details_all.columns
            else pd.DataFrame()
        )
        if sym.empty and sy.empty and smy.empty:
            continue
        is_default = subsection == subsection_meta.get("default")
        distinct_counts = (
            all_distinct_episode_count_by_site
            if is_default
            else subsection_merged_counts.get(subsection)
        )
        distinct_year_counts = (
            all_distinct_episode_count_by_site_year
            if is_default
            else subsection_merged_year_counts.get(subsection)
        )
        sub_data, _ = build_data_from_site_panels(
            existing_data,
            smy,
            sym,
            sy,
            details,
            distinct_episode_count_by_site=distinct_counts,
            distinct_episode_count_by_site_year=distinct_year_counts,
        )
        for site in sub_data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            layer = _layer_from_site(site)
            if not layer.get("has_disruption_data"):
                continue
            label = subsection_labels.get(subsection, {}).get("label", subsection)
            for month, detail in (layer.get("monthly_details") or {}).items():
                detail["ob_subsection"] = subsection
                detail["ob_subsection_label"] = label
            sublayers_by_site.setdefault(site_id, {})[subsection] = layer

    for site in ob_data.get("sites", []):
        site_id = str(site.get("name", "")).lower().strip()
        sublayers = sublayers_by_site.get(site_id, {})
        if sublayers:
            site["sublayers"] = sublayers
            base_details = site.get("monthly_details") or {}
            for month, detail in base_details.items():
                labels = []
                for subsection, layer in sublayers.items():
                    if subsection == subsection_meta.get("default"):
                        continue
                    if float((layer.get("monthly") or {}).get(month) or 0) > 0:
                        label = subsection_labels.get(subsection, {}).get("label", subsection)
                        if label not in labels:
                            labels.append(label)
                if labels:
                    detail["ob_subsections"] = labels
                    detail["ob_subsections_text"] = "; ".join(labels)
    return ob_data, subsection_meta


def build_ob_layer_from_output_dir(existing_data, output_dir, write_merged_ledger=True):
    """Read OB/maternity pipeline outputs and return (data, year_summary, unmapped)."""
    output_dir = Path(output_dir)
    smy = pd.read_csv(output_dir / "ob_maternity_site_multi_year_summary.csv")
    sym = pd.read_csv(output_dir / "ob_maternity_site_year_month_panel.csv")
    sy = pd.read_csv(output_dir / "ob_maternity_site_year_panel.csv")
    details_path = output_dir / "ob_maternity_site_month_service_details.csv"
    details = pd.read_csv(details_path) if details_path.exists() else pd.DataFrame()
    merged_by_site = None
    merged_by_site_year = None
    merged_total = None
    active_path = output_dir / "ob_maternity_episodes_active_all_years.csv"
    if active_path.exists():
        alias_map = _build_alias_to_canonical(existing_data)
        active = pd.read_csv(active_path)
        merged_by_site, merged_by_site_year, merged_total, merged_episode_years, merged_annual, merged_ledger = compute_merged_episode_counts(
            active,
            alias_map=alias_map,
            merge_scope="site",
        )
        if write_merged_ledger and merged_ledger is not None and not merged_ledger.empty:
            merged_ledger.to_csv(output_dir / "ob_maternity_merged_disruption_episodes.csv", index=False)
            merged_ledger.to_csv(output_dir / "ob_maternity_site_level_merged_disruption_episodes.csv", index=False)
    data, unmapped = build_data_from_site_panels(
        existing_data,
        smy,
        sym,
        sy,
        details,
        distinct_episode_count_by_site=merged_by_site,
    )
    if merged_by_site_year:
        all_years = sorted({
            str(y)
            for site in data.get("sites", [])
            for y in (site.get("episodes_by_year") or {}).keys()
        })
        for site in data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            if site_id in merged_by_site_year:
                site["episodes_by_year"] = {
                    year: int(merged_by_site_year[site_id].get(year, 0))
                    for year in all_years
                }
    data, subsection_meta = _attach_ob_sublayers(
        existing_data,
        data,
        output_dir,
        merged_by_site,
        merged_by_site_year,
    )
    year_summary = build_year_summary(data)
    if merged_total is not None:
        year_summary["all"]["episodes"] = int(merged_total)
    return data, year_summary, unmapped, subsection_meta


def _derive_acute_public_panels(existing_data, output_dir):
    output_dir = Path(output_dir)
    sym = pd.read_csv(output_dir / "acute_inpatient_source_verified_site_year_month_panel.csv")
    intervals = pd.read_csv(output_dir / "acute_inpatient_intervals_source_verified_active_all_years.csv")

    alias_map = _build_alias_to_canonical(existing_data)
    public = intervals.copy()
    public["public_service"] = "acute_inpatient"
    public["first_interval_in_window"] = public["interval_start_clipped"]
    public["last_interval_in_window"] = public["interval_end_clipped"]
    (
        merged_by_site,
        merged_by_site_year,
        merged_total,
        merged_episode_years,
        merged_annual,
        merged_ledger,
    ) = compute_merged_episode_counts(
        public,
        alias_map=alias_map,
        service_col="public_service",
        start_col="first_interval_in_window",
        end_col="last_interval_in_window",
    )

    if merged_ledger is None:
        merged_ledger = pd.DataFrame()
    if not merged_ledger.empty:
        merged_ledger.to_csv(output_dir / "acute_inpatient_public_merged_disruption_episodes.csv", index=False)

    months = sym.copy()
    if "site_best" in months.columns:
        months["_canonical"] = months["site_best"].map(lambda s: _canonicalize_site(s, alias_map))
    else:
        months["_canonical"] = months["site_id"].map(lambda s: _canonicalize_site(s, alias_map))
    site_month_hours = months.groupby(["_canonical", "analysis_year", "year_month"], as_index=False).agg(
        raw_interval_count=("raw_interval_count", "sum"),
        raw_interval_hours=("raw_interval_hours", "sum"),
        unioned_closure_hours=("unioned_closure_hours", "sum"),
    )
    site_month_hours["site_id"] = site_month_hours["_canonical"]
    site_month_hours["site_best"] = site_month_hours["_canonical"]
    subtype_month_rows = _month_rows_from_intervals(public, alias_map)
    if not subtype_month_rows.empty and "acute_inpatient_subtype_label" in subtype_month_rows.columns:
        subtype_text = (
            subtype_month_rows.assign(
                acute_inpatient_subtype_label=subtype_month_rows["acute_inpatient_subtype_label"].fillna("").astype(str).str.strip()
            )
            .loc[lambda df: df["acute_inpatient_subtype_label"].ne("")]
            .groupby(["site_id", "analysis_year", "year_month"], as_index=False)
            .agg(
                acute_inpatient_subtypes=(
                    "acute_inpatient_subtype_label",
                    lambda s: " | ".join(sorted(set(x for x in s if x and x.lower() != "nan"))),
                )
            )
        )
        site_month_hours = site_month_hours.merge(
            subtype_text,
            on=["site_id", "analysis_year", "year_month"],
            how="left",
        )
        site_month_hours["acute_inpatient_subtypes"] = site_month_hours["acute_inpatient_subtypes"].fillna("")
    else:
        site_month_hours["acute_inpatient_subtypes"] = ""
    site_month_hours = site_month_hours[
        [
            "site_id",
            "site_best",
            "analysis_year",
            "year_month",
            "raw_interval_count",
            "raw_interval_hours",
            "unioned_closure_hours",
            "acute_inpatient_subtypes",
        ]
    ]

    years = sorted(set(site_month_hours["analysis_year"].astype(int)) | {int(y) for y in merged_annual.keys()})
    sy_rows = []
    for site_id in sorted(set(site_month_hours["site_id"])):
        site_months = site_month_hours[site_month_hours["site_id"].eq(site_id)]
        for year in years:
            y_months = site_months[site_months["analysis_year"].astype(int).eq(int(year))]
            if y_months.empty and not merged_by_site_year.get(site_id, {}).get(str(year), 0):
                continue
            sy_rows.append(
                {
                    "site_id": site_id,
                    "site_best": site_id,
                    "analysis_year": int(year),
                    "raw_interval_count": int(y_months["raw_interval_count"].sum()) if not y_months.empty else 0,
                    "raw_interval_hours": float(y_months["raw_interval_hours"].sum()) if not y_months.empty else 0.0,
                    "unioned_closure_hours": float(y_months["unioned_closure_hours"].sum()) if not y_months.empty else 0.0,
                    "active_episode_count": int(merged_by_site_year.get(site_id, {}).get(str(year), 0)),
                }
            )
    sy = pd.DataFrame(sy_rows)

    smy_rows = []
    for site_id in sorted(set(site_month_hours["site_id"]) | set(merged_by_site)):
        site_y = sy[sy["site_id"].eq(site_id)] if not sy.empty else pd.DataFrame()
        smy_rows.append(
            {
                "site_id": site_id,
                "site_best": site_id,
                "years_active": int((site_y["unioned_closure_hours"] > 0).sum()) if not site_y.empty else 0,
                "active_episode_count": int(merged_by_site.get(site_id, 0)),
                "raw_interval_hours": float(site_y["raw_interval_hours"].sum()) if not site_y.empty else 0.0,
                "unioned_closure_hours": float(site_y["unioned_closure_hours"].sum()) if not site_y.empty else 0.0,
            }
        )
    smy = pd.DataFrame(smy_rows)

    details = public.copy()
    details["acute_type"] = details["acute_inpatient_subtype"].map(_acute_type_for_subtype)
    details["acute_type_label"] = details["acute_type"].map(_acute_type_label)
    details = _month_rows_from_intervals(details, alias_map, type_col="acute_type", type_label_fn=_acute_type_label)
    detail_cols = [
        "site_id",
        "site_best",
        "year_month",
        "analysis_year",
        "acute_type",
        "acute_type_label",
        "acute_inpatient_subtype",
        "acute_inpatient_subtype_label",
        "bed_or_space_reduction_text",
        "reason_text",
        "start_date_text",
        "anticipated_end_date_text",
    ]
    details = details[[c for c in detail_cols if c in details.columns]].drop_duplicates()

    public_active_rows = []
    if not merged_ledger.empty:
        for _, row in merged_ledger.iterrows():
            start = pd.Timestamp(row["merged_start"])
            end = pd.Timestamp(row["merged_end"])
            for year in range(int(start.year), int(end.year) + 1):
                year_start = pd.Timestamp(f"{year}-01-01")
                year_end = pd.Timestamp(f"{year + 1}-01-01")
                clipped_start = max(start, year_start)
                clipped_end = min(end, year_end)
                if clipped_end <= clipped_start:
                    continue
                public_active_rows.append(
                    {
                        "site_id": row["site_id"],
                        "site_best": row["site_id"],
                        "program_or_service": "Acute Care",
                        "start_date_text": pd.Timestamp(row["merged_start"]).strftime("%Y-%m-%d %H:%M:%S"),
                        "anticipated_end_date_text": pd.Timestamp(row["merged_end"]).strftime("%Y-%m-%d %H:%M:%S"),
                        "first_seen_snapshot_date": "public_merged_interval",
                        "analysis_year": int(year),
                        "first_interval_in_window": clipped_start,
                        "last_interval_in_window": clipped_end,
                        "merged_episode_id": int(row["merged_episode_id"]),
                    }
                )
    public_active = pd.DataFrame(public_active_rows)
    public_year = pd.DataFrame(
        [
            {
                "analysis_year": int(year),
                "affected_sites": int(sy[sy["analysis_year"].eq(int(year)) & (sy["unioned_closure_hours"] > 0)]["site_id"].nunique()) if not sy.empty else 0,
                "active_episode_count": int(merged_annual.get(str(year), 0)),
                "unioned_closure_hours": float(sy[sy["analysis_year"].eq(int(year))]["unioned_closure_hours"].sum()) if not sy.empty else 0.0,
            }
            for year in years
        ]
    )

    smy.to_csv(output_dir / "acute_inpatient_public_site_multi_year_summary.csv", index=False)
    sy.to_csv(output_dir / "acute_inpatient_public_site_year_panel.csv", index=False)
    site_month_hours.to_csv(output_dir / "acute_inpatient_public_site_year_month_panel.csv", index=False)
    details.to_csv(output_dir / "acute_inpatient_public_site_month_service_details.csv", index=False)
    public_active.to_csv(output_dir / "acute_inpatient_public_episodes_active_all_years.csv", index=False)
    public_year.to_csv(output_dir / "acute_inpatient_public_year_summary.csv", index=False)
    return smy, site_month_hours, sy, details, merged_by_site, merged_by_site_year, merged_total, public_year, merged_ledger


def build_acute_layer_from_output_dir(existing_data, output_dir):
    """Read acute/inpatient pipeline outputs and return (data, year_summary, unmapped)."""
    output_dir = Path(output_dir)
    smy, sym, sy, details, merged_by_site, merged_by_site_year, merged_total, public_year, parent_ledger = _derive_acute_public_panels(existing_data, output_dir)
    alias_map = _build_alias_to_canonical(existing_data)
    data, unmapped = build_data_from_site_panels(
        existing_data,
        smy,
        sym,
        sy,
        details,
        distinct_episode_count_by_site=merged_by_site,
    )
    if merged_by_site_year:
        all_years = sorted({
            str(y)
            for site in data.get("sites", [])
            for y in (site.get("episodes_by_year") or {}).keys()
        })
        for site in data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            if site_id in merged_by_site_year:
                site["episodes_by_year"] = {
                    year: int(merged_by_site_year[site_id].get(year, 0))
                    for year in all_years
                }
    year_summary = build_year_summary(data)
    if merged_total is not None:
        year_summary["all"]["episodes"] = int(merged_total)
    for _, row in public_year.iterrows():
        year = str(int(row["analysis_year"]))
        if year in year_summary:
            year_summary[year]["episodes"] = int(row.get("active_episode_count") or 0)
    type_frames = _collapse_acute_type_frames(output_dir, alias_map)
    if type_frames:
        sublayers_by_site = {}
        for type_id, frames in type_frames.items():
            type_counts, type_year_counts = restrict_episode_counts_to_parent_spans(
                parent_ledger, frames.get("merged_ledger")
            )
            type_data, type_unmapped = build_data_from_site_panels(
                existing_data,
                frames["smy"],
                frames["sym"],
                frames["sy"],
                frames["details"],
                distinct_episode_count_by_site=type_counts,
                distinct_episode_count_by_site_year=type_year_counts,
            )
            if type_unmapped:
                unmapped = sorted(set(unmapped) | set(type_unmapped))
            for site in type_data.get("sites", []):
                site_id = str(site.get("name", "")).lower().strip()
                layer = _layer_from_site(site)
                sublayers_by_site.setdefault(site_id, {})[type_id] = layer
        for site in data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            if site_id not in sublayers_by_site:
                continue
            layer = _layer_from_site(site)
            layer["sublayers"] = {
                ACUTE_TYPE_META["default"]: _layer_from_site(site),
                **sublayers_by_site[site_id],
            }
            for ym, detail in (layer.get("monthly_details") or {}).items():
                labels = []
                for type_id, sublayer in sublayers_by_site[site_id].items():
                    if float((sublayer.get("monthly") or {}).get(ym, 0) or 0) > 0:
                        labels.append(_acute_type_label(type_id))
                if labels:
                    detail["acute_types"] = labels
                    detail["acute_types_text"] = "; ".join(labels)
            site.update(layer)
    return data, year_summary, unmapped


def _derive_surgery_public_panels(existing_data, output_dir):
    output_dir = Path(output_dir)
    sym = pd.read_csv(output_dir / "surgery_source_verified_site_year_month_panel.csv")
    intervals = pd.read_csv(output_dir / "surgery_intervals_source_verified_active_all_years.csv")

    alias_map = _build_alias_to_canonical(existing_data)
    public = intervals.copy()
    public["public_service"] = "surgery_or"
    public["first_interval_in_window"] = public["interval_start_clipped"]
    public["last_interval_in_window"] = public["interval_end_clipped"]
    (
        merged_by_site,
        merged_by_site_year,
        merged_total,
        merged_episode_years,
        merged_annual,
        merged_ledger,
    ) = compute_merged_episode_counts(
        public,
        alias_map=alias_map,
        service_col="public_service",
        start_col="first_interval_in_window",
        end_col="last_interval_in_window",
    )

    if merged_ledger is None:
        merged_ledger = pd.DataFrame()
    if not merged_ledger.empty:
        merged_ledger.to_csv(output_dir / "surgery_public_merged_disruption_episodes.csv", index=False)

    subtype_month_rows = _month_rows_from_intervals(public, alias_map)
    if not subtype_month_rows.empty and "surgery_subtype_label" in subtype_month_rows.columns:
        subtype_labels_by_month = (
            subtype_month_rows.groupby(["site_id", "analysis_year", "year_month"], dropna=False)["surgery_subtype_label"]
            .apply(lambda values: " | ".join(sorted({str(v).strip() for v in values if pd.notna(v) and str(v).strip()})))
            .to_dict()
        )
    else:
        subtype_labels_by_month = {}

    months = sym.copy()
    if "site_best" in months.columns:
        months["_canonical"] = months["site_best"].map(lambda s: _canonicalize_site(s, alias_map))
    else:
        months["_canonical"] = months["site_id"].map(lambda s: _canonicalize_site(s, alias_map))
    site_month_hours = months.groupby(["_canonical", "analysis_year", "year_month"], as_index=False).agg(
        raw_interval_count=("raw_interval_count", "sum"),
        raw_interval_hours=("raw_interval_hours", "sum"),
        unioned_closure_hours=("unioned_closure_hours", "sum"),
    )
    site_month_hours["site_id"] = site_month_hours["_canonical"]
    site_month_hours["site_best"] = site_month_hours["_canonical"]
    site_month_hours["surgery_subtypes"] = site_month_hours.apply(
        lambda row: subtype_labels_by_month.get(
            (str(row["_canonical"]), int(row["analysis_year"]), str(row["year_month"])),
            "",
        ),
        axis=1,
    )
    site_month_hours = site_month_hours[
        [
            "site_id",
            "site_best",
            "analysis_year",
            "year_month",
            "raw_interval_count",
            "raw_interval_hours",
            "unioned_closure_hours",
            "surgery_subtypes",
        ]
    ]

    years = sorted(set(site_month_hours["analysis_year"].astype(int)) | {int(y) for y in merged_annual.keys()})
    sy_rows = []
    for site_id in sorted(set(site_month_hours["site_id"])):
        site_months = site_month_hours[site_month_hours["site_id"].eq(site_id)]
        for year in years:
            y_months = site_months[site_months["analysis_year"].astype(int).eq(int(year))]
            if y_months.empty and not merged_by_site_year.get(site_id, {}).get(str(year), 0):
                continue
            sy_rows.append(
                {
                    "site_id": site_id,
                    "site_best": site_id,
                    "analysis_year": int(year),
                    "raw_interval_count": int(y_months["raw_interval_count"].sum()) if not y_months.empty else 0,
                    "raw_interval_hours": float(y_months["raw_interval_hours"].sum()) if not y_months.empty else 0.0,
                    "unioned_closure_hours": float(y_months["unioned_closure_hours"].sum()) if not y_months.empty else 0.0,
                    "active_episode_count": int(merged_by_site_year.get(site_id, {}).get(str(year), 0)),
                }
            )
    sy = pd.DataFrame(sy_rows)

    smy_rows = []
    for site_id in sorted(set(site_month_hours["site_id"]) | set(merged_by_site)):
        site_y = sy[sy["site_id"].eq(site_id)] if not sy.empty else pd.DataFrame()
        smy_rows.append(
            {
                "site_id": site_id,
                "site_best": site_id,
                "years_active": int((site_y["unioned_closure_hours"] > 0).sum()) if not site_y.empty else 0,
                "active_episode_count": int(merged_by_site.get(site_id, 0)),
                "raw_interval_hours": float(site_y["raw_interval_hours"].sum()) if not site_y.empty else 0.0,
                "unioned_closure_hours": float(site_y["unioned_closure_hours"].sum()) if not site_y.empty else 0.0,
            }
        )
    smy = pd.DataFrame(smy_rows)

    details = public.copy()
    details["surgery_type"] = details["surgery_subtype"].map(_surgery_type_for_subtype)
    details["surgery_type_label"] = details["surgery_type"].map(_surgery_type_label)
    details = _month_rows_from_intervals(details, alias_map, type_col="surgery_type", type_label_fn=_surgery_type_label)
    detail_cols = [
        "site_id",
        "site_best",
        "year_month",
        "analysis_year",
        "surgery_type",
        "surgery_type_label",
        "surgery_subtype",
        "surgery_subtype_label",
        "bed_or_space_reduction_text",
        "reason_text",
        "start_date_text",
        "anticipated_end_date_text",
    ]
    details = details[[c for c in detail_cols if c in details.columns]].drop_duplicates()

    public_active_rows = []
    if not merged_ledger.empty:
        for _, row in merged_ledger.iterrows():
            start = pd.Timestamp(row["merged_start"])
            end = pd.Timestamp(row["merged_end"])
            for year in range(int(start.year), int(end.year) + 1):
                year_start = pd.Timestamp(f"{year}-01-01")
                year_end = pd.Timestamp(f"{year + 1}-01-01")
                clipped_start = max(start, year_start)
                clipped_end = min(end, year_end)
                if clipped_end <= clipped_start:
                    continue
                public_active_rows.append(
                    {
                        "site_id": row["site_id"],
                        "site_best": row["site_id"],
                        "program_or_service": "Surgery/OR",
                        "start_date_text": pd.Timestamp(row["merged_start"]).strftime("%Y-%m-%d %H:%M:%S"),
                        "anticipated_end_date_text": pd.Timestamp(row["merged_end"]).strftime("%Y-%m-%d %H:%M:%S"),
                        "first_seen_snapshot_date": "public_merged_interval",
                        "analysis_year": int(year),
                        "first_interval_in_window": clipped_start,
                        "last_interval_in_window": clipped_end,
                        "merged_episode_id": int(row["merged_episode_id"]),
                    }
                )
    public_active = pd.DataFrame(public_active_rows)
    public_year = pd.DataFrame(
        [
            {
                "analysis_year": int(year),
                "affected_sites": int(sy[sy["analysis_year"].eq(int(year)) & (sy["unioned_closure_hours"] > 0)]["site_id"].nunique()) if not sy.empty else 0,
                "active_episode_count": int(merged_annual.get(str(year), 0)),
                "unioned_closure_hours": float(sy[sy["analysis_year"].eq(int(year))]["unioned_closure_hours"].sum()) if not sy.empty else 0.0,
            }
            for year in years
        ]
    )

    smy.to_csv(output_dir / "surgery_public_site_multi_year_summary.csv", index=False)
    sy.to_csv(output_dir / "surgery_public_site_year_panel.csv", index=False)
    site_month_hours.to_csv(output_dir / "surgery_public_site_year_month_panel.csv", index=False)
    details.to_csv(output_dir / "surgery_public_site_month_service_details.csv", index=False)
    public_active.to_csv(output_dir / "surgery_public_episodes_active_all_years.csv", index=False)
    public_year.to_csv(output_dir / "surgery_public_year_summary.csv", index=False)
    return smy, site_month_hours, sy, details, merged_by_site, merged_by_site_year, merged_total, public_year, merged_ledger


def build_surgery_layer_from_output_dir(existing_data, output_dir):
    """Read surgery/OR pipeline outputs and return (data, year_summary, unmapped)."""
    output_dir = Path(output_dir)
    smy, sym, sy, details, merged_by_site, merged_by_site_year, merged_total, public_year, parent_ledger = _derive_surgery_public_panels(existing_data, output_dir)
    alias_map = _build_alias_to_canonical(existing_data)
    data, unmapped = build_data_from_site_panels(
        existing_data,
        smy,
        sym,
        sy,
        details,
        distinct_episode_count_by_site=merged_by_site,
    )
    if merged_by_site_year:
        all_years = sorted({
            str(y)
            for site in data.get("sites", [])
            for y in (site.get("episodes_by_year") or {}).keys()
        })
        for site in data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            if site_id in merged_by_site_year:
                site["episodes_by_year"] = {
                    year: int(merged_by_site_year[site_id].get(year, 0))
                    for year in all_years
                }
    year_summary = build_year_summary(data)
    if merged_total is not None:
        year_summary["all"]["episodes"] = int(merged_total)
    for _, row in public_year.iterrows():
        year = str(int(row["analysis_year"]))
        if year in year_summary:
            year_summary[year]["episodes"] = int(row.get("active_episode_count") or 0)
    type_frames = _collapse_surgery_type_frames(output_dir, alias_map)
    if type_frames:
        sublayers_by_site = {}
        for type_id, frames in type_frames.items():
            type_counts, type_year_counts = restrict_episode_counts_to_parent_spans(
                parent_ledger, frames.get("merged_ledger")
            )
            type_data, type_unmapped = build_data_from_site_panels(
                existing_data,
                frames["smy"],
                frames["sym"],
                frames["sy"],
                frames["details"],
                distinct_episode_count_by_site=type_counts,
                distinct_episode_count_by_site_year=type_year_counts,
            )
            if type_unmapped:
                unmapped = sorted(set(unmapped) | set(type_unmapped))
            for site in type_data.get("sites", []):
                site_id = str(site.get("name", "")).lower().strip()
                layer = _layer_from_site(site)
                sublayers_by_site.setdefault(site_id, {})[type_id] = layer
        for site in data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            if site_id not in sublayers_by_site:
                continue
            layer = _layer_from_site(site)
            layer["sublayers"] = {
                SURGERY_TYPE_META["default"]: _layer_from_site(site),
                **sublayers_by_site[site_id],
            }
            for ym, detail in (layer.get("monthly_details") or {}).items():
                labels = []
                for type_id, sublayer in sublayers_by_site[site_id].items():
                    if float((sublayer.get("monthly") or {}).get(ym, 0) or 0) > 0:
                        labels.append(_surgery_type_label(type_id))
                if labels:
                    detail["surgery_types"] = labels
                    detail["surgery_types_text"] = "; ".join(labels)
            site.update(layer)
    return data, year_summary, unmapped


def _other_type_label(type_id):
    return OTHER_TYPE_META["options"].get(type_id, {}).get("label", type_id)


def _other_active_rows_for_details(active, alias_map):
    if active is None or active.empty:
        return pd.DataFrame()
    details = active.copy()
    details["interval_start_clipped"] = details["first_interval_in_window"]
    details["interval_end_clipped"] = details["last_interval_in_window"]
    details["program_or_service"] = "Other Services"
    details["other_type"] = details["other_subtype"]
    details["other_type_label"] = details["other_subtype"].map(_other_type_label)
    return _month_rows_from_intervals(details, alias_map, type_col="other_type", type_label_fn=_other_type_label)


def _other_type_frames(output_dir: Path, alias_map, active):
    subtype_month_path = output_dir / "other_services_public_subtype_site_year_month_panel.csv"
    if not subtype_month_path.exists():
        return {}
    subtype_months = pd.read_csv(subtype_month_path).copy()
    if subtype_months.empty or "other_subtype" not in subtype_months.columns:
        return {}
    source_col = "site_best" if "site_best" in subtype_months.columns else "site_id"
    subtype_months["site_id"] = subtype_months[source_col].map(lambda s: _canonicalize_site(s, alias_map))
    subtype_months["site_best"] = subtype_months["site_id"]
    subtype_months["raw_interval_count"] = subtype_months.get("active_episode_count", 0)
    subtype_months["raw_interval_hours"] = subtype_months["unioned_closure_hours"]
    details = _other_active_rows_for_details(active, alias_map)
    active = active.copy() if active is not None and not active.empty else pd.DataFrame()
    if not active.empty:
        active["site_id"] = active["site_best"].map(lambda s: _canonicalize_site(s, alias_map))
        active["site_best"] = active["site_id"]
        active["first_interval_in_window"] = active["first_interval_in_window"]
        active["last_interval_in_window"] = active["last_interval_in_window"]

    out = {}
    for type_id in OTHER_TYPE_META["order"]:
        if type_id == OTHER_TYPE_META["default"]:
            continue
        months = subtype_months[subtype_months["other_subtype"].eq(type_id)].copy()
        type_active = active[active["other_subtype"].eq(type_id)].copy() if not active.empty else pd.DataFrame()
        if months.empty and type_active.empty:
            continue
        if not type_active.empty:
            (
                merged_by_site,
                merged_by_site_year,
                merged_total,
                _merged_episode_years,
                _merged_annual,
                _merged_ledger,
            ) = compute_merged_episode_counts(
                type_active,
                alias_map=alias_map,
                service_col="other_subtype",
                start_col="first_interval_in_window",
                end_col="last_interval_in_window",
            )
        else:
            merged_by_site, merged_by_site_year, merged_total, _merged_ledger = {}, {}, 0, pd.DataFrame()
        sym = months[
            [
                "site_id",
                "site_best",
                "analysis_year",
                "year_month",
                "raw_interval_count",
                "raw_interval_hours",
                "unioned_closure_hours",
            ]
        ].copy() if not months.empty else pd.DataFrame(columns=["site_id", "site_best", "analysis_year", "year_month", "raw_interval_count", "raw_interval_hours", "unioned_closure_hours"])
        years = sorted(
            set(int(y) for y in sym["analysis_year"].dropna().astype(int).tolist())
            | {int(y) for d in merged_by_site_year.values() for y in d.keys()}
        )
        sy_rows = []
        for site_id in sorted(set(sym["site_id"]) | set(merged_by_site_year)):
            site_months = sym[sym["site_id"].eq(site_id)] if not sym.empty else pd.DataFrame()
            for year in years:
                y_months = site_months[site_months["analysis_year"].astype(int).eq(int(year))] if not site_months.empty else pd.DataFrame()
                if y_months.empty and not merged_by_site_year.get(site_id, {}).get(str(year), 0):
                    continue
                sy_rows.append(
                    {
                        "site_id": site_id,
                        "site_best": site_id,
                        "analysis_year": int(year),
                        "raw_interval_count": int(y_months["raw_interval_count"].sum()) if not y_months.empty else 0,
                        "raw_interval_hours": float(y_months["raw_interval_hours"].sum()) if not y_months.empty else 0.0,
                        "unioned_closure_hours": float(y_months["unioned_closure_hours"].sum()) if not y_months.empty else 0.0,
                        "active_episode_count": int(merged_by_site_year.get(site_id, {}).get(str(year), 0)),
                    }
                )
        sy = pd.DataFrame(sy_rows)
        smy_rows = []
        for site_id in sorted(set(sym["site_id"]) | set(merged_by_site)):
            site_y = sy[sy["site_id"].eq(site_id)] if not sy.empty else pd.DataFrame()
            smy_rows.append(
                {
                    "site_id": site_id,
                    "site_best": site_id,
                    "years_active": int((site_y["unioned_closure_hours"] > 0).sum()) if not site_y.empty else 0,
                    "active_episode_count": int(merged_by_site.get(site_id, 0)),
                    "raw_interval_hours": float(site_y["raw_interval_hours"].sum()) if not site_y.empty else 0.0,
                    "unioned_closure_hours": float(site_y["unioned_closure_hours"].sum()) if not site_y.empty else 0.0,
                }
            )
        type_details = details[details["other_type"].eq(type_id)].copy() if not details.empty else pd.DataFrame()
        out[type_id] = {
            "smy": pd.DataFrame(smy_rows),
            "sym": sym,
            "sy": sy,
            "details": type_details,
            "merged_by_site": merged_by_site,
            "merged_by_site_year": merged_by_site_year,
            "merged_total": merged_total,
            "merged_ledger": _merged_ledger,
        }
    return out


def _derive_other_public_panels(existing_data, output_dir):
    output_dir = Path(output_dir)
    smy = pd.read_csv(output_dir / "other_services_public_site_multi_year_summary.csv")
    sym = pd.read_csv(output_dir / "other_services_public_site_year_month_panel.csv")
    sy = pd.read_csv(output_dir / "other_services_public_site_year_panel.csv")
    public_year = pd.read_csv(output_dir / "other_services_public_year_summary.csv")
    active = pd.read_csv(output_dir / "other_services_public_episodes_active_all_years.csv")
    merged = pd.read_csv(output_dir / "other_services_public_merged_disruption_episodes.csv")

    alias_map = _build_alias_to_canonical(existing_data)
    for df in (smy, sym, sy, public_year, active, merged):
        if df is None or df.empty:
            continue
        if "site_best" in df.columns:
            df["site_id"] = df["site_best"].map(lambda s: _canonicalize_site(s, alias_map))
            df["site_best"] = df["site_id"]
        elif "site_id" in df.columns:
            df["site_id"] = df["site_id"].map(lambda s: _canonicalize_site(s, alias_map))
            df["site_best"] = df["site_id"]

    for df in (smy, sym, sy):
        if "raw_interval_count" not in df.columns:
            df["raw_interval_count"] = df.get("active_episode_count", 0)
        if "raw_interval_hours" not in df.columns:
            df["raw_interval_hours"] = df["unioned_closure_hours"]

    details = _other_active_rows_for_details(active, alias_map)
    detail_cols = [
        "site_id",
        "site_best",
        "year_month",
        "analysis_year",
        "other_type",
        "other_type_label",
        "other_subtype",
        "other_subtype_label",
        "program_or_service",
        "merged_start",
        "merged_end",
    ]
    details = details[[c for c in detail_cols if c in details.columns]].drop_duplicates() if not details.empty else pd.DataFrame(columns=detail_cols)
    return smy, sym, sy, details, active, merged, public_year


def build_other_layer_from_output_dir(existing_data, output_dir):
    """Read Other Services public outputs and return (data, year_summary, unmapped)."""
    output_dir = Path(output_dir)
    smy, sym, sy, details, active, merged, public_year = _derive_other_public_panels(existing_data, output_dir)
    alias_map = _build_alias_to_canonical(existing_data)
    merged_by_site = {}
    merged_by_site_year = {}
    if not merged.empty:
        for site_id, group in merged.groupby("site_id", dropna=False):
            merged_by_site[str(site_id)] = int(group["merged_episode_id"].nunique())
        for (site_id, year), group in active.groupby(["site_id", "analysis_year"], dropna=False):
            merged_by_site_year.setdefault(str(site_id), {})[str(int(year))] = int(group["merged_episode_id"].nunique())
    data, unmapped = build_data_from_site_panels(
        existing_data,
        smy,
        sym,
        sy,
        details,
        distinct_episode_count_by_site=merged_by_site,
        distinct_episode_count_by_site_year=merged_by_site_year,
    )
    year_summary = build_year_summary(data)
    year_summary["all"]["episodes"] = int(len(merged))
    for _, row in public_year.iterrows():
        year = str(int(row["analysis_year"]))
        if year in year_summary:
            year_summary[year]["episodes"] = int(row.get("active_episode_count") or 0)
    type_frames = _other_type_frames(output_dir, alias_map, active)
    if type_frames:
        sublayers_by_site = {}
        for type_id, frames in type_frames.items():
            type_counts, type_year_counts = restrict_episode_counts_to_parent_spans(
                merged, frames.get("merged_ledger")
            )
            type_data, type_unmapped = build_data_from_site_panels(
                existing_data,
                frames["smy"],
                frames["sym"],
                frames["sy"],
                frames["details"],
                distinct_episode_count_by_site=type_counts,
                distinct_episode_count_by_site_year=type_year_counts,
            )
            if type_unmapped:
                unmapped = sorted(set(unmapped) | set(type_unmapped))
            for site in type_data.get("sites", []):
                site_id = str(site.get("name", "")).lower().strip()
                sublayers_by_site.setdefault(site_id, {})[type_id] = _layer_from_site(site)
        for site in data.get("sites", []):
            site_id = str(site.get("name", "")).lower().strip()
            if site_id not in sublayers_by_site:
                continue
            layer = _layer_from_site(site)
            layer["sublayers"] = {
                OTHER_TYPE_META["default"]: _layer_from_site(site),
                **sublayers_by_site[site_id],
            }
            for ym, detail in (layer.get("monthly_details") or {}).items():
                labels = []
                for type_id, sublayer in sublayers_by_site[site_id].items():
                    if float((sublayer.get("monthly") or {}).get(ym, 0) or 0) > 0:
                        labels.append(_other_type_label(type_id))
                if labels:
                    detail["other_types"] = labels
                    detail["other_types_text"] = "; ".join(labels)
            site.update(layer)
    return data, year_summary, unmapped


def _empty_layer(years):
    return {
        "total_hours": 0,
        "years_active": 0,
        "episode_count": 0,
        "monthly": {},
        "monthly_details": {},
        "has_disruption_data": False,
        "episodes_by_year": {str(y): 0 for y in years},
    }


def _combine_site_layers(layers_by_id, years, service_ids):
    monthly = {}
    monthly_details = {}
    episodes_by_year = {str(y): 0 for y in years}
    episode_count = 0
    for service_id in service_ids:
        layer = layers_by_id.get(service_id) or _empty_layer(years)
        episode_count += int(layer.get("episode_count") or 0)
        for y, n in (layer.get("episodes_by_year") or {}).items():
            episodes_by_year[str(y)] = episodes_by_year.get(str(y), 0) + int(n or 0)
        for ym, h in (layer.get("monthly") or {}).items():
            h = float(h or 0)
            if h <= 0:
                continue
            monthly[ym] = monthly.get(ym, 0.0) + h
            detail = (layer.get("monthly_details") or {}).get(ym) or {}
            services = detail.get("services") or []
            if not services:
                services = [SERVICE_LAYER_META_DEFAULT.get(service_id, {}).get("label", service_id)]
            md = monthly_details.setdefault(ym, {"services": [], "services_by_layer": {}})
            for s in services:
                if s not in md["services"]:
                    md["services"].append(s)
            md["services_by_layer"][service_id] = services
    total = _round_hours(sum(monthly.values()))
    return {
        "total_hours": total,
        "years_active": len({ym[:4] for ym, h in monthly.items() if float(h) > 0}),
        "episode_count": episode_count,
        "monthly": {ym: _round_hours(h) for ym, h in sorted(monthly.items())},
        "monthly_details": monthly_details,
        "has_disruption_data": bool(total > 0 or episode_count > 0),
        "episodes_by_year": {str(y): int(episodes_by_year.get(str(y), 0)) for y in years},
    }


def reconcile_all_service_summary(layered_data, service_year_summary):
    """Use supplied layer headline counts while keeping all-layer affected-site counts."""
    out = dict(service_year_summary)
    combined = build_year_summary(layered_data)
    reference_sites = len(layered_data.get("sites", []))
    for summary in out.values():
        if isinstance(summary, dict):
            for block in summary.values():
                if isinstance(block, dict):
                    block["reference_sites"] = reference_sites
    service_ids = [sid for sid in out.keys() if sid != "all"]
    for period, block in combined.items():
        hours = 0.0
        episodes = 0
        for sid in service_ids:
            layer_block = (out.get(sid) or {}).get(period) or {}
            hours += float(layer_block.get("hours") or 0)
            episodes += int(layer_block.get("episodes") or 0)
        block["hours"] = _round_hours(hours)
        block["episodes"] = episodes
    out["all"] = combined
    return out


def build_layered_data(existing_data, layer_data_by_id, service_meta=None, year_summary_by_id=None):
    """
    Return DATA with per-site `layers` plus top-level all-service totals, and
    SERVICE_YEAR_SUMMARY for all/each service.
    """
    service_meta = service_meta or SERVICE_LAYER_META_DEFAULT
    service_ids = [sid for sid in layer_data_by_id.keys() if sid != "all"]
    alias_map = _build_alias_to_canonical(existing_data)

    years = set()
    meta_lookup = {}
    layer_lookup = {}
    for layer_id, data in layer_data_by_id.items():
        if layer_id == "all":
            continue
        for site in data.get("sites", []):
            site_id = _preferred_canonical_site(site.get("name", ""), alias_map)
            if not site_id:
                continue
            meta_lookup.setdefault(site_id, _site_metadata(site))
            layer_lookup.setdefault(site_id, {})
            layer = _layer_from_site(site)
            if layer_id in layer_lookup[site_id]:
                layer_lookup[site_id][layer_id] = _merge_layer_blocks(layer_lookup[site_id][layer_id], layer)
            else:
                layer_lookup[site_id][layer_id] = layer
            years.update(int(y) for y in (layer.get("episodes_by_year") or {}).keys() if str(y).isdigit())
            years.update(int(ym[:4]) for ym in (layer.get("monthly") or {}).keys())

    for site in existing_data.get("sites", []):
        site_id = str(site.get("name", "")).lower().strip()
        canonical = _preferred_canonical_site(site_id, alias_map)
        if canonical != site_id:
            existing_meta = _site_metadata(site)
            meta_lookup.setdefault(canonical, existing_meta)
            meta_lookup[canonical]["name"] = canonical
            meta_lookup[canonical].setdefault("aliases", [])
            if not _is_ahs_zero_label(site_id) and site_id not in meta_lookup[canonical]["aliases"]:
                meta_lookup[canonical]["aliases"].append(site_id)
            for alias in existing_meta.get("aliases", []) or []:
                if _is_ahs_zero_label(alias):
                    continue
                if alias not in meta_lookup[canonical]["aliases"]:
                    meta_lookup[canonical]["aliases"].append(alias)
            continue
        meta_lookup.setdefault(site_id, _site_metadata(site))
    for site_id, meta in OB_EXTRA_SITE_METADATA.items():
        if _is_existing_alias(site_id, alias_map):
            continue
        meta_lookup.setdefault(site_id, dict(meta))

    years = sorted(years)
    sites = []
    for site_id in sorted(meta_lookup):
        site = dict(meta_lookup[site_id])
        site["name"] = site_id
        layers = {}
        for service_id in service_ids:
            layers[service_id] = _layer_from_site(
                {"name": site_id, **(layer_lookup.get(site_id, {}).get(service_id) or _empty_layer(years))},
                all_years=years,
            )
        layers["all"] = _combine_site_layers(layers, years, service_ids)
        site["layers"] = layers
        site.update(layers["all"])
        sites.append(site)

    data = {"sites": sites}
    service_year_summary = {"all": build_year_summary(data)}
    for service_id in service_ids:
        service_year_summary[service_id] = build_year_summary(extract_service_layer_data(data, service_id))
    if year_summary_by_id:
        for service_id, summary in year_summary_by_id.items():
            if summary:
                service_year_summary[service_id] = summary
        service_year_summary = reconcile_all_service_summary(data, service_year_summary)
    return data, service_year_summary


def compute_data_range_info(data):
    """
    Returns dict describing the data coverage range:
      earliest_ym, latest_ym, start_year, end_year,
      earliest_month_name_year (e.g. "August 2021"),
      latest_month_name_year (e.g. "May 2026"),
      start_year_str (e.g. "August–December 2021"),
      end_year_str (e.g. "January–May 2026"),
      latest_month_num (1-12)
    Returns empty dict if data has no months.
    """
    months_by_year = {}
    for site in data.get("sites", []):
        if site.get("has_disruption_data") is False:
            continue
        for ym, hours in (site.get("monthly") or {}).items():
            try:
                if float(hours) > 0:
                    y, m = ym.split("-")
                    months_by_year.setdefault(int(y), set()).add(int(m))
            except Exception:
                continue
    if not months_by_year:
        return {}
    month_names = ["January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]
    def fmt_partial(year, months):
        ms = sorted(months)
        first = month_names[ms[0] - 1]
        last = month_names[ms[-1] - 1]
        return f"{first} {year}" if first == last else f"{first}\u2013{last} {year}"
    years = sorted(months_by_year.keys())
    start_year, end_year = years[0], years[-1]
    start_months = months_by_year[start_year]
    end_months = months_by_year[end_year]
    earliest_month = min(start_months)
    latest_month = max(end_months)
    return {
        "start_year": start_year,
        "end_year": end_year,
        "earliest_ym": f"{start_year}-{earliest_month:02d}",
        "latest_ym": f"{end_year}-{latest_month:02d}",
        "earliest_month_name_year": f"{month_names[earliest_month - 1]} {start_year}",
        "latest_month_name_year": f"{month_names[latest_month - 1]} {end_year}",
        "start_year_str": fmt_partial(start_year, start_months),
        "end_year_str": fmt_partial(end_year, end_months),
        "latest_month_num": latest_month,
    }


SUMMARY_STATS_PATTERN = re.compile(
    r'(<div class="total-stat" id="summaryStats"[^>]*>).*?(</div>)',
    re.DOTALL,
)

LEGACY_FOOTER_DATA_SCOPE_PATTERN = re.compile(
    r"Data updated quarterly\s*&middot;\s*"
    r"Disruption hours reconstructed from public AHS archives\s*&middot;\s*"
    r"Affected-site count reflects sites with captured posted disruption hours; "
    r"reference layer includes \d+ searchable AHS locations\s*&middot;\s*"
)

RAW_ARCHIVE_SCOPE_PATTERN = re.compile(
    r"(?:Displayed service layers reflect completed parser-specific validation; "
    r"additional archive service categories remain out of scope until separately analyzed|"
    r"Additional service layers will be added as analyzed; raw archive records include "
    r"acute care/inpatient, obstetrics, surgery/operating rooms, endoscopy, "
    r"ambulatory/urgent care, orthopedics, long-term care, home care, "
    r"inpatient rehabilitation, detox, ophthalmology, and cardiology|"
    r"Included service layers: [^&<]+)"
)

YEAR_OPTIONS_PATTERN = re.compile(
    r'(<select id="yearSelect"[^>]*>\s*)'
    r'(<option value="all">[^<]+</option>\s*)'
    r'((?:<option value="\d{4}">\d{4}</option>\s*)+)'
    r'(</select>)',
    re.DOTALL,
)

COPYRIGHT_PATTERN = re.compile(r"(SORC,\s*)\d{4}")


def _access_haversine_km(lat1, lng1, lat2, lng2):
    radius = 6371.0088
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lng2) - float(lng1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _site_is_ed_reference(site):
    note = str(site.get("metadata_note") or "")
    return (
        "not present in the ED reference layer" not in note
        and site.get("lat") is not None
        and site.get("lng") is not None
    )


def _site_has_access_coords(site):
    return site.get("lat") is not None and site.get("lng") is not None


def _site_is_access_candidate(site, layer_id):
    if not _site_has_access_coords(site):
        return False
    capability = (site.get("service_capability") or {}).get(layer_id) or {}
    if "nearby_candidate" in capability:
        return bool(capability.get("nearby_candidate"))
    if layer_id == "ed":
        return _site_is_ed_reference(site)
    if layer_id == "all":
        return True
    layer = (site.get("layers") or {}).get(layer_id) or {}
    return bool(layer.get("has_disruption_data")) or float(layer.get("total_hours") or 0) > 0


def _norm_access_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _truthy_csv_value(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _load_service_directory_capability_csv(path):
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    required = {"site_name", "layer_id", "nearby_candidate"}
    if not required.issubset(df.columns):
        raise ValueError(f"Service directory CSV is missing required columns: {sorted(required - set(df.columns))}")
    lookup = {}
    for _, row in df.iterrows():
        site_key = _norm_access_key(row.get("site_name"))
        layer_id = _norm_access_key(row.get("layer_id"))
        if not site_key or not layer_id:
            continue
        capability = {
            "status": str(row.get("capability_status") or row.get("status") or "").strip() or (
                "confirmed_by_ahs_facility_directory" if _truthy_csv_value(row.get("nearby_candidate")) else "not_listed_in_ahs_facility_directory"
            ),
            "nearby_candidate": _truthy_csv_value(row.get("nearby_candidate")),
            "source": str(row.get("source") or "ahs_facility_directory").strip(),
        }
        for key in ("facility_url", "facility_id", "ahs_facility_name", "matched_services", "retrieved_at"):
            if key in df.columns and pd.notna(row.get(key)):
                capability[key] = str(row.get(key)).strip()
        lookup[(site_key, layer_id)] = capability
    return lookup


def _site_directory_capability(site, layer_id, directory_lookup):
    if not directory_lookup:
        return None
    keys = [
        _norm_access_key(site.get("name")),
        _norm_access_key(site.get("facility_name")),
        _norm_access_key(site.get("display_name")),
    ]
    for key in keys:
        if not key:
            continue
        found = directory_lookup.get((key, _norm_access_key(layer_id)))
        if found is not None:
            return dict(found)
    return None


def _site_service_capability(site, layer_id, directory_lookup=None):
    directory_capability = _site_directory_capability(site, layer_id, directory_lookup)
    if directory_capability is not None:
        return directory_capability
    if not _site_has_access_coords(site):
        return {
            "status": "missing_coordinates",
            "nearby_candidate": False,
            "source": "html_site_coordinates",
        }
    if layer_id == "all":
        return {
            "status": "searchable_analyzed_site",
            "nearby_candidate": True,
            "source": "html_searchable_site_layer",
        }
    if layer_id == "ed":
        is_reference = _site_is_ed_reference(site)
        return {
            "status": "confirmed_ed_reference" if is_reference else "not_in_ed_reference_layer",
            "nearby_candidate": is_reference,
            "source": "ed_reference_layer",
        }
    layer = (site.get("layers") or {}).get(layer_id) or {}
    has_evidence = bool(layer.get("has_disruption_data")) or float(layer.get("total_hours") or 0) > 0
    return {
        "status": "confirmed_by_disruption_evidence" if has_evidence else "needs_external_capability_confirmation",
        "nearby_candidate": has_evidence,
        "source": "validated_service_layer_disruption_evidence" if has_evidence else "no_positive_disruption_evidence_in_current_tool",
    }


def attach_service_capability_master(new_data, service_layers=None, service_directory_csv=None):
    sites = new_data.get("sites") or []
    layer_ids = _access_layer_ids(sites, service_layers)
    directory_lookup = _load_service_directory_capability_csv(service_directory_csv)
    for site in sites:
        site["service_capability"] = {
            layer_id: _site_service_capability(site, layer_id, directory_lookup)
            for layer_id in layer_ids
        }
    return new_data


def _load_access_road_matrix_csv(path):
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    required = {"origin_site_name", "destination_site_name"}
    if not required.issubset(df.columns):
        raise ValueError(f"Road matrix is missing required columns: {sorted(required - set(df.columns))}")
    matrix = {}
    for _, row in df.iterrows():
        origin = str(row.get("origin_site_name") or "").strip()
        dest = str(row.get("destination_site_name") or "").strip()
        if not origin or not dest:
            continue
        road_km = pd.to_numeric(row.get("road_km"), errors="coerce")
        road_minutes = pd.to_numeric(row.get("road_minutes"), errors="coerce")
        matrix[(origin, dest)] = {
            "road_km": None if pd.isna(road_km) else round(float(road_km), 1),
            "road_minutes": None if pd.isna(road_minutes) else round(float(road_minutes), 1),
        }
    return matrix


def _write_access_road_matrix_csv(path, rows):
    if not path or not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(p, index=False)


def _fetch_access_road_matrix_from_osrm(candidates, osrm_url):
    coords = ";".join(f"{site['lng']},{site['lat']}" for site in candidates)
    query = urllib.parse.urlencode({"annotations": "duration,distance", "skip_waypoints": "true"})
    url = osrm_url.rstrip("/") + "/table/v1/driving/" + coords + "?" + query
    with urllib.request.urlopen(url, timeout=300) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != "Ok":
        message = payload.get("message") or ""
        hint = ""
        if payload.get("code") in {"TooBig", "TooBigTable"} or "too big" in message.lower():
            hint = " Start osrm-routed with --max-table-size 15000 for the current nearest-site matrix."
        raise RuntimeError(f"OSRM table request failed: {payload.get('code')} {message}.{hint}")
    distances = payload.get("distances") or []
    durations = payload.get("durations") or []
    if len(distances) != len(candidates) or len(durations) != len(candidates):
        raise RuntimeError("OSRM table dimensions did not match candidate count")
    matrix = {}
    rows = []
    for i, origin in enumerate(candidates):
        for j, dest in enumerate(candidates):
            if i == j:
                continue
            road_m = distances[i][j]
            duration_s = durations[i][j]
            road_km = None if road_m is None else round(float(road_m) / 1000.0, 1)
            road_minutes = None if duration_s is None else round(float(duration_s) / 60.0, 1)
            linear_km = round(_access_haversine_km(origin.get("lat"), origin.get("lng"), dest.get("lat"), dest.get("lng")), 1)
            matrix[(origin.get("name"), dest.get("name"))] = {
                "road_km": road_km,
                "road_minutes": road_minutes,
            }
            rows.append({
                "origin_site_name": origin.get("name"),
                "origin_display_name": origin.get("facility_name") or origin.get("display_name") or origin.get("name"),
                "destination_site_name": dest.get("name"),
                "destination_display_name": dest.get("facility_name") or dest.get("display_name") or dest.get("name"),
                "linear_km": linear_km,
                "road_km": road_km,
                "road_minutes": road_minutes,
            })
    return matrix, rows


def _access_layer_ids(sites, configured=None):
    if configured:
        return [str(layer).strip() for layer in configured if str(layer).strip()]
    preferred = ["ed", "ob", "acute", "surgery", "other", "all"]
    present = set()
    for site in sites:
        present.update((site.get("layers") or {}).keys())
    ordered = [layer for layer in preferred if layer in present or layer == "all"]
    ordered.extend(sorted(layer for layer in present if layer not in set(ordered)))
    return ordered


def attach_nearest_access(
    new_data,
    road_matrix_csv=None,
    osrm_url=None,
    road_matrix_output_csv=None,
    rank_by="road",
    service_layers=None,
    service_directory_csv=None,
):
    """Attach nearest-three service-aware healthcare sites with linear and optional road distance."""
    # Road distance is the public ranking contract; linear distance is displayed
    # as a secondary geographic measure only.
    rank_by = "road"
    sites = new_data.get("sites") or []
    sites_with_coords = [site for site in sites if _site_has_access_coords(site)]
    sites_with_coords.sort(key=lambda site: str(site.get("name") or ""))
    road_matrix = _load_access_road_matrix_csv(road_matrix_csv)
    required_route_pairs = {
        (origin.get("name"), destination.get("name"))
        for origin in sites_with_coords
        for destination in sites_with_coords
        if origin is not destination
    }
    missing_route_pairs = [
        pair for pair in required_route_pairs
        if (road_matrix.get(pair) or {}).get("road_km") is None
        or (road_matrix.get(pair) or {}).get("road_minutes") is None
    ]
    if missing_route_pairs and osrm_url:
        road_matrix, matrix_rows = _fetch_access_road_matrix_from_osrm(sites_with_coords, osrm_url)
        _write_access_road_matrix_csv(road_matrix_output_csv, matrix_rows)
    elif missing_route_pairs:
        examples = ", ".join(f"{origin} -> {destination}" for origin, destination in missing_route_pairs[:5])
        raise RuntimeError(
            "Nearest-site road matrix is incomplete for the current mapped site set "
            f"({len(missing_route_pairs)} missing route pair(s), e.g. {examples}). "
            "Refresh nearest_access_road_distance_matrix.csv with the local OSRM router before running the updater."
        )
    layer_ids = _access_layer_ids(sites, service_layers)
    attach_service_capability_master(new_data, layer_ids, service_directory_csv=service_directory_csv)
    candidates_by_layer = {
        layer_id: [site for site in sites_with_coords if _site_is_access_candidate(site, layer_id)]
        for layer_id in layer_ids
    }
    for site in sites:
        if not _site_has_access_coords(site):
            site.pop("nearest_access", None)
            site.pop("ed_nearest_sites", None)
            continue
        access = {}
        for layer_id, candidates in candidates_by_layer.items():
            nearest = []
            for other in candidates:
                if other is site:
                    continue
                try:
                    linear_km = _access_haversine_km(site.get("lat"), site.get("lng"), other.get("lat"), other.get("lng"))
                except Exception:
                    continue
                nearest.append({
                    "site_name": other.get("name"),
                    "display_name": other.get("facility_name") or other.get("display_name") or other.get("name"),
                    "linear_km": round(float(linear_km), 1),
                    "road_km": (road_matrix.get((site.get("name"), other.get("name"))) or {}).get("road_km"),
                    "road_minutes": (road_matrix.get((site.get("name"), other.get("name"))) or {}).get("road_minutes"),
                })
            nearest.sort(key=lambda row: (
                row.get("road_km") is None,
                row.get("road_km") if row.get("road_km") is not None else 1e9,
                row["linear_km"],
            ))
            if nearest:
                access[layer_id] = nearest[:3]
        if access:
            site["nearest_access"] = access
        else:
            site.pop("nearest_access", None)
        if access.get("ed"):
            site["ed_nearest_sites"] = access["ed"]
        else:
            site.pop("ed_nearest_sites", None)
    for site in sites:
        for rows in (site.get("nearest_access") or {}).values():
            road_values = [row.get("road_km") for row in rows]
            if any(value is None for value in road_values) or road_values != sorted(road_values):
                raise RuntimeError("Nearest-site access must contain road-ranked, road-routable candidates only.")
    return new_data


def attach_ed_nearest_access(*args, **kwargs):
    """Backward-compatible wrapper for the generalized nearest-site access attachment."""
    return attach_nearest_access(*args, **kwargs)


def update_static_summary(text, year_summary, hour_label="disruption hours"):
    """Update the top-of-page hardcoded summary stats to match YEAR_SUMMARY['all'].""" 
    all_block = year_summary.get("all") if year_summary else None
    if not all_block:
        return text
    hours = round(float(all_block.get("hours", 0)))
    affected = int(all_block.get("affected_sites", 0))
    new_inner = (
        f"<strong>{hours:,}</strong>{hour_label}"
        f"<strong>{affected:,}</strong>sites affected"
    )

    def replace(m):
        return m.group(1) + "\n      " + new_inner + "\n    " + m.group(2)

    return SUMMARY_STATS_PATTERN.sub(replace, text, count=1)


def update_reference_count(text, total_searchable):
    """Remove legacy footer source/cadence/reference-site prose."""
    return LEGACY_FOOTER_DATA_SCOPE_PATTERN.sub("", text)


def update_archive_scope_note(text, service_meta=None):
    """Keep footer scope language aligned with validated displayed layers."""
    labels = []
    if service_meta:
        for layer_id, meta in sorted(service_meta.items(), key=lambda kv: kv[1].get("sort_order", 999)):
            if layer_id == "all":
                continue
            labels.append(meta.get("label") or layer_id)
    if not labels:
        labels = ["Emergency Department", "Obstetrics"]
    if len(labels) == 1:
        label_text = labels[0]
    elif len(labels) == 2:
        label_text = f"{labels[0]} and {labels[1]}"
    else:
        label_text = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return RAW_ARCHIVE_SCOPE_PATTERN.sub(
        f"Included service layers: {label_text} ",
        text,
    )


def update_year_picker_options(text, years):
    """Rebuild the <option> tags inside <select id='yearSelect'> for the given years."""
    if not years:
        return text
    sorted_years = sorted(set(int(y) for y in years))
    options_html = "\n          ".join(
        f'<option value="{y}">{y}</option>' for y in sorted_years
    )

    def replace(m):
        open_tag = m.group(1)
        all_option = m.group(2).rstrip()
        close_tag = m.group(4)
        return f"{open_tag}{all_option}\n          {options_html}\n        {close_tag}"

    return YEAR_OPTIONS_PATTERN.sub(replace, text, count=1)


def update_project_months_loop(text, old_info, new_info):
    """
    Update both the start (_cy, _cm) and the end (while condition) of the
    PROJECT_MONTHS JS loop.
    """
    if not old_info or not new_info:
        return text
    # Start: "let _cy = 2021, _cm = 8;"
    old_start_year = old_info.get("start_year")
    old_start_month_num = None
    # Extract start month from earliest_ym
    if old_info.get("earliest_ym"):
        try:
            old_start_month_num = int(old_info["earliest_ym"].split("-")[1])
        except Exception:
            pass
    new_start_year = new_info.get("start_year")
    new_start_month_num = None
    if new_info.get("earliest_ym"):
        try:
            new_start_month_num = int(new_info["earliest_ym"].split("-")[1])
        except Exception:
            pass
    if (old_start_year and new_start_year and
            old_start_month_num and new_start_month_num and
            (old_start_year != new_start_year or old_start_month_num != new_start_month_num)):
        start_pat = re.compile(rf"let _cy = {old_start_year}, _cm = {old_start_month_num};")
        text = start_pat.sub(f"let _cy = {new_start_year}, _cm = {new_start_month_num};", text)

    # End: "while (_cy < 2026 || (_cy === 2026 && _cm <= 3))"
    old_end_year = old_info.get("end_year")
    old_end_month = old_info.get("latest_month_num")
    new_end_year = new_info.get("end_year")
    new_end_month = new_info.get("latest_month_num")
    if old_end_year and new_end_year and (old_end_year != new_end_year or old_end_month != new_end_month):
        loop_pattern = re.compile(
            rf"while\s*\(\s*_cy\s*<\s*{old_end_year}\s*\|\|\s*\(\s*_cy\s*===\s*{old_end_year}\s*&&\s*_cm\s*<=\s*{old_end_month}\s*\)\s*\)"
        )
        new_loop = f"while (_cy < {new_end_year} || (_cy === {new_end_year} && _cm <= {new_end_month}))"
        text = loop_pattern.sub(new_loop, text)

    return text


def update_date_strings(text, old_info, new_info):
    """
    Replace old date references in the HTML with new ones.

    Targets four kinds of strings:
      1. "[Month Year] to [Month Year]"  (subtitle, chart captions)
      2. "Partial-year coverage: X and Y" parts (footer)
      3. The PROJECT_MONTHS JS while loop start AND end conditions
    """
    if not old_info or not new_info:
        return text

    # 1. Subtitle and chart captions
    old_earliest = old_info.get("earliest_month_name_year")
    new_earliest = new_info.get("earliest_month_name_year")
    old_latest = old_info.get("latest_month_name_year")
    new_latest = new_info.get("latest_month_name_year")
    if old_latest and new_latest and old_latest != new_latest:
        text = text.replace(old_latest, new_latest)
    if old_earliest and new_earliest and old_earliest != new_earliest:
        text = text.replace(old_earliest, new_earliest)

    # 2. Footer partial-year coverage strings
    for old_part, new_part in [
        (old_info.get("end_year_str"), new_info.get("end_year_str")),
        (old_info.get("start_year_str"), new_info.get("start_year_str")),
    ]:
        if old_part and new_part and old_part != new_part:
            text = text.replace(old_part, new_part)
            text = text.replace(old_part.replace("\u2013", "-"), new_part)

    # 3. PROJECT_MONTHS JS loop
    text = update_project_months_loop(text, old_info, new_info)

    return text


def _upsert_js_json_block(text, prefix, value, insert_after_prefix=YEAR_SUMMARY_PREFIX):
    block = prefix + json.dumps(value, separators=(",", ":")) + ";"
    found = _find_json_block(text, prefix)
    if found:
        start, end, _ = found
        return text[:start] + block + text[end:]
    found_after = _find_json_block(text, insert_after_prefix)
    if found_after:
        _, end, _ = found_after
        return text[:end] + "\n" + block + text[end:]
    return block + "\n" + text


def _find_function_blocks(text, function_name):
    pattern = re.compile(rf"\bfunction\s+{re.escape(function_name)}\s*\(")
    blocks = []
    search_pos = 0
    while True:
        match = pattern.search(text, search_pos)
        if not match:
            break
        start = match.start()
        brace = text.find("{", match.end())
        if brace < 0:
            break
        depth = 0
        in_string = False
        quote = None
        escape = False
        i = brace
        while i < len(text):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
            else:
                if ch in ("'", '"', "`"):
                    in_string = True
                    quote = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blocks.append((start, i + 1))
                        search_pos = i + 1
                        break
            i += 1
        else:
            break
    return blocks


def _find_function_block(text, function_name):
    blocks = _find_function_blocks(text, function_name)
    if not blocks:
        return None
    return blocks[0]


def _dedupe_function_definitions(text, function_names):
    """Keep the last definition of repeated top-level helper functions."""
    for function_name in function_names:
        blocks = _find_function_blocks(text, function_name)
        if len(blocks) <= 1:
            continue
        for start, end in reversed(blocks[:-1]):
            text = text[:start].rstrip() + "\n\n" + text[end:].lstrip()
    return text


def _replace_function(text, function_name, replacement):
    found = _find_function_block(text, function_name)
    if not found:
        return text
    start, end = found
    return text[:start] + replacement.strip() + text[end:]


SERVICE_LAYER_HELPERS = r"""
function obSubsectionConfig() {
  if (typeof OB_SUBSECTION_META !== 'undefined' && OB_SUBSECTION_META && OB_SUBSECTION_META.options) return OB_SUBSECTION_META;
  return { default: 'all_ob_maternity_signals', order: [], options: {} };
}

function activeObSubsectionId() {
  const cfg = obSubsectionConfig();
  if (selectedService !== 'ob') return cfg.default || 'all_ob_maternity_signals';
  if (!selectedObSubsection || !(cfg.options || {})[selectedObSubsection]) return cfg.default || 'all_ob_maternity_signals';
  return selectedObSubsection;
}

function activeObSubsectionMeta() {
  const cfg = obSubsectionConfig();
  const id = activeObSubsectionId();
  return (cfg.options || {})[id] || { label: 'All obstetrics disruptions', note: '' };
}

function emptyServiceLayer() {
  return { total_hours: 0, years_active: 0, episode_count: 0, monthly: {}, monthly_details: {}, has_disruption_data: false, episodes_by_year: {} };
}

function wrapTooltipText(prefix, text, maxChars) {
  const raw = Array.isArray(text) ? text.filter(Boolean).join('; ') : String(text || '');
  if (!raw) return [];
  const limit = maxChars || 52;
  const words = raw.split(/\s+/).filter(Boolean);
  const lines = [];
  let line = prefix || '';
  words.forEach(word => {
    const sep = line && line !== prefix ? ' ' : '';
    const candidate = line + sep + word;
    if (line !== prefix && candidate.length > limit) {
      lines.push(line);
      line = '  ' + word;
    } else if (line === prefix && candidate.length > limit) {
      lines.push(prefix + word);
      line = '  ';
    } else {
      line = candidate;
    }
  });
  if (line.trim()) lines.push(line);
  return lines;
}

function pushWrappedTooltipLine(lines, prefix, text, maxChars) {
  wrapTooltipText(prefix, text, maxChars).forEach(line => lines.push(line));
}

function serviceLayerDisplayName(layerId) {
  const meta = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[layerId]) ? SERVICE_LAYER_META[layerId] : null;
  return (meta && meta.label) ? meta.label : layerId;
}

function hourMetricLabel() {
  return 'disruption hours';
}

function tooltipHourUnit() {
  return selectedService === 'all' ? ' service-hours' : ' hours';
}

function serviceMeta() {
  const base = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[selectedService])
    ? SERVICE_LAYER_META[selectedService]
    : { label: selectedService === 'ed' ? 'Emergency Department' : 'All analyzed services', note: '' };
  if (selectedService === 'ob') {
    const sub = activeObSubsectionMeta();
    const cfg = obSubsectionConfig();
    const defaultId = cfg.default || 'all_ob_maternity_signals';
    if (activeObSubsectionId() && activeObSubsectionId() !== defaultId) {
      return { label: (base.label || 'Obstetrics') + ': ' + sub.label, note: sub.note || base.note || '' };
    }
}

  return base;
}

function siteLayer(site, layerId) {
  const id = layerId || selectedService;
  if (site.layers && site.layers[id]) {
    const layer = site.layers[id];
    if (id === 'ob') {
      const cfg = obSubsectionConfig();
      const subId = activeObSubsectionId();
      const defaultId = cfg.default || 'all_ob_maternity_signals';
      if (subId && subId !== defaultId && layer.sublayers && layer.sublayers[subId]) return layer.sublayers[subId];
      if (subId && subId !== defaultId) return emptyServiceLayer();
    }
    return layer;
  }
  return site;
}

function siteHasDisruptionData(site) {
  const layer = siteLayer(site);
  if (layer.has_disruption_data === false) return false;
  return (Number(layer.total_hours) || 0) > 0 || (Number(layer.episode_count) || 0) > 0;
}

function siteMonthDetail(site, month) {
  const layer = siteLayer(site);
  return (layer.monthly_details || {})[month] || null;
}

function currentLayerSummary(year) {
  const stats = { hours: 0, episodes: 0, affected_sites: 0, reference_sites: DATA.sites.length };
  DATA.sites.forEach(site => {
    const h = siteHours(site, year);
    stats.hours += Number(h) || 0;
    stats.episodes += siteEpisodes(site, year);
    if (h > 0) stats.affected_sites += 1;
  });
  return stats;
}

function updateObSubsectionControl() {
  const cfg = obSubsectionConfig();
  if (!obSubsectionSelect) return;
  const hasOptions = (cfg.order || []).length > 0;
  const wrapper = document.getElementById('obSubsectionControl');
  if (!hasOptions || selectedService !== 'ob') {
    obSubsectionSelect.disabled = true;
    if (wrapper) wrapper.style.display = 'none';
    return;
  }
  obSubsectionSelect.disabled = false;
  if (wrapper) wrapper.style.display = '';
  const options = cfg.options || {};
  obSubsectionSelect.innerHTML = (cfg.order || []).map(id => {
    const label = (options[id] && options[id].label) ? options[id].label : id;
    return '<option value="' + escapeHtml(id) + '">' + escapeHtml(label) + '</option>';
  }).join('');
  if (!selectedObSubsection || !options[selectedObSubsection]) selectedObSubsection = cfg.default || (cfg.order || [])[0];
  obSubsectionSelect.value = selectedObSubsection;
}
"""


CHART_SERVICE_HELPERS = r"""
function serviceLayerColor(layerId) {
  const meta = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[layerId]) ? SERVICE_LAYER_META[layerId] : null;
  if (meta && meta.color) return meta.color;
  const fallback = { ed: '#C23B3B', ob: '#2F8C83', all: '#5A6A85' };
  return fallback[layerId] || '#3B82C4';
}

function serviceLayerSortRank(layerId) {
  const meta = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[layerId]) ? SERVICE_LAYER_META[layerId] : null;
  if (meta && Number.isFinite(Number(meta.sort_order))) return Number(meta.sort_order);
  const fallback = { ed: 10, ob: 20, all: 0 };
  return fallback[layerId] || 100;
}

function serviceLayerOrder(layerIds) {
  return layerIds.slice().sort((a, b) => {
    const rankDelta = serviceLayerSortRank(a) - serviceLayerSortRank(b);
    if (rankDelta) return rankDelta;
    return serviceLayerDisplayName(a).localeCompare(serviceLayerDisplayName(b));
  });
}

function chartDatasetsForSite(site, months) {
  if (selectedService === 'all' && site.layers) {
    const serviceIds = serviceLayerOrder(Object.keys(site.layers).filter(id => id !== 'all'));
    return serviceIds.map(id => {
      const layer = site.layers[id] || emptyServiceLayer();
      const monthly = layer.monthly || {};
      const data = months.map(m => Number(monthly[m]) || 0);
      return {
        label: serviceLayerDisplayName(id),
        layerId: id,
        data: data,
        backgroundColor: serviceLayerColor(id),
        borderWidth: 0,
        barPercentage: 1.0,
        categoryPercentage: 0.95,
        stack: 'services'
      };
    }).filter(ds => ds.data.some(value => value > 0));
  }
  const layer = siteLayer(site);
  const monthly = layer.monthly || {};
  return [{
    label: serviceLabel(),
    layerId: selectedService,
    data: months.map(m => Number(monthly[m]) || 0),
    backgroundColor: serviceLayerColor(selectedService),
    borderWidth: 0,
    barPercentage: 1.0,
    categoryPercentage: 0.95
  }];
}

function chartDatasetsForProvince(months) {
  if (selectedService === 'all') {
    const serviceIds = serviceLayerOrder(Array.from(new Set(DATA.sites.flatMap(site => Object.keys(site.layers || {}).filter(id => id !== 'all')))));
    return serviceIds.map(id => {
      const data = months.map(month => DATA.sites.reduce((sum, site) => {
        const layer = site.layers && site.layers[id] ? site.layers[id] : null;
        return sum + (layer && layer.monthly ? (Number(layer.monthly[month]) || 0) : 0);
      }, 0));
      return {
        label: serviceLayerDisplayName(id),
        layerId: id,
        data: data,
        backgroundColor: serviceLayerColor(id),
        borderWidth: 0,
        barPercentage: 1.0,
        categoryPercentage: 0.95,
        stack: 'services'
      };
    }).filter(ds => ds.data.some(value => value > 0));
  }
  const data = months.map(month => DATA.sites.reduce((sum, site) => {
    const layer = siteLayer(site);
    const monthly = layer.monthly || {};
    return sum + (Number(monthly[month]) || 0);
  }, 0));
  return [{
    label: serviceLabel(),
    layerId: selectedService,
    data: data,
    backgroundColor: serviceLayerColor(selectedService),
    borderWidth: 0,
    barPercentage: 1.0,
    categoryPercentage: 0.95
  }].filter(ds => ds.data.some(value => value > 0));
}
"""


SERVICE_DISPLAY_HELPERS = r"""
function serviceLayerDisplayName(layerId) {
  const meta = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[layerId]) ? SERVICE_LAYER_META[layerId] : null;
  return (meta && meta.label) ? meta.label : layerId;
}

function hourMetricLabel() {
  return 'disruption hours';
}

function tooltipHourUnit() {
  return selectedService === 'all' ? ' service-hours' : ' hours';
}


"""

EXTERNAL_CHART_TOOLTIP_HELPER = r"""
function externalChartTooltip(context) {
  const chart = context.chart;
  const tooltip = context.tooltip;
  let tooltipEl = document.getElementById('chartExternalTooltip');
  let leaderEl = document.getElementById('chartExternalTooltipLeader');
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.id = 'chartExternalTooltip';
    tooltipEl.style.position = 'fixed';
    tooltipEl.style.pointerEvents = 'none';
    tooltipEl.style.zIndex = '9999';
    tooltipEl.style.opacity = '0';
    tooltipEl.style.background = 'rgba(15, 35, 71, 0.94)';
    tooltipEl.style.color = '#fff';
    tooltipEl.style.borderRadius = '6px';
    tooltipEl.style.padding = '8px 10px';
    tooltipEl.style.boxShadow = '0 10px 24px rgba(15, 35, 71, 0.24)';
    tooltipEl.style.fontFamily = 'Lato, sans-serif';
    tooltipEl.style.fontSize = '11px';
    tooltipEl.style.lineHeight = '1.35';
    tooltipEl.style.maxWidth = '280px';
    tooltipEl.style.transition = 'opacity 80ms ease';
    document.body.appendChild(tooltipEl);
  }
  if (!leaderEl) {
    leaderEl = document.createElement('div');
    leaderEl.id = 'chartExternalTooltipLeader';
    leaderEl.style.position = 'fixed';
    leaderEl.style.pointerEvents = 'none';
    leaderEl.style.zIndex = '9998';
    leaderEl.style.opacity = '0';
    leaderEl.style.height = '1px';
    leaderEl.style.background = 'rgba(15, 35, 71, 0.45)';
    leaderEl.style.transformOrigin = '0 0';
    leaderEl.style.transition = 'opacity 80ms ease';
    document.body.appendChild(leaderEl);
  }
  if (!tooltip || tooltip.opacity === 0) {
    tooltipEl.style.opacity = '0';
    leaderEl.style.opacity = '0';
    return;
  }
  const title = (tooltip.title || []).join(' ');
  const body = tooltip.body || [];
  const colors = tooltip.labelColors || [];
  const rows = body.map((item, idx) => {
    const text = (item.lines || []).join(' ');
    const color = colors[idx] || {};
    const bg = color.backgroundColor || '#D0D8E8';
    const border = color.borderColor || bg;
    return '<div style="display:flex;align-items:flex-start;gap:6px;margin-top:4px;">' +
      '<span style="width:9px;height:9px;border-radius:2px;background:' + bg + ';border:1px solid ' + border + ';flex:0 0 auto;margin-top:3px;"></span>' +
      '<span>' + escapeHtml(text) + '</span>' +
    '</div>';
  }).join('');
  tooltipEl.innerHTML =
    '<div style="font-weight:700;margin-bottom:3px;">' + escapeHtml(title) + '</div>' +
    rows;
  tooltipEl.style.maxWidth = Math.min(320, Math.max(180, window.innerWidth - 16)) + 'px';
  tooltipEl.style.opacity = '1';

  const rect = chart.canvas.getBoundingClientRect();
  const box = tooltipEl.getBoundingClientRect();
  const pad = 8;
  const verticalGap = 10;
  const anchorX = rect.left + tooltip.caretX;
  const anchorY = rect.top + tooltip.caretY;
  const chartCenterX = rect.left + (rect.width / 2);
  let left = chartCenterX - (box.width / 2);
  left = Math.min(Math.max(left, pad), window.innerWidth - box.width - pad);
  let top = rect.top - box.height - verticalGap;
  let attachY = top + box.height;
  if (top < pad) {
    top = rect.top + verticalGap;
    attachY = top;
  }
  top = Math.min(Math.max(top, pad), window.innerHeight - box.height - pad);
  tooltipEl.style.left = left + 'px';
  tooltipEl.style.top = top + 'px';

  const attachX = Math.min(Math.max(anchorX, left + 12), left + box.width - 12);
  const dx = anchorX - attachX;
  const dy = anchorY - attachY;
  const length = Math.sqrt(dx * dx + dy * dy);
  const angle = Math.atan2(dy, dx) * 180 / Math.PI;
  leaderEl.style.left = attachX + 'px';
  leaderEl.style.top = attachY + 'px';
  leaderEl.style.width = Math.max(0, length) + 'px';
  leaderEl.style.transform = 'rotate(' + angle + 'deg)';
  leaderEl.style.opacity = length > 8 ? '1' : '0';
}
"""


OB_SUBSECTION_HELPERS = r"""
function obSubsectionConfig() {
  if (typeof OB_SUBSECTION_META !== 'undefined' && OB_SUBSECTION_META && OB_SUBSECTION_META.options) return OB_SUBSECTION_META;
  return { default: 'all_ob_maternity_signals', order: [], options: {} };
}

function activeObSubsectionId() {
  const cfg = obSubsectionConfig();
  if (selectedService !== 'ob') return cfg.default || 'all_ob_maternity_signals';
  if (!selectedObSubsection || !(cfg.options || {})[selectedObSubsection]) return cfg.default || 'all_ob_maternity_signals';
  return selectedObSubsection;
}

function activeObSubsectionMeta() {
  const cfg = obSubsectionConfig();
  const id = activeObSubsectionId();
  return (cfg.options || {})[id] || { label: 'All obstetrics disruptions', note: '' };
}

function emptyServiceLayer() {
  return { total_hours: 0, years_active: 0, episode_count: 0, monthly: {}, monthly_details: {}, has_disruption_data: false, episodes_by_year: {} };
}

function wrapTooltipText(prefix, text, maxChars) {
  const raw = Array.isArray(text) ? text.filter(Boolean).join('; ') : String(text || '');
  if (!raw) return [];
  const limit = maxChars || 52;
  const words = raw.split(/\s+/).filter(Boolean);
  const lines = [];
  let line = prefix || '';
  words.forEach(word => {
    const sep = line && line !== prefix ? ' ' : '';
    const candidate = line + sep + word;
    if (line !== prefix && candidate.length > limit) {
      lines.push(line);
      line = '  ' + word;
    } else if (line === prefix && candidate.length > limit) {
      lines.push(prefix + word);
      line = '  ';
    } else {
      line = candidate;
    }
  });
  if (line.trim()) lines.push(line);
  return lines;
}

function pushWrappedTooltipLine(lines, prefix, text, maxChars) {
  wrapTooltipText(prefix, text, maxChars).forEach(line => lines.push(line));
}

function serviceLayerDisplayName(layerId) {
  const meta = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[layerId]) ? SERVICE_LAYER_META[layerId] : null;
  return (meta && meta.label) ? meta.label : layerId;
}

function hourMetricLabel() {
  return 'disruption hours';
}

function tooltipHourUnit() {
  return selectedService === 'all' ? ' service-hours' : ' hours';
}

function currentLayerSummary(year) {
  const stats = { hours: 0, episodes: 0, affected_sites: 0, reference_sites: DATA.sites.length };
  DATA.sites.forEach(site => {
    const h = siteHours(site, year);
    stats.hours += Number(h) || 0;
    stats.episodes += siteEpisodes(site, year);
    if (h > 0) stats.affected_sites += 1;
  });
  return stats;
}

function updateObSubsectionControl() {
  const cfg = obSubsectionConfig();
  if (!obSubsectionSelect) return;
  const hasOptions = (cfg.order || []).length > 0;
  const wrapper = document.getElementById('obSubsectionControl');
  if (!hasOptions || selectedService !== 'ob') {
    obSubsectionSelect.disabled = true;
    if (wrapper) wrapper.style.display = 'none';
    return;
  }
  obSubsectionSelect.disabled = false;
  if (wrapper) wrapper.style.display = '';
  const options = cfg.options || {};
  obSubsectionSelect.innerHTML = (cfg.order || []).map(id => {
    const label = (options[id] && options[id].label) ? options[id].label : id;
    return '<option value="' + escapeHtml(id) + '">' + escapeHtml(label) + '</option>';
  }).join('');
  if (!selectedObSubsection || !options[selectedObSubsection]) selectedObSubsection = cfg.default || (cfg.order || [])[0];
  obSubsectionSelect.value = selectedObSubsection;
}
"""


def _service_select_options(service_meta):
    order = [sid for sid in ("all", "ed", "ob") if sid in service_meta]
    order.extend([sid for sid in service_meta.keys() if sid not in order])
    return "\n".join(
        f'          <option value="{sid}">{service_meta[sid].get("label", sid)}</option>'
        for sid in order
    )


def _ensure_service_select_options(text, service_meta):
    pattern = re.compile(r'(<select id="serviceSelect"[^>]*>)(.*?)(\s*</select>)', re.DOTALL)
    options = "\n" + _service_select_options(service_meta) + "\n        "
    return pattern.sub(lambda m: m.group(1) + options + m.group(3), text, count=1)


def _ensure_ob_subsection_select(text):
    if 'id="obSubsectionSelect"' in text:
        text = re.sub(
            r'<label\s+for="obSubsectionSelect">.*?</label>',
            '<label for="obSubsectionSelect">Type</label>',
            text,
            count=1,
            flags=re.DOTALL,
        )
        text = re.sub(
            r'<select\s+id="obSubsectionSelect"\s+aria-label="[^"]*"',
            '<select id="obSubsectionSelect" aria-label="Select disruption type"',
            text,
            count=1,
        )
        return text
    block = """
      <div class="view-control" id="obSubsectionControl" style="display: none;">
        <label for="obSubsectionSelect">Type</label>
        <select id="obSubsectionSelect" aria-label="Select disruption type"></select>
      </div>
"""
    pattern = re.compile(r'(<select id="serviceSelect"[^>]*>.*?</select>\s*</div>)', re.DOTALL)
    return pattern.sub(lambda m: m.group(1) + "\n" + block, text, count=1)


def _ensure_sidebar_responsive_visibility(text):
    """Keep the sidebar reachable in narrow embedded/browser panes."""
    text = text.replace(
        "html, body { overflow: hidden; }",
        "html, body { overflow-x: hidden; overflow-y: auto; }",
    )
    text = text.replace(
        'grid-template-areas: "header" "map" "sidebar" "footer";',
        'grid-template-areas: "header" "sidebar" "map" "footer";',
    )
    return text


COMPACT_SIDEBAR_CSS = """
/* Compact sidebar/list fit for the province chart plus top-ten list. */
:root { --blue: #216B9E; }
.sidebar { padding: 0.45rem 1rem 0.75rem; max-width: 100%; overflow-x: hidden; }
.sidebar > * { max-width: 100%; }
.search-panel { margin: 0 0 0.42rem; padding-bottom: 0.42rem; }
.search-input, .view-control select, .stats-row, .chart-wrap, .rank-list, .site-detail-view-control, .nearest-service-list, .nearest-service-source { max-width: 100%; }
.panel-label { margin-bottom: 0.32rem; font-size: 0.66rem; }
.chart-wrap { height: 128px; margin-bottom: 0.36rem; }
.overview-chart-wrap { height: 150px; }
.rank-list { margin-top: 0.28rem; }
.rank-row { display: grid; grid-template-columns: minmax(0, 1fr) max-content; column-gap: 0.52rem; padding: 0.17rem 0.2rem 0.17rem 0; font-size: 0.74rem; line-height: 1.22; align-items: start; }
.rank-name { min-width: 0; overflow-wrap: anywhere; }
.rank-hours { color: var(--muted); text-align: right; white-space: nowrap; line-height: 1.2; padding-right: 0.1rem; max-width: 9.8rem; }
@media (max-width: 768px) {
  header { max-width: 100vw; overflow-x: hidden; gap: 0.45rem; padding-right: 0.85rem; }
  .total-stat { max-width: 100%; min-width: 0; white-space: normal; line-height: 1.35; overflow-wrap: anywhere; }
  .view-control { max-width: 100%; min-width: 0; white-space: normal; }
  .view-control select { max-width: 100%; min-width: 0; min-height: 2.75rem; }
  .back-btn { min-width: 2.75rem; min-height: 2.75rem; display: inline-flex; align-items: center; padding: 0 0.22rem; }
  .sidebar { width: 100%; padding: 0.38rem 0.88rem 0.65rem 0.85rem; overflow-x: clip; }
  .search-panel { margin-bottom: 0.36rem; padding-bottom: 0.36rem; }
  .panel-label { margin-bottom: 0.28rem; font-size: 0.64rem; }
  .chart-wrap { height: 120px; margin-bottom: 0.32rem; }
  .chart-wrap canvas { max-width: 100% !important; }
  .overview-chart-wrap { height: 140px; }
  .rank-list { margin-top: 0.24rem; }
  .rank-row { column-gap: 0.38rem; padding: 0.16rem 0.38rem 0.16rem 0; font-size: 0.72rem; line-height: 1.18; }
  .rank-hours { max-width: 7.2rem; padding-right: 0.22rem; }
}
""".strip()


ED_ACCESS_CSS = """
/* Service-aware nearest-site access overlay. */
.site-detail-view-control { display: grid; grid-template-columns: 1fr 1fr; gap: 0.28rem; margin: 0.36rem 0 0.5rem; }
.site-detail-view-btn { min-width: 0; border: 1px solid rgba(90, 106, 133, 0.24); border-radius: 4px; padding: 0.34rem 0.38rem; background: #fff; color: var(--muted); font-family: inherit; font-size: 0.72rem; font-weight: 700; line-height: 1.15; cursor: pointer; }
.site-detail-view-btn.active { border-color: rgba(27, 58, 107, 0.42); background: rgba(59, 130, 196, 0.09); color: var(--ink); }
.nearest-service-list { display: grid; gap: 0.32rem; margin: 0.18rem 0 0.52rem; }
.nearest-service-row { display: grid; grid-template-columns: 1.35rem minmax(0, 1fr) max-content; align-items: start; gap: 0.42rem; padding: 0.32rem 0; border-bottom: 1px solid rgba(90, 106, 133, 0.16); font-size: 0.73rem; line-height: 1.22; }
.nearest-service-index { color: var(--muted); font-weight: 700; }
.nearest-service-name { min-width: 0; overflow-wrap: anywhere; color: var(--ink); font-weight: 700; }
.nearest-service-link { appearance: none; border: 0; background: transparent; padding: 0; margin: 0; text-align: left; font: inherit; line-height: inherit; color: var(--ink); cursor: pointer; text-decoration: none; }
.nearest-service-link:hover,
.nearest-service-link:focus-visible { color: var(--navy); outline: none; }
.nearest-service-meta { color: var(--muted); text-align: right; white-space: nowrap; }
.nearest-service-burden { grid-column: 2 / 4; color: var(--ink); font-size: 0.7rem; line-height: 1.25; }
.nearest-service-source { margin-top: -0.24rem; color: var(--muted); font-size: 0.66rem; line-height: 1.28; }
.nearest-service-source a { color: var(--muted); text-decoration: underline; text-underline-offset: 2px; }
@media (max-width: 768px) {
  .site-detail-view-control { gap: 0.22rem; margin: 0.32rem 0 0.44rem; }
  .site-detail-view-btn { min-height: 2.15rem; padding: 0.32rem 0.3rem; font-size: 0.74rem; }
  .nearest-service-row { grid-template-columns: 1.15rem minmax(0, 1fr); }
  .nearest-service-meta { grid-column: 2; text-align: left; white-space: normal; }
  .nearest-service-burden { grid-column: 2; }
}
""".strip()


def _ensure_compact_sidebar_css(text):
    """Make the home sidebar fit the province chart, ranked list, and mobile hour labels."""
    marker = "/* Compact sidebar/list fit for the province chart plus top-ten list. */"
    if marker in text:
        if ".sidebar > * { max-width: 100%; }" in text and "overflow-x: clip" in text and ".total-stat { max-width: 100%; min-width: 0; white-space: normal;" in text:
            return text
        text = re.sub(
            r"/\* Compact sidebar/list fit for the province chart plus top-ten list\. \*/.*?(?=\n/\* Service-aware nearest-site access overlay\. \*/|\n</style>)",
            COMPACT_SIDEBAR_CSS + "\n",
            text,
            count=1,
            flags=re.DOTALL,
        )
        return text
    if "</style>" in text:
        return text.replace("</style>", "\n" + COMPACT_SIDEBAR_CSS + "\n</style>", 1)
    return text


def ensure_service_layer_runtime(text, service_meta):
    """Patch the existing SORCTracks HTML runtime to read per-service site layers."""
    service_meta = service_meta or SERVICE_LAYER_META_DEFAULT
    text = _ensure_sidebar_responsive_visibility(text)
    text = _ensure_compact_sidebar_css(text)
    text = _ensure_ed_access_css(text)
    text = _ensure_service_select_options(text, service_meta)
    text = _ensure_ob_subsection_select(text)

    if "function siteLayer(site, layerId)" not in text:
        idx = text.find("function serviceLabel")
        if idx >= 0:
            text = text[:idx] + SERVICE_LAYER_HELPERS.strip() + "\n\n" + text[idx:]
    elif "function obSubsectionConfig()" not in text:
        idx = text.find("function serviceMeta")
        if idx >= 0:
            text = text[:idx] + OB_SUBSECTION_HELPERS.strip() + "\n\n" + text[idx:]
    display_helper_names = (
        "function serviceLayerDisplayName(layerId)",
        "function hourMetricLabel()",
        "function tooltipHourUnit()",
    )
    if any(name not in text for name in display_helper_names):
        idx = text.find("function serviceLayerColor")
        if idx < 0:
            idx = text.find("function serviceMeta")
        if idx < 0:
            idx = text.find("function siteLayer")
        if idx >= 0:
            text = text[:idx] + SERVICE_DISPLAY_HELPERS.strip() + "\n\n" + text[idx:]
    if "const nearestAccessLineLayer = L.layerGroup().addTo(map);" not in text:
        text = text.replace(
            "const markerRows = [];",
            "const markerRows = [];\nconst nearestAccessLineLayer = L.layerGroup().addTo(map);",
            1,
        )
    text = text.replace("const nearestEdLineLayer = L.layerGroup().addTo(map);", "")
    for access_fn in (
        "effectiveNearbyAccessMode",
        "nearestAccessLayerId",
        "nearestAccessRows",
        "nearestAccessLabel",
        "siteDetailViewControlHtml",
        "bindSiteDetailViewControl",
        "nearbyAccessToggleHtml",
        "nearbyAccessControlHtml",
        "bindNearbyAccessToggle",
        "bindNearbyAccessControl",
        "nearbyAccessPanelHtml",
        "nearestAccessHtml",
        "nearestAccessBurdenText",
        "nearestAccessSourceHtml",
        "updateNearestAccessLines",
        "nearestEdAccessRows",
        "nearestEdAccessHtml",
        "updateNearestEdLines",
    ):
        text = _replace_function(text, access_fn, "")
    idx = text.find("function renderHome")
    if idx < 0:
        idx = text.find("function updateMarkers")
    if idx >= 0:
        text = text[:idx] + ED_ACCESS_JS + "\n\n" + text[idx:]
    if "function externalChartTooltip(context)" in text:
        text = _replace_function(text, "externalChartTooltip", EXTERNAL_CHART_TOOLTIP_HELPER.strip())
    else:
        idx = text.find("function currentLayerSummary")
        if idx < 0:
            idx = text.find("function updateObSubsectionControl")
        if idx < 0:
            idx = text.find("function chartDatasetsForProvince")
        if idx >= 0:
            text = text[:idx] + EXTERNAL_CHART_TOOLTIP_HELPER.strip() + "\n\n" + text[idx:]
    if "function chartDatasetsForProvince(months)" not in text:
        if "function chartDatasetsForSite(site, months)" in text:
            text = _replace_function(text, "chartDatasetsForSite", CHART_SERVICE_HELPERS)
        else:
            idx = text.find("function serviceMeta")
            if idx < 0:
                idx = text.find("function siteLayer")
            if idx >= 0:
                text = text[:idx] + CHART_SERVICE_HELPERS.strip() + "\n\n" + text[idx:]

    if "const obSubsectionSelect = document.getElementById('obSubsectionSelect');" not in text:
        text = re.sub(
            r"const\s+serviceSelect\s*=\s*document\.getElementById\('serviceSelect'\);",
            "const serviceSelect = document.getElementById('serviceSelect');\nconst obSubsectionSelect = document.getElementById('obSubsectionSelect');",
            text,
            count=1,
        )
    if "let selectedObSubsection =" not in text:
        text = re.sub(
            r"let\s+selectedService\s*=\s*'all';",
            "let selectedService = 'all';\nlet selectedObSubsection = 'all_ob_maternity_signals';",
            text,
            count=1,
        )
    if "let siteDetailView =" not in text:
        text = re.sub(
            r"let\s+selectedObSubsection\s*=\s*'all_ob_maternity_signals';",
            "let selectedObSubsection = 'all_ob_maternity_signals';\nlet siteDetailView = 'data';",
            text,
            count=1,
        )
    text = re.sub(r"\nlet\s+nearbyAccessMode\s*=\s*'off';", "", text, count=1)
    if "yearSelect.value = selectedYear;" not in text:
        text = text.replace(
            "let selectedYear = 'all';",
            "let selectedYear = 'all';\nif (yearSelect) yearSelect.value = selectedYear;",
            1,
        )
    if "serviceSelect.value = selectedService;" not in text:
        text = text.replace(
            "let selectedService = 'all';",
            "let selectedService = 'all';\nif (serviceSelect) serviceSelect.value = selectedService;",
            1,
        )
    text = re.sub(
        r"selectedService\s*=\s*serviceSelect\.value;\s*(?:(?:nearbySitesVisible\s*=\s*false|nearbyAccessMode\s*=\s*'off')\s*;\s*)?",
        "selectedService = serviceSelect.value;\n  siteDetailView = 'data';\n  ",
        text,
        count=1,
    )
    listener = """
if (obSubsectionSelect) {
  obSubsectionSelect.addEventListener('change', () => {
    selectedObSubsection = obSubsectionSelect.value;
    updateSummaryStats();
    refreshMapScale();
    if (currentSite) showSite(currentSite);
    else renderHome();
  });
  updateObSubsectionControl();
}
"""
    text = re.sub(
        r"\nif \(obSubsectionSelect\) \{\s+obSubsectionSelect\.addEventListener\('change'.*?updateObSubsectionControl\(\);\s+\}\n",
        "\n",
        text,
        count=1,
        flags=re.DOTALL,
    )
    if "obSubsectionSelect.addEventListener('change'" not in text:
        idx = text.find("serviceSelect.addEventListener('change'")
        if idx >= 0:
            text = text[:idx] + listener + "\n" + text[idx:]

    text = _replace_function(
        text,
        "obSubsectionConfig",
        """
function serviceTypeConfig(serviceId) {
  const id = serviceId || selectedService;
  if (typeof SERVICE_TYPE_META !== 'undefined' && SERVICE_TYPE_META && SERVICE_TYPE_META[id]) return SERVICE_TYPE_META[id];
  if (id === 'ob' && typeof OB_SUBSECTION_META !== 'undefined' && OB_SUBSECTION_META && OB_SUBSECTION_META.options) return OB_SUBSECTION_META;
  return { default: '', order: [], options: {} };
}

function obSubsectionConfig() {
  return serviceTypeConfig(selectedService);
}
""",
    )
    text = _replace_function(
        text,
        "activeObSubsectionId",
        """
function activeObSubsectionId() {
  const cfg = serviceTypeConfig(selectedService);
  const fallback = cfg.default || (cfg.order || [])[0] || '';
  if (!fallback) return '';
  if (!selectedObSubsection || !(cfg.options || {})[selectedObSubsection]) return fallback;
  return selectedObSubsection;
}
""",
    )
    text = _replace_function(
        text,
        "activeObSubsectionMeta",
        """
function activeObSubsectionMeta() {
  const cfg = serviceTypeConfig(selectedService);
  const id = activeObSubsectionId();
  return (cfg.options || {})[id] || { label: 'All types', note: '' };
}
""",
    )
    text = _replace_function(
        text,
        "updateObSubsectionControl",
        """
function updateObSubsectionControl() {
  const cfg = serviceTypeConfig(selectedService);
  if (!obSubsectionSelect) return;
  const hasOptions = (cfg.order || []).length > 0;
  const wrapper = document.getElementById('obSubsectionControl');
  if (!hasOptions) {
    obSubsectionSelect.disabled = true;
    if (wrapper) wrapper.style.display = 'none';
    return;
  }
  obSubsectionSelect.disabled = false;
  if (wrapper) wrapper.style.display = '';
  const options = cfg.options || {};
  obSubsectionSelect.innerHTML = (cfg.order || []).map(id => {
    const label = (options[id] && options[id].label) ? options[id].label : id;
    return '<option value="' + escapeHtml(id) + '">' + escapeHtml(label) + '</option>';
  }).join('');
  if (!selectedObSubsection || !options[selectedObSubsection]) selectedObSubsection = cfg.default || (cfg.order || [])[0];
  obSubsectionSelect.value = selectedObSubsection;
}
""",
    )
    text = _replace_function(
        text,
        "serviceMeta",
        """
function serviceMeta() {
  const base = (typeof SERVICE_LAYER_META !== 'undefined' && SERVICE_LAYER_META[selectedService])
    ? SERVICE_LAYER_META[selectedService]
    : { label: selectedService === 'ed' ? 'Emergency Department' : 'All analyzed services', note: '' };
  if (serviceTypeConfig(selectedService).order && serviceTypeConfig(selectedService).order.length) {
    const sub = activeObSubsectionMeta();
    const cfg = serviceTypeConfig(selectedService);
    const defaultId = cfg.default || (cfg.order || [])[0] || '';
    if (activeObSubsectionId() && activeObSubsectionId() !== defaultId) {
      return { label: (base.label || selectedService) + ': ' + sub.label, note: sub.note || base.note || '' };
    }
  }
  return base;
}
""",
    )
    text = _replace_function(
        text,
        "serviceLabel",
        """
function serviceLabel() {
  return serviceMeta().label || selectedService;
}
""",
    )
    text = _replace_function(
        text,
        "serviceLayerNote",
        """
function serviceLayerNote() {
  return serviceMeta().note || '';
}
""",
    )
    text = _replace_function(
        text,
        "siteLayer",
        """
function siteLayer(site, layerId) {
  const id = layerId || selectedService;
  if (site.layers && site.layers[id]) {
    const layer = site.layers[id];
    const cfg = serviceTypeConfig(id);
    if ((cfg.order || []).length && id === selectedService) {
      const subId = activeObSubsectionId();
      const defaultId = cfg.default || (cfg.order || [])[0] || '';
      if (subId && subId !== defaultId && layer.sublayers && layer.sublayers[subId]) return layer.sublayers[subId];
      if (subId && subId !== defaultId) return emptyServiceLayer();
    }
    return layer;
  }
  return site;
}
""",
    )
    text = _replace_function(
        text,
        "siteHours",
        """
function siteHours(site, year) {
  const layer = siteLayer(site);
  const monthly = layer.monthly || {};
  if (year === 'all') return Number(layer.total_hours) || 0;
  return Object.entries(monthly).reduce((sum, [month, value]) => {
    return month.startsWith(year + '-') ? sum + (Number(value) || 0) : sum;
  }, 0);
}
""",
    )
    text = _replace_function(
        text,
        "siteEpisodes",
        """
function siteEpisodes(site, year) {
  const layer = siteLayer(site);
  if (layer.has_disruption_data === false) return 0;
  if (year === 'all') return Number(layer.episode_count) || 0;
  return Number((layer.episodes_by_year || {})[year]) || 0;
}
""",
    )
    text = _replace_function(
        text,
        "displayName",
        """
function displayName(site) {
  return site.facility_name || site.display_name || DISPLAY_NAMES[site.name] || titleCaseName(site.name);
}
""",
    )
    text = _replace_function(
        text,
        "siteIdNote",
        """
function siteIdNote(site) {
  if (!siteHasDisruptionData(site)) {
    return 'AHS location with no posted disruption hours captured for the currently analyzed service layer.';
  }
  const community = site.community || ((site.display_name && normalizeSearch(site.display_name) !== normalizeSearch(displayName(site))) ? site.display_name : '');
  if (community && normalizeSearch(community) !== normalizeSearch(displayName(site))) {
    return 'Community: ' + community;
  }
  return '';
}
""",
    )
    text = _replace_function(
        text,
        "showSiteInCurrentMode",
        """
function showSiteInCurrentMode(site) {
  if (siteHasDisruptionData(site)) return true;
  if (!showNoDataSites) return false;
  if (typeof hourFrameworkHasSubtypeFilter === 'function' && hourFrameworkHasSubtypeFilter()) return false;
  if (selectedService === 'all') return true;
  const capability = (site.service_capability || {})[selectedService] || {};
  return capability.nearby_candidate === true;
}
""",
    )
    text = _replace_function(
        text,
        "currentScaleMaxHours",
        """
function currentScaleMaxHours() {
  const visibleHours = currentVisibleSites()
    .map(site => siteHours(site, selectedYear))
    .filter(h => h > 0);
  if (visibleHours.length) return Math.max(...visibleHours);
  const filteredHours = currentSearchSites()
    .map(site => siteHours(site, selectedYear))
    .filter(h => h > 0);
  if (filteredHours.length) return Math.max(...filteredHours);
  const allHours = DATA.sites.filter(site => siteHasDisruptionData(site)).map(site => siteHours(site, selectedYear)).filter(h => h > 0);
  return allHours.length ? Math.max(...allHours) : 0;
}
""",
    )
    text = _replace_function(
        text,
        "updateSummaryStats",
        """
function updateSummaryStats() {
  if (!summaryStats) return;
  const cfg = serviceTypeConfig(selectedService);
  const defaultSub = cfg.default || (cfg.order || [])[0] || '';
  const useComputedSubsection = (cfg.order || []).length && activeObSubsectionId() !== defaultSub;
  const summaries = (typeof SERVICE_YEAR_SUMMARY !== 'undefined' && SERVICE_YEAR_SUMMARY[selectedService]) ? SERVICE_YEAR_SUMMARY[selectedService] : YEAR_SUMMARY;
  const stats = useComputedSubsection
    ? currentLayerSummary(selectedYear)
    : (summaries[selectedYear] || summaries.all || YEAR_SUMMARY[selectedYear] || YEAR_SUMMARY.all);
  summaryStats.innerHTML =
    '<strong>' + fmtHours(stats.hours) + '</strong>' + hourMetricLabel() +
    '<strong>' + fmtHours(stats.affected_sites) + '</strong>sites affected';
  summaryStats.title = stats.reference_sites + ' AHS locations are searchable when the zero-site reference layer is shown.';
}
""",
    )
    text = _replace_function(
        text,
        "yearCount",
        """
function yearCount(site) {
  const layer = siteLayer(site);
  return new Set(Object.keys(layer.monthly || {}).map(m => m.slice(0, 4))).size;
}
""",
    )
    text = _replace_function(
        text,
        "avgHoursPerEpisode",
        """
function activeMonthCount(site, year) {
  const monthly = siteLayer(site).monthly || {};
  return Object.entries(monthly).filter(([month, hours]) => {
    return (year === 'all' || month.startsWith(year + '-')) && (Number(hours) || 0) > 0;
  }).length;
}
""",
    )
    text = _replace_function(
        text,
        "renderHome",
        """
function renderHome() {
  currentSite = null;
  updateNearestAccessLines(null);
  destroyChart();
  const q = searchQuery;
  const hasQuery = normalizeSearch(q).length > 0;
  let rows = DATA.sites
    .filter(showSiteInCurrentMode)
    .filter(site => hasQuery ? siteMatchesSearch(site, q) : siteHours(site, selectedYear) > 0)
    .map(site => ({
      site,
      hours: siteHours(site, selectedYear),
      total: siteHours(site, 'all'),
      rank: siteSearchRank(site, q)
    }))
    .sort((a, b) => {
      if (hasQuery && a.rank !== b.rank) return a.rank - b.rank;
      if (b.hours !== a.hours) return b.hours - a.hours;
      if (b.total !== a.total) return b.total - a.total;
      return displayName(a.site).localeCompare(displayName(b.site));
    });
  const totalMatches = rows.length;
  const visibleRows = rows.slice(0, HOME_LIST_LIMIT);
  const label = hasQuery
    ? 'Search Results &middot; ' + periodLabel(selectedYear)
    : 'Highest Burden Sites &middot; ' + periodLabel(selectedYear);
  sidebar.innerHTML =
    '<div class="search-panel">' +
      '<div class="panel-label">Find an AHS Location</div>' +
      '<input id="siteSearch" class="search-input" type="search" placeholder="Search community or facility name" value="' + escapeHtml(q) + '" autocomplete="off" />' +
    '</div>' +
    '<div class="panel-label">Alberta Overview</div>' +
    '<div class="chart-wrap overview-chart-wrap"><canvas id="tsChart"></canvas></div>' +
    '<div class="panel-label">' + label + '</div>' +
    '<div class="rank-list">' +
      (visibleRows.length ? visibleRows.map((row, idx) => {
        const selectedText = !siteHasDisruptionData(row.site)
          ? 'No posted data'
          : (row.hours > 0 ? fmtHours(row.hours) + ' h' : '0 h in view');
        const allYearText = selectedYear === 'all' ? '' : ' <span class="rank-hours">(' + fmtHours(row.total) + ' h all-year)</span>';
        const prefix = hasQuery ? '' : (idx + 1) + '. ';
        return '<div class="rank-row" data-site-name="' + escapeHtml(row.site.name) + '">' +
          '<span class="rank-name">' + prefix + escapeHtml(displayName(row.site)) + '</span>' +
          '<span class="rank-hours">' + selectedText + allYearText + '</span>' +
        '</div>';
      }).join('') : '<div class="search-note">No matching visible locations.</div>') +
    '</div>';
  const input = document.getElementById('siteSearch');
  if (input) {
    input.addEventListener('input', () => {
      searchQuery = input.value;
      refreshMapScale();
      renderHome();
      fitToSearchResults();
      const nextInput = document.getElementById('siteSearch');
      if (nextInput) {
        nextInput.focus();
        const len = nextInput.value.length;
        nextInput.setSelectionRange(len, len);
      }
    });
    if (hasQuery) {
      input.focus();
      const len = input.value.length;
      input.setSelectionRange(len, len);
    }
  }
  sidebar.querySelectorAll('.rank-row').forEach(row => {
    row.addEventListener('click', () => {
      const site = DATA.sites.find(s => s.name === row.getAttribute('data-site-name'));
      if (site) focusSite(site);
    });
  });
  const months = projectMonthsForYear(selectedYear);
  const datasets = chartDatasetsForProvince(months);
  const canvas = document.getElementById('tsChart');
  if (canvas && datasets.length) {
    activeChart = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: { labels: months, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            display: selectedService === 'all',
            labels: { font: { family: 'Lato', size: 10 }, color: '#1B3A6B', boxWidth: 10, boxHeight: 10 }
          },
          tooltip: {
            enabled: false,
            external: externalChartTooltip,
            mode: selectedService === 'all' ? 'index' : 'nearest',
            intersect: selectedService !== 'all',
            filter: item => selectedService !== 'all' || Number(item.raw) > 0,
            callbacks: {
              title: items => fmtMonth(items[0].label),
              label: item => {
                const itemValue = Number(item.raw) || 0;
                return selectedService === 'all'
                  ? item.dataset.label + ': ' + Math.round(itemValue) + tooltipHourUnit()
                  : Math.round(itemValue) + tooltipHourUnit();
              }
            }
          }
        },
        scales: {
          x: {
            stacked: selectedService === 'all',
            ticks: {
              font: { family: 'Lato', size: 9 },
              color: '#5A6A85',
              autoSkip: selectedYear !== 'all',
              callback: function(val) {
                const lbl = this.getLabelForValue(val);
                if (selectedYear !== 'all') return lbl.slice(5);
                return lbl.endsWith('-01') ? lbl.slice(0, 4) : '';
              }
            },
            grid: { display: false }
          },
          y: {
            stacked: selectedService === 'all',
            beginAtZero: true,
            ticks: { font: { family: 'Lato', size: 9 }, color: '#5A6A85' },
            grid: { color: 'rgba(15,35,71,0.06)' },
            title: { display: true, text: selectedService === 'all' ? 'Service-hours' : 'Hours', font: { family: 'Lato', size: 10 }, color: '#1B3A6B' }
          }
        }
      }
    });
  }
}
""",
    )
    text = _replace_function(
        text,
        "showSite",
        """
function showSite(site) {
  currentSite = site;
  destroyChart();
  updateMarkers();
  const selectedRow = markerRows.find(r => r.site.name === site.name);
  if (selectedRow && map.hasLayer(selectedRow.marker)) selectedRow.marker.bringToFront();
  const layer = siteLayer(site);
  const months = projectMonthsForYear(selectedYear);
  const datasets = chartDatasetsForSite(site, months);
  const selectedHours = siteHours(site, selectedYear);
  const hourLabel = hourMetricLabel();
  const chartPanelLabel = 'Monthly Disruption Hours';
  const chartLabel = selectedYear === 'all'
    ? 'Monthly ' + hourLabel + ', August 2021 to May 2026. Zero-hour months indicate no posted disruption at this site.'
    : 'Monthly ' + hourLabel + ' within ' + selectedYear + '. Zero-hour months indicate no posted disruption at this site.';

  const headerHtml =
    '<button class="back-btn" id="backToList">&larr; Back to search/list</button>' +
    '<div class="panel-label">' + (siteHasDisruptionData(site) ? 'Site Detail' : 'AHS Location') + '</div>' +
    '<div class="site-name">' + displayName(site) + '</div>' +
    (siteIdNote(site) ? '<div class="period-note">' + siteIdNote(site) + '</div>' : '') +
    siteDetailViewControlHtml();

  if (!siteHasDisruptionData(site)) {
    const referenceStats = selectedYear === 'all'
      ? statBlock('Captured Hours', '0') + statBlock('Status', 'Reference') + statBlock('Analyzed Layer', serviceLabel())
      : statBlock(selectedYear + ' Hours', '0') + statBlock('All-Year Hours', '0') + statBlock('Status', 'Reference');
    const referenceDataHtml =
      '<div class="stats-row">' + referenceStats + '</div>' +
      '<div class="panel-label">Interpretation</div>' +
      '<div class="chart-caption">This AHS location is included as a searchable reference site, but no posted disruption hours were captured for the selected analyzed service layer in the August 2021 to May 2026 reconstruction.</div>';
    sidebar.innerHTML = headerHtml + (siteDetailView === 'nearby' ? nearbyAccessPanelHtml(site) : referenceDataHtml);
    document.getElementById('backToList').addEventListener('click', returnToList);
    bindSiteDetailViewControl();
    bindNearbyAccessControl();
    return;
  }

  const statsHtml = selectedYear === 'all'
    ? statBlock('Total Hours', fmtHours(siteHours(site, 'all'))) +
      statBlock('Years with Disruption', yearCount(site)) +
      statBlock('Months with Disruption', activeMonthCount(site, 'all'))
    : statBlock(selectedYear + ' Hours', fmtHours(selectedHours)) +
      statBlock('All-Year Hours', fmtHours(siteHours(site, 'all'))) +
      statBlock('Months with Disruption', activeMonthCount(site, selectedYear));
  const dataHtml =
    '<div class="stats-row">' + statsHtml + '</div>' +
    '<div class="panel-label">' + chartPanelLabel + '</div>' +
    '<div class="chart-wrap"><canvas id="tsChart"></canvas></div>' +
    '<div class="chart-caption">' + chartLabel + '</div>';
  sidebar.innerHTML = headerHtml + (siteDetailView === 'nearby' ? nearbyAccessPanelHtml(site) : dataHtml);
  document.getElementById('backToList').addEventListener('click', returnToList);
  bindSiteDetailViewControl();
  bindNearbyAccessControl();
  if (siteDetailView === 'nearby') return;
  const ctx = document.getElementById('tsChart').getContext('2d');
  activeChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: months,
      datasets: datasets
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: {
          display: selectedService === 'all',
          labels: { font: { family: 'Lato', size: 10 }, color: '#1B3A6B', boxWidth: 10, boxHeight: 10 }
        },
        tooltip: {
          enabled: false,
          external: externalChartTooltip,
          mode: selectedService === 'all' ? 'index' : 'nearest',
          intersect: selectedService !== 'all',
          filter: item => selectedService !== 'all' || Number(item.raw) > 0,
          callbacks: {
            title: items => fmtMonth(items[0].label),
            label: item => {
              const month = months[item.dataIndex];
              const layerId = (item.dataset && item.dataset.layerId) ? item.dataset.layerId : selectedService;
              const itemValue = Number(item.raw) || 0;
              if (selectedService === 'all') {
                return item.dataset.label + ': ' + Math.round(itemValue) + tooltipHourUnit();
              }
              const lines = [Math.round(itemValue) + tooltipHourUnit()];
              const detail = selectedService === 'all' && site.layers && site.layers[layerId]
                ? ((site.layers[layerId].monthly_details || {})[month] || null)
                : siteMonthDetail(site, month);
              if (detail) {
                if (detail.services && detail.services.length) {
                  pushWrappedTooltipLine(lines, 'Services down: ', detail.services, 54);
                }
              }
              return lines;
            }
          }
        }
      },
      scales: {
        x: {
          stacked: selectedService === 'all',
          ticks: {
            font: { family: 'Lato', size: 9 },
            color: '#5A6A85',
            autoSkip: selectedYear !== 'all',
            callback: function(val) {
              const lbl = this.getLabelForValue(val);
              if (selectedYear !== 'all') return lbl.slice(5);
              return lbl.endsWith('-01') ? lbl.slice(0, 4) : '';
            }
          },
          grid: { display: false }
        },
        y: {
          stacked: selectedService === 'all',
          beginAtZero: true,
          ticks: { font: { family: 'Lato', size: 9 }, color: '#5A6A85' },
          grid: { color: 'rgba(15,35,71,0.06)' },
          title: { display: true, text: selectedService === 'all' ? 'Service-hours' : 'Hours', font: { family: 'Lato', size: 10 }, color: '#1B3A6B' }
        }
      }
    }
  });
}
""",
    )
    text = _replace_function(
        text,
        "updateMarkers",
        """
function updateMarkers() {
  const maxH = currentScaleMaxHours();
  const filtering = hasActiveSearch();
  markerRows.forEach(row => {
    const eligible = showSiteInCurrentMode(row.site);
    const matchesSearch = !filtering || siteMatchesSearch(row.site, searchQuery);
    if (!eligible || !matchesSearch) {
      if (map.hasLayer(row.marker)) map.removeLayer(row.marker);
      return;
    }
    if (!map.hasLayer(row.marker)) row.marker.addTo(map);
    const isReferenceSite = !siteHasDisruptionData(row.site);
    const isSelectedSite = currentSite && currentSite.name === row.site.name;
    const h = siteHours(row.site, selectedYear);
    const r = isReferenceSite ? 4 : radiusFor(h, maxH);
    row.marker.setRadius(r);
    row.marker.setStyle(isReferenceSite ? {
      fillColor: '#D0D8E8',
      color: isSelectedSite ? '#0F2347' : '#5A6A85',
      weight: isSelectedSite ? 2 : 0.8,
      opacity: isSelectedSite ? 1 : 0.75,
      fillOpacity: isSelectedSite ? 0.6 : (filtering ? 0.7 : 0.35)
    } : {
      fillColor: '#3B82C4',
      color: '#0F2347',
      weight: isSelectedSite ? 3 : (h > 0 ? 1 : 0.5),
      opacity: isSelectedSite ? 1 : (h > 0 ? 0.9 : 0.35),
      fillOpacity: isSelectedSite ? 0.72 : (h > 0 ? 0.55 : 0.12)
    });
    row.marker.unbindTooltip();
    const tooltipText = isReferenceSite
      ? 'No posted data for the selected view'
      : fmtHours(h) + (selectedService === 'all' ? ' service-hours' : ' hours') + ' (' + periodLabel(selectedYear) + ')';
    row.marker.bindTooltip(
      '<div class="popup-title">' + displayName(row.site) + '</div>' +
      '<div class="popup-stat">' + tooltipText + '</div>',
      { direction: 'top', offset: [0, -r], opacity: 1 }
    );
  });
  updateNearestAccessLines(currentSite);
}
""",
    )
    text = _replace_function(
        text,
        "updateLegend",
        """
function updateLegend() {
  if (!legendDiv) return;
  const title = selectedService === 'all' ? 'Service Burden' : 'Disruption Burden';
  const unit = selectedService === 'all' ? ' service-hours' : ' h';
  const matched = currentSearchSites();
  if (hasActiveSearch() && !matched.length) {
    legendDiv.innerHTML =
      '<div class="legend-title">' + title + '</div>' +
      '<div class="legend-note">No matching mapped site for this search.</div>';
    return;
  }
  const maxH = currentScaleMaxHours();
  const rows = legendExamples(maxH).map(h => {
    const d = Math.round(radiusFor(h, maxH) * 2);
    return '<div class="legend-row"><span class="legend-dot" style="width:' + d + 'px;height:' + d + 'px;"></span><span>~' + fmtHours(h) + unit + '</span></div>';
  }).join('');
  const filterNote = hasActiveSearch()
    ? 'Bubbles and examples are filtered to the current search.'
    : 'Examples recalculate for the selected year and current visible map extent.';
  const zeroRow = showNoDataSites
    ? '<div class="legend-row"><span class="legend-dot" style="width:8px;height:8px;background:#D0D8E8;border-color:#5A6A85;opacity:0.75;"></span><span>No posted data</span></div>'
    : '';
  legendDiv.innerHTML =
    '<div class="legend-title">' + title + '</div>' +
    '<div class="legend-note">' + filterNote + '</div>' +
    rows + zeroRow;
}
""",
    )
    text = re.sub(
        r"const\s+isReferenceSite\s*=\s*site\.has_disruption_data\s*===\s*false;",
        "const isReferenceSite = !siteHasDisruptionData(site);",
        text,
    )
    text = re.sub(
        r"radius:\s*isReferenceSite\s*\?\s*4\s*:\s*radiusFor\(\s*site\.total_hours\s*,\s*Math\.max\(\s*\.\.\.DATA\.sites\.map\(s\s*=>\s*s\.total_hours\)\s*\)\s*\)",
        "radius: isReferenceSite ? 4 : radiusFor(siteHours(site, 'all'), Math.max(...DATA.sites.map(s => siteHours(s, 'all'))))",
        text,
    )
    text = _dedupe_function_definitions(
        text,
        [
            "serviceTypeConfig",
            "serviceLayerColor",
            "serviceLayerSortRank",
            "serviceLayerOrder",
        ],
    )
    return text


def _ensure_ed_access_css(text):
    marker = "/* Service-aware nearest-site access overlay. */"
    if marker in text:
        text, count = re.subn(
            r"/\* Service-aware nearest-site access overlay\. \*/.*?(?=\n/\*|\n</style>)",
            ED_ACCESS_CSS,
            text,
            count=1,
            flags=re.DOTALL,
        )
        return text
    text = re.sub(r"/\* ED nearest-site access overlay\. \*/.*?@media \(max-width: 768px\) \{\s*\.nearest-ed-row.*?\}\s*\}", "", text, count=1, flags=re.DOTALL)
    if "</style>" in text:
        return text.replace("</style>", "\n" + ED_ACCESS_CSS + "\n</style>", 1)
    return text


ED_ACCESS_JS = r"""
function effectiveNearbyAccessMode() {
  if (siteDetailView !== 'nearby') return 'off';
  return 'service';
}

function nearestAccessLayerId() {
  return selectedService || 'all';
}

function nearestAccessRows(site) {
  if (effectiveNearbyAccessMode() === 'off' || !site) return [];
  const layerId = nearestAccessLayerId();
  const access = site.nearest_access || {};
  const rows = Array.isArray(access[layerId])
    ? access[layerId]
    : (layerId === 'ed' && Array.isArray(site.ed_nearest_sites) ? site.ed_nearest_sites : []);
  return rows.slice(0, 3).map((row, idx) => {
    const target = DATA.sites.find(s => s.name === row.site_name);
    return Object.assign({}, row, {
      index: idx + 1,
      target: target || null
    });
  });
}

function nearestAccessLabel() {
  if (selectedService === 'all') return 'Nearest analyzed sites';
  if (selectedService === 'ed') return 'Nearest EDs';
  return 'Nearest ' + serviceLabel() + ' sites';
}

function siteDetailViewControlHtml() {
  const dataActive = siteDetailView !== 'nearby';
  const nearbyActive = siteDetailView === 'nearby';
  return '<div class="site-detail-view-control" role="group" aria-label="Site detail view">' +
    '<button type="button" class="site-detail-view-btn' + (dataActive ? ' active' : '') + '" data-site-detail-view="data" aria-pressed="' + dataActive + '">Data</button>' +
    '<button type="button" class="site-detail-view-btn' + (nearbyActive ? ' active' : '') + '" data-site-detail-view="nearby" aria-pressed="' + nearbyActive + '">Nearby Sites</button>' +
  '</div>';
}

function bindSiteDetailViewControl() {
  document.querySelectorAll('[data-site-detail-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      const next = btn.getAttribute('data-site-detail-view') || 'data';
      siteDetailView = next === 'nearby' ? 'nearby' : 'data';
      if (currentSite) showSite(currentSite);
      else updateNearestAccessLines(null);
    });
  });
}

function bindNearbyAccessControl() {
  document.querySelectorAll('[data-nearest-site-name]').forEach(button => {
    button.addEventListener('click', () => {
      const siteName = button.getAttribute('data-nearest-site-name');
      const target = DATA.sites.find(candidate => candidate.name === siteName);
      if (target) focusSite(target);
    });
  });
}

function nearbyAccessPanelHtml(site) {
  const panel = nearestAccessHtml(site);
  if (panel) return panel;
  return '<div class="panel-label">' + escapeHtml(nearestAccessLabel()) + '</div>' +
    '<div class="chart-caption">No nearby-site candidates are available for this site and selected service.</div>';
}

function nearestAccessBurdenText(target) {
  if (!target) return '';
  const hours = Number(siteHours(target, selectedYear)) || 0;
  const scope = selectedService === 'all' ? 'All analyzed services' : serviceLabel();
  const hourUnit = selectedService === 'all' ? ' service-hours' : ' h';
  const period = periodLabel(selectedYear);
  if (hours > 0) {
    return scope + ' burden, ' + period + ': ' + fmtHours(hours) + hourUnit;
  }
  return scope + ' burden, ' + period + ': no posted disruption data';
}

function nearestAccessSourceHtml() {
  return '<div class="nearest-service-source">Sites are ranked by road distance. Linear is great-circle distance between mapped site coordinates; road km and drive minutes use an OSRM car-profile table over the Geofabrik Alberta OpenStreetMap extract. Sources: ' +
    '<a href="https://www.albertahealthservices.ca/findhealth/" target="_blank" rel="noopener">AHS Find Healthcare directory service listings</a> (retrieved 2026-07-26), ' +
    '<a href="https://project-osrm.org/docs/v5.24.0/api/" target="_blank" rel="noopener">OSRM table API</a>, ' +
    '<a href="https://download.geofabrik.de/north-america/canada/alberta.html" target="_blank" rel="noopener">Geofabrik Alberta extract</a>, ' +
    '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>.</div>';
}

function nearestAccessHtml(site) {
  const rows = nearestAccessRows(site);
  if (!rows.length) return '';
  return '<div class="panel-label">' + escapeHtml(nearestAccessLabel()) + '</div>' +
    '<div class="nearest-service-list">' +
      rows.map(row => {
        const linear = Number(row.linear_km);
        const road = Number(row.road_km);
        const roadMinutes = Number(row.road_minutes);
        const parts = [];
        if (Number.isFinite(road)) parts.push(road.toFixed(1) + ' km road' + (Number.isFinite(roadMinutes) ? ', ' + Math.round(roadMinutes) + ' min' : ''));
        if (Number.isFinite(linear)) parts.push(linear.toFixed(1) + ' km linear');
        return '<div class="nearest-service-row">' +
          '<div class="nearest-service-index">' + row.index + '</div>' +
          '<button type="button" class="nearest-service-name nearest-service-link" data-nearest-site-name="' + escapeHtml(row.site_name || '') + '">' +
            escapeHtml(row.display_name || row.site_name || '') +
          '</button>' +
          '<div class="nearest-service-meta">' + escapeHtml(parts.join(' / ') || 'distance n/a') + '</div>' +
          '<div class="nearest-service-burden">' + escapeHtml(nearestAccessBurdenText(row.target)) + '</div>' +
        '</div>';
      }).join('') +
    '</div>' +
    nearestAccessSourceHtml();
}

function updateNearestAccessLines(site) {
  if (typeof nearestAccessLineLayer === 'undefined') return;
  nearestAccessLineLayer.clearLayers();
  if (effectiveNearbyAccessMode() === 'off' || !site) return;
  const origin = displayLatLng(site);
  nearestAccessRows(site).forEach(row => {
    if (!row.target) return;
    const targetLatLng = displayLatLng(row.target);
    const line = L.polyline([origin, targetLatLng], {
      color: '#5A6A85',
      weight: 2,
      opacity: 0.62,
      dashArray: '4 7',
      interactive: false
    });
    line.addTo(nearestAccessLineLayer);
  });
}
""".strip()


RELEASE_HARDENING_CSS = r"""
/* RELEASE_HARDENING_START */
:root { --ink: #0F2347; }
body { grid-template-rows: auto clamp(500px, calc(100vh - 150px), 600px) auto; height: auto; }
#map { min-height: 0; }
.sidebar { overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.runtime-fallback { margin: 1rem; padding: 0.8rem 1rem; border: 1px solid var(--rule); background: #fff; color: var(--mid); font-size: 0.82rem; line-height: 1.4; }
.leaflet-container .leaflet-interactive:focus { outline: 3px solid #216B9E; outline-offset: 2px; }
@media (max-width: 768px) { body { grid-template-rows: auto auto auto auto; } #map { min-height: 240px; } .sidebar { overflow: visible; } }
/* RELEASE_HARDENING_END */
""".strip()


RELEASE_HARDENING_BOOTSTRAP = r"""
/* RELEASE_HARDENING_BOOTSTRAP_START */
if (typeof window.Chart === 'undefined') {
  window.__sorcChartUnavailable = true;
  window.Chart = function() {
    return { options: { plugins: { legend: { display: false } } }, destroy: function(){}, update: function(){} };
  };
}
/* RELEASE_HARDENING_BOOTSTRAP_END */
""".strip()


def _upsert_marked_block(text, start_marker, end_marker, block, anchor):
    text = re.sub(re.escape(start_marker) + r".*?" + re.escape(end_marker), "", text, flags=re.DOTALL)
    return text.replace(anchor, block + "\n" + anchor, 1)


def _apply_release_hardening(text):
    """Apply release-level layout, terminology, accessibility, and dependency safeguards."""
    text = _upsert_marked_block(
        text,
        "/* RELEASE_HARDENING_START */",
        "/* RELEASE_HARDENING_END */",
        "<style>\n" + RELEASE_HARDENING_CSS + "\n</style>",
        "</head>",
    )
    text = _upsert_marked_block(
        text,
        "/* RELEASE_HARDENING_BOOTSTRAP_START */",
        "/* RELEASE_HARDENING_BOOTSTRAP_END */",
        "<script>\n" + RELEASE_HARDENING_BOOTSTRAP + "\n</script>",
        "</head>",
    )
    text = re.sub(r"<style>\s*</style>\s*", "", text)
    text = re.sub(r"<script>\s*</script>\s*", "", text)
    text = text.replace('<option value="ed">Emergency Department</option>', '<option value="ed">Emergency</option>')
    text = text.replace('<option value="surgery">Surgery/operative capability</option>', '<option value="surgery">Surgery</option>')
    text = text.replace(
        "All analyzed services reports additive disruption hours across completed service layers.",
        "All analyzed services reports additive service-hours across completed service layers.",
    )
    text = re.sub(r"(?:\n\s*siteDetailView = 'data';){2,}", "\n  siteDetailView = 'data';", text)
    text = re.sub(r"(?:\n\s*updateObSubsectionControl\(\);){2,}", "\n  updateObSubsectionControl();", text)
    text = re.sub(r"(?:\n\s*if \(!map\) return;){2,}", "\n  if (!map) return;", text)
    text = _dedupe_function_definitions(
        text,
        (
            "serviceLayerDisplayName",
            "hourMetricLabel",
            "tooltipHourUnit",
            "wrapTooltipText",
            "pushWrappedTooltipLine",
        ),
    )
    text = _replace_function(
        text,
        "hourMetricLabel",
        """function hourMetricLabel() {
  return selectedService === 'all' ? 'service-hours' : 'disruption hours';
}""",
    )
    text = _replace_function(
        text,
        "nearestAccessSourceHtml",
        """function nearestAccessSourceHtml() {
  return '<details class="nearest-service-source"><summary>Distance sources</summary><div>Sites are ranked by road distance. Linear is great-circle distance between mapped site coordinates; road km and drive minutes use an OSRM car-profile table over the Geofabrik Alberta OpenStreetMap extract. Sources: ' +
    '<a href="https://www.albertahealthservices.ca/findhealth/" target="_blank" rel="noopener">AHS Find Healthcare directory service listings</a> (checked 2026-08-01; Westview Health Centre, Stony Plain was carried forward from the prior verified directory record), ' +
    '<a href="https://project-osrm.org/docs/v5.24.0/api/" target="_blank" rel="noopener">OSRM table API</a>, ' +
    '<a href="https://download.geofabrik.de/north-america/canada/alberta.html" target="_blank" rel="noopener">Geofabrik Alberta extract</a>, ' +
    '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>.</div></details>';
}""",
    )
    text = text.replace(
        "const chartPanelLabel = 'Monthly Disruption Hours';",
        "const chartPanelLabel = selectedService === 'all' ? 'Monthly Service-Hours' : 'Monthly Disruption Hours';",
    )
    text = text.replace(
        "statBlock('Total Hours', fmtHours(siteHours(site, 'all')))",
        "statBlock(selectedService === 'all' ? 'Total Service-Hours' : 'Total Hours', fmtHours(siteHours(site, 'all')))",
    )
    text = text.replace(
        "statBlock(selectedYear + ' Hours', fmtHours(selectedHours))",
        "statBlock(selectedYear + (selectedService === 'all' ? ' Service-Hours' : ' Hours'), fmtHours(selectedHours))",
    )
    text = text.replace(
        "statBlock('All-Year Hours', fmtHours(siteHours(site, 'all')))",
        "statBlock(selectedService === 'all' ? 'All-Year Service-Hours' : 'All-Year Hours', fmtHours(siteHours(site, 'all')))",
    )
    text = text.replace(
        "'<strong>' + fmtHours(stats.hours) + '</strong>' + hourMetricLabel() +\n     '<strong>' + fmtHours(stats.affected_sites) + '</strong>sites affected';",
        "'<strong>' + fmtHours(stats.hours) + '</strong> ' + hourMetricLabel() + ' ' +\n     '<strong>' + fmtHours(stats.affected_sites) + '</strong> sites affected';",
    )
    text = text.replace(
        ": (row.hours > 0 ? fmtHours(row.hours) + ' h' : '0 h in view');",
        ": (row.hours > 0 ? fmtHours(row.hours) + (selectedService === 'all' ? ' service-hours' : ' h') : '0 ' + (selectedService === 'all' ? 'service-hours' : 'h') + ' in view');",
    )
    text = text.replace(
        "const allYearText = selectedYear === 'all' ? '' : ' <span class=\"rank-hours\">(' + fmtHours(row.total) + ' h all-year)</span>';",
        "const allYearText = selectedYear === 'all' ? '' : ' <span class=\"rank-hours\">(' + fmtHours(row.total) + (selectedService === 'all' ? ' service-hours all-year' : ' h all-year') + ')</span>';",
    )
    text = text.replace(
        "'<button type=\"button\" class=\"site-detail-view-btn' + (dataActive ? ' active' : '') + '\" data-site-detail-view=\"data\">Data</button>' +\n"
        "    '<button type=\"button\" class=\"site-detail-view-btn' + (nearbyActive ? ' active' : '') + '\" data-site-detail-view=\"nearby\">Nearby Sites</button>' +",
        "'<button type=\"button\" class=\"site-detail-view-btn' + (dataActive ? ' active' : '') + '\" data-site-detail-view=\"data\" aria-pressed=\"' + dataActive + '\">Data</button>' +\n"
        "    '<button type=\"button\" class=\"site-detail-view-btn' + (nearbyActive ? ' active' : '') + '\" data-site-detail-view=\"nearby\" aria-pressed=\"' + nearbyActive + '\">Nearby Sites</button>' +",
    )
    text = re.sub(
        r'(<div class="total-stat" id="summaryStats"[^>]*>).*?(</div>)',
        r'\1<strong>982,657</strong> service-hours <strong>80</strong> sites affected\2',
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = re.sub(
        r'(\s*<h1 class="sr-only">SORCTracks Alberta service disruption data</h1>){2,}',
        '\n    <h1 class="sr-only">SORCTracks Alberta service disruption data</h1>',
        text,
    )
    if '<h1 class="sr-only">SORCTracks Alberta service disruption data</h1>' not in text:
        text = text.replace('<header>', '<header>\n    <h1 class="sr-only">SORCTracks Alberta service disruption data</h1>', 1)
    text = text.replace(
        '<div>\n      <div class="app-title">SORCTracks</div>\n      <div class="subtitle">Alberta publicly posted service disruptions, August 2021 to June 2026</div>',
        '<div>\n      <div class="subtitle">Reconstructed from publicly archived AHS postings, August 2021 to June 2026</div>',
    )
    text = text.replace('<div class="control-pair" aria-label="Map filters">', '<div class="control-pair" role="group" aria-label="Map filters">')
    text = text.replace('<div id="map"></div>', '<div id="map" role="region" aria-label="Map of Alberta service disruption locations"></div>', 1)
    text = text.replace('<div class="sidebar" id="sidebar">', '<main class="sidebar" id="sidebar" aria-label="Service disruption data and site details">', 1)
    text = text.replace('<div class="empty-state"></div>\n  </div>\n  <footer>', '<div class="empty-state"></div>\n  </main>\n  <footer>', 1)
    text = text.replace(
        "const map = L.map('map', { zoomControl: true }).setView([54.0, -114.5], 6);\nL.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {\n  attribution: '&copy; OpenStreetMap, &copy; CARTO',\n  maxZoom: 18\n}).addTo(map);",
        """const mapRuntimeAvailable = typeof L !== 'undefined' && typeof L.map === 'function';
const map = mapRuntimeAvailable ? L.map('map', { zoomControl: true }).setView([54.0, -114.5], 6) : null;
if (map) {
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OpenStreetMap, &copy; CARTO',
    maxZoom: 18
  }).addTo(map);
} else {
  document.getElementById('map').innerHTML = '<div class="runtime-fallback" role="status">Map display is unavailable. The searchable data and monthly tables remain available.</div>';
}""",
    )
    text = text.replace(
        "const nearestAccessLineLayer = L.layerGroup().addTo(map);",
        "const nearestAccessLineLayer = map && typeof L.layerGroup === 'function' ? L.layerGroup().addTo(map) : null;",
    )
    text = text.replace("if (typeof nearestAccessLineLayer === 'undefined') return;", "if (!map || !nearestAccessLineLayer || typeof L === 'undefined') return;")
    text = text.replace("if (row) {\n    if (!map.hasLayer(row.marker))", "if (row && map) {\n    if (!map.hasLayer(row.marker))")
    if "function fitToSearchResults() {\n  if (!map || typeof L === 'undefined') return;" not in text:
        text = text.replace("function fitToSearchResults() {", "function fitToSearchResults() {\n  if (!map || typeof L === 'undefined') return;")
    text = text.replace("if (selectedRow && map.hasLayer(selectedRow.marker))", "if (map && selectedRow && map.hasLayer(selectedRow.marker))")
    update_markers_prefix = "function updateMarkers() {\n"
    update_markers_guard = "  if (!map) return;\n"
    if update_markers_prefix + update_markers_guard not in text:
        text = text.replace(update_markers_prefix, update_markers_prefix + update_markers_guard, 1)
    text = re.sub(
        r"(function updateMarkers\(\) \{\n  if \(!map\) return;\n)(?:  if \(!map\) return;\n)+",
        r"\1",
        text,
        count=1,
    )
    if "if (map) {\nDATA.sites.forEach(site => {" not in text:
        text = text.replace(
            "DATA.sites.forEach(site => {\n  const ll = displayLatLng(site);\n  const isReferenceSite = !siteHasDisruptionData(site);\n  const marker = L.circleMarker",
            "if (map) {\nDATA.sites.forEach(site => {\n  const ll = displayLatLng(site);\n  const isReferenceSite = !siteHasDisruptionData(site);\n  const marker = L.circleMarker",
            1,
        )
        text = text.replace(
            "  markerRows.push({ site, marker });\n});\nlet legendDiv = null;",
            "  markerRows.push({ site, marker });\n});\n}\nlet legendDiv = null;",
            1,
        )
    text = re.sub(
        r"let legendDiv = null;\nconst legend = L\.control\(\{ position: 'bottomleft' \}\);\nlegend\.onAdd = function\(\) \{(.*?)\n\};\nlegend\.addTo\(map\);",
        "let legendDiv = null;\nif (map) {\n  const legend = L.control({ position: 'bottomleft' });\n  legend.onAdd = function() {\\1\n  };\n  legend.addTo(map);\n}",
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?:if \(map\)\s*)*map\.on\('zoomend moveend resize', refreshMapScale\);",
        "if (map) map.on('zoomend moveend resize', refreshMapScale);",
        text,
        count=1,
    )
    text = text.replace("if (canvas && datasets.length) {\n    activeChart = new Chart", "if (canvas && datasets.length && !window.__sorcChartUnavailable) {\n    activeChart = new Chart")
    text = text.replace("const ctx = document.getElementById('tsChart').getContext('2d');\n  activeChart = new Chart", "const ctx = document.getElementById('tsChart').getContext('2d');\n  if (!window.__sorcChartUnavailable) activeChart = new Chart")
    text = text.replace(
        "'<div class=\"search-panel\">' +\n      '<div class=\"panel-label\">Find an AHS Location</div>' +\n      '<input id=\"siteSearch\"",
        "'<div class=\"search-panel\">' +\n      '<div class=\"panel-label\">Find an AHS Location</div>' +\n      '<input id=\"siteSearch\"",
    )
    if "totalMatches + ' matching locations'" not in text:
        text = text.replace(
            "'</div>' +\n    '<div class=\"panel-label\">Alberta Overview</div>' +",
            "'</div>' +\n    '<div class=\"result-count\" role=\"status\" aria-live=\"polite\">' + (hasQuery ? totalMatches + ' matching locations' : '') + '</div>' +\n    '<div class=\"panel-label\">Alberta Overview</div>' +",
            1,
        )
    return re.sub(r"\n{3,}", "\n\n", text)


def write_new_html(current_html_path, new_data, output_path, update_date,
                   year_summary=None, old_data_range=None, new_data_range=None,
                   service_year_summary_by_layer=None, service_meta=None,
                   ob_subsection_meta=None, ed_access_config=None,
                   preserve_existing_runtime=False):
    """Write a new HTML file with all dynamic content patched."""
    text = Path(current_html_path).read_text(encoding="utf-8")
    ed_access_config = ed_access_config or {}
    new_data = attach_ed_nearest_access(new_data, **ed_access_config)

    # Swap DATA block
    found = _find_json_block(text, DATA_PREFIX)
    if not found:
        raise ValueError("DATA block not found while writing.")
    data_start, data_end, _ = found
    new_data_block = DATA_PREFIX + json.dumps(new_data, separators=(",", ":")) + ";"
    text = text[:data_start] + new_data_block + text[data_end:]

    # Swap YEAR_SUMMARY block
    if year_summary is not None:
        found_ys = _find_json_block(text, YEAR_SUMMARY_PREFIX)
        if found_ys:
            ys_start, ys_end, _ = found_ys
            new_ys_block = YEAR_SUMMARY_PREFIX + json.dumps(year_summary, separators=(",", ":")) + ";"
            text = text[:ys_start] + new_ys_block + text[ys_end:]

    # Add/replace service-layer summary metadata for the layer-aware runtime.
    if service_year_summary_by_layer is not None:
        service_meta = service_meta or SERVICE_LAYER_META_DEFAULT
        text = _upsert_js_json_block(text, SERVICE_YEAR_SUMMARY_PREFIX, service_year_summary_by_layer)
        text = _upsert_js_json_block(text, SERVICE_LAYER_META_PREFIX, service_meta)
        service_type_meta = {}
        if ob_subsection_meta:
            if isinstance(ob_subsection_meta, str):
                ob_subsection_meta = json.loads(ob_subsection_meta)
            text = _upsert_js_json_block(text, OB_SUBSECTION_META_PREFIX, ob_subsection_meta)
            service_type_meta["ob"] = ob_subsection_meta
        if service_meta and "acute" in service_meta:
            service_type_meta["acute"] = ACUTE_TYPE_META
        if service_meta and "surgery" in service_meta:
            service_type_meta["surgery"] = SURGERY_TYPE_META
        if service_meta and "other" in service_meta:
            service_type_meta["other"] = OTHER_TYPE_META
        if service_type_meta:
            text = _upsert_js_json_block(text, SERVICE_TYPE_META_PREFIX, service_type_meta)
        has_service_runtime = "function siteLayer(site, layerId)" in text
        if not (preserve_existing_runtime and has_service_runtime):
            text = ensure_service_layer_runtime(text, service_meta)

    # Update date-range strings (subtitle, chart captions, footer partial-year, JS loop)
    if old_data_range and new_data_range:
        text = update_date_strings(text, old_data_range, new_data_range)

    # Preserve a validated headline during classification-only rebuilds. A true
    # data update still refreshes it because the annual summary changes.
    existing_year_summary = parse_existing_year_summary(current_html_path) if preserve_existing_runtime else None
    if year_summary is not None and (not preserve_existing_runtime or existing_year_summary != year_summary):
        static_hour_label = "disruption hours"
        text = update_static_summary(text, year_summary, hour_label=static_hour_label)

    # Update the footer "reference layer includes X searchable AHS locations" number
    text = update_reference_count(text, len(new_data.get("sites", [])))
    if not preserve_existing_runtime:
        text = update_archive_scope_note(text, service_meta=service_meta if service_year_summary_by_layer is not None else None)

    # Rebuild the year picker <option> tags to match the actual years present
    if new_data_range and new_data_range.get("start_year") and new_data_range.get("end_year"):
        years = list(range(int(new_data_range["start_year"]), int(new_data_range["end_year"]) + 1))
        text = update_year_picker_options(text, years)

    # Update the footer "Updated" date
    text = UPDATED_PATTERN.sub(rf"\1Updated {update_date}\2", text, count=1)

    # Update the SORC copyright year in the footer to the current year
    from datetime import datetime as _dt
    text = COPYRIGHT_PATTERN.sub(rf"\g<1>{_dt.now().year}", text)
    text = _apply_release_hardening(text)

    Path(output_path).write_text(text, encoding="utf-8")


def _round_hours(x):
    """Match the rounding behavior in the existing HTML (mostly half-up, some long decimals)."""
    if x is None:
        return 0.0
    # Keep up to 4 decimals to match existing precision but drop trailing zeros
    return round(float(x), 4)


def _all_years_from_episodes(episodes_by_year_by_site):
    years = set()
    for d in episodes_by_year_by_site.values():
        years.update(d.keys())
    if not years:
        return []
    return sorted(int(y) for y in years)


def _zero_year_map(years):
    return {str(y): 0 for y in years}

