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
import hashlib
from urllib.parse import urlparse
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from public_data_reader import public_constant
from current_card_guard import require_own_current_census, require_advertised_pagination_closed
from live_timezone import LOCAL_TZ, browser_clock_contract
from live_source_capture import SourceCapture, write_private_json


LIVE_URL = "https://www.albertahealthservices.ca/br/Page17594.aspx"
LIVE_SCHEMA_VERSION = 8
MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"), 1
)}
MONTH_PATTERN = "|".join(MONTHS)


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().replace("&", " and ")
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
    text = _text(row.get("bed_or_space_reduction_text"))
    if not text:
        return False
    weekday = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"
    return bool(re.search(
        rf"\b(?:daily|nightly|overnight|each\s+night|regular\s+operating\s+hours|adjusted\s+hours|weekends?|weekdays?|closed\s+(?:from|on)|open\s+(?:{weekday}|24\s*hours?|24/7)|{weekday})\b",
        text,
        flags=re.I,
    ))


def _service_layer(value: object) -> str:
    """Qualified programme labels; substrings such as 'allied' are not ED."""
    text = _norm(value)
    if re.search(r"\bed\b|\bemergency(?: department| services| care)?\b", text):
        return "ed"
    if re.search(r"\bob\b|\bobstetric(?:s|al)?\b|\bmaternity\b|\blabou?r and delivery\b|\bc section\b|\bca?esarean\b", text):
        return "ob"
    if re.search(r"\bacute(?: care)?\b|\binpatient\b|\b(?:medical|medicine) unit\b", text):
        return "acute"
    if re.search(r"\bsurg(?:ery|ical)\b|\boperating rooms?\b|\boperative\b|\bana?esthesia\b", text):
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
        if not canonical or canonical in sites:
            raise RuntimeError("Duplicate or blank registry identity")
        sites[canonical] = site
        facility = _norm(site.get("facility_name"))
        # Short names and locality headings are not physical-facility evidence.
        values = [site.get("facility_name")]
        values.extend(alias for alias in site.get("aliases") or [] if len(_norm(alias).split()) >= 3)
        for value in values:
            key = _norm(value)
            if not key:
                continue
            if key in lookup and lookup[key] != canonical:
                raise RuntimeError("Ambiguous exact facility alias in public registry")
            lookup[key] = canonical
    return lookup, sites


def _resolve_site(row: dict, lookup: dict[str, str], sites: dict[str, dict]) -> str | None:
    caption = _norm(row.get("facility_name"))
    if caption == _norm("Grimshaw/Berwyn District Community Health Centre") and _norm(row.get("program_or_service")) == "emergency department":
        # Version 1.5: qualified exact caption variation, supported by official
        # AHS directory facility1000192 and the current own ED card. This is
        # not a locality match or a rule deleting 'and' from unseen site names.
        caption = _norm("Grimshaw / Berwyn and District Community Health Centre")
    return lookup.get(caption)


def _parse_date(value: object) -> pd.Timestamp | None:
    text = _text(value)
    if not text or not re.search(r"\b\d{4}\b", text):
        # A publisher-owned explicit year is required; pandas' current-year
        # fallback must never become a date authority.
        return None
    date_match = re.match(rf"^((?:{MONTH_PATTERN})\s+\d{{1,2}},?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})(?:\s|$)", text, re.I)
    parsed = pd.to_datetime(date_match.group(1) if date_match else text, errors="coerce")
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
        if hour < 1 or hour > 12:
            return None
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _require_unique_local_clock(value: datetime) -> datetime:
    """Reject nonexistent/ambiguous Alberta wall clocks without source offsets."""
    wall = value.replace(tzinfo=None)
    candidates = {}
    for fold in (0, 1):
        possible = wall.replace(tzinfo=LOCAL_TZ, fold=fold)
        utc = possible.astimezone(timezone.utc)
        if utc.astimezone(LOCAL_TZ).replace(tzinfo=None) == wall:
            candidates[utc] = possible
    if len(candidates) != 1:
        raise RuntimeError("Printed local clock is nonexistent or ambiguous; source offset/fold authority required")
    return next(iter(candidates.values()))


