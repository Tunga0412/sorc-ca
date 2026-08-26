"""Build the SORCTracks Live static page from the current AHS disruption page.

The live page intentionally contains only current-page rows and stable site
metadata (names and coordinates). It does not embed the historical DATA,
YEAR_SUMMARY, or monthly archive values from the normal SORCTracks page.
"""

from __future__ import annotations

import argparse
import difflib
import html
import importlib.util
import json
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import html_patcher


LIVE_URL = "https://www.albertahealthservices.ca/br/Page17594.aspx"
LOCAL_TZ = ZoneInfo("America/Edmonton")
MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"), 1
)}
MONTH_PATTERN = "|".join(MONTHS)


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _row_detail_text(row: dict) -> str:
    return " ".join(
        _text(row.get(key)) for key in
        ("bed_or_space_reduction_text", "reason_text", "raw_block_text")
    )


def _has_schedule_cue(row: dict) -> bool:
    """Identify wording that requires a machine-readable time schedule."""
    text = _row_detail_text(row)
    if not text:
        return False
    weekday = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"
    return bool(re.search(
        rf"\b(?:daily|nightly|overnight|each\s+night|regular\s+operating\s+hours|adjusted\s+hours|weekends?|weekdays?|closed\s+(?:from|on)|open\s+(?:{weekday}|24\s*hours?|24/7)|{weekday})\b",
        text,
        flags=re.I,
    ))


def _service_layer(value: object) -> str:
    """Map Live service labels to the same filter layers as SORCTracks."""
    text = _norm(value)
    if any(token in text for token in ("emergency", "emergency department", "ed ")):
        return "ed"
    if any(token in text for token in ("obstetric", "maternity", "labour and delivery", "labor and delivery")):
        return "ob"
    if any(token in text for token in ("acute", "inpatient", "medical unit", "medicine unit")):
        return "acute"
    if any(token in text for token in ("surgery", "operating room", "operative", "anesthesia", "anaesthesia")):
        return "surgery"
    return "other"


