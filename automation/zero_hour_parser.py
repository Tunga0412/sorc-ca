"""
Zero-hour rescue parser.

When v84 flags an episode as "zero hours pending review", it usually means the
raw bed_or_space_reduction_text contains an irregular format that v84's parser
cannot reduce to a single span. Many of these texts actually list discrete
closure intervals inline. This module reads those intervals and recovers the
hours so they can be added to the monthly time series.

Handles two patterns:
  1. Cross-day with parens:   "April 9 (4 pm) - April 10 (8 am)"
  2. Same-day paren-wrapped:  "April 10 (6 am - 6 pm)"

Patterns NOT handled (returned as unparsed for manual review):
  - Bare time ranges like "6pm-8pm" with no date prefix (ambiguous regarding
    which date(s) and whether the listed range is the disruption or the regular
    closing schedule)
  - Rows with no start_date_text (cannot infer year)
"""

import re
from datetime import date, datetime, timedelta


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
MONTH_NAMES = "|".join(MONTHS.keys())

# "April 9 (4 pm) - April 10 (8 am)"
CROSS_DAY_PATTERN = re.compile(
    rf"\b({MONTH_NAMES})\s+(\d{{1,2}})\s*\(\s*(\d{{1,2}})(?::(\d{{2}}))?\s*(a\.?m\.?|p\.?m\.?)\s*\)"
    rf"\s*[-\u2013]\s*"
    rf"({MONTH_NAMES})\s+(\d{{1,2}})\s*\(\s*(\d{{1,2}})(?::(\d{{2}}))?\s*(a\.?m\.?|p\.?m\.?)\s*\)",
    re.IGNORECASE,
)

# "April 10 (6 am - 6 pm)"
SAME_DAY_PATTERN = re.compile(
    rf"\b({MONTH_NAMES})\s+(\d{{1,2}})\s*\("
    rf"\s*(\d{{1,2}})(?::(\d{{2}}))?\s*(a\.?m\.?|p\.?m\.?)\s*[-\u2013]\s*"
    rf"(\d{{1,2}})(?::(\d{{2}}))?\s*(a\.?m\.?|p\.?m\.?)\s*\)",
    re.IGNORECASE,
)

# "April 25 (8 am) - April 27 (8 am)" multi-day with parens - same as cross-day pattern


def _to_hour24(hour, minute, ampm):
    h = int(hour)
    m = int(minute) if minute else 0
    ampm = ampm.lower().replace(".", "")
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return h, m


def _build_datetime(year, month_name, day, hour, minute, ampm):
    month = MONTHS[month_name.lower()]
    h, m = _to_hour24(hour, minute, ampm)
    return datetime(year, month, int(day), h, m)