def _schedule_windows(text: str, as_of: datetime, row: dict | None = None) -> list[tuple[datetime, datetime]]:
    """Parse complete explicit clock calendars using the card's own year anchors.

    The retrieval clock is never an event-year anchor. Date-only card fields
    constrain year choices; ambiguous or malformed finite calendars stop release.
    """
    row = row or {}
    date_part = rf"({MONTH_PATTERN})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?"
    clock = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{3,4}|noon|midnight)"
    pattern = re.compile(
        rf"(?P<sm>{MONTH_PATTERN})\s+(?P<sd>\d{{1,2}})(?:,?\s+(?P<sy>\d{{4}}))?\s*\(\s*"
        rf"(?P<st>{clock})\s*(?:"
        rf"(?P<same>[-–—]|to)\s*(?P<sameet>{clock})\s*\)"
        rf"|\)\s*(?:[-–—]|to)\s*(?P<em>{MONTH_PATTERN})\s+(?P<ed>\d{{1,2}})"
        rf"(?:,?\s+(?P<ey>\d{{4}}))?\s*\(\s*(?P<et>{clock})\s*\))",
        re.I,
    )
    windows = []
    broad_start = _parse_date(row.get("start_date_text"))
    broad_end = _parse_date(row.get("anticipated_end_date_text"))
    source_years = {int(x.year) for x in (broad_start, broad_end) if x is not None}
    matches = list(pattern.finditer(text))
    # Count every printed Month/day(clock...) token. Each same-day range has
    # one token, a paired multi-date range has two. Never accept a partial list.
    month_date_pattern = MONTH_PATTERN + "|Jan\\.?|Feb\\.?|Mar\\.?|Apr\\.?|Jun\\.?|Jul\\.?|Aug\\.?|Sept?\\.?|Oct\\.?|Nov\\.?|Dec\\.?"
    date_tokens = list(re.finditer(rf"\b(?:{month_date_pattern})\s+\d{{1,2}}(?:,?\s+\d{{4}})?", text, re.I))
    if matches:
        covered = {(m.start(), m.end()) for m in matches}
        if any(not any(a <= token.start() and token.end() <= b for a, b in covered) for token in date_tokens):
            raise RuntimeError("Unparsed later date/clock clause in current finite calendar")
        remainder = text
        for match in reversed(matches):
            remainder = remainder[:match.start()] + " " * (match.end() - match.start()) + remainder[match.end():]
        if re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:at|from)\s+\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)\b|\b\d{1,2}(?:st|nd|rd|th)\s*\(", remainder, re.I):
            raise RuntimeError("Unparsed later clock clause in current finite calendar")
    elif date_tokens and not (len(date_tokens) == 1 and re.search(r"\bstarting\s+(?:" + MONTH_PATTERN + r")", text, re.I)):
        raise RuntimeError("Unreceived dated source-body calendar; current notice needs clarification")
    if not matches and len(date_tokens) == 1 and re.search(r"\bstarting\s+(?:" + MONTH_PATTERN + r")", text, re.I):
        # A sole printed onset clock for an explicit recurring schedule is
        # handled by the recurrence parser, not a finite start/end pair.
        return []
    if sum(1 if m.group("same") else 2 for m in matches) != len(date_tokens):
        raise RuntimeError("Unparsed explicit clock calendar in current source card")
    for match in matches:
        start_clock = _parse_clock(match.group("st"))
        end_clock = _parse_clock(match.group("sameet") or match.group("et"))
        if start_clock is None or end_clock is None:
            raise RuntimeError("Invalid printed clock in current source card")
        sm = MONTHS[match.group("sm").lower()]
        em = sm if match.group("same") else MONTHS[match.group("em").lower()]
        sd = int(match.group("sd"))
        ed = sd if match.group("same") else int(match.group("ed"))
        explicit_sy = match.group("sy")
        explicit_ey = match.group("ey")
        if explicit_sy:
            years = {int(explicit_sy)}
        elif explicit_ey:
            years = {int(explicit_ey) - int(em < sm)}
        elif broad_start is not None and broad_end is not None:
            if broad_end.year - broad_start.year > 40:
                raise RuntimeError("Source calendar exceeds the supported own-year range")
            years = set(range(int(broad_start.year), int(broad_end.year) + 1))
        elif broad_start is not None:
            # A same-year list can be anchored by its first printed onset
            # exactly matching the card's own dated start. A retrieval clock
            # or an unrelated older start is not a year anchor.
            first = matches[0]
            anchored = (MONTHS[first.group("sm").lower()], int(first.group("sd"))) == (broad_start.month, broad_start.day)
            same_year = all((MONTHS[m.group("sm").lower()], int(m.group("sd"))) >= (broad_start.month, broad_start.day)
                            and (m.group("same") or MONTHS[m.group("em").lower()] >= MONTHS[m.group("sm").lower()]) for m in matches)
            if not anchored or not same_year:
                raise RuntimeError("Finite source calendar lacks a bounded unique year authority")
            years = {int(broad_start.year)}
        else:
            years = source_years
        if not years:
            raise RuntimeError("Explicit clock calendar has no publisher-owned year anchor")
        candidates = []
        for year in years:
            end_year = int(explicit_ey) if explicit_ey else year + int(em < sm)
            try:
                start = datetime(year, sm, sd, start_clock.hour, start_clock.minute, tzinfo=LOCAL_TZ)
                end = datetime(end_year, em, ed, end_clock.hour, end_clock.minute, tzinfo=LOCAL_TZ)
            except ValueError:
                continue
            if match.group("same") and end <= start:
                end += timedelta(days=1)
            start = _require_unique_local_clock(start)
            end = _require_unique_local_clock(end)
            if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
                continue
            if broad_start is not None and start.date() < broad_start.date():
                continue
            if broad_end is not None and end.date() > broad_end.date() + timedelta(days=1):
                continue
            candidates.append((start, end))
        if len(candidates) != 1:
            raise RuntimeError("Current finite clock calendar has ambiguous or invalid source years")
        windows.append(candidates[0])
    return windows


def _local_datetime(value: object) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    parsed = pd.Timestamp(value).to_pydatetime()
    # Legacy-generated values do not establish source offset/fold authority.
    return _require_unique_local_clock(parsed.astimezone(LOCAL_TZ) if parsed.tzinfo is not None else parsed)


def _unscoped_assertion_polarity(row: dict) -> dict:
    """Classify each own restriction clause; negative facts cannot become loss.

    Reason is not an eligibility field. Clause boundaries preserve separate
    positive physician/service assertions when a different loss is denied.
    """
    text = _text(row.get("bed_or_space_reduction_text"))
    text = re.sub(r"\b([ap])\.m\.", r"\1m", text, flags=re.I)
    clauses = re.split(r"[;\n]|(?<=[.!?])\s+|\b(?:but|however|although|whereas|except|yet)\b", text, flags=re.I)
    loss = r"(?:shortage|lack|gap|unavailability|disruption|cancellation|cancelation|closure|reduction|restriction|delay)"
    descriptors = r"(?:any|a|current|new|reported|ongoing|further|on[ -]?site|physician|doctor|physician coverage|coverage|staffing|staff|nursing|ED|OB|obstetric|obstetrics|service|services|unit|operative|surgical|capacity|bed|beds|treatment|space|clinical|personnel)"
    negatives = [
        re.compile(r"\b(?:no|neither|without|not\s+(?:a|an|any)|isn't|aren't|wasn't|weren't)\s+(?:(?!(?:due|because|since|while|but|however|and|or|except|as|although|whereas|yet|is|are|was|were|will|has|have|had|not|never|no)\b)[a-z][a-z0-9/-]*\s+){0,8}" + loss + r"s?\b", re.I),
        re.compile(r"\bno\s+(?:on[ -]?site\s+)?(?:physicians?|doctors?|physician coverage|services?|department|unit)\s+(?:(?:is|are|remains?)\s+)?(?:unavailable|absent|missing|closed|restricted|reduced|paused|suspended|cancelled|canceled)\b", re.I),
        re.compile(r"\b(?:not|never)\s+(?:(?:be|been|being|currently|now|actually|fully|entirely)\s+)?(?:unavailable|absent|missing|closed|restricted|reduced|paused|suspended|cancelled|canceled)\b", re.I),
        re.compile(r"\bno longer\s+(?:(?:currently|now)\s+)?(?:unavailable|absent|missing|closed|restricted|reduced|paused|suspended|cancelled|canceled|experiencing\s+" + loss + r")\b", re.I),
        re.compile(r"\b" + loss + r"s?\b.{0,80}\b(?:denied|resolved|ended|cleared|withdrawn|not confirmed|ruled out|has not occurred|has not been reported)\b", re.I),
        re.compile(r"\b(?:denied|denies|resolved|ruled out|withdrawn)\b.{0,60}\b" + loss + r"s?\b", re.I),
        re.compile(r"\b(?:physicians?|doctors?|physician coverage|onsite coverage|services?|department|unit)\s+(?:is|are|remains?|has|have|has been|have been|will be)\s+(?:(?:fully|normally|currently|now|still)\s+)?(?:available|open|restored|resumed|unrestricted)\b", re.I),
    ]
    closed = re.compile(r"\b(?:emergency department|ED|department|services?|unit)\s+(?:(?:is|remains|will be|has been)\s+)?(?:closed|unavailable|paused|suspended|cancelled|canceled)\b", re.I)
    adjusted = re.compile(r"\b(?:adjusted|reduced|limited|modified)\s+(?:hours(?: of operation)?|operating hours)\b|\bopen only\b", re.I)
    physician = re.compile(
        r"\bno\s+(?:(?:on[ -]?site|ED|OB|obstetric|obstetrics)\s+)*(?:physician|doctor)\s+(?:(?:is|remains)\s+)?(?:on[ -]?site|available|coverage|present)\b"
        r"|\bno\s+on[ -]?site\s+(?:(?:ED|OB|obstetric|obstetrics)\s+)?(?:physician|doctor)\s*(?=[.;,]|$)"
        r"|\bwithout\s+(?:(?:on[ -]?site|ED|OB|obstetric|obstetrics)\s+)*(?:physician|doctor)(?!\s+(?:shortage|lack|gap|unavailability|disruption|cancellation))\b"
        r"|\b(?:onsite|on site)\s+(?:physician|doctor)\s+(?:unavailable)\b"
        r"|\b(?:on[ -]?site\s+)?(?:physician|doctor|physician coverage)\s+(?:(?:is|remains|will be)\s+)?(?:unavailable|absent|not available|not present|not on[ -]?site)\b",
        re.I,
    )
    services = re.compile(r"\bno\s+(?:obstetrical|obstetric|obstetrics|maternity)\s*(?:services?|physician|coverage)?\b", re.I)
    positive = {"closed":False, "adjusted":False, "physician":False, "service":False}
    negative_count = 0
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        negative_spans = []
        for number, rule in enumerate(negatives):
            for match in rule.finditer(clause):
                if number == len(negatives) - 1 and re.search(r"\bno\s+(?:" + descriptors + r"\s+){0,8}$", clause[:match.start()], re.I):
                    # 'No physician is available' asserts absence. It must not
                    # be rewritten as 'physician is available' by substring.
                    continue
                negative_spans.append(match.span())
        negative_count += len(negative_spans)
        for kind, rule in (("closed",closed),("adjusted",adjusted),("physician",physician),("service",services)):
            for match in rule.finditer(clause):
                # Denied/resolved availability statements concern the entire
                # own clause. Separate clauses are still inspected independently.
                if any(a < match.end() and match.start() < b for a,b in negative_spans):
                    continue
                # A denial after the positive phrase, e.g. 'onsite physician
                # shortage has resolved', must suppress that same statement.
                tail = clause[match.end():]
                if re.match(r"\s+(?:(?:is|has|has been|was|is now)\s+)?(?:denied|resolved|ended|cleared|withdrawn|not confirmed|ruled out)\b", tail, re.I):
                    continue
                positive[kind] = True
    positive["negative_only"] = negative_count > 0 and not any(positive.values())
    positive["negative_statement_count"] = negative_count
    return positive



# Scope is applied before either polarity or calendar parsing. This is Live
# CURRENT_POSTING eligibility, including partial service/staffing restrictions.
_SERVICE_SUBJECTS = {
    "ed": r"\bemergency\s+(?:department|services?)\b|\bED\b",
    "ob": r"\bOB\b|\bobstetric(?:s|al)?\b|\bmaternity\b|\b(?:labour|labor)\s+and\s+delivery\b|\bc[ -]?section\b|\bcaesarean\b|\bepidural\b",
    "acute": r"\bacute(?:\s+care)?\b|\binpatient\b|\b(?:medical|medicine)\s+unit\b",
    "surgery": r"\bsurg(?:ery|ical)\b|\boperating\s+rooms?\b|\b(?:anaesthesia|anesthesia)\b",
    "other": r"\b(?:diagnostic imaging|radiology|CT|MRI|ultrasound|x[ -]?ray|laboratory|endoscopy|endoscopic|cardiology|cardiac|ophthalmology|ophthalmic|rehabilitation|rehab|detox|dialysis|ambulatory|outpatient|palliative)\b|\blong[ -]term\s+care\b|\bhome\s+care\b",
}
_FOREIGN_SUBJECT = re.compile(
    r"\b(?:nearby|neighbou?ring|adjacent|other|another|alternate|alternative|nearest|receiving)"
    r"\s+(?:(?:emergency|obstetrics?|acute|surgical)\s+)?(?:hospital|facility|centre|center|site|department|unit)\b"
    r"|\b(?:at|to|from)\s+(?:a\s+)?different\s+(?:hospital|facility|site|department|unit)\b"
    r"|\belsewhere\b", re.I)
_FACILITY_NAME = re.compile(
    r"\b(?:[A-Z][A-Za-z'/-]*\s+){1,7}(?:Hospital(?:\s+(?:and|&)\s+Care\s+Centre)?|"
    r"(?:Health|Healthcare|Care)\s+(?:Centre|Center|Complex))\b")
_CLOCK_CUE = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\s*(?:,?\s+\d{4})?\s*\(|\b\d{1,2}:?\d{2}\s*(?:[-–—]|to)", re.I)


def _subject_clauses(text: str):
    """Return exact offsets; clock punctuation is not a sentence boundary."""
    masked = re.sub(r"\b[ap]\.m\.", lambda m: m.group().replace(".", "~"), text, flags=re.I)
    subject = (r"(?:our|this|the|another|other|nearby|neighbou?ring)\s+"
               r"(?:emergency|obstetrics?|maternity|acute|surgical|surgery|inpatient|"
               r"endoscopy|rehabilitation|hospital|facility|department|unit|[A-Za-z/-]+\s+(?:unit|department|services?))")
    delimiters = re.compile(
        r"[;\n]|(?<=[.!?])\s+|\b(?:but|however|although|whereas|except|yet|while)\b"
        r"|,\s*(?=" + subject + r")|\band\s+(?=" + subject + r")", re.I)
    start = 0
    for match in delimiters.finditer(masked):
        left, right = start, match.start()
        while left < right and text[left].isspace(): left += 1
        while right > left and text[right - 1].isspace(): right -= 1
        if left < right: yield left, right, text[left:right]
        start = match.end()
    left, right = start, len(text)
    while left < right and text[left].isspace(): left += 1
    while right > left and text[right - 1].isspace(): right -= 1
    if left < right: yield left, right, text[left:right]


def _resource_capacity_assertion(clause: str) -> bool:
    """A positive own reduction field can specify capacity without 'closed'."""
    resource = r"(?:beds?|(?:treatment|care|operating|procedure|delivery)\s+(?:spaces?|rooms?)|scanners?)"
    bare = re.fullmatch(r"\s*(?P<lost>\d+)\s*(?:/|of|out\s+of)\s*(?P<total>\d+)\s+" + resource + r"\s*(?:closed|unavailable|out of service)?[.\s]*", clause, re.I)
    if bare:
        lost, total = int(bare.group("lost")), int(bare.group("total"))
        if total <= 0 or lost > total:
            raise RuntimeError("Current numerical capacity has invalid own bounds")
        return lost > 0
    single = re.fullmatch(r"\s*(?P<count>[1-9]\d*)\s+" + resource + r"\s*(?:closed|unavailable|out of service)?[.\s]*", clause, re.I)
    if single:
        return True
    equipment = r"(?:CT(?:\s+scanner)?|MRI(?:\s+scanner)?|ultrasound|x[ -]?ray|diagnostic imaging|scanner|endoscopy equipment|dialysis equipment)"
    return bool(re.fullmatch(r"\s*(?:no|without)\s+(?:on[ -]?site\s+)?" + equipment
                            + r"\s*(?:(?:is|are)\s+)?(?:available|operational|working|on[ -]?site)?[.\s]*", clause, re.I))

def _neutral_own_context(clause: str) -> bool:
    return bool(re.search(r"\b(?:nursing staff|nurses?|nursing)\s+(?:remain|remains|is|are)\s+(?:available\s+)?on[ -]?site\b"
                          r"|\bVEP\s+(?:coverage\s+)?(?:may be|is|remains)\s+available\b"
                          r"|\b(?:regular|normal|usual)\s+operating hours\b"
                          r"|\b(?:no|zero|0)\s+(?:current\s+)?(?:reductions?|restrictions?|closures?)\b"
                          r"|\b0\s*/\s*[1-9]\d*\s+(?:beds?|treatment spaces?|operating rooms?)\b", clause, re.I))

def _other_subject_family(value: str) -> str:
    token = _norm(value)
    if token in {"diagnostic imaging", "radiology", "ct", "mri", "ultrasound", "x ray"}:
        return "diagnostic imaging"
    if token in {"cardiology", "cardiac"}:
        return "cardiac"
    if token in {"rehabilitation", "rehab"}:
        return "rehabilitation"
    return token

def _live_subject_scope(row: dict) -> dict:
    """Bind own assertions and clock antecedents; ambiguous scope stops release."""
    text = _text(row.get("bed_or_space_reduction_text"))
    programme = _text(row.get("program_or_service"))
    facility = _text(row.get("facility_name"))
    own = _service_layer(programme)
    programme_groups = {k for k, rule in _SERVICE_SUBJECTS.items() if re.search(rule, programme, re.I)}
    if len(programme_groups) > 1:
        raise RuntimeError("Ambiguous multi-service current card; current notice needs clarification")
    own_other = set(re.findall(_SERVICE_SUBJECTS["other"], programme, re.I))
    last_subject = "own"
    own_asserted = False
    quotes = []
    retained = []
    excluded_clock = False
    own_clock = False
    unresolved_own = False
    for left, right, clause in _subject_clauses(text):
        groups = {k for k, rule in _SERVICE_SUBJECTS.items() if re.search(rule, clause, re.I)}
        # An explicitly named unfamiliar service is not lent to the card's
        # programme. Keep unseen nouns unresolved rather than guessing scope.
        generic = {"the", "our", "this", "all", "no", "any", "a", "an", "these",
                   "its", "their", "department", "clinical", "health", "physician",
                   "doctor", "nursing", "normal", "regular", "usual", "existing",
                   "support", "provide", "receive", "reduced", "limited",
                   "temporary", "additional", "available", "scheduled", "vep"}
        unfamiliar = []
        for label in re.findall(r"\b([A-Za-z][A-Za-z/-]*)\s+(?:unit|department|services?|program(?:me)?)\b", clause, re.I):
            if _norm(label) in generic or any(re.search(rule, label, re.I) for rule in _SERVICE_SUBJECTS.values()):
                continue
            if _norm(label) in _norm(programme).split():
                groups.add(own)
            else:
                unfamiliar.append(label)
        if unfamiliar:
            if own in groups or not groups:
                raise RuntimeError("Unresolved additional named service subject; current notice needs clarification")
            groups.add("unreceived_other_service")
        foreign_facility = bool(_FOREIGN_SUBJECT.search(clause))
        names = [m.group().strip() for m in _FACILITY_NAME.finditer(clause)]
        for name in names:
            name = re.sub(r"^(?:The|Our|This|At|To)\s+", "", name)
            if _norm(name) != _norm(facility):
                foreign_facility = True
        # Lower-case named facility mentions also need scope. This only
        # compares a mention within the already received own caption; it is
        # never a facility-registry alias or locality identity rule.
        for name in re.findall(r"\b[A-Za-z][A-Za-z/-]*\s+(?:Hospital|Healthcare\s+Centre|Health\s+Centre|Care\s+Centre|Health\s+Complex)\b", clause, re.I):
            if _norm(name).split()[0] in {"the", "our", "this", "local", "and"}:
                continue
            if (" " + _norm(name) + " ") not in (" " + _norm(facility) + " "):
                foreign_facility = True
        explicit_own = bool(re.search(r"\b(?:our|this)\s+(?:hospital|facility|site|department|unit|emergency|obstetrics?|maternity|acute|surgery|surgical|inpatient|endoscopy|rehabilitation)\b", clause, re.I))
        # A referral sentence is context, even if its patients are 'our' patients.
        referral = bool(re.search(r"\b(?:go to|attend|redirected|referred|transferred|seek care|alternate care)\b", clause, re.I))
        if foreign_facility and explicit_own and not referral:
            raise RuntimeError("Own and foreign facility subjects share an unresolved clause; current notice needs clarification")
        if own in groups and len(groups) > 1:
            raise RuntimeError("Multiple service subjects share an unresolved clause; current notice needs clarification")
        subject = ("foreign_facility" if foreign_facility or referral else
                   "other_service" if groups and own not in groups else
                   "own" if groups or explicit_own else last_subject)
        if own == "other" and groups == {"other"} and own_other:
            mentioned = set(re.findall(_SERVICE_SUBJECTS["other"], clause, re.I))
            if {_other_subject_family(x) for x in mentioned}.isdisjoint({_other_subject_family(x) for x in own_other}):
                subject = "other_service"
        if groups or foreign_facility or referral or explicit_own:
            last_subject = subject
        facts = _unscoped_assertion_polarity({"bed_or_space_reduction_text": clause})
        baseline = bool(re.search(r"\b(?:regular|normal|usual)\s+operating hours\b", clause, re.I))
        vep = bool(re.search(r"\b(?:VEP|Virtual Emergency Physician)\b", clause, re.I)) and own == "ed"
        explicit_vep_mode = bool(re.search(r"\bopen with\s+(?:Virtual Emergency Physician|VEP)|\bcovered by\s+(?:Virtual Emergency Physician|VEP)", clause, re.I))
        if facts["negative_only"] and not explicit_vep_mode:
            vep = False
        # Live also reports the card's own partial restrictions and staffing.
        broader = bool(re.search(
            r"\b(?:shortage|lack|gap|reduced|reduction|restricted|restriction|limited|"
            r"unavailable|suspended|cancelled|canceled|paused|closed|no\s+staff|no\s+beds)\b"
            r"|\bno\s+(?:[\w/-]+\s+){0,4}(?:services?|beds?|staff|department|unit|treatment\s+spaces?)\b",
            clause, re.I))
        positive = any(facts[k] for k in ("closed", "adjusted", "physician", "service"))
        partial_available = bool(re.search(r"\breduced\s+(?:level|capacity|staffing|number)|\blimited\s+(?:capacity|staffing)", clause, re.I)) and not re.search(r"\b(?:no|not|without|resolved|denied|ended|restored)\b", clause, re.I)
        capacity = _resource_capacity_assertion(clause) if subject == "own" and not facts["negative_only"] else False
        positive = positive or vep or partial_available or capacity or (broader and not facts["negative_only"] and not baseline)
        zero_capacity = bool(re.fullmatch(r"\s*0\s*(?:/|of|out\s+of)\s*[1-9]\d*\s+(?:beds?|(?:treatment|care|operating|procedure|delivery)\s+(?:spaces?|rooms?))\s*(?:closed|unavailable|out of service)?[.\s]*", clause, re.I))
        if zero_capacity:
            positive = False
        clock = bool(_CLOCK_CUE.search(clause))
        negative = (facts["negative_only"] and not vep) or zero_capacity
        if subject == "own" and not positive and not negative and not _neutral_own_context(clause) and clause.strip():
            unresolved_own = True
        keep = subject == "own" and not (negative and not positive)
        # An available/open-only clock is not a loss calendar. Contextual
        # nursing/VEP clauses may carry the preceding own restriction's dates.
        if keep and clock and not positive and not own_asserted:
            keep = False
        if keep:
            retained.append(clause)
            own_asserted = own_asserted or positive
            own_clock = own_clock or (clock and (positive or own_asserted))
        elif clock:
            excluded_clock = True
        quotes.append({"start": left, "end": right, "quote": clause,
                       "subject": subject, "retained": keep,
                       "positive_posting_assertion": positive,
                       "clock_cue": clock})
    if own_asserted and excluded_clock and not own_clock:
        raise RuntimeError("Own restriction has no own clock binding beside a foreign/negative calendar; current notice needs clarification")
    # Keep original text exactly when every clause belongs to the accepted
    # own posting. The complete raw field is always retained for display.
    projection = text if all(q["retained"] for q in quotes) else " ".join(retained)
    return {"rule": "live_own_subject_service_v8", "facility": facility,
            "programme": programme, "service_layer": own,
            "field_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "disposition": "own_posting" if own_asserted else "unresolved" if unresolved_own or not text else "no_own_restriction",
            "owned_reduction_text": projection, "clauses": quotes}


def _scoped_row(row: dict, scope: dict) -> dict:
    return dict(row, bed_or_space_reduction_text=scope["owned_reduction_text"])


def _own_assertion_polarity(row: dict) -> dict:
    # Low-level polarity probes without a source header remain pure lexical
    # probes. Runtime eligibility always requires the complete own card.
    if not row.get("program_or_service") or not row.get("facility_name"):
        return _unscoped_assertion_polarity(row)
    scope = _live_subject_scope(row)
    return _unscoped_assertion_polarity(_scoped_row(row, scope))


def _structured_bound(value: object) -> datetime | None:
    """Only own full printed dates/clocks provide interval envelope authority."""
    text = _text(value)
    if not text or _norm(text) in {"tbd", "to be determined", "unknown", "ongoing", "until further notice"}:
        return None
    full = re.fullmatch(
        rf"\s*(?P<day>(?:{MONTH_PATTERN})\s+\d{{1,2}},?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})"
        r"(?:\s*(?:\(\s*(?P<paren>[^)]+)\s*\)|(?:at\s+)?(?P<clock>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{4})))?\s*",
        text, re.I)
    if full is None:
        raise RuntimeError("Unreceived or imprecise own structured date bound; current notice needs clarification")
    parsed = pd.to_datetime(full.group("day"), errors="coerce")
    if pd.isna(parsed):
        raise RuntimeError("Invalid own structured date bound; current notice needs clarification")
    printed_clock = full.group("paren") or full.group("clock")
    clock = _parse_clock(printed_clock) if printed_clock else time.min
    if clock is None:
        raise RuntimeError("Invalid own structured bound clock; current notice needs clarification")
    return _require_unique_local_clock(datetime.combine(parsed.date(), clock, tzinfo=LOCAL_TZ))


def _clip_own_bounds(row: dict, as_of: datetime, windows) -> list[tuple[datetime, datetime]]:
    """Common envelope for finite, complementary, recurring and date boxes."""
    lower = _structured_bound(row.get("start_date_text"))
    upper = _structured_bound(row.get("anticipated_end_date_text"))
    # A more specific own finite end clock on the forecast's printed date
    # wins over the date-only midnight. Foreign clauses were already removed.
    if upper is not None and re.fullmatch(rf"(?:{MONTH_PATTERN})\s+\d{{1,2}},?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}", _text(row.get("anticipated_end_date_text")), re.I):
        finite = _schedule_windows(_text(row.get("bed_or_space_reduction_text")), as_of, row)
        ends = [end for _, end in finite if end.date() == upper.date()]
        if ends:
            upper = max(ends, key=lambda x: x.astimezone(timezone.utc))
    low_utc = lower.astimezone(timezone.utc) if lower else None
    high_utc = upper.astimezone(timezone.utc) if upper else None
    if low_utc is not None and high_utc is not None and high_utc <= low_utc:
        raise RuntimeError("Own structured bounds do not establish a positive interval")
    result = []
    for start, end in windows:
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if low_utc is not None: start_utc = max(start_utc, low_utc)
        if high_utc is not None: end_utc = min(end_utc, high_utc)
        if start_utc < end_utc:
            result.append((start_utc.astimezone(LOCAL_TZ), end_utc.astimezone(LOCAL_TZ)))
    if windows and not result:
        raise RuntimeError("Own printed calendar lies entirely outside own structured bounds; current notice needs clarification")
    return result


def _runtime_windows(row: dict, as_of: datetime, base=None) -> list[tuple[datetime, datetime]]:
    scope = _live_subject_scope(row)
    if scope["disposition"] != "own_posting":
        return []
    owned = _scoped_row(row, scope)
    return _clip_own_bounds(owned, as_of, _runtime_windows_unclipped(owned, as_of, base))


def _negated_closed_state(row: dict) -> bool:
    return _own_assertion_polarity(row)["negative_only"]


def _positive_closed_state(row: dict) -> bool:
    return _own_assertion_polarity(row)["closed"]


def _positive_adjusted_opening(row: dict) -> bool:
    return _own_assertion_polarity(row)["adjusted"]


def _independent_positive_restriction(row: dict) -> bool:
    facts = _own_assertion_polarity(row)
    return any(facts[k] for k in ("closed", "adjusted", "physician", "service"))


def _ordinary_hours_without_restriction(row: dict) -> bool:
    """A baseline timetable is not a positive posted restriction assertion."""
    text = _text(row.get("bed_or_space_reduction_text"))
    ordinary = bool(re.search(r"\b(?:regular|normal|usual)\s+operating hours\b", text, re.I))
    return ordinary and not _independent_positive_restriction(row)


def _regular_hours_complement_windows(row: dict, as_of: datetime, base=None) -> list[tuple[datetime, datetime]]:
    """Use opening complement only for an explicit positive adjusted-open restriction.

    Ordinary baseline hours and a negated full closure do not establish loss
    outside those hours. Source-owned opening restrictions must be positive.
    """
    text = _text(row.get("bed_or_space_reduction_text"))
    if _negated_closed_state(row) or _positive_closed_state(row) or not _positive_adjusted_opening(row):
        return []
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


def _hardisty_announced_weekend_windows(row: dict, as_of: datetime):
    text = _text(row.get("bed_or_space_reduction_text"))
    if _norm(row.get("facility_name")) != _norm("Hardisty Health Centre") or _norm(row.get("program_or_service")) != "emergency department":
        return None
    if not re.search(r"emergency department closed Fridays, Saturdays and Sundays", text, re.I):
        return None
    onset = _parse_date(row.get("start_date_text"))
    anchor = re.search(r"starting (" + MONTH_PATTERN + r")\s+(\d{1,2})\s*\(([^)]+)\)", text, re.I)
    if onset is None or anchor is None:
        raise RuntimeError("Hardisty announced weekday pattern lacks source onset")
    first_clock = _parse_clock(anchor.group(3))
    if first_clock is None or (onset.month, onset.day) != (MONTHS[anchor.group(1).lower()], int(anchor.group(2))):
        raise RuntimeError("Hardisty onset date/clock conflict")
    if onset.date().weekday() != 4:
        raise RuntimeError("Unreceived Hardisty weekday-pattern onset; current notice needs clarification")
    first = _require_unique_local_clock(datetime.combine(onset.date(), first_clock, tzinfo=LOCAL_TZ))
    bound = _parse_date(row.get("anticipated_end_date_text"))
    end_date = bound.date() if bound is not None else as_of.date() + timedelta(days=366)
    end = _structured_bound(row.get("anticipated_end_date_text")) or datetime.combine(end_date, time.min, tzinfo=LOCAL_TZ)
    day = onset.date()
    windows = []
    while day < end_date:
        if day.weekday() == 4:
            start = max(first, datetime.combine(day, time.min, tzinfo=LOCAL_TZ))
            stop = min(end, datetime.combine(day + timedelta(days=3), time.min, tzinfo=LOCAL_TZ))
            if start < stop:
                windows.append((start, stop))
        day += timedelta(days=1)
    return windows


def _runtime_windows_unclipped(row: dict, as_of: datetime, base=None) -> list[tuple[datetime, datetime]]:
    """Return local-time windows for browser-side Active now classification."""
    # The card's explicit finite calendar takes precedence over a legacy
    # episode builder that can trim dates relative to its internal clock.
    announced_weekends = _hardisty_announced_weekend_windows(row, as_of)
    if announced_weekends is not None:
        return announced_weekends
    explicit = _schedule_windows(_text(row.get("bed_or_space_reduction_text")), as_of, row=row)
    if explicit:
        return explicit
    reduction = _text(row.get("bed_or_space_reduction_text"))
    if re.search(r"\b(?:regular|normal|usual) operating hours\b", reduction, re.I) and not _positive_adjusted_opening(row):
        # An independent whole closure/physician assertion can coexist with a
        # baseline timetable. The timetable never defines its complement.
        own_assertion = re.split(r"\b(?:regular|normal|usual) operating hours\b", reduction, maxsplit=1, flags=re.I)[0]
        if _has_schedule_cue({"bed_or_space_reduction_text": own_assertion}):
            raise RuntimeError("Unreceived restriction schedule mixed with ordinary baseline context; current notice needs clarification")
        return []
    regular_complement = _regular_hours_complement_windows(row, as_of, base=base)
    if regular_complement:
        return regular_complement
    recurring_cue = bool(re.search(r"\b(?:daily|nightly|overnight|each night|weekends?|weekdays?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|adjusted hours|regular operating hours)\b", reduction, re.I))
    if recurring_cue and re.search(r"\b(?:alternate|alternating|every other|except|excluding)\b", reduction, re.I):
        raise RuntimeError("Unreceived recurrence exception or alternating pattern; current notice needs clarification")
    end_bound = _structured_bound(row.get("anticipated_end_date_text"))
    if not recurring_cue and end_bound is not None:
        onset = _parse_date(row.get("start_date_text"))
        if onset is None:
            raise RuntimeError("Date-only current interval has no defensible source onset")
        start = _structured_bound(row.get("start_date_text"))
        if start >= end_bound:
            raise RuntimeError("Date-only current bounds do not establish a positive interval")
        return [(start, end_bound)]
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
        parsed = [(start, end) for start, end in parsed if start < end]
        if parsed:
            if recurring_cue and any(item.get("interval_method") not in {"daily_window_schedule", "listed_weekday_schedule", "regular_hours_complement", "weekend_schedule", "weekly_clock_schedule"} for item in intervals):
                raise RuntimeError("Recurring wording received only a generic date fallback; current notice needs clarification")
            return parsed
    if recurring_cue:
        raise RuntimeError("Unparsed current recurring schedule; current notice needs clarification")

    # Finite clocks were already checked in the own reduction field. The
    # combined raw block contains labelled start/end dates and Reason context;
    # those fields must not become a second calendar grammar authority.

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
    end_day = (end_date or start_date).date()
    end = datetime.combine(end_day, time.min, tzinfo=LOCAL_TZ)
    return [(start, end)]


def _classify_row(row: dict, as_of: datetime, base=None) -> str | None:
    scope = _live_subject_scope(row)
    if scope["disposition"] == "unresolved":
        upper = _structured_bound(row.get("anticipated_end_date_text"))
        if upper is not None and upper.astimezone(timezone.utc) <= as_of.astimezone(timezone.utc):
            return None
        return "unclear"
    if scope["disposition"] != "own_posting":
        return None
    row = _scoped_row(row, scope)
    lower = _structured_bound(row.get("start_date_text"))
    upper = _structured_bound(row.get("anticipated_end_date_text"))
    windows = _runtime_windows(row, as_of, base=base)
    moment = as_of.astimezone(timezone.utc)
    if windows:
        if any(start.astimezone(timezone.utc) <= moment < end.astimezone(timezone.utc) for start, end in windows):
            return "active_now"
        if any(start.astimezone(timezone.utc) > moment for start, _ in windows):
            return "scheduled"
        return None
    if lower is not None and lower.astimezone(timezone.utc) > moment:
        return "scheduled"
    if upper is not None and upper.astimezone(timezone.utc) <= moment:
        return None
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
        "status_label": "Active now" if status == "active_now" else "Status unclear" if status == "unclear" else "Scheduled / recurring",
        "status_explanation": "The posting is retained, but its current timing or service restriction needs clarification." if status == "unclear" else "",
        "start_date_key": (_parse_date(row.get("start_date_text")) or pd.Timestamp.min).strftime("%Y-%m-%d") if _parse_date(row.get("start_date_text")) is not None else "",
        "end_date_key": (_parse_date(row.get("anticipated_end_date_text")) or pd.Timestamp.min).strftime("%Y-%m-%d") if _parse_date(row.get("anticipated_end_date_text")) is not None else "",
        "schedule_windows": [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in (schedule_windows or [])
        ],
        "timing_note": ("Posted Friday–Sunday pattern; calendar-day bounds are 00:00 to Monday 00:00. Only the first December 5 onset uses the printed 07:00 clock. Future occurrences are announced schedules, not verified downtime." if _norm(row.get("facility_name")) == _norm("Hardisty Health Centre") and re.search(r"closed Fridays, Saturdays and Sundays", reduction, re.I) else "Anticipated dates are forecasts. A date-only end uses local 00:00 as an analytical boundary unless the card prints a more specific clock; it is not a verified restoration time." if _parse_date(row.get("anticipated_end_date_text")) is not None else ""),
        "details": raw or "; ".join(x for x in (service, reduction, reason) if x),
        "source_url": _text(row.get("snapshot_url")) or LIVE_URL,
    }


def build_live_data(template_path: Path, base_script: Path, as_of: str | None = None, *, private_receipts_dir=None, fetch_source=None, decision_sink=None) -> tuple[dict, list[dict]]:
    existing_data = public_constant(template_path.read_text(encoding="utf-8"), "DATA")
    if not isinstance(existing_data, dict) or not existing_data.get("sites"):
        raise RuntimeError("Historical public registry contains no sites")
    lookup, sites = _site_lookup(existing_data)
    live_moment = _as_of_datetime(as_of)
    live_date = pd.Timestamp(live_moment.date()).normalize()
    base = _load_base_script(base_script)
    live_url = getattr(base, "LIVE_CURRENT_URL", LIVE_URL)
    capture = SourceCapture(private_receipts_dir, fetch_source)
    decisions = []
    original_get_html = base.get_html
    from bs4 import BeautifulSoup
    def checked_html(url):
        text = capture.get_html(url)
        # The legacy scraper can perform POSTs outside get_html. Reject those
        # advertised actions before it can enqueue or execute any such request.
        if any(x.get("kind") != "get" for x in base._discover_snapshot_page_candidates(BeautifulSoup(text, "lxml"), url)):
            raise RuntimeError("Unreceived POST pagination; preserving accepted Live data")
        return text
    base.get_html = checked_html
    receipt = {"schema": "LIVE_PRIVATE_COLLECTION_RECEIPT_V8", "schema_version": 8,
               "status": "HOLD", "candidate_accepted": False, "complete_census": False,
               "source_pages": capture.pages, "source_sections": 0, "decision_count": 0,
               "retained": 0, "excluded": 0, "unclear": 0, "unmapped": 0}
    try:
        try:
            rows = base.scrape_snapshot(live_date, live_url)
        finally:
            base.get_html = original_get_html
        if capture.failures or not capture.pages or rows is None or rows.empty:
            raise RuntimeError("Incomplete or empty current AHS collection; preserving accepted Live data")
        if len(rows) > 2000:
            raise RuntimeError("Current AHS card count exceeds the bounded census")
        pagination = require_advertised_pagination_closed(base, capture.pages, capture.source_text)
        census = require_own_current_census(rows.to_dict(orient="records"), capture.source_text)
        receipt.update(source_sections=census["source_sections"], complete_census=True,
                       pagination=pagination, census=census)
        grouped = {}
        unmapped = []
        seen = set()
        for number, original in enumerate(rows.to_dict(orient="records")):
            row = dict(original)
            raw_fields = {key: _text(row.get(key)) for key in ("facility_name", "community_heading",
                "program_or_service", "bed_or_space_reduction_text", "reason_text", "start_date_text",
                "anticipated_end_date_text", "raw_block_text", "snapshot_url")}
            row_id = hashlib.sha256(json.dumps(raw_fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            decision = {"source_row": number, "row_id": row_id, "raw_fields": raw_fields}
            diagnostic = None
            try:
                if any(len(value) > 32768 for value in raw_fields.values()):
                    raise RuntimeError("Current field exceeds the bounded interpretation size")
                scope = _live_subject_scope(row)
                decision["scope"] = scope
                status = _classify_row(row, live_moment, base=base)
                windows = _runtime_windows(row, live_moment, base=base) if status not in (None, "unclear") else []
            except Exception as exc:
                status, windows = "unclear", []
                diagnostic = {"type": type(exc).__name__, "message": str(exc)[:300]}
            if status is None:
                scope = decision.get("scope", {})
                fate = "excluded_no_own_restriction" if scope.get("disposition") == "no_own_restriction" else "excluded_expired"
                decision.update(fate=fate, status=None)
                receipt["excluded"] += 1
            else:
                canonical = _resolve_site(row, lookup, sites)
                if canonical is None:
                    status = "unclear"
                    windows = []
                disruption = _row_to_disruption(row, status, schedule_windows=windows)
                key = (canonical or raw_fields["facility_name"], disruption["service"], row_id)
                if key in seen:
                    decision.update(fate="excluded_exact_duplicate", status=None)
                    receipt["excluded"] += 1
                else:
                    seen.add(key)
                    decision.update(fate="retained_" + status, status=status, site=canonical,
                                    service_layer=disruption["service_layer"],
                                    schedule_windows=disruption["schedule_windows"])
                    if diagnostic:
                        decision["diagnostic"] = diagnostic
                    receipt["retained"] += 1
                    receipt["unclear"] += int(status == "unclear")
                    if canonical is None:
                        receipt["unmapped"] += 1
                        disruption.update(facility_name=raw_fields["facility_name"], community=raw_fields["community_heading"])
                        unmapped.append(disruption)
                    else:
                        site = sites[canonical]
                        if canonical not in grouped:
                            grouped[canonical] = {"name": site.get("name", canonical),
                                "display_name": site.get("display_name") or site.get("facility_name") or site.get("name", canonical),
                                "facility_name": site.get("facility_name", ""), "community": site.get("community", ""),
                                "lat": site.get("lat"), "lng": site.get("lng"), "disruptions": []}
                        grouped[canonical]["disruptions"].append(disruption)
            decisions.append(decision)
            if decision_sink:
                decision_sink(dict(decision))
        receipt["decision_count"] = len(decisions)
        if len(decisions) != receipt["source_sections"] or receipt["retained"] + receipt["excluded"] != len(decisions):
            raise RuntimeError("Not every current source card received an explicit fate")
        live_sites = sorted(grouped.values(), key=lambda site: _norm(site.get("display_name")))
        for site in live_sites:
            site["disruption_count"] = len(site["disruptions"])
            site["active_now_count"] = sum(x["status"] == "active_now" for x in site["disruptions"])
            site["scheduled_count"] = sum(x["status"] == "scheduled" for x in site["disruptions"])
        all_items = [item for site in live_sites for item in site["disruptions"]] + unmapped
        data = {"schema_version": LIVE_SCHEMA_VERSION, "as_of": live_date.strftime("%Y-%m-%d"),
            "as_of_timestamp": live_moment.isoformat(), "generated_at": datetime.now(LOCAL_TZ).isoformat(),
            "source_url": live_url, "source_fetched_at": capture.pages[-1]["fetched_at"],
            "source_publisher_updated": capture.publisher_updated(), "source_page_count": len(capture.pages),
            "demo_fixture": capture.injected, "sites": live_sites, "unmapped_disruptions": unmapped,
            "active_disruption_count": len(all_items), "active_site_count": len(live_sites),
            "active_now_disruption_count": sum(x["status"] == "active_now" for x in all_items),
            "scheduled_disruption_count": sum(x["status"] == "scheduled" for x in all_items),
            "unclear_disruption_count": sum(x["status"] == "unclear" for x in all_items),
            "active_now_site_count": sum(site["active_now_count"] > 0 for site in live_sites),
            "scheduled_site_count": sum(site["scheduled_count"] > 0 for site in live_sites),
            "clock_contract": browser_clock_contract()}
        receipt.update(status="ACCEPTED", candidate_accepted=True, source_capture_id=capture.capture_id)
        write_private_json(private_receipts_dir, "Card_Decisions.json",
                           {"schema": "LIVE_PRIVATE_CARD_DECISIONS_V8", "count": len(decisions), "decisions": decisions})
        write_private_json(private_receipts_dir, "Collection_Receipt.json", receipt)
        return data, unmapped
    except Exception as exc:
        base.get_html = original_get_html
        receipt.update(status="HOLD", candidate_accepted=False, source_capture_id=capture.capture_id,
                       source_pages=capture.pages, decision_count=len(decisions),
                       error={"type": type(exc).__name__, "message": str(exc)[:300]})
        write_private_json(private_receipts_dir, "Card_Decisions.json",
                           {"schema": "LIVE_PRIVATE_CARD_DECISIONS_V8", "count": len(decisions), "decisions": decisions})
        write_private_json(private_receipts_dir, "Collection_Receipt.json", receipt)
        write_private_json(private_receipts_dir, "Collection_Error.json", {"status":"HOLD", "error":receipt["error"]})
        raise



def _js_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


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
    frontend = Path(__file__).with_name("live_frontend_v8.js").read_text(encoding="utf-8")
    observed = html.escape(_format_mt(data["source_fetched_at"]), quote=True)
    publisher_date = html.escape(data.get("source_publisher_updated") or "not stated", quote=True)
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
    .live-card.unclear { border-left-color:#667085; background:#F4F5F7; }
    .status-dot.unclear { background:#667085; }
    .status-dot.mixed { background:#7A3E9D; }
    .live-status { display:inline-block; margin-bottom:.25rem; color:#B42318; font-size:.64rem; font-weight:400; letter-spacing:.1em; text-transform:uppercase; }
    .live-status.scheduled { color:#9A6500; }
    .live-status.unclear { color:#475467; }
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
      <div class="subtitle">Current AHS disruption postings · status follows the posted schedule{' · <span class="demo-badge">sample fixture data</span>' if data.get('demo_fixture') else ''}<br><span id="sourceObserved">Last checked: {observed}</span><br><span id="sourcePublisher">AHS page updated: {publisher_date}</span><br><span id="liveClock">Current Alberta time loading…</span></div>
    </div>
    <nav class="sorc-mode-switch" aria-label="SORCTracks mode">
      <a href="sorctracks_tool.html">SORCTracks</a>
      <a class="active" href="sorctracks_live.html" aria-current="page">SORCTracks Live</a>
    </nav>
    <div class="live-summary" id="summaryStats"><strong>{data["active_now_disruption_count"]}</strong> active now · <strong>{data["scheduled_disruption_count"]}</strong> scheduled / recurring · <strong>{data["active_site_count"]}</strong> sites shown</div>
      <div class="control-pair" role="group" aria-label="Live map filters">
      <div class="view-control"><label for="statusSelect">Map view</label><select id="statusSelect"><option value="all">All notices</option><option value="active_now">Active now</option><option value="scheduled">Scheduled / recurring</option><option value="unclear">Status unclear</option></select></div>
      <div class="view-control"><label for="serviceSelect">Service</label><select id="serviceSelect"><option value="all">All analyzed services</option><option value="ed">Emergency</option><option value="ob">Obstetrics</option><option value="acute">Acute Care</option><option value="surgery">Surgery</option><option value="other">Other Services</option></select></div>
      </div>
      <div id="liveHealth" class="live-health" role="status" aria-live="polite" hidden></div>
  </header>
  <div id="map" role="region" aria-label="Map of current Alberta service disruptions"></div>
  <main class="sidebar" id="sidebar" aria-label="Current service disruption data">
    <div class="panel-label">Live disruption notices</div>
    <div class="search-panel"><input id="search" class="search-input" type="search" placeholder="Search site or service" aria-label="Search live disruptions" /><div class="live-filter-note">Status follows AHS postings and announced schedules; it is not a real-time confirmation of service availability. Grey notices need clarification. Future windows are forecasts.</div><div class="status-legend"><span class="status-key"><span class="status-dot now"></span> Active now</span><span class="status-key"><span class="status-dot scheduled"></span> Scheduled / recurring</span><span class="status-key"><span class="status-dot mixed"></span> Mixed</span><span class="status-key"><span class="status-dot unclear"></span> Status unclear</span></div><div id="resultCount" class="result-count"></div></div>
    <div id="list"></div>
  </main>
  <footer><div>Current AHS service-disruption notices &middot; SORC, 2026</div><div>Source: <a href="{source}" target="_blank" rel="noopener">AHS current service-disruption page</a> &middot; <a href="https://sorc.ca">sorc.ca</a></div></footer>
  <script>
    const LIVE_DATA = {_js_json(data)};
{frontend}
  </script>
</body>
</html>'''


def build_live_page(template_path: Path, base_script: Path, output_path: Path, as_of: str | None = None, *, private_receipts_dir=None, fetch_source=None, decision_sink=None) -> dict:
    data, unmapped = build_live_data(template_path, base_script, as_of=as_of,
        private_receipts_dir=private_receipts_dir, fetch_source=fetch_source, decision_sink=decision_sink)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_live_html(data, template_path=template_path), encoding="utf-8")
    receipt = json.loads((Path(private_receipts_dir) / "Collection_Receipt.json").read_text()) if private_receipts_dir is not None else {}
    return {"output": str(output_path), "schema_version": 8, "candidate_accepted": True,
        "complete_census": True, "sites": data["active_site_count"],
        **{key: receipt.get(key) for key in ("decision_count", "retained", "excluded", "unclear", "unmapped", "source_capture_id")},
        "active_now_disruptions": data["active_now_disruption_count"],
        "scheduled_disruptions": data["scheduled_disruption_count"],
        "unclear_disruptions": data["unclear_disruption_count"], "unmapped_disruptions": len(unmapped),
        "as_of": data["as_of"]}



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True, help="Current historical sorctracks_tool.html")
    parser.add_argument("--base-script", type=Path, required=True, help="AHS v84 base scraper script")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", help="Override current date (YYYY-MM-DD), mainly for testing")
    parser.add_argument("--private-receipts-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_live_page(args.template, args.base_script, args.output, as_of=args.as_of, private_receipts_dir=args.private_receipts_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