def _load_base_script(path: Path):
    spec = importlib.util.spec_from_file_location("sorc_live_ahs_base", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load AHS base script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _site_lookup(existing_data: dict) -> tuple[dict[str, str], dict[str, dict]]:
    lookup: dict[str, str] = {}
    sites: dict[str, dict] = {}
    for site in existing_data.get("sites", []):
        canonical = _norm(site.get("name"))
        if not canonical:
            continue
        sites[canonical] = site
        values = [site.get("name"), site.get("display_name"), site.get("facility_name"), site.get("community")]
        values.extend(site.get("aliases") or [])
        for value in values:
            key = _norm(value)
            if key:
                lookup.setdefault(key, canonical)
    return lookup, sites


def _resolve_site(row: dict, lookup: dict[str, str], sites: dict[str, dict]) -> str | None:
    candidates = [row.get("facility_name"), row.get("community_heading")]
    normalized = [_norm(value) for value in candidates if _norm(value)]
    for value in normalized:
        if value in lookup:
            return lookup[value]

    # Use a conservative unique containment match for labels such as
    # "AHS - Grande Prairie Regional Hospital".
    possible = []
    for value in normalized:
        matches = [alias for alias in lookup if len(alias) >= 5 and (alias in value or value in alias)]
        possible.extend(lookup[m] for m in matches)
    unique = sorted(set(possible))
    if len(unique) == 1:
        return unique[0]

    # A high-confidence similarity fallback catches minor punctuation/name
    # changes while refusing ambiguous matches.
    for value in normalized:
        close = difflib.get_close_matches(value, list(lookup), n=2, cutoff=0.92)
        targets = sorted(set(lookup[item] for item in close))
        if len(targets) == 1:
            return targets[0]
    return None


def _parse_date(value: object) -> pd.Timestamp | None:
    text = _text(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).normalize()


def _as_of_datetime(as_of: str | None) -> datetime:
    if not as_of:
        return datetime.now(LOCAL_TZ)
    parsed = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Invalid --as-of value: {as_of}")
    value = parsed.to_pydatetime()
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def _parse_clock(value: str) -> time | None:
    text = re.sub(r"\s+", " ", value.strip().lower())
    text = text.replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem:
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _schedule_windows(text: str, as_of: datetime) -> list[tuple[datetime, datetime]]:
    """Extract explicit date/time windows printed in AHS notices.

    AHS commonly writes overnight notices like “August 4 (8 pm) - August 5
    (8 am)”. These windows are used only to label a row; the original notice
    text remains in the page for the user to verify.
    """
    pattern = re.compile(
        rf"(?P<sm>{MONTH_PATTERN})\s+(?P<sd>\d{{1,2}})(?:,\s*(?P<sy>\d{{4}}))?\s*"
        rf"\(\s*(?P<st>[^)]+)\s*\)\s*(?:-|–|—|to)\s*"
        rf"(?:(?P<em>{MONTH_PATTERN})\s+)?(?P<ed>\d{{1,2}})(?:,\s*(?P<ey>\d{{4}}))?\s*"
        rf"\(\s*(?P<et>[^)]+)\s*\)",
        re.IGNORECASE,
    )
    windows: list[tuple[datetime, datetime]] = []
    for match in pattern.finditer(text):
        start_clock = _parse_clock(match.group("st"))
        end_clock = _parse_clock(match.group("et"))
        if start_clock is None or end_clock is None:
            continue
        year = int(match.group("sy") or match.group("ey") or as_of.year)
        end_year = int(match.group("ey") or year)
        start_month = MONTHS[match.group("sm").lower()]
        end_month = MONTHS[(match.group("em") or match.group("sm")).lower()]
        try:
            start = datetime(year, start_month, int(match.group("sd")), tzinfo=LOCAL_TZ).replace(
                hour=start_clock.hour, minute=start_clock.minute
            )
            end = datetime(end_year, end_month, int(match.group("ed")), tzinfo=LOCAL_TZ).replace(
                hour=end_clock.hour, minute=end_clock.minute
            )
        except ValueError:
            continue
        if end <= start:
            end += timedelta(days=1)
        windows.append((start, end))
    return windows


def _local_datetime(value: object) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    parsed = pd.Timestamp(value).to_pydatetime()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def _regular_hours_complement_windows(row: dict, as_of: datetime, base=None) -> list[tuple[datetime, datetime]]:
    """Interpret listed regular operating hours as the open-time complement.

    Some current notices say the department is closed and then list its regular
    operating hours (for example, Monday-Thursday 0900-1700 and Friday 0900-1200).
    For Live status, the disruption is outside those open windows, including
    omitted weekend days.
    """
    text = " ".join(
        _text(row.get(key)) for key in
        ("bed_or_space_reduction_text", "reason_text", "raw_block_text")
    )
    if not re.search(r"\bregular\s+operating\s+hours\s+are\b", text, flags=re.I):
        return []
    if re.search(r"\b(?:24\s*hours?|24/7)\b", text, flags=re.I):
        return []
    parser = getattr(base, "parse_open_hours_schedule", None)
    complement = getattr(base, "complement_closure_intervals_for_day", None)
    if parser is None or complement is None:
        return []
    try:
        schedules = parser(text) or {}
    except Exception:
        return []
    start_date = _parse_date(row.get("start_date_text"))
    end_date = _parse_date(row.get("anticipated_end_date_text")) or pd.Timestamp(as_of.date() + timedelta(days=366))
    if start_date is None or not schedules:
        return []
    windows: list[tuple[datetime, datetime]] = []
    current = start_date.date()
    while current <= end_date.date():
        day_ts = pd.Timestamp(current)
        open_blocks = schedules.get(day_ts.weekday(), [])
        blocks = complement(day_ts, open_blocks) if open_blocks else [(day_ts, day_ts + pd.Timedelta(days=1))]
        for start, end in blocks:
            local_start = _local_datetime(start)
            local_end = _local_datetime(end)
            if local_start and local_end and local_end > local_start:
                windows.append((local_start, local_end))
        current += timedelta(days=1)
    return windows


def _runtime_windows(row: dict, as_of: datetime, base=None) -> list[tuple[datetime, datetime]]:
    """Return local-time windows for browser-side Active now classification."""
    regular_complement = _regular_hours_complement_windows(row, as_of, base=base)
    if regular_complement:
        return regular_complement
    interval_builder = getattr(base, "build_episode_intervals", None)
    if interval_builder is not None:
        try:
            # The daily Live scraper returns the public date fields as text, while
            # the established v84 interval builder expects its cleaned date
            # columns. Without these fields, recurring notices such as
            # “closed 2100-0900 daily” fall through to a broad all-day window.
            parser_row = dict(row)
            start_date = _parse_date(row.get("start_date_text"))
            end_date = _parse_date(row.get("anticipated_end_date_text"))
            # Recurring notices sometimes have no stated end date (TBD). Give
            # the established schedule parser a bounded working horizon while
            # keeping the original open-ended date fields unchanged in LIVE_DATA.
            parser_end_date = end_date or pd.Timestamp(as_of.date() + timedelta(days=366))
            parser_row["start_date_parsed_clean"] = start_date if start_date is not None else pd.NaT
            parser_row["anticipated_end_date_parsed_clean"] = parser_end_date
            parser_row["start_date_parsed"] = parser_row["start_date_parsed_clean"]
            intervals = interval_builder(parser_row) or []
        except Exception:
            intervals = []
        parsed = [
            (_local_datetime(item.get("interval_start")), _local_datetime(item.get("interval_end")))
            for item in intervals
        ]
        parsed = [(start, end) for start, end in parsed if start and end]
        if parsed:
            return parsed

    detail_text = _row_detail_text(row)
    explicit = _schedule_windows(detail_text, as_of)
    if explicit:
        return explicit

    # A notice with only start/end dates is treated as an all-day window. The
    # browser will later replace its status as the local date changes.
    start_date = _parse_date(row.get("start_date_text"))
    end_date = _parse_date(row.get("anticipated_end_date_text"))
    if start_date is None and end_date is None:
        return []
    if end_date is None:
        # Preserve an open-ended date-only notice as active; the browser will
        # use the blank end_date_key to keep it active until a later scrape.
        return []
    start = datetime.combine((start_date or end_date).date(), time.min, tzinfo=LOCAL_TZ)
    end_day = (end_date or start_date).date() + timedelta(days=1)
    end = datetime.combine(end_day, time.min, tzinfo=LOCAL_TZ)
    return [(start, end)]


def _classify_row(row: dict, as_of: datetime, base=None) -> str | None:
    broad_start = _parse_date(row.get("start_date_text"))
    broad_end = _parse_date(row.get("anticipated_end_date_text"))
    today = pd.Timestamp(as_of.date())
    if broad_start is not None and broad_start > today:
        return "scheduled"
    if broad_end is not None and broad_end < today:
        return None

    windows = _runtime_windows(row, as_of, base=base)
    if windows:
        if any(start <= as_of < end for start, end in windows):
            return "active_now"
        if any(start > as_of for start, _ in windows):
            return "scheduled"
        return None

    # Without a printed time window the current AHS page is authoritative for
    # the date range, so retain the row as active now. This is also the safe
    # fallback for notices whose current status is not machine-readable.
    return "active_now"


def _row_to_disruption(row: dict, status: str, schedule_windows: list[tuple[datetime, datetime]] | None = None) -> dict:
    service = _text(row.get("program_or_service")) or "Service disruption"
    reason = _text(row.get("reason_text"))
    reduction = _text(row.get("bed_or_space_reduction_text"))
    raw = _text(row.get("raw_block_text"))
    return {
        "service": service,
        "service_layer": _service_layer(service),
        "reduction": reduction,
        "reason": reason,
        "start": _text(row.get("start_date_text")),
        "end": _text(row.get("anticipated_end_date_text")),
        "status": status,
        "status_label": "Active now" if status == "active_now" else "Scheduled / recurring",
        "start_date_key": (_parse_date(row.get("start_date_text")) or pd.Timestamp.min).strftime("%Y-%m-%d") if _parse_date(row.get("start_date_text")) is not None else "",
        "end_date_key": (_parse_date(row.get("anticipated_end_date_text")) or pd.Timestamp.min).strftime("%Y-%m-%d") if _parse_date(row.get("anticipated_end_date_text")) is not None else "",
        "schedule_windows": [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in (schedule_windows or [])
        ],
        "details": raw or "; ".join(x for x in (service, reduction, reason) if x),
        "source_url": _text(row.get("snapshot_url")) or LIVE_URL,
    }


def build_live_data(template_path: Path, base_script: Path, as_of: str | None = None) -> tuple[dict, list[dict]]:
    existing_data = html_patcher.parse_existing_data(template_path)
    lookup, sites = _site_lookup(existing_data)
    live_moment = _as_of_datetime(as_of)
    live_date = pd.Timestamp(live_moment.date()).normalize()
    base = _load_base_script(base_script)
    live_url = getattr(base, "LIVE_CURRENT_URL", LIVE_URL)
    rows = base.scrape_snapshot(live_date, live_url)
    if rows is None or rows.empty:
        raise RuntimeError("The AHS current disruption page returned no rows; refusing to publish an empty Live page.")

    grouped: dict[str, dict] = {}
    unmapped: list[dict] = []
    unparsed_schedules: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows.to_dict(orient="records"):
        status = _classify_row(row, live_moment, base=base)
        if status is None:
            continue
        runtime_windows = _runtime_windows(row, live_moment, base=base)
        if _has_schedule_cue(row) and not runtime_windows:
            unparsed_schedules.append({
                "facility_name": _text(row.get("facility_name")),
                "community_heading": _text(row.get("community_heading")),
                "service": _text(row.get("program_or_service")),
                "details": _row_detail_text(row)[:240],
            })
            continue
        canonical = _resolve_site(row, lookup, sites)
        if canonical is None:
            unmapped.append({
                "facility_name": _text(row.get("facility_name")),
                "community_heading": _text(row.get("community_heading")),
                "service": _text(row.get("program_or_service")),
            })
            continue
        disruption = _row_to_disruption(row, status, schedule_windows=runtime_windows)
        dedup_key = (canonical, _norm(disruption["service"]), _norm(disruption["details"]), status)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        site = sites[canonical]
        if canonical not in grouped:
            grouped[canonical] = {
                "name": site.get("name", canonical),
                "display_name": site.get("display_name") or site.get("facility_name") or site.get("name", canonical),
                "facility_name": site.get("facility_name", ""),
                "community": site.get("community", ""),
                "lat": site.get("lat"),
                "lng": site.get("lng"),
                "disruptions": [],
            }
        grouped[canonical]["disruptions"].append(disruption)

    live_sites = sorted(grouped.values(), key=lambda site: _norm(site.get("display_name")))
    for site in live_sites:
        site["disruption_count"] = len(site["disruptions"])
        site["active_now_count"] = sum(item["status"] == "active_now" for item in site["disruptions"])
        site["scheduled_count"] = sum(item["status"] == "scheduled" for item in site["disruptions"])
    data = {
        "as_of": live_date.strftime("%Y-%m-%d"),
        "as_of_timestamp": live_moment.isoformat(),
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "source_url": live_url,
        "demo_fixture": live_url.startswith("https://example.test"),
        "sites": live_sites,
        "active_disruption_count": sum(site["disruption_count"] for site in live_sites),
        "active_site_count": len(live_sites),
        "active_now_disruption_count": sum(site["active_now_count"] for site in live_sites),
        "scheduled_disruption_count": sum(site["scheduled_count"] for site in live_sites),
        "active_now_site_count": sum(site["active_now_count"] > 0 for site in live_sites),
        "scheduled_site_count": sum(site["scheduled_count"] > 0 for site in live_sites),
    }
    if unparsed_schedules:
        preview = "; ".join(
            f"{item['facility_name']} / {item['service']}"
            for item in unparsed_schedules[:8]
        )
        raise RuntimeError(
            f"Live page has {len(unparsed_schedules)} schedule-like row(s) that the SORCTracks parser could not interpret; refusing to publish. Examples: {preview}"
        )
    return data, unmapped


def _js_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _format_mt(value: str) -> str:
    moment = datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    hour_12 = moment.hour % 12 or 12
    return (
        f"{moment.strftime('%B')} {moment.day}, {moment.year}, "
        f"{hour_12}:{moment.minute:02d} {moment.strftime('%p')} MT"
    )


def render_live_html(data: dict, template_path: Path | None = None) -> str:
    title = "SORCTracks Live"
    source = html.escape(data["source_url"], quote=True)
    template_styles = ""
    if template_path is not None:
        template_text = template_path.read_text(encoding="utf-8")
        template_styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", template_text, re.IGNORECASE | re.DOTALL))
    live_css = """
    .sorc-mode-switch { display:flex; gap:.45rem; margin-left:auto; align-items:center; }
    .sorc-mode-switch a { border:1px solid #1b3a6b; border-radius:5px; padding:.35rem .6rem; color:#1b3a6b; text-decoration:none; background:#fff; font-size:.82rem; }
    .sorc-mode-switch a.active { color:#fff; background:#1b3a6b; }
    header { align-items:center; row-gap:.7rem; }
    .sorc-header-title { order:1; min-width:0; flex:1 1 auto; }
    .sorc-mode-switch { order:2; }
    header > .total-stat, header > .live-summary { order:3; width:100%; }
    header > .control-pair { order:4; }
    header > .toggle-control { order:5; margin-left:auto; }
    header .view-control select { width:8.8rem; min-width:8.8rem; height:1.55rem; }
    .live-badge { color:#B42318; font-family:'Cormorant Garamond',serif; font-size:1.12rem; font-weight:600; letter-spacing:.05em; white-space:nowrap; }
    .demo-badge { color:#9A6500; font-size:.64rem; letter-spacing:.1em; text-transform:uppercase; }
    .live-summary { width:100%; color:var(--mid); font-size:.82rem; }
    .live-summary strong { color:var(--navy); font-family:'Cormorant Garamond',serif; font-size:1.08rem; }
    .status-legend { display:flex; gap:.9rem; flex-wrap:wrap; margin:.45rem 0 .75rem; color:var(--muted); font-size:.7rem; }
    .status-key { display:inline-flex; align-items:center; gap:.3rem; }
    .status-dot { width:.58rem; height:.58rem; display:inline-block; border-radius:50%; border:1px solid var(--navy); }
    .status-dot.now { background:#B42318; } .status-dot.scheduled { background:#C8871A; }
    .live-card { margin:.55rem 0; padding:.55rem .65rem; border-left:3px solid #B42318; background:#fff8f7; }
    .live-card.scheduled { border-left-color:#C8871A; background:#fffaf0; }
    .live-status { display:inline-block; margin-bottom:.25rem; color:#B42318; font-size:.64rem; font-weight:400; letter-spacing:.1em; text-transform:uppercase; }
    .live-status.scheduled { color:#9A6500; }
    .live-card .service { color:var(--navy); font-size:.82rem; font-weight:400; }
    .live-card .details { color:var(--mid); font-size:.74rem; line-height:1.4; }
    .live-card a { color:var(--blue); font-size:.7rem; text-decoration:none; }
    .live-filter-note { color:var(--muted); font-size:.7rem; line-height:1.35; margin:.35rem 0 .7rem; }
    .live-health { order:6; width:100%; padding:.5rem .65rem; border:1px solid #D8E2EC; background:#F5F9FC; color:var(--mid); font-size:.75rem; line-height:1.4; }
    .live-health.warning { border-color:#E4BF73; background:#FFF8E7; color:#795500; }
    .live-health.down { border-color:#E5A7A2; background:#FFF1F0; color:#8C211A; }
    .live-health strong { font-weight:400; letter-spacing:.02em; }
    .empty-state a { color:var(--blue); }
    @media (max-width:768px) { .sorc-mode-switch { margin-left:0; } header > .toggle-control { margin-left:0; } }
    """
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Lato:wght@300;400&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>{template_styles}
{live_css}</style>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
  <header>
    <div class="sorc-header-title">
      <div class="title">SORCTracks <span class="live-badge">● LIVE</span></div>
      <div class="subtitle">Current AHS service disruptions · status evaluated at the current MT time<br><span id="liveClock">Current time loading…</span>{' · <span class="demo-badge">sample fixture data</span>' if data.get('demo_fixture') else ''}</div>
    </div>
    <nav class="sorc-mode-switch" aria-label="SORCTracks mode">
      <a href="sorctracks_tool.html">SORCTracks</a>
      <a class="active" href="sorctracks_live.html" aria-current="page">SORCTracks Live</a>
    </nav>
    <div class="live-summary" id="summaryStats"><strong>{data["active_now_disruption_count"]}</strong> active now · <strong>{data["scheduled_disruption_count"]}</strong> scheduled / recurring · <strong>{data["active_site_count"]}</strong> sites shown</div>
      <div class="control-pair" role="group" aria-label="Live map filters">
      <div class="view-control"><label for="statusSelect">Map view</label><select id="statusSelect"><option value="all">All notices</option><option value="active_now">Active now</option><option value="scheduled">Scheduled / recurring</option></select></div>
      <div class="view-control"><label for="serviceSelect">Service</label><select id="serviceSelect"><option value="all">All analyzed services</option><option value="ed">Emergency</option><option value="ob">Obstetrics</option><option value="acute">Acute Care</option><option value="surgery">Surgery</option><option value="other">Other Services</option></select></div>
      </div>
      <div id="liveHealth" class="live-health" role="status" aria-live="polite" hidden></div>
  </header>
  <div id="map" role="region" aria-label="Map of current Alberta service disruptions"></div>
  <main class="sidebar" id="sidebar" aria-label="Current service disruption data">
    <div class="panel-label">Live disruption notices</div>
    <div class="search-panel"><input id="search" class="search-input" type="search" placeholder="Search site or service" aria-label="Search live disruptions" /><div class="live-filter-note">Red markers are active at the current time. Gold markers are scheduled or recurring notices that may not be active right now.</div><div class="status-legend"><span class="status-key"><span class="status-dot now"></span> Active now</span><span class="status-key"><span class="status-dot scheduled"></span> Scheduled / recurring</span></div><div id="resultCount" class="result-count"></div></div>
    <div id="list"></div>
  </main>
  <footer><div>Current AHS service-disruption notices &middot; SORC, 2026</div><div>Source: <a href="{source}" target="_blank" rel="noopener">AHS current service-disruption page</a> &middot; <a href="https://sorc.ca">sorc.ca</a></div></footer>
  <script>
    const LIVE_DATA = {_js_json(data)};
    const map = L.map('map', {{ zoomControl:true }}).setView([54.0, -114.5], 6);
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{ attribution:'&copy; OpenStreetMap, &copy; CARTO' }}).addTo(map);
    const markers = new Map();
    const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[ch]));
    const serviceSelect = document.getElementById('serviceSelect');
    const statusSelect = document.getElementById('statusSelect');
    const LIVE_TIME_ZONE = 'America/Edmonton';
    const LIVE_STALE_AFTER_HOURS = 36;
    const LIVE_DOWN_AFTER_HOURS = 48;
    const dateKey = (instant) => {{ const parts = new Intl.DateTimeFormat('en-CA', {{timeZone:LIVE_TIME_ZONE,year:'numeric',month:'2-digit',day:'2-digit'}}).formatToParts(instant); const get = type => parts.find(part => part.type === type).value; return `${{get('year')}}-${{get('month')}}-${{get('day')}}`; }};
    function runtimeStatus(item, now) {{
      const windows = (item.schedule_windows || []).map(window => ({{start:new Date(window.start),end:new Date(window.end)}}));
      if (windows.length) {{ if (windows.some(window => window.start <= now && now < window.end)) return 'active_now'; if (windows.some(window => window.start > now)) return 'scheduled'; return null; }}
      const today = dateKey(now); if (item.start_date_key && today < item.start_date_key) return 'scheduled'; if (item.end_date_key && today > item.end_date_key) return null; return 'active_now';
    }}
    function statusLabel(status) {{ return status === 'active_now' ? 'Active now' : 'Scheduled / recurring'; }}
    function currentItems(site, now = new Date()) {{ return site.disruptions.map(item => ({{...item, status:runtimeStatus(item, now)}})).filter(item => item.status); }}
    function selectedItems(site, now = new Date()) {{ const status = statusSelect.value; const service = serviceSelect.value; return currentItems(site, now).filter(item => (!status || status === 'all' || item.status === status) && (!service || service === 'all' || item.service_layer === service)); }}
    function matches(site, now = new Date()) {{ const q = document.getElementById('search').value.trim().toLowerCase(); const items = selectedItems(site, now); const hay = [site.display_name,site.community,...items.map(item => item.service+' '+item.details)].join(' ').toLowerCase(); return items.length > 0 && (!q || hay.includes(q)); }}
    function popup(site, now = new Date()) {{ const items = currentItems(site, now); return `<div class="popup-title">${{esc(site.display_name)}}</div><div class="popup-stat">${{esc(site.community)}} · ${{items.length}} notice(s)</div>${{items.map(item => `<div style="margin-top:7px"><b>${{esc(statusLabel(item.status))}}</b><br>${{esc(item.service)}}<br>${{esc(item.details)}}</div>`).join('')}}`; }}
    function markerStyle(site, now = new Date()) {{ const items = currentItems(site, now); const hasNow = items.some(item => item.status === 'active_now'); const hasScheduled = items.some(item => item.status === 'scheduled'); const color = hasNow && hasScheduled ? '#7A3E9D' : hasNow ? '#B42318' : '#C8871A'; return {{ radius:Math.min(18,6+items.length*2), color:'#0F2347', fillColor:color, fillOpacity:.78, weight:2 }}; }}
    function updateSummary(now = new Date()) {{ const items = LIVE_DATA.sites.flatMap(site => currentItems(site, now)); const active = items.filter(item => item.status === 'active_now').length; const scheduled = items.filter(item => item.status === 'scheduled').length; const sites = LIVE_DATA.sites.filter(site => currentItems(site, now).length).length; document.getElementById('summaryStats').innerHTML = `<strong>${{active}}</strong> active now · <strong>${{scheduled}}</strong> scheduled / recurring · <strong>${{sites}}</strong> sites shown`; }}
    function formatMt(instant) {{ return new Intl.DateTimeFormat('en-US', {{timeZone:LIVE_TIME_ZONE,month:'long',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true}}).format(instant).replace(' AM',' AM MT').replace(' PM',' PM MT'); }}
    function updateClock(now = new Date()) {{ document.getElementById('liveClock').textContent = `Current time: ${{formatMt(now)}}`; }}
    function updateHealth(now = new Date()) {{ const box = document.getElementById('liveHealth'); const published = new Date(LIVE_DATA.generated_at); const source = new Date(LIVE_DATA.as_of_timestamp); const ageHours = (now - published) / 3600000; box.hidden = false; if (!Number.isFinite(ageHours) || ageHours >= LIVE_DOWN_AFTER_HOURS) {{ box.className='live-health down'; box.innerHTML = `<strong>SORCTracks Live may be unavailable.</strong> The page is showing the last successfully gathered data from ${{formatMt(source)}}.`; }} else if (ageHours >= LIVE_STALE_AFTER_HOURS) {{ box.className='live-health warning'; box.innerHTML = `<strong>SORCTracks Live may be delayed.</strong> The page is showing data from the last successful update at ${{formatMt(source)}}.`; }} else {{ box.hidden = true; }} }}
    function render() {{
      const now = new Date(); updateClock(now); updateSummary(now); updateHealth(now); const list = document.getElementById('list'); const resultCount = document.getElementById('resultCount'); const bounds = []; list.innerHTML = ''; let siteCount = 0; let itemCount = 0;
      LIVE_DATA.sites.forEach(site => {{ const items = selectedItems(site, now); const visible = matches(site, now); const marker = markers.get(site.name); if (marker) {{ marker.setStyle(markerStyle(site, now)).setPopupContent(popup(site, now)); if (visible) marker.addTo(map); else map.removeLayer(marker); }} if (!visible) return; siteCount += 1; itemCount += items.length; bounds.push([site.lat,site.lng]); const section = document.createElement('section'); section.className='site'; section.innerHTML = `<div class="site-name">${{esc(site.display_name)}}</div><div class="community">${{esc(site.community)}} · ${{items.length}} matching notice(s)</div>${{items.map(item => `<div class="live-card ${{item.status === 'scheduled' ? 'scheduled' : ''}}"><div class="live-status ${{item.status === 'scheduled' ? 'scheduled' : ''}}">${{esc(statusLabel(item.status))}}</div><div class="service">${{esc(item.service)}}</div>${{item.reduction ? `<div class="details">${{esc(item.reduction)}}</div>` : ''}}${{item.reason ? `<div class="details">${{esc(item.reason)}}</div>` : ''}}${{item.start || item.end ? `<div class="details">${{esc(item.start || 'current')}}${{item.end ? ' – ' + esc(item.end) : ''}}</div>` : ''}}<a href="${{esc(item.source_url)}}" target="_blank" rel="noopener">Source notice</a></div>`).join('')}}`; section.addEventListener('click', () => {{ map.setView([site.lat,site.lng], Math.max(map.getZoom(),8)); }}); list.appendChild(section); }});
      resultCount.textContent = `${{itemCount}} notice(s) at ${{siteCount}} site(s)`; if (!list.children.length) list.innerHTML = '<div class="empty-state">No disruptions match this filter.</div>'; if (bounds.length) map.fitBounds(bounds, {{ padding:[30,30], maxZoom:8 }});
    }}
    LIVE_DATA.sites.forEach(site => {{ const marker = L.circleMarker([site.lat,site.lng], markerStyle(site)).bindPopup(popup(site)); markers.set(site.name, marker); }});
    document.getElementById('search').addEventListener('input', render); serviceSelect.addEventListener('change', render); statusSelect.addEventListener('change', render); render(); setInterval(render, 60000);
  </script>
</body>
</html>'''


def build_live_page(template_path: Path, base_script: Path, output_path: Path, as_of: str | None = None) -> dict:
    data, unmapped = build_live_data(template_path, base_script, as_of=as_of)
    if unmapped:
        preview = "; ".join(sorted({row.get("facility_name") or row.get("community_heading") for row in unmapped})[:8])
        raise RuntimeError(f"Live page has {len(unmapped)} unmapped current row(s); refusing to publish. Examples: {preview}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_live_html(data, template_path=template_path), encoding="utf-8")
    return {
        "output": str(output_path),
        "sites": data["active_site_count"],
        "active_now_disruptions": data["active_now_disruption_count"],
        "scheduled_disruptions": data["scheduled_disruption_count"],
        "as_of": data["as_of"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True, help="Current historical sorctracks_tool.html")
    parser.add_argument("--base-script", type=Path, required=True, help="AHS v84 base scraper script")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", help="Override current date (YYYY-MM-DD), mainly for testing")
    args = parser.parse_args()
    result = build_live_page(args.template, args.base_script, args.output, as_of=args.as_of)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