def parse_intervals(text, default_year):
    """
    Returns list of (start_datetime, end_datetime) closure intervals found in text.
    """
    if not isinstance(text, str) or not text:
        return []

    intervals = []
    consumed_spans = []  # to avoid re-matching same substring

    # Cross-day pattern first (longest)
    for m in CROSS_DAY_PATTERN.finditer(text):
        try:
            year1 = default_year
            year2 = default_year
            start = _build_datetime(year1, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
            end = _build_datetime(year2, m.group(6), m.group(7), m.group(8), m.group(9), m.group(10))
            # If end month < start month, end crossed into next year
            if end < start:
                end = end.replace(year=year2 + 1)
            if end > start:
                intervals.append((start, end))
                consumed_spans.append((m.start(), m.end()))
        except Exception:
            continue

    # Same-day pattern, skipping spans already matched by cross-day
    def in_consumed(pos):
        return any(s <= pos < e for s, e in consumed_spans)

    for m in SAME_DAY_PATTERN.finditer(text):
        if in_consumed(m.start()):
            continue
        try:
            year1 = default_year
            start = _build_datetime(year1, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
            end = _build_datetime(year1, m.group(1), m.group(2), m.group(6), m.group(7), m.group(8))
            if end > start:
                intervals.append((start, end))
        except Exception:
            continue

    intervals.sort(key=lambda x: x[0])
    return intervals


def bucket_hours_by_month(intervals):
    """
    Returns dict {year_month_str: hours_float}, splitting any interval that
    crosses a month boundary at midnight on the 1st.
    """
    buckets = {}
    for start, end in intervals:
        cursor = start
        while cursor < end:
            # Compute end of cursor's current month
            if cursor.month == 12:
                next_month_start = datetime(cursor.year + 1, 1, 1)
            else:
                next_month_start = datetime(cursor.year, cursor.month + 1, 1)
            chunk_end = min(end, next_month_start)
            ym = f"{cursor.year:04d}-{cursor.month:02d}"
            buckets[ym] = buckets.get(ym, 0.0) + (chunk_end - cursor).total_seconds() / 3600
            cursor = chunk_end
    return buckets


def clip_intervals_to_as_of(intervals, as_of_date=None):
    """
    Keep only elapsed portions of intervals as of the run date.

    Live AHS pages can list scheduled future closures. Those should stay in the
    audit summary, but they must not contribute disruption hours until the time
    has actually occurred.
    """
    if as_of_date is None:
        return list(intervals), 0
    if isinstance(as_of_date, datetime):
        as_of = as_of_date.date()
    elif isinstance(as_of_date, date):
        as_of = as_of_date
    else:
        as_of = datetime.fromisoformat(str(as_of_date)[:10]).date()
    cutoff = datetime(as_of.year, as_of.month, as_of.day) + timedelta(days=1)

    clipped = []
    future_or_empty = 0
    for start, end in intervals:
        if start >= cutoff:
            future_or_empty += 1
            continue
        clipped_end = min(end, cutoff)
        if clipped_end > start:
            clipped.append((start, clipped_end))
        else:
            future_or_empty += 1
    return clipped, future_or_empty


def _infer_year(start_date_text):
    """Pull a 4-digit year out of a date string like 'April 9, 2026'."""
    if not isinstance(start_date_text, str):
        return None
    m = re.search(r"\b(20\d{2})\b", start_date_text)
    return int(m.group(1)) if m else None


def rescue_zero_hour_episodes(zero_hours_df, alias_map, as_of_date=None):
    """
    Process the v84 zero_hours episodes DataFrame and return per-(canonical-site, year-month)
    rescued hours, plus a list of unrescued rows for the report.

    alias_map: dict mapping every lowercased alias to canonical site name.

    Returns:
      rescued: dict {canonical_site: {year_month: hours}}
      summary: list of dicts, one per zero-hour row, with fields:
        site, start_date_text, end_date_text, rescued_hours, rescued_breakdown, status
    """
    rescued = {}
    summary = []
    if zero_hours_df is None or len(zero_hours_df) == 0:
        return rescued, summary

    for _, row in zero_hours_df.iterrows():
        site_raw = str(row.get("site_best", "")).lower().strip()
        canonical = alias_map.get(site_raw, site_raw)
        text = row.get("bed_or_space_reduction_text", "") or ""
        start_text = row.get("start_date_text")
        year = _infer_year(start_text) or _infer_year(row.get("anticipated_end_date_text"))

        entry = {
            "site": canonical or site_raw,
            "site_display": row.get("site_best", site_raw),
            "start_date_text": start_text,
            "end_date_text": row.get("anticipated_end_date_text"),
            "raw_text": text[:200],
            "rescued_hours": 0.0,
            "rescued_breakdown": {},
            "status": "unparsed",
        }

        if not year:
            entry["status"] = "no_year"
            summary.append(entry)
            continue

        intervals = parse_intervals(text, year)
        if not intervals:
            entry["status"] = "no_intervals_found"
            summary.append(entry)
            continue

        original_interval_count = len(intervals)
        intervals, future_interval_count = clip_intervals_to_as_of(intervals, as_of_date)
        entry["interval_count"] = original_interval_count
        entry["future_interval_count"] = future_interval_count
        entry["as_of_date"] = str(as_of_date)[:10] if as_of_date else ""
        if not intervals:
            entry["status"] = "future_interval_not_counted"
            summary.append(entry)
            continue

        buckets = bucket_hours_by_month(intervals)
        total = sum(buckets.values())
        entry["rescued_hours"] = round(total, 2)
        entry["rescued_breakdown"] = {k: round(v, 2) for k, v in sorted(buckets.items())}
        entry["status"] = "rescued"
        entry["counted_interval_count"] = len(intervals)

        if canonical not in rescued:
            rescued[canonical] = {}
        for ym, h in buckets.items():
            rescued[canonical][ym] = rescued[canonical].get(ym, 0.0) + h

        summary.append(entry)

    return rescued, summary

