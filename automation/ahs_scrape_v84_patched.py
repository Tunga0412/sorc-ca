
import os
import re
import sys
import calendar
import hashlib
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ============================================================
# CONFIG
# ============================================================
PIPELINE_VERSION = "v84"


ARCHIVE_URL = "https://www.albertahealthservices.ca/br/Page17634.aspx"
BASE_URL = "https://www.albertahealthservices.ca"
NOTICE_ARCHIVE_URL = "https://www.albertahealthservices.ca/br/Page17601.aspx"
NOTICE_RELEASES_INDEX_URL = "https://www.albertahealthservices.ca/news/newsreleases.aspx"
NOTICE_ARCHIVE_FALLBACK_LISTING_URLS = [
    "https://www.albertahealthservices.ca/news/Listing16961.aspx",
    "https://www.albertahealthservices.ca/news/Listing16960.aspx",
    "https://www.albertahealthservices.ca/news/Listing16959.aspx",
    "https://www.albertahealthservices.ca/news/Listing16958.aspx",
    "https://www.albertahealthservices.ca/news/Listing16957.aspx",
]

SCRAPE_START = "2021-08-01"
SCRAPE_END = "2026-12-31"

ANALYSIS_START = "2021-08-01"
ANALYSIS_END = "2026-12-31"

# Parser year-capture policy: a bare 4-digit token is treated as a calendar
# year only if it falls in this deliberately narrow source-era band. Outside
# this band, 4-digit tokens remain available to be parsed as military times
# where time grammar expects them. This prevents 2300/0700 from being consumed
# as years while retaining current/future AHS data through mid-century.
PARSER_YEAR_MIN = 2020
PARSER_YEAR_MAX = 2050
PARSER_YEAR_REGEX = r"(?:20[2-4]\d|2050)"
# In free-text interval grammars, a plausible 4-digit year is only accepted
# as a year when it appears in an explicit year slot, normally Month Day, Year.
# Bare Month Day 2030 is treated as Month Day at 20:30 when a time is expected.

MAX_PAGES = None
REQUEST_TIMEOUT = 30

OUTPUT_PREFIX = "v84_ahs_archive"
SITE_ALIASES_FILE = "site_aliases_template.csv"
SITE_METADATA_FILE = "v49_site_metadata_template.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AHS-ED-scraper/17.0)"
}

# ============================================================
# PARSER EXCEPTION QA
# ============================================================

PARSER_EXCEPTION_RECORDS = []

def _parser_row_context(row):
    try:
        r = dict(row) if row is not None else {}
    except Exception:
        r = {}
    return {
        "snapshot_url": r.get("snapshot_url") or r.get("notice_url"),
        "snapshot_date": r.get("snapshot_date"),
        "site_best": r.get("site_best") or r.get("site_id_final") or r.get("site_id_cmp"),
        "facility_name": r.get("facility_name"),
        "community_heading": r.get("community_heading"),
        "program_or_service": r.get("program_or_service"),
        "start_date_text": r.get("start_date_text"),
        "anticipated_end_date_text": r.get("anticipated_end_date_text"),
        "bed_or_space_reduction_text": r.get("bed_or_space_reduction_text"),
        "reason_text": r.get("reason_text"),
        "raw_block_text": r.get("raw_block_text"),
    }

def log_parser_exception(row, parser_stage: str, exc: Exception):
    ctx = _parser_row_context(row)
    ctx.update({
        "parser_stage": parser_stage,
        "exception_type": type(exc).__name__,
        "exception_text": str(exc),
    })
    PARSER_EXCEPTION_RECORDS.append(ctx)

def get_parser_exception_records():
    return list(PARSER_EXCEPTION_RECORDS)

# ============================================================
# CONSTANTS
# ============================================================

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
# Accept AHS month abbreviations with trailing periods (e.g., Jan., Feb., Mar.).
for _m_key, _m_val in list(MONTHS.items()):
    if _m_key and not _m_key.endswith(".") and len(_m_key) <= 4:
        MONTHS[_m_key + "."] = _m_val
MONTHS["sept."] = 9

WEEKDAY_NAME_TO_NUM = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

KNOWN_LABELS = [
    "Program or Service",
    "Bed or Space Reduction",
    "Reason",
    "Start Date",
    "Anticipated End Date",
]

MAJOR_URBAN_KEYWORDS = {
    "edmonton",
    "calgary",
    "red deer",
    "lethbridge",
    "medicine hat",
    "fort mcmurray",
    "grand prairie",
    "lloydminster",
}

REGIONAL_NONMETRO_KEYWORDS = {
    "peace river",
    "lac la biche",
    "drumheller",
    "hinton",
    "edson",
    "ponoka",
    "lacombe",
    "barrhead",
    "stettler",
    "high prairie",
    "rocky mountain house",
    "athabasca",
    "st paul",
    "wainwright",
    "coronation",
    "smoky lake",
    "redwater",
    "elk point",
    "milk river",
    "bow island",
    "beaverlodge",
    "boyle",
    "fort macleod",
    "swan hills",
    "wabasca",
    "cold lake",
    "wetaskiwin",
    "camrose",
    "vermilion",
    "fairview",
    "fox creek",
    "hanna",
    "hardisty",
    "tofield",
    "viking",
    "brooks",
    "banff",
    "canmore",
    "spirit river",
    "fort vermilion",
    "two hills",
    "grimshaw",
    "consort",
    "oyen",
}

HIGH_CONF_METHODS = {
    "explicit_multi_interval",
    "single_day_parenthetical",
    "begin_resume",
    "narrative_overnight_explicit",
    "notice_custom_same_day_window",
    "notice_custom_multi_date_window",
    "notice_custom_date_at_time_range",
    "notice_custom_closed_reopen",
    "notice_custom_duration_begin_reopen",
    "notice_custom_duration_beginning",
    "notice_custom_explicit_range",
    "notice_custom_from_time_date_to_time_date",
    "notice_custom_close_reopen",
    "notice_custom_between",
    "explicit_parenthetical_variants",
    "explicit_dated_natural_language",
    "same_day_named_date_window",
    "explicit_weekday_date_time_range",
}

ESTIMATED_METHODS = {
    "daily_window_schedule",
    "weekday_night_schedule",
    "regular_hours_complement",
    "regular_hours_only_closure",
    "weekend_schedule",
    "weekly_named_range_schedule",
    "overnight_date_range_schedule",
    "full_closure_addon_schedule",
    "listed_weekday_full_day_schedule",
    "daily_window_schedule_tbd_proxy",
    "weekday_night_schedule_tbd_proxy",
    "regular_hours_complement_tbd_proxy",
    "regular_hours_only_closure_tbd_proxy",
    "listed_weekday_schedule",
    "listed_weekday_schedule_tbd_proxy",
    "listed_weekday_full_day_schedule_tbd_proxy",
    "weekend_schedule_tbd_proxy",
    "weekly_named_range_schedule_tbd_proxy",
    "overnight_date_range_schedule_tbd_proxy",
    "full_closure_addon_schedule_tbd_proxy",
}

COARSE_METHODS = {
    "fallback_datetime_range",
}

GENERIC_SITE_TOKENS = {
    "health", "healthcare", "hospital", "centre", "center",
    "complex", "community", "municipal", "general", "care",
    "district", "medical", "services", "service", "facility",
    "clinic"
}

DIRECTION_TOKENS = {"e", "w", "n", "s", "east", "west", "north", "south"}

SITE_CANONICAL_OVERRIDES = {
    "william j cadzow lac la biche": "lac la biche",
    "george mcdougall smoky lake": "smoky lake",
    "george mcdougall smoky lake smoky lake": "smoky lake",
    "high prairie health complex": "high prairie",
    "peace river community": "peace river",
    "beaverlodge municipal": "beaverlodge",
    "central peace health complex": "spirit river",
    "st theresa general": "fort vermilion",
    # Historical AHS rows sometimes spell the same Fort Vermilion site as
    # "Fort Vermillion". Canonicalize the typo before site-year aggregation
    # so multi-year summaries, monthly panels, and episode-year files agree.
    "fort vermillion": "fort vermilion",
    # Sacred Heart Community Health Centre is the McLennan ED site. Snapshot
    # rows often use the community heading McLennan while notice/manual rows
    # can use Sacred Heart. Treat them as one site before unioning.
    "sacred heart": "mclennan",
    "sacred heart community": "mclennan",
    "wabasca desmarais": "wabasca",
    "big country": "oyen",
    "grimshaw berwyn": "grimshaw",
    "grimshaw berwyn district": "grimshaw",
    "grimshaw berwyn district community": "grimshaw",
    "daylsand": "daysland",
}

# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(x):
    if x is None or pd.isna(x):
        return None
    x = str(x)
    x = x.replace("\xa0", " ")
    x = x.replace("–", "-").replace("—", "-")
    # Normalize observed AHS typo variants before interval parsing.
    # Example: Cold Lake notices sometimes say "Juen 24"; treating this as
    # unparseable caused half-parsed military-time lists and downstream artifacts.
    x = re.sub(r"\bJuen\b", "June", x, flags=re.I)
    x = re.sub(r"\s+", " ", x).strip()
    month_pat = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    x = re.sub(rf"\b({month_pat})\s+\1\b", r"\1", x, flags=re.I)
    return x or None


def safe_lower(x):
    if x is None or pd.isna(x):
        return ""
    return clean_text(x).lower()


def month_regex():
    # Supports full month names and AHS abbreviations with optional periods, e.g., Jan., Feb., Mar., Sept.
    return (
        r"(?:Jan\.?(?:uary)?|Feb\.?(?:ruary)?|Mar\.?(?:ch)?|Apr\.?(?:il)?|May|"
        r"Jun\.?(?:e)?|Jul\.?(?:y)?|Aug\.?(?:ust)?|Sep\.?(?:t\.?)?(?:ember)?|Oct\.?(?:ober)?|"
        r"Nov\.?(?:ember)?|Dec\.?(?:ember)?)"
    )


def weekday_regex():
    return r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"


def time_expr_regex():
    return r"(?:\d{3,4}\s*h\b|\d{3,4}h?|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)|noon|midnight)"


def get_html(url):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def get_soup(url):
    return BeautifulSoup(get_html(url), "lxml")




def normalize_interval_parse_text(x):
    s = clean_text(x) or ""
    if not s:
        return s
    month_pat = month_regex()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(rf"\b({month_pat})\s+\1\b", r"\1", s, flags=re.I)
    # For Coronation-style VEP notices, the first list gives the actual no-onsite
    # coverage/closure windows. The subsequent "with VEP coverage" list gives virtual
    # coverage subset windows and should not be parsed as additional closure intervals.
    s = re.split(r"the\s+site\s+will\s+be\s+without\s+onsite\s+physician\s+coverage\s+and\s+with\s+vep\s+coverage\s*:", s, flags=re.I)[0]
    s = re.sub(
        rf"(\)\s*(?:-|–|—|to|until)\s*(?:{month_pat}\s+)?\d{{1,2}}(?:\s*\([^)]+\)|\s+\d{{3,4}}|\s+\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm))?)\s+(?=(?:{month_pat})\s+\d{{1,2}}\s*\()",
        r"\1; ",
        s,
        flags=re.I,
    )
    return s
def parse_dt_any(x):
    if x is None or pd.isna(x):
        return pd.NaT
    return pd.to_datetime(str(x), errors="coerce")


def overlap_hours(start_a, end_a, start_b, end_b):
    latest_start = max(start_a, start_b)
    earliest_end = min(end_a, end_b)
    if earliest_end <= latest_start:
        return 0.0
    return (earliest_end - latest_start).total_seconds() / 3600.0


def sanitize_block_text(x):
    s = clean_text(x)
    if not s:
        return None

    stop_patterns = [
        r"\bClose\b.*$",
        r"\bShare\b.*$",
        r"\bReport a problem\b.*$",
        r"\bGo to top\b.*$",
        r"\b811 HEALTH LINK\b.*$",
        r"\bSEND US YOUR FEEDBACK\b.*$",
        r"\bONLINE PAYMENTS\b.*$",
        r"\bAbout this site\b.*$",
        r"\bDATA & REPORTING\b.*$",
        r"\bCONNECT WITH US\b.*$",
        r"©\s*\d{4}.*$",
    ]
    for pat in stop_patterns:
        s = re.sub(pat, "", s, flags=re.I)

    s = re.sub(r"\s+\|\s+", " | ", s)
    s = re.sub(r"\s+", " ", s).strip(" |")
    return s or None


def extract_first_date_substring(text):
    s = clean_text(text)
    if not s:
        return None

    patterns = [
        rf"({month_regex()}\s+\d{{1,2}},\s+\d{{4}}(?:\s+\d{{1,2}}:\d{{2}}(?:\s*[APap][Mm])?)?)",
        rf"({month_regex()}\s+\d{{1,2}},\s+\d{{4}})",
    ]
    for pat in patterns:
        m = re.search(pat, s, flags=re.I)
        if m:
            return clean_text(m.group(1))
    return None


def parse_clean_date_field(text):
    s = extract_first_date_substring(text)
    if not s:
        return pd.NaT
    return pd.to_datetime(s, errors="coerce")


def parse_time_token(tok, reject_year_like: bool = False):
    if tok is None or pd.isna(tok):
        return None
    s = str(tok).strip().lower()
    s = s.replace("–", "-").replace("—", "-").replace(".", "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\b(\d{3,4})h\b", r"\1", s)
    s = re.sub(r"(?<=\d)\s*h\b", "h", s)
    s = re.sub(r"(?<=\d)h\b", "", s)

    if s == "noon":
        return 12 * 60
    if s == "midnight":
        return 0

    # 3-4 digit military time, optionally written with trailing h.
    # Ambiguous 4-digit tokens such as 2030 are treated as time when the parser
    # grammar is asking for a time. Calendar years are accepted only by explicit
    # year-slot grammar, not by parse_time_token(). The reject_year_like argument
    # is retained for backward compatibility but intentionally ignored.
    if re.fullmatch(r"\d{3,4}", s):
        if len(s) == 3:
            s = "0" + s
        hh = int(s[:2])
        mm = int(s[2:])
        # AHS occasionally writes same-day intervals ending at 2400. Treat this
        # as midnight at the end of the stated date, not as an invalid time.
        if hh == 24 and mm == 0:
            return 24 * 60
        if hh > 23 or mm > 59:
            return None
        return hh * 60 + mm

    m = re.match(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = m.group(3)

    if ampm:
        if hh == 12:
            hh = 0
        if ampm == "pm":
            hh += 12

    if hh > 23 or mm > 59:
        return None

    return hh * 60 + mm




def is_calendar_year_token(tok) -> bool:
    """True when a candidate time token is actually a calendar year.

    Guards against calendar years or adjacent military-time tokens being
    consumed as date years/times in explicit interval regexes.
    """
    s_tok = str(tok or "").strip().lower().replace("h", "")
    return bool(re.fullmatch(r"\d{4}", s_tok) and PARSER_YEAR_MIN <= int(s_tok) <= PARSER_YEAR_MAX)

def is_valid_calendar_year_value(y) -> bool:
    try:
        y_int = int(y)
    except Exception:
        return False
    return PARSER_YEAR_MIN <= y_int <= PARSER_YEAR_MAX

def make_timestamp(year, month_name, day, time_token):
    mn = MONTHS.get(month_name.lower())
    mins = parse_time_token(time_token)
    if mn is None or mins is None:
        return pd.NaT
    hh, mm = divmod(mins, 60)
    try:
        if hh == 24 and mm == 0:
            return pd.Timestamp(year=year, month=mn, day=int(day)) + pd.Timedelta(days=1)
        return pd.Timestamp(year=year, month=mn, day=int(day), hour=hh, minute=mm)
    except Exception:
        return pd.NaT


def classify_interval_confidence(method):
    if method == "manual_fixed_interval":
        return "manual_fixed"
    if method in HIGH_CONF_METHODS:
        return "high"
    if method in ESTIMATED_METHODS:
        return "estimated"
    if method in COARSE_METHODS:
        return "coarse"
    return "unknown"


# ============================================================
# SITE TEXT CLEANING
# ============================================================

def looks_like_date_string(s):
    s = clean_text(s)
    if not s:
        return False

    if re.fullmatch(rf"{month_regex()}\s+\d{{1,2}},\s+\d{{4}}", s, flags=re.I):
        return True

    dt = pd.to_datetime(s, errors="coerce")
    return (not pd.isna(dt)) and bool(re.search(month_regex(), s, flags=re.I))


def is_bad_site_text(s):
    s = clean_text(s)
    if not s:
        return True

    low = s.lower()
    junk = [
        "close",
        "close ×",
        "share",
        "report a problem",
        "go to top",
        "temporary service disruptions",
        "archive",
        "details",
        "page",
        "alberta health services",
        "facilities",
        "updated",
        "health link",
        "feedback",
        "tbd",
        "to be determined",
        "unknown",
        "not available",
        "n/a",
    ]
    if any(j == low or j in low for j in junk):
        return True
    if low in {"x", "×", "na", "none"}:
        return True
    if looks_like_date_string(s):
        return True
    return False


def looks_like_site_heading(s):
    s = clean_text(s)
    if not s or is_bad_site_text(s):
        return False
    low = s.lower()
    site_terms = [
        "health centre",
        "health center",
        "hospital",
        "healthcare centre",
        "healthcare center",
        "community",
        "municipal",
        "complex",
        "medical",
        "centre",
        "center",
    ]
    if any(t in low for t in site_terms):
        return True
    if len(s) <= 60 and s.count(".") == 0:
        return True
    return False


def choose_best_site_label(facility_name, community_heading):
    """Choose the best human-readable site label using a community-first policy."""
    facility_name = clean_text(facility_name)
    community_heading = clean_text(community_heading)

    facility_ok = looks_like_site_heading(facility_name)
    community_ok = looks_like_site_heading(community_heading)

    if community_ok:
        return community_heading
    if facility_ok:
        return facility_name
    if community_heading and not is_bad_site_text(community_heading):
        return community_heading
    if facility_name and not is_bad_site_text(facility_name):
        return facility_name
    return None


def compress_duplicate_tokens(tokens):
    if not tokens:
        return tokens

    out = [tokens[0]]
    for tok in tokens[1:]:
        if tok != out[-1]:
            out.append(tok)
    tokens = out

    if len(tokens) >= 4:
        half = len(tokens) // 2
        if len(tokens) % 2 == 0 and tokens[:half] == tokens[half:]:
            return tokens[:half]

    for n in range(min(4, len(tokens) // 2), 0, -1):
        if tokens[-2 * n:-n] == tokens[-n:]:
            return tokens[:-n]

    return tokens


def normalize_site_tokens(x):
    x = clean_text(x)
    if not x or is_bad_site_text(x):
        return None

    x = x.lower()
    x = x.replace("&", " and ")
    x = x.replace("/", " ")
    x = x.replace("-", " ")
    x = x.replace(".", " ")
    x = x.replace("'", "")

    replacements = [
        "health centre",
        "health center",
        "healthcare centre",
        "healthcare center",
        "hospital and care centre",
        "hospital and care center",
        "hospital & care centre",
        "hospital & care center",
        "community health services",
        "community health service",
        "emergency department",
    ]
    for r in replacements:
        x = x.replace(r, " ")

    x = re.sub(r"[^a-z0-9]+", " ", x)
    tokens = [t for t in x.split() if t]

    while tokens and tokens[-1] in GENERIC_SITE_TOKENS:
        tokens.pop()
    while tokens and tokens[-1] in DIRECTION_TOKENS:
        tokens.pop()

    tokens = compress_duplicate_tokens(tokens)
    out = " ".join(tokens).strip()
    return out or None


def canonical_site_id(facility_name, community_heading):
    community_id = normalize_site_tokens(community_heading)
    facility_id = normalize_site_tokens(facility_name)

    # Conservative heading-bleed guard: if the community heading is a major urban catch-all
    # (for example Edmonton or Calgary) but the facility resolves to a specific non-urban site,
    # prefer the facility. This avoids rows such as Elk Point being mislabeled as Edmonton
    # when a rolling heading assignment bleeds across blocks.
    if community_id and facility_id and community_id != facility_id:
        community_is_major_urban = any(k == community_id or k in community_id for k in MAJOR_URBAN_KEYWORDS)
        facility_is_major_urban = any(k == facility_id or k in facility_id for k in MAJOR_URBAN_KEYWORDS)
        if community_is_major_urban and not facility_is_major_urban:
            candidate = facility_id
        else:
            candidate = community_id
    else:
        candidate = community_id if community_id else facility_id

    if candidate in SITE_CANONICAL_OVERRIDES:
        return SITE_CANONICAL_OVERRIDES[candidate]
    return candidate


# ============================================================
# RURALITY HELPERS
# ============================================================

def guess_rurality_from_site(site_best):
    s = safe_lower(site_best)
    if not s:
        return "unknown"
    if any(k in s for k in MAJOR_URBAN_KEYWORDS):
        return "urban"
    if any(k in s for k in REGIONAL_NONMETRO_KEYWORDS):
        return "regional_nonmetro"
    return "rural_small_town"


def guess_rurality_from_population(pop):
    if pop is None or pd.isna(pop):
        return None
    try:
        pop = float(pop)
    except Exception:
        return None
    if pop >= 100000:
        return "urban"
    if pop >= 10000:
        return "regional_nonmetro"
    return "rural_small_town"


# ============================================================
# NOTICE ARCHIVE HELPERS
# ============================================================

NOTICE_PAGE_STOP_MARKERS = {
    "share", "report a problem", "go to top", "811 health link",
    "send us your feedback", "online payments", "about this site"
}


def looks_like_notice_page_href(href):
    if not href:
        return False
    return bool(re.search(r"/news/Page\d+\.aspx(?:$|[?#])", href, flags=re.I))


def extract_listing_seed_urls_from_soup(soup, base_url):
    urls = []
    # direct linked seed pages from News Releases index (years, zones, etc.)
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        low = href.lower()
        if re.search(r"/news/page\d+\.aspx(?:$|[?#])", low) or re.search(r"/zones/page\d+\.aspx(?:$|[?#])", low):
            urls.append(href)
        if re.search(r"/news/listing\d+\.aspx(?:$|[?#])", low):
            urls.append(href)
    # some zone archive pages expose the real listing endpoint as plain text, not a link
    text = soup.get_text(" ", strip=True)
    for m in re.finditer(r"/news/listing\d+\.aspx", text, flags=re.I):
        urls.append(urljoin(BASE_URL, m.group(0)))
    # also catch plain-text URLs in raw html source
    html = str(soup)
    for m in re.finditer(r"/news/listing\d+\.aspx", html, flags=re.I):
        urls.append(urljoin(BASE_URL, m.group(0)))
    out = []
    seen = set()
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse_notice_archive_links(notice_archive_url=None):
    """
    Scrape the AHS notice-history stream.

    v46 fix:
    - do NOT treat Page17601 as a wrapper for a single Listing endpoint
    - start from the News Releases index
    - harvest year archive pages, zone archive pages, and any discovered /news/Listing*.aspx endpoints
    - then collect direct /news/Page*.aspx notice links across all seed pages
    """
    discovery_sources = []
    if notice_archive_url:
        discovery_sources.append(notice_archive_url)
    discovery_sources.extend([NOTICE_RELEASES_INDEX_URL, NOTICE_ARCHIVE_URL])

    seed_queue = []
    seen_seed = set()

    for url in discovery_sources:
        if not url or url in seen_seed:
            continue
        seen_seed.add(url)
        try:
            soup = get_soup(url)
        except Exception:
            continue
        seed_queue.extend(extract_listing_seed_urls_from_soup(soup, url))

    for url in NOTICE_ARCHIVE_FALLBACK_LISTING_URLS:
        if url not in seen_seed:
            seed_queue.append(url)

    # Breadth-first over discovered seed pages to catch zone archive -> listing indirection
    all_seed_pages = []
    seed_seen = set()
    while seed_queue:
        url = seed_queue.pop(0)
        if not url or url in seed_seen:
            continue
        seed_seen.add(url)
        all_seed_pages.append(url)
        try:
            soup = get_soup(url)
        except Exception:
            continue
        for extra in extract_listing_seed_urls_from_soup(soup, url):
            if extra not in seed_seen:
                seed_queue.append(extra)

    rows = []
    seen_notice_urls = set()
    for url in all_seed_pages:
        try:
            soup = get_soup(url)
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, a["href"])
            if not looks_like_notice_page_href(href):
                continue
            if href in seen_notice_urls:
                continue
            seen_notice_urls.add(href)
            rows.append({
                "notice_url": href,
                "notice_title_hint": clean_text(a.get_text(" ", strip=True)),
                "notice_archive_url": url,
            })

    out = pd.DataFrame(rows).drop_duplicates(subset=["notice_url"]).reset_index(drop=True)
    return out

def normalize_notice_title(title):
    title = clean_text(title)
    if not title:
        return None
    title = re.sub(r"^(?:update[d]?\s*:\s*)", "", title, flags=re.I)
    return clean_text(title)


NOTICE_TITLE_ED_PATTERN = re.compile(
    r"(?i)\b(?:emergency department|(?:^|[^A-Za-z])ED(?:$|[^A-Za-z]))\b"
)

NOTICE_TITLE_DISRUPTION_PATTERN = re.compile(
    r"""(?ix)
    \b(
        temporarily\ close|
        temporary\ closure|
        temporarily\ closed|
        closed\b|
        closure\b|
        close\ overnight|
        remain\ closed|
        remain\ temporarily\ closed|
        reducing\ hours|
        reduced\ hours|
        reduction\ in\ .*staffing|
        service\ advisory|
        service\ disruption|
        extending\ hours|
        temporarily\ closing|
        temporary\ closures
    )\b
    """
)

NOTICE_TITLE_EXCLUSION_PATTERN = re.compile(
    r"""(?ix)
    \b(
        upgrade|upgrades|entrance|renovation|renovations|construction|parking|
        funding|teddy\ bear|paramedics|safety|safe|influenza|immunization|
        covid|stroke|asthma|midwifery|healing\ gardens|quit\ smoking|vaping|
        public\ invited|engagement\ session|website|walk|bike|ATV|injury|
        child|children|window|balcon|west\ nile|pertussis|mental\ health|
        support\ line|volunteers|executive\ team|parking\ fees|family\ physician|
        after-hours\ clinic|ambulatory\ clinic|ambulatory\ care|clinic\b|
        registration|north\ entrance|reopen\b|open\b|resume\ full\ services|
        avoids?\ additional\ closures|now\ available|available\b|diagnose|treat|
        hospitality|moved?\b|move\b|relocating|relocate|services\ reopen|
        coverage\ found|coverage\ has\ been\ found|service\ advisory\ has\ now\ been\ cancelled|has\ now\ been\ cancelled|
        previously\ announced\ temporary\ service\ advisory
    )\b
    """
)

def classify_notice_title(title):
    title = normalize_notice_title(title) or ""
    if not title:
        return False, "missing_title"
    if not NOTICE_TITLE_ED_PATTERN.search(title):
        return False, "title_missing_ed"
    if NOTICE_TITLE_EXCLUSION_PATTERN.search(title):
        return False, "title_excluded"
    if not NOTICE_TITLE_DISRUPTION_PATTERN.search(title):
        return False, "title_missing_disruption_phrase"
    return True, "accepted"


def is_likely_ed_disruption_notice(title, body_text=None):
    keep, _reason = classify_notice_title(title)
    return keep


def extract_notice_facility_from_title(title):
    title = normalize_notice_title(title) or ""
    patterns = [
        r"^(?:temporary closure of (?:the )?)?(.*?)(?:\s+emergency department\b|\s+ed\b)",
        r"^(.*?)(?:\s+health centre\b.*?emergency department\b)",
    ]
    for pat in patterns:
        m = re.search(pat, title, flags=re.I)
        if m:
            facility = clean_text(m.group(1))
            if facility and not is_bad_site_text(facility):
                return facility
    return None


def extract_notice_heading_from_body(body_text):
    body_text = clean_text(body_text)
    if not body_text:
        return None
    m = re.match(r"^([A-Z][A-Z\s'\-]+?)\s+[–-]\s+", body_text)
    if m:
        heading = clean_text(m.group(1).title())
        if heading and not is_bad_site_text(heading):
            return heading
    return None



def parse_notice_page(notice_url):
    soup = get_soup(notice_url)
    lines = [clean_text(x) for x in soup.stripped_strings]
    lines = [x for x in lines if x]
    if not lines:
        return None

    title = None
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))
        if title and "|" in title:
            title = clean_text(title.split("|")[0])
    if not title:
        for line in lines:
            if len(line) > 10 and not looks_like_date_string(line) and line.lower() not in {"share", "news", "main menu"}:
                title = line
                break
    title = normalize_notice_title(title)
    if not title:
        return None

    # The useful article content appears near the last occurrence of the title in the
    # stripped-text stream, not near the top of the page chrome.
    norm_lines = [normalize_notice_title(x) or "" for x in lines]
    title_idx = None
    for i, line in enumerate(norm_lines):
        if line == title:
            title_idx = i
    if title_idx is None:
        title_low = safe_lower(title)
        for i, line in enumerate(lines):
            if safe_lower(line) == title_low or title_low in safe_lower(line):
                title_idx = i
    if title_idx is None:
        title_idx = 0

    date_idx = None
    date_text = None
    notice_date = pd.NaT
    search_end = min(len(lines), title_idx + 15)
    for i in range(title_idx + 1, search_end):
        dt = pd.to_datetime(lines[i], errors="coerce")
        if pd.isna(dt):
            continue
        if 2015 <= int(dt.year) <= 2100:
            date_idx = i
            date_text = lines[i]
            notice_date = pd.Timestamp(dt).normalize()
            break

    body_start = (date_idx + 1) if date_idx is not None else (title_idx + 1)
    body_lines = []
    for line in lines[body_start:]:
        low = safe_lower(line)
        if low in NOTICE_PAGE_STOP_MARKERS:
            break
        if low.startswith("for health advice 24/7"):
            break
        if low.startswith("report a problem"):
            break
        if low.startswith("submit feedback"):
            break
        if low.startswith("make a payment"):
            break
        if low == "about this site":
            break
        body_lines.append(line)

    body_text = clean_text(" ".join(body_lines))
    if body_text:
        body_text = re.sub(r"Alberta Health Services provides acute care services for more than five million Albertans.*$", "", body_text, flags=re.I)
        body_text = re.sub(r"Alberta Health Services \(AHS\) is the provincial health authority.*$", "", body_text, flags=re.I)
        body_text = clean_text(body_text)

    keep_title, filter_reason = classify_notice_title(title)
    is_ed_notice = keep_title
    facility_name = extract_notice_facility_from_title(title)
    community_heading = extract_notice_heading_from_body(body_text)

    reason_text = None
    if body_text:
        m = re.search(r"((?:Due to|Because of)[^.]+(?:\.)?)", body_text, flags=re.I)
        if m:
            reason_text = clean_text(m.group(1))

    return {
        "notice_url": notice_url,
        "notice_title": title,
        "notice_date_text": date_text,
        "notice_date": notice_date,
        "notice_body_text": body_text,
        "facility_name": facility_name,
        "community_heading": community_heading,
        "reason_text": reason_text,
        "is_emergency_department_notice": is_ed_notice,
        "notice_title_filter_reason": filter_reason,
    }

def notice_page_to_raw_row(page):

    title = clean_text(page.get("notice_title"))
    body = clean_text(page.get("notice_body_text"))
    date_text = clean_text(page.get("notice_date_text"))
    reason_text = clean_text(page.get("reason_text"))
    snapshot_date = pd.to_datetime(page.get("notice_date"), errors="coerce")

    reduction_text = body
    raw_block_parts = [p for p in [title, body] if p]
    raw_block_text = sanitize_block_text(" | ".join(raw_block_parts))

    return {
        "snapshot_date": snapshot_date,
        "snapshot_url": page.get("notice_url"),
        "snapshot_root_url": NOTICE_RELEASES_INDEX_URL,
        "snapshot_page_label": "notice_archive",
        "community_heading": page.get("community_heading"),
        "facility_name": page.get("facility_name"),
        "program_or_service": "Emergency Department",
        "bed_or_space_reduction_text": reduction_text,
        "reason_text": reason_text,
        "start_date_text": date_text,
        "anticipated_end_date_text": None,
        "raw_block_text": raw_block_text,
        "notice_title": title,
        "notice_date_text": date_text,
        "notice_body_text": body,
    }

# ============================================================
# ARCHIVE LINK EXTRACTION
# ============================================================

def parse_archive_links():
    soup = get_soup(ARCHIVE_URL)
    rows = []
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True))
        href = a["href"]
        if not text or not href:
            continue
        if "Page" not in href:
            continue
        dt = pd.to_datetime(text, errors="coerce")
        if pd.isna(dt):
            continue
        rows.append(
            {
                "snapshot_date_text": text,
                "snapshot_date": dt.normalize(),
                "snapshot_url": urljoin(BASE_URL, href),
            }
        )

    df = pd.DataFrame(rows).drop_duplicates()
    if df.empty:
        return df

    scrape_start = pd.Timestamp(SCRAPE_START)
    scrape_end = pd.Timestamp(SCRAPE_END)
    df = df[(df["snapshot_date"] >= scrape_start) & (df["snapshot_date"] <= scrape_end)].copy()
    df = df.sort_values("snapshot_date").reset_index(drop=True)

    if MAX_PAGES is not None:
        df = df.head(MAX_PAGES).copy()
    return df


# ============================================================
# SNAPSHOT SCRAPING
# ============================================================

def canonical_known_label(label):
    if not label:
        return None
    low = clean_text(label).lower()
    for known in KNOWN_LABELS:
        if known.lower() == low:
            return known
    return None


def parse_label_line(line):
    if not line:
        return None, None
    for label in KNOWN_LABELS:
        m = re.match(rf"^{re.escape(label)}\s*:\s*(.*)$", line, flags=re.I)
        if not m:
            continue
        value = clean_text(m.group(1))
        return label, value
    return None, None


def build_labeled_notice_block(fields):
    parts = []
    for label in KNOWN_LABELS:
        value = clean_text(fields.get(label))
        if value:
            parts.append(f"{label}: {value}")
    return sanitize_block_text(" | ".join(parts))


def split_combined_notice_blocks(text):
    s = sanitize_block_text(text) or clean_text(text)
    if not s:
        return []

    pattern = re.compile(r"(?=(?:Program or Service)\s*:\s*)", flags=re.I)
    positions = [m.start() for m in pattern.finditer(s)]
    if not positions:
        return [s]

    blocks = []
    for idx, start in enumerate(positions):
        end = positions[idx + 1] if idx + 1 < len(positions) else len(s)
        block = sanitize_block_text(s[start:end])
        if block:
            blocks.append(block)
    return blocks or [s]


def extract_trailing_heading_text(text):
    s = clean_text(text)
    if not s:
        return None
    m = re.search(r"Close\s*[×x]\s+(.+)$", s, flags=re.I)
    if not m:
        return None
    trailing = clean_text(m.group(1))
    if not trailing or is_bad_site_text(trailing):
        return None
    return trailing


def extract_trailing_heading_from_row_like(obj):
    """Search multiple text-bearing fields for a trailing 'Close × <heading>' artifact.

    This guards against malformed multi-service rows where the next site heading is appended
    into the current notice blob rather than remaining in page structure.
    """
    if obj is None:
        return None

    if isinstance(obj, dict):
        getters = obj.get
    else:
        getters = lambda k, default=None: getattr(obj, 'get', lambda kk, dd=None: default)(k, default) if hasattr(obj, 'get') else default

    for key in [
        'raw_block_text',
        'program_or_service',
        'bed_or_space_reduction_text',
        'reason_text',
        'start_date_text',
        'anticipated_end_date_text',
    ]:
        trailing = extract_trailing_heading_text(getters(key))
        if trailing:
            return trailing
    return None


def strip_trailing_heading_artifact(text):
    s = clean_text(text)
    if not s:
        return s
    s = re.sub(r"(?:\s*\|\s*)?Close\s*[×x]\s+.+$", "", s, flags=re.I)
    return clean_text(s)


def extract_fields_from_labeled_block(text):
    s = sanitize_block_text(text) or clean_text(text)
    if not s:
        return {k: None for k in KNOWN_LABELS}

    pattern = re.compile(
        rf"({'|'.join(re.escape(k) for k in KNOWN_LABELS)})\s*:\s*",
        flags=re.I,
    )
    matches = list(pattern.finditer(s))
    out = {k: None for k in KNOWN_LABELS}
    if not matches:
        return out

    for idx, m in enumerate(matches):
        label = canonical_known_label(m.group(1))
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(s)
        value = sanitize_block_text(strip_trailing_heading_artifact(s[start:end]))
        if label and value and not out.get(label):
            out[label] = value
    return out


def has_embedded_notice_payload(text):
    s = sanitize_block_text(text) or clean_text(text)
    if not s:
        return False
    blocks = split_combined_notice_blocks(s)
    if len(blocks) > 1:
        return True
    pattern = re.compile(rf"({'|'.join(re.escape(k) for k in KNOWN_LABELS)})\s*:\s*", flags=re.I)
    return len(list(pattern.finditer(s))) >= 2


def repair_notice_fields(fields):
    repaired = {k: clean_text(fields.get(k)) for k in KNOWN_LABELS}
    block = build_labeled_notice_block(repaired)
    if not block:
        return repaired
    blocks = split_combined_notice_blocks(block)
    if not blocks:
        return repaired
    parsed = extract_fields_from_labeled_block(blocks[0])
    for label in KNOWN_LABELS:
        if parsed.get(label):
            repaired[label] = parsed[label]
    return repaired


def expand_structured_notice_rows(base_row, fields):
    repaired = repair_notice_fields(fields)
    block = build_labeled_notice_block(repaired)
    blocks = split_combined_notice_blocks(block) or [block]

    out_rows = []
    for block in blocks:
        parsed = extract_fields_from_labeled_block(block)
        if not any(parsed.values()):
            continue
        row = dict(base_row)
        row["program_or_service"] = clean_text(parsed.get("Program or Service"))
        row["bed_or_space_reduction_text"] = sanitize_block_text(parsed.get("Bed or Space Reduction"))
        row["reason_text"] = sanitize_block_text(parsed.get("Reason"))
        row["start_date_text"] = sanitize_block_text(parsed.get("Start Date"))
        row["anticipated_end_date_text"] = sanitize_block_text(parsed.get("Anticipated End Date"))
        row["raw_block_text"] = build_labeled_notice_block(parsed)
        out_rows.append(row)

    if out_rows:
        return out_rows

    fallback = dict(base_row)
    fallback["program_or_service"] = clean_text(repaired.get("Program or Service"))
    fallback["bed_or_space_reduction_text"] = sanitize_block_text(repaired.get("Bed or Space Reduction"))
    fallback["reason_text"] = sanitize_block_text(repaired.get("Reason"))
    fallback["start_date_text"] = sanitize_block_text(repaired.get("Start Date"))
    fallback["anticipated_end_date_text"] = sanitize_block_text(repaired.get("Anticipated End Date"))
    fallback["raw_block_text"] = build_labeled_notice_block(repaired)
    return [fallback]


def row_to_labeled_notice_block(row):
    raw_block = sanitize_block_text(row.get("raw_block_text"))
    if raw_block:
        return raw_block
    fields = {
        "Program or Service": row.get("program_or_service"),
        "Bed or Space Reduction": row.get("bed_or_space_reduction_text"),
        "Reason": row.get("reason_text"),
        "Start Date": row.get("start_date_text"),
        "Anticipated End Date": row.get("anticipated_end_date_text"),
    }
    return build_labeled_notice_block(fields)


def explode_embedded_notice_rows(df):
    if df is None or df.empty:
        return df.copy()

    expanded_rows = []
    for _, row in df.iterrows():
        trailing_heading = extract_trailing_heading_from_row_like(row)

        block = row_to_labeled_notice_block(row)
        blocks = split_combined_notice_blocks(block) if block else []
        if not blocks:
            expanded_rows.append(row.to_dict())
            continue

        parsed_blocks = [extract_fields_from_labeled_block(block) for block in blocks]
        parsed_blocks = [p for p in parsed_blocks if any(p.values())]
        if not parsed_blocks:
            expanded_rows.append(row.to_dict())
            continue

        reassign_idx = None
        if trailing_heading and len(parsed_blocks) > 1:
            emergency_indexes = [
                i for i, parsed in enumerate(parsed_blocks)
                if is_emergency_department_service(parsed.get("Program or Service"))
            ]
            reassign_idx = emergency_indexes[-1] if emergency_indexes else len(parsed_blocks) - 1

        for idx, parsed in enumerate(parsed_blocks):
            new_row = row.to_dict()
            new_row["program_or_service"] = clean_text(parsed.get("Program or Service"))
            new_row["bed_or_space_reduction_text"] = sanitize_block_text(parsed.get("Bed or Space Reduction"))
            new_row["reason_text"] = sanitize_block_text(parsed.get("Reason"))
            new_row["start_date_text"] = sanitize_block_text(parsed.get("Start Date"))
            new_row["anticipated_end_date_text"] = sanitize_block_text(parsed.get("Anticipated End Date"))
            new_row["raw_block_text"] = build_labeled_notice_block(parsed)

            if trailing_heading and reassign_idx is not None and idx == reassign_idx:
                new_row["facility_name"] = trailing_heading
                new_row["community_heading"] = None

            expanded_rows.append(new_row)

    return pd.DataFrame(expanded_rows)


def is_empty_notice_stub_row(row):
    """Detect scraper stub rows that captured only the service label and no usable notice payload."""
    service = clean_text(row.get("program_or_service"))
    bed = clean_text(row.get("bed_or_space_reduction_text"))
    reason = clean_text(row.get("reason_text"))
    start = clean_text(row.get("start_date_text"))
    end = clean_text(row.get("anticipated_end_date_text"))
    raw_block = sanitize_block_text(row.get("raw_block_text"))
    if not service:
        return False
    if any([bed, reason, start, end]):
        return False
    if raw_block and raw_block.strip().lower() != f"program or service: {service}".lower():
        return False
    return True


def is_truncated_ed_stub_candidate(row):
    service = clean_text(row.get("program_or_service"))
    if not is_emergency_department_service(service):
        return False
    bed = clean_text(row.get("bed_or_space_reduction_text")) or ""
    reason = clean_text(row.get("reason_text")) or ""
    raw_block = sanitize_block_text(row.get("raw_block_text")) or ""
    start = clean_text(row.get("start_date_text"))
    end = clean_text(row.get("anticipated_end_date_text"))

    stub_like = safe_lower(bed) in {"the ed will", "the emergency department will", ""}
    raw_stub_like = any(tok in safe_lower(raw_block) for tok in ["the ed will", "the emergency department will"])
    has_dates = bool(start or end)
    return has_dates and (stub_like or raw_stub_like)


def find_truncated_ed_stub_candidates(raw_df):
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    tmp = raw_df.copy()
    for col in [
        "community_heading",
        "facility_name",
        "program_or_service",
        "bed_or_space_reduction_text",
        "reason_text",
        "start_date_text",
        "anticipated_end_date_text",
        "raw_block_text",
    ]:
        if col in tmp.columns:
            tmp[col] = tmp[col].map(clean_text)
    mask = tmp.apply(is_truncated_ed_stub_candidate, axis=1)
    cols = [c for c in [
        "snapshot_date", "snapshot_url", "community_heading", "facility_name", "program_or_service",
        "bed_or_space_reduction_text", "reason_text", "start_date_text", "anticipated_end_date_text", "raw_block_text"
    ] if c in tmp.columns]
    return tmp.loc[mask, cols].copy().reset_index(drop=True)


def is_emergency_department_service(program_or_service):
    s = safe_lower(program_or_service)
    if not s:
        return False

    if re.search(r"\bemergency departments?\b", s, flags=re.I):
        return True
    if re.search(r"\bemergency depts?\b", s, flags=re.I):
        return True
    if re.match(r"^emergency(?:\b|\s*(?:departments?|depts?)\b)", s, flags=re.I):
        return True
    return False


def is_scope_excluded_non_ed_burden_row(row):
    """Return True for mixed-service rows that should not contribute ED closure burden.

    These rows may include "Emergency Department" in a mixed program label, but
    the disruption described in the burden text is for another service only
    (for example internal medicine consult coverage, acute-care/detox beds, or
    inpatient admissions).  They should be removed before ED episode grouping so
    they do not become positive closure hours or zero-hour review rows.
    """
    if row is None or not hasattr(row, "get"):
        return False
    bed = safe_lower(row.get("bed_or_space_reduction_text"))
    raw = safe_lower(row.get("raw_block_text"))
    prog = safe_lower(row.get("program_or_service"))
    text = " ".join([bed, raw])

    # Drumheller-type rows: the ED program is listed alongside acute care, but
    # the actual disruption is internal-medicine consultation coverage, not ED
    # closure/access.
    if re.search(r"\b(?:no|unable to support|temporarily unable to support)\s+(?:internal medicine\s+)?consult", bed, flags=re.I) or re.search(r"consultations?\s+with\s+internal\s+medicine", bed, flags=re.I):
        return True

    # Swan Hills-type rows: acute-care/detox/inpatient bed closures scraped under
    # a mixed Acute Care & Emergency Department label. Keep the row only if the
    # burden text itself explicitly states ED/emergency closure, ED care spaces,
    # or no physician in the ED.
    non_ed_bed_language = re.search(r"\b(?:acute care|detox|inpatient|admissions? held|beds? unavailable|beds? closed)\b", bed, flags=re.I)
    explicit_ed_burden = re.search(
        r"\b(?:emergency department|emergency dept|\bED\b|ED care spaces?|no (?:on[- ]?site )?physician (?:in|available in) (?:the )?emergency|no physician in the emergency)\b",
        bed,
        flags=re.I,
    )
    if non_ed_bed_language and not explicit_ed_burden and ("emergency" in prog or "emergency" in raw):
        return True

    return False


def looks_like_heading(line):
    if not line:
        return False
    label, _ = parse_label_line(line)
    if label:
        return False
    if is_bad_site_text(line):
        return False
    low = line.lower()
    junk_phrases = [
        "temporary service disruptions",
        "archive",
        "icu updates",
        "alberta health services",
        "print",
        "share",
        "page",
        "updated",
        "facilities",
        "details",
        "report a problem",
        "go to top",
        "health link",
        "feedback",
    ]
    if any(p in low for p in junk_phrases):
        return False
    if len(line) > 120:
        return False
    if line.count(".") >= 2:
        return False
    return True


def parse_snapshot_html(snapshot_date, snapshot_url, html, root_snapshot_url=None, page_label=None):
    """Parse a single snapshot HTML page into disruption rows."""
    soup = BeautifulSoup(html, "lxml")
    raw_lines = [clean_text(x) for x in soup.stripped_strings]
    lines = [x for x in raw_lines if x]

    rows = []
    recent_headings = []  # list of (line_index, heading_text)
    i = 0

    while i < len(lines):
        line = lines[i]
        label, value = parse_label_line(line)

        if label == "Program or Service":
            fields = {k: None for k in KNOWN_LABELS}
            fields["Program or Service"] = value

            current_label = label
            current_parts = []
            if value:
                current_parts.append(value)

            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                next_low = next_line.lower()

                if any(tok in next_low for tok in [
                    "report a problem",
                    "go to top",
                    "health link",
                    "send us your feedback",
                    "online payments",
                    "about this site",
                    "data & reporting",
                    "connect with us",
                ]):
                    break

                next_label, next_value = parse_label_line(next_line)
                if next_label == "Program or Service":
                    break

                if next_label is not None:
                    if current_parts:
                        fields[current_label] = clean_text(" ".join(current_parts))
                    current_label = next_label
                    current_parts = []
                    if next_value:
                        current_parts.append(next_value)
                else:
                    if re.fullmatch(r"(?:Close|×|x)", next_line, flags=re.I) or re.match(r"^Close\s*[×x]\b", next_line, flags=re.I):
                        break
                    if looks_like_heading(next_line):
                        current_text = clean_text(" ".join(current_parts))
                        if current_label == "Anticipated End Date":
                            break
                        if current_label == "Program or Service" and has_embedded_notice_payload(current_text):
                            break
                    current_parts.append(next_line)
                j += 1

            if current_parts:
                fields[current_label] = clean_text(" ".join(current_parts))

            facility_name = recent_headings[-1][1] if len(recent_headings) >= 1 else None
            community_heading = None
            if len(recent_headings) >= 2:
                prev_idx, prev_heading = recent_headings[-2]
                last_idx, _ = recent_headings[-1]
                # Only use the penultimate heading as the community if it appears nearby
                # to the facility heading; this reduces heading bleed across blocks.
                if last_idx - prev_idx <= 2:
                    community_heading = prev_heading

            base_row = {
                "snapshot_date": snapshot_date,
                "snapshot_url": snapshot_url,
                "snapshot_root_url": root_snapshot_url or snapshot_url,
                "snapshot_page_label": page_label,
                "community_heading": clean_text(community_heading),
                "facility_name": clean_text(facility_name),
            }
            rows.extend(expand_structured_notice_rows(base_row, fields))
            i = j
            continue

        if looks_like_heading(line) and looks_like_site_heading(line):
            recent_headings.append((i, line))
            recent_headings = recent_headings[-5:]
        i += 1

    return pd.DataFrame(rows)


def _extract_form_state(soup, current_url):
    form = soup.find("form")
    if not form:
        return None, None
    action = form.get("action") or current_url
    action = urljoin(current_url, action)
    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "").lower()
        if typ in {"checkbox", "radio"} and not inp.has_attr("checked"):
            continue
        data[name] = inp.get("value", "")
    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            data[name] = ta.text or ""
    return action, data


def _discover_snapshot_page_candidates(soup, current_url):
    """Discover likely pagination links/postbacks within a snapshot page."""
    candidates = []
    current_parsed = urlparse(current_url)
    seen = set()

    def add_candidate(cand):
        sig = tuple(sorted(cand.items()))
        if sig not in seen:
            seen.add(sig)
            candidates.append(cand)

    def is_pager_text(text):
        low = (text or "").strip().lower()
        return bool(re.fullmatch(r"\d+", low) or low in {"next", "prev", "previous", ">", ">>", "›", "»"})

    for a in soup.find_all("a"):
        text = clean_text(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        onclick = (a.get("onclick") or "").strip()

        if href.startswith("javascript:") or "__doPostBack" in href or "__doPostBack" in onclick:
            js = href if "__doPostBack" in href else onclick
            m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", js)
            if m and is_pager_text(text):
                add_candidate({
                    "kind": "postback",
                    "target": m.group(1),
                    "argument": m.group(2),
                    "label": text or m.group(2) or m.group(1),
                })
            continue

        if not href:
            continue
        abs_url = urljoin(current_url, href)
        parsed = urlparse(abs_url)
        same_path = parsed.path.lower() == current_parsed.path.lower()
        pagerish = bool(re.search(r"(?:[?&](?:page|p|pg|pageindex|pagenumber)=\d+)", abs_url, flags=re.I))
        if same_path and (is_pager_text(text) or pagerish):
            if abs_url != current_url:
                add_candidate({
                    "kind": "get",
                    "url": abs_url,
                    "label": text or abs_url,
                })

    return candidates


def _fetch_snapshot_pages(snapshot_url, max_pages=10):
    """Fetch a snapshot page and any internal paginated subpages if present."""
    pages = []
    seen_actions = set()
    seen_page_hashes = set()
    queue = [{"kind": "get", "url": snapshot_url, "label": "1"}]

    while queue and len(pages) < max_pages:
        cand = queue.pop(0)
        action_sig = tuple(sorted(cand.items()))
        if action_sig in seen_actions:
            continue
        seen_actions.add(action_sig)

        try:
            if cand["kind"] == "get":
                url = cand["url"]
                html = get_html(url)
                current_url = url
            else:
                # Post back to the snapshot form to fetch another pager state.
                base_html = pages[0]["html"] if pages else get_html(snapshot_url)
                base_soup = BeautifulSoup(base_html, "lxml")
                action, data = _extract_form_state(base_soup, snapshot_url)
                if not action or data is None:
                    continue
                data = dict(data)
                data["__EVENTTARGET"] = cand.get("target", "")
                data["__EVENTARGUMENT"] = cand.get("argument", "")
                r = requests.post(action, data=data, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                html = r.text
                current_url = action
        except Exception:
            continue

        page_hash = hashlib.md5(re.sub(r"\s+", " ", html).encode("utf-8", errors="ignore")).hexdigest()
        if page_hash in seen_page_hashes:
            continue
        seen_page_hashes.add(page_hash)

        pages.append({"url": current_url, "html": html, "label": cand.get("label")})

        soup = BeautifulSoup(html, "lxml")
        for nxt in _discover_snapshot_page_candidates(soup, current_url):
            nxt_sig = tuple(sorted(nxt.items()))
            if nxt_sig not in seen_actions:
                queue.append(nxt)

    return pages


def scrape_snapshot(snapshot_date, snapshot_url):
    """Scrape one archive snapshot, including any paginated subpages."""
    page_payloads = _fetch_snapshot_pages(snapshot_url)
    if not page_payloads:
        return pd.DataFrame()

    frames = []
    for idx, payload in enumerate(page_payloads, start=1):
        label = payload.get("label") or str(idx)
        df_page = parse_snapshot_html(
            snapshot_date,
            payload["url"],
            payload["html"],
            root_snapshot_url=snapshot_url,
            page_label=str(label),
        )
        if not df_page.empty:
            frames.append(df_page)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    # Remove exact duplicates that can occur if a pager loops back to page 1 or repeats content.
    dedup_cols = [
        "snapshot_date", "snapshot_root_url", "program_or_service",
        "bed_or_space_reduction_text", "reason_text", "start_date_text",
        "anticipated_end_date_text", "facility_name", "community_heading",
    ]
    out = out.drop_duplicates(subset=dedup_cols).reset_index(drop=True)
    return out


# ============================================================
# SITE ALIASES
# ============================================================

def load_site_aliases():
    if not os.path.exists(SITE_ALIASES_FILE):
        return {}
    try:
        df = pd.read_csv(SITE_ALIASES_FILE)
        if not {"site_id_raw", "site_id_canonical"}.issubset(df.columns):
            return {}
        df["site_id_raw"] = df["site_id_raw"].map(clean_text)
        df["site_id_canonical"] = df["site_id_canonical"].map(clean_text)
        return {
            r["site_id_raw"]: r["site_id_canonical"]
            for _, r in df.dropna(subset=["site_id_raw", "site_id_canonical"]).iterrows()
        }
    except Exception:
        return {}


def write_or_update_site_alias_template(site_ids):
    new_df = pd.DataFrame({"site_id_raw": sorted([x for x in site_ids if isinstance(x, str)])})
    new_df["site_id_canonical"] = new_df["site_id_raw"]

    if os.path.exists(SITE_ALIASES_FILE):
        try:
            old = pd.read_csv(SITE_ALIASES_FILE)
            if {"site_id_raw", "site_id_canonical"}.issubset(old.columns):
                old["site_id_raw"] = old["site_id_raw"].map(clean_text)
                old["site_id_canonical"] = old["site_id_canonical"].map(clean_text)
                merged = new_df.merge(old, on="site_id_raw", how="left", suffixes=("_new", "_old"))
                merged["site_id_canonical"] = merged["site_id_canonical_old"].fillna(merged["site_id_canonical_new"])
                out = merged[["site_id_raw", "site_id_canonical"]].drop_duplicates().sort_values("site_id_raw")
                out.to_csv(SITE_ALIASES_FILE, index=False)
                return
        except Exception:
            pass

    new_df.to_csv(SITE_ALIASES_FILE, index=False)



# ============================================================
# CLASSIFICATION
# ============================================================

def site_placeholder_like(x):
    low = safe_lower(x)
    return (not low) or low in {"tbd", "unknown", "na", "n/a", "none"} or low.startswith("unresolved_")


def site_resolution_fingerprint(row):
    parts = [
        clean_text(row.get("program_or_service")),
        clean_text(row.get("reason_text")),
        clean_text(row.get("start_date_text")),
        clean_text(row.get("anticipated_end_date_text")),
        clean_text(row.get("bed_or_space_reduction_text")),
    ]
    return " | ".join([p or "" for p in parts])


def unresolved_site_slug_from_row(row):
    fp = site_resolution_fingerprint(row)
    digest = hashlib.md5(fp.encode("utf-8")).hexdigest()[:12]
    return f"unresolved_{digest}"


def row_has_any_usable_site_heading(row):
    facility = clean_text(row.get("facility_name"))
    community = clean_text(row.get("community_heading"))
    facility_ok = bool(facility) and not is_bad_site_text(facility)
    community_ok = bool(community) and not is_bad_site_text(community)
    return facility_ok or community_ok


def apply_site_resolution(df):
    """
    Build stable site_id_raw / site_best_raw_label columns.

    Strategy:
    1. use canonical community/facility-based site ID where available
    2. try conservative exact fingerprint-based imputation only for rows with no usable site heading at all,
       and only when at least two resolved occurrences of that same fingerprint agree on a single site
    3. if still unresolved, assign a deterministic unresolved_* placeholder based on the notice fingerprint
       rather than collapsing all such rows into a single generic 'tbd' bucket
    """
    out = df.copy()

    out["site_best_raw_label"] = out.apply(
        lambda r: choose_best_site_label(r.get("facility_name"), r.get("community_heading")),
        axis=1,
    )

    out["site_id_raw"] = out.apply(
        lambda r: canonical_site_id(r.get("facility_name"), r.get("community_heading")),
        axis=1,
    )

    fallback_mask = out["site_id_raw"].isna()
    out.loc[fallback_mask, "site_id_raw"] = out.loc[fallback_mask, "site_best_raw_label"].map(normalize_site_tokens)

    out["site_resolution_fp"] = out.apply(site_resolution_fingerprint, axis=1)
    out["has_any_usable_site_heading"] = out.apply(row_has_any_usable_site_heading, axis=1)

    resolved = out[~out["site_id_raw"].map(site_placeholder_like)].copy()
    if not resolved.empty:
        resolved_grouped = (
            resolved.groupby("site_resolution_fp")["site_id_raw"]
            .agg(lambda s: [x for x in s if isinstance(x, str) and x])
        )
        resolved_map = {}
        for fp, vals in resolved_grouped.items():
            uniq = sorted(set(vals))
            if len(uniq) == 1 and len(vals) >= 2:
                resolved_map[fp] = uniq[0]

        unresolved_mask = out["site_id_raw"].map(site_placeholder_like)
        can_impute_mask = unresolved_mask & (~out["has_any_usable_site_heading"])
        inferred = out.loc[can_impute_mask, "site_resolution_fp"].map(resolved_map)
        out.loc[can_impute_mask & inferred.notna(), "site_id_raw"] = inferred.dropna()

    unresolved_mask = out["site_id_raw"].map(site_placeholder_like)
    if unresolved_mask.any():
        out.loc[unresolved_mask, "site_id_raw"] = out.loc[unresolved_mask].apply(unresolved_site_slug_from_row, axis=1)

    return out


def prepare_alias_map_from_raw(raw_df, write_template=True):
    """Build a stable alias map once from the full raw corpus."""
    if raw_df is None or raw_df.empty:
        return load_site_aliases()

    temp = raw_df.copy()
    for col in [
        "community_heading",
        "facility_name",
        "program_or_service",
        "bed_or_space_reduction_text",
        "reason_text",
        "start_date_text",
        "anticipated_end_date_text",
        "raw_block_text",
    ]:
        if col in temp.columns:
            temp[col] = temp[col].map(clean_text)

    temp = explode_embedded_notice_rows(temp)

    if "program_or_service" in temp.columns:
        temp = temp[temp["program_or_service"].map(is_emergency_department_service)].copy()

    if temp.empty:
        return load_site_aliases()

    temp = apply_site_resolution(temp)

    unique_ids = temp["site_id_raw"].dropna().unique()
    if write_template:
        write_or_update_site_alias_template(unique_ids)
    return load_site_aliases()


def normalize_reason(reason):
    """
    Map heterogeneous public-facing reason text into manuscript summary categories.

    This stays intentionally conservative, but it now also captures obvious wording
    variants that were previously falling into "other" despite being semantically clear,
    such as "Temporary lack of physician coverage" and
    "Workforce shortage - clinical".
    """
    s = safe_lower(reason)
    if not s:
        return None

    s = re.sub(r"\s+", " ", s).strip()

    if re.search(
        r"\b(?:clinical personnel shortage|workforce shortage\s*-\s*clinical|clinical workforce shortage|staff shortage\s*-\s*clinical)\b",
        s,
        flags=re.I,
    ):
        return "clinical_personnel_shortage"

    if re.search(r"\b(?:temporary lack of physician coverage|lack of physician coverage)\b", s, flags=re.I):
        return "physician_shortage"

    if "physician" in s and any(word in s for word in ["shortage", "coverage"]):
        return "physician_shortage"

    if any(word in s for word in ["vacation", "vacancies", "illness", "leave"]):
        return "vacancy_leave_illness"

    return "other"


def infer_closure_mode(text):
    s = safe_lower(text)
    if not s:
        return None
    if "high acuity" in s:
        return "high_acuity_only"
    if "regular operating hours are" in s:
        return "regular_hours_schedule"
    if "each night" in s:
        return "weekday_night_schedule"
    if "weekend" in s or re.search(rf"{weekday_regex()}.*?(until|through).*?{weekday_regex()}", s, flags=re.I):
        return "weekly_named_range_schedule"
    if "reduced hours" in s:
        return "reduced_hours"
    if "closed from" in s:
        return "daily_closed_hours"
    if "beginning" in s and "resume" in s:
        return "single_continuous_interval"
    if re.search(rf"{month_regex()}\s+\d{{1,2}}\s*\([^)]+\)\s*-\s*{month_regex()}\s+\d{{1,2}}\s*\([^)]+\)", s, flags=re.I):
        return "multi_interval"
    if re.search(rf"{month_regex()}\s+\d{{1,2}}\s*\([^)]+-\s*[^)]+\)", s, flags=re.I):
        return "single_day_interval"
    if "closed" in s:
        return "closure"
    return "other"


# ============================================================
# INTERVAL PARSERS
# ============================================================

def base_year_for_row(row):
    """Choose the most defensible base year for month/day parsing.

    v50 fix:
    - prefer explicitly parsed date fields when available
    - otherwise try raw date-bearing text fields
    - then prefer row-specific snapshot / notice / cluster dates
    - only fall back to _analysis_base_year as a last resort
    This prevents notice rows from defaulting to the analysis year (for example
    2025) when the actual notice belongs to 2022, 2023, or 2026.
    """
    parsed_candidates = [
        row.get("start_date_parsed_clean"),
        row.get("anticipated_end_date_parsed_clean"),
        row.get("start_date_parsed"),
        row.get("end_date_parsed"),
    ]
    for c in parsed_candidates:
        dt = parse_dt_any(c)
        if not pd.isna(dt):
            return int(dt.year)

    raw_text_candidates = [
        row.get("start_date_text"),
        row.get("anticipated_end_date_text"),
        row.get("notice_date_text"),
    ]
    for c in raw_text_candidates:
        dt = parse_dt_any(c)
        if not pd.isna(dt):
            return int(dt.year)

    row_date_candidates = [
        row.get("snapshot_date"),
        row.get("notice_date"),
        row.get("notice_effective_date"),
        row.get("cluster_first_notice_date"),
        row.get("cluster_last_notice_date"),
        row.get("first_seen_snapshot_date"),
        row.get("last_seen_snapshot_date"),
    ]
    for c in row_date_candidates:
        dt = parse_dt_any(c)
        if not pd.isna(dt):
            return int(dt.year)

    explicit_year = row.get("_analysis_base_year")
    if explicit_year not in (None, "") and not pd.isna(explicit_year):
        try:
            return int(explicit_year)
        except Exception:
            pass

    return pd.Timestamp(ANALYSIS_START).year


def infer_year_progression(start_month_names, base_year):
    years = []
    current_year = base_year
    prev_month_num = None
    for name in start_month_names:
        mn = MONTHS.get(name.lower())
        if prev_month_num is not None and mn is not None and mn < prev_month_num:
            current_year += 1
        years.append(current_year)
        prev_month_num = mn
    return years




def resolve_schedule_end_date(row, method_name):
    """Return (end_date, adjusted_method_name) for schedule parsers.

    For notices with a TBD anticipated end date, use a conservative snapshot-based
    proxy end so schedule-derived burden is represented in the broad layer. These
    methods are tagged with a _tbd_proxy suffix so they can be excluded from the
    restricted estimate via the uncertainty flagging logic.
    """
    end_date = parse_boundary_datetime_field(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(end_date):
        end_date = parse_boundary_datetime_field(row.get("anticipated_end_date_text"))
    if not pd.isna(end_date):
        return end_date, method_name

    end_text = safe_lower(row.get("anticipated_end_date_text"))
    if "tbd" not in end_text:
        return end_date, method_name

    start_date = parse_boundary_datetime_field(row.get("start_date_parsed_clean"))
    if pd.isna(start_date):
        start_date = parse_boundary_datetime_field(row.get("start_date_text"))
    proxy_end = parse_dt_any(row.get("tbd_proxy_end_date"))
    if pd.isna(start_date) or pd.isna(proxy_end) or proxy_end <= start_date:
        return pd.NaT, method_name

    return proxy_end, f"{method_name}_tbd_proxy"



def parse_boundary_datetime_field(value):
    dt = parse_dt_any(value)
    if not pd.isna(dt):
        return dt
    return parse_clean_date_field(value)


def get_episode_context_bounds(row):
    """Return conservative lower/upper bounds for row-level interval plausibility.

    This is a guardrail for schedule-derived parsers. It is intentionally generous on
    the upper bound so inclusive date wording like "from Aug 18 to Sep 2" can still
    produce an overnight closure starting on Sep 2 and ending on Sep 3.
    """
    lower = pd.NaT
    upper = pd.NaT

    lower_candidates = [
        row.get("manual_interval_start"),
        row.get("start_date_parsed_clean"),
        row.get("start_date_text"),
        row.get("start_date_parsed"),
        row.get("notice_effective_date"),
        row.get("notice_date"),
        row.get("cluster_first_notice_date"),
        row.get("first_seen_snapshot_date"),
        row.get("snapshot_date"),
    ]
    for value in lower_candidates:
        dt = parse_boundary_datetime_field(value)
        if pd.notna(dt):
            lower = dt
            break

    upper_candidates = [
        row.get("manual_interval_end"),
        row.get("anticipated_end_date_parsed_clean"),
        row.get("anticipated_end_date_text"),
        row.get("end_date_parsed"),
        row.get("notice_end_date"),
        row.get("tbd_proxy_end_date"),
        row.get("inferred_end_date_from_snapshots"),
        row.get("cluster_last_notice_date"),
        row.get("last_seen_snapshot_date"),
    ]
    for value in upper_candidates:
        dt = parse_boundary_datetime_field(value)
        if pd.notna(dt):
            upper = dt
            break

    # Inclusive end-date allowance for date-only phrasing and overnight schedule tails.
    # If the source text embeds a more specific schedule-validity end date than
    # the structured AHS Anticipated End Date field, use the text-bound schedule
    # end for schedule-parser context. This preserves rows such as Milk River
    # 2022, where the structured end date reflects one fixed sub-closure but the
    # recurring weekday schedule is explicitly stated to continue later.
    try:
        _src_sd, _src_ed = _source_specific_schedule_bounds_from_text(row)
        if pd.notna(_src_ed):
            if pd.isna(upper) or pd.Timestamp(_src_ed).normalize() > pd.Timestamp(upper).normalize():
                upper = pd.Timestamp(_src_ed).normalize()
    except Exception:
        pass

    if pd.notna(upper):
        upper = upper.normalize() + pd.Timedelta(days=2)

    return lower, upper


def constrain_intervals_to_row_context(row, intervals):
    if not intervals:
        return []

    lower, upper = get_episode_context_bounds(row)
    if pd.isna(lower) and pd.isna(upper):
        return dedupe_intervals_exact(intervals)

    out = []
    for iv in intervals:
        start = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        end = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(start) or pd.isna(end) or end <= start:
            continue

        method = iv.get("interval_method")
        conf = classify_interval_confidence(method)
        is_schedule_like = conf in {"estimated", "coarse"}
        if not is_schedule_like:
            out.append(iv)
            continue

        clipped_start = start
        clipped_end = end
        if pd.notna(lower) and clipped_end <= lower:
            continue
        if pd.notna(upper) and clipped_start >= upper:
            continue
        if pd.notna(lower) and clipped_start < lower:
            clipped_start = lower
        if pd.notna(upper) and clipped_end > upper:
            clipped_end = upper
        if clipped_end <= clipped_start:
            continue

        new_iv = dict(iv)
        new_iv["interval_start"] = clipped_start
        new_iv["interval_end"] = clipped_end
        out.append(new_iv)

    return dedupe_intervals_exact(out)


def _time_expr_extended_regex():
    """Time expression regex including AHS variants such as '12 noon'."""
    return rf"(?:12\s+noon|12\s+midnight|{time_expr_regex()})"


def parse_time_token_extended(tok, reject_year_like: bool = False):
    s = clean_text(tok)
    if not s:
        return None
    low = s.lower().replace('.', '').strip()
    low = re.sub(r"\s+", " ", low)
    if low in {"12 noon", "twelve noon"}:
        return 12 * 60
    if low in {"12 midnight", "twelve midnight"}:
        return 0
    return parse_time_token(s, reject_year_like=reject_year_like)


def _make_timestamp_extended(year, month_name, day, time_token):
    mn = MONTHS.get(str(month_name).lower()) if month_name is not None else None
    # Guard against regexes accidentally capturing a calendar year as a military time
    # in full-date phrases such as "June 18, 2025 until 8 a.m.". Legitimate nearby
    # military times like 2030 remain valid in other years.
    raw_time = str(time_token or "").strip().lower().replace("h", "")
    if re.fullmatch(r"\d{4}", raw_time):
        try:
            if int(raw_time) == int(year):
                return pd.NaT
        except Exception:
            pass
    mins = parse_time_token_extended(time_token)
    if mn is None or mins is None:
        return pd.NaT
    hh, mm = divmod(mins, 60)
    try:
        if hh == 24 and mm == 0:
            return pd.Timestamp(year=int(year), month=mn, day=int(day)) + pd.Timedelta(days=1)
        return pd.Timestamp(year=int(year), month=mn, day=int(day), hour=hh, minute=mm)
    except Exception:
        return pd.NaT


def _append_interval_if_valid(intervals, start_dt, end_dt, method="explicit_weekday_date_time_range"):
    start_dt = pd.to_datetime(start_dt, errors="coerce")
    end_dt = pd.to_datetime(end_dt, errors="coerce")
    if pd.isna(start_dt) or pd.isna(end_dt):
        return
    if end_dt <= start_dt:
        end_dt += pd.Timedelta(days=1)
    if end_dt <= start_dt:
        return
    intervals.append({
        "interval_start": start_dt,
        "interval_end": end_dt,
        "interval_method": method,
        "interval_quality": "exact_text_interval",
    })


def _infer_year_for_month_day(month_name, previous_month_num, current_year):
    mn = MONTHS.get(str(month_name).lower())
    if mn is None:
        return current_year, previous_month_num
    if previous_month_num is not None and mn < previous_month_num:
        current_year += 1
    return current_year, mn



def _event_year_for_unyear_month(month_name, base_year, row=None):
    """Infer year for unyear-qualified month/day in notice text.

    If a late-year notice (for example Dec 30, 2025) announces January dates
    without explicit years, those dates refer to the upcoming calendar year.
    This prevents upcoming January closures from being backdated by one year.
    """
    y = int(base_year)
    mn = MONTHS.get(str(month_name).lower()) if month_name is not None else None
    if mn is None:
        return y
    snap = pd.NaT
    if row is not None and hasattr(row, "get"):
        snap = pd.to_datetime(row.get("snapshot_date"), errors="coerce")
    if pd.notna(snap) and int(snap.year) == y and int(snap.month) >= 10 and mn <= 3:
        return y + 1
    return y


def _date_token_regex(capture_prefix=""):
    # Weekday is optional but common in AHS natural language notices.
    pref = capture_prefix
    return rf"(?:({weekday_regex()})\s*,?\s*)?({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?"


def extract_weekday_dated_explicit_intervals(row, base_year):
    """High-confidence fixed-date natural-language intervals.

    Covers forms such as:
      - 5 p.m. on Sunday, May 17 to 8 a.m. on Tuesday, May 19
      - Sunday, May 17 at 5 p.m. to Tuesday, May 19 at 8 a.m.
      - Noon to 5 p.m. on Friday, May 29
      - from 5 p.m. to 8 a.m. on Wednesday, May 20 and Thursday, May 21

    These are explicit dated closures and must not be treated as recurring weekly schedules.
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    texpr = _time_expr_extended_regex()
    mpat = month_regex()
    wpat = weekday_regex()
    intervals = []

    # Pattern 1: time on Weekday, Month Day -> time on Weekday, Month Day
    pat_time_on_date_to_time_on_date = re.compile(
        rf"(?:closed\s+|closure\s+|from\s+)?({texpr})\s+(?:on\s+)?(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|until|through)\s*({texpr})\s+(?:on\s+)?(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat_time_on_date_to_time_on_date.finditer(s):
        t1, mon1, d1, y1_txt, t2, mon2, d2, y2_txt = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(mon2.lower()) < MONTHS.get(mon1.lower()) else 0)
        start_dt = _make_timestamp_extended(y1, mon1, d1, t1)
        end_dt = _make_timestamp_extended(y2, mon2, d2, t2)
        _append_interval_if_valid(intervals, start_dt, end_dt)

    # Pattern 2: Weekday, Month Day at time -> Weekday, Month Day at time
    pat_date_at_time_to_date_at_time = re.compile(
        rf"(?:closed\s+|closure\s+|from\s+)?(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:at\s+|from\s+)?({texpr})\s*(?:-|–|—|to|until|through)\s*(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:at\s+)?({texpr})",
        flags=re.I,
    )
    for m in pat_date_at_time_to_date_at_time.finditer(s):
        mon1, d1, y1_txt, t1, mon2, d2, y2_txt, t2 = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(mon2.lower()) < MONTHS.get(mon1.lower()) else 0)
        start_dt = _make_timestamp_extended(y1, mon1, d1, t1)
        end_dt = _make_timestamp_extended(y2, mon2, d2, t2)
        _append_interval_if_valid(intervals, start_dt, end_dt)

    # Pattern 2b: Weekday Month Day (time) through Weekday Month Day (time).
    # Observed in Bassano-style rows: "plus Friday September 2 (0800)
    # through Tuesday September 6 (0800)". This is a fixed add-on interval,
    # not a recurring Friday/Tuesdays schedule.
    pat_weekday_date_parenthetical_to_weekday_date_parenthetical = re.compile(
        rf"(?:plus\s+|and\s+)?(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until|through)\s*(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*\(\s*({texpr})\s*\)",
        flags=re.I,
    )
    for m in pat_weekday_date_parenthetical_to_weekday_date_parenthetical.finditer(s):
        mon1, d1, y1_txt, t1, mon2, d2, y2_txt, t2 = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(mon2.lower()) < MONTHS.get(mon1.lower()) else 0)
        start_dt = _make_timestamp_extended(y1, mon1, d1, t1)
        end_dt = _make_timestamp_extended(y2, mon2, d2, t2)
        _append_interval_if_valid(intervals, start_dt, end_dt)

    # Pattern 3: Noon to 5 p.m. on Friday, May 29 / from noon to 5 p.m. on May 29
    pat_same_day_time_to_time_on_date = re.compile(
        rf"(?:closed\s+|closure\s+|from\s+)?({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat_same_day_time_to_time_on_date.finditer(s):
        # Avoid re-consuming the right half of cross-date matches already handled above.
        t1, t2, mon, day, y_txt = m.groups()
        pre = s[max(0, m.start() - 80):m.start()]
        around_start = s[max(0, m.start() - 80):m.start() + 15]
        if re.search(rf"(?:{mpat})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s+(?:from|at)\s*$", pre, flags=re.I) or re.search(rf"(?:{mpat})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s+(?:from|at)\s+{texpr}", around_start, flags=re.I):
            continue
        # If this time-window/date phrase is preceded by a relative anchor such
        # as "today at 7 p.m. until 7 a.m. on Tuesday, March 31", pattern 7 below
        # should anchor the start to the notice/snapshot date. Do not also
        # reinterpret the end date as the start date and create a future extra night.
        if re.search(r"\b(?:today|tonight|tomorrow)\b[^.;]{0,80}$", pre, flags=re.I):
            continue
        y = int(y_txt) if y_txt else _event_year_for_unyear_month(mon, base_year, row)
        start_dt = _make_timestamp_extended(y, mon, day, t1)
        end_dt = _make_timestamp_extended(y, mon, day, t2)
        _append_interval_if_valid(intervals, start_dt, end_dt)

    # Pattern 4: from 5 p.m. to 8 a.m. on Wednesday, May 20 and Thursday, May 21
    pat_shared_window_dates = re.compile(
        rf"(?:closed\s+|closure\s+|from\s+)?({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+on\s+(.+?)(?=$|[.;])",
        flags=re.I,
    )
    date_pat = re.compile(rf"(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?", flags=re.I)
    for m in pat_shared_window_dates.finditer(s):
        t1, t2, tail = m.group(1), m.group(2), m.group(3)
        # Skip if this was just the same-day pattern with one date; it is harmless but avoid duplicate work.
        date_matches = list(date_pat.finditer(tail))
        if len(date_matches) < 2:
            continue
        current_year = int(base_year)
        prev_mn = None
        for dm in date_matches:
            mon, day, y_txt = dm.group(1), dm.group(2), dm.group(3)
            if y_txt:
                y = int(y_txt)
                prev_mn = MONTHS.get(mon.lower())
                current_year = y
            else:
                y, prev_mn = _infer_year_for_month_day(mon, prev_mn, current_year)
                current_year = y
            start_dt = _make_timestamp_extended(y, mon, day, t1)
            end_dt = _make_timestamp_extended(y, mon, day, t2)
            _append_interval_if_valid(intervals, start_dt, end_dt)


    # Pattern 4b: shared time window on two weekday+month-day tokens where month abbreviations
    # contain periods; do not stop the list at "Nov." / "Dec.".
    pat_two_weekday_month_dates = re.compile(
        rf"(?:closed\s+|closure\s+|from\s+)?({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+on\s+"
        rf"(?:{wpat})\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:,?\s*and\s*|,\s*)"
        rf"(?:{wpat})\s*,?\s*(?:({mpat})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat_two_weekday_month_dates.finditer(s):
        t1, t2, mon1, d1, y1_txt, mon2, d2, y2_txt = m.groups()
        mon2 = mon2 or mon1
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        _append_interval_if_valid(intervals, _make_timestamp_extended(y1, mon1, d1, t1), _make_timestamp_extended(y1, mon1, d1, t2), method="explicit_weekday_date_time_range")
        _append_interval_if_valid(intervals, _make_timestamp_extended(y2, mon2, d2, t1), _make_timestamp_extended(y2, mon2, d2, t2), method="explicit_weekday_date_time_range")

    # Pattern 5: shared time window on multiple fully/partially specified weekday dates,
    # e.g. "from 5 p.m. to 8 a.m. on Wednesday, Nov. 22 and Thursday, Nov. 23".
    pat_same_window_multi_dates = re.compile(
        rf"(?:closed\s+|closure\s+|from\s+)?({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+on\s+(.+?)(?=$|[.;])",
        flags=re.I,
    )
    # date token with optional month; if month omitted, carry forward the last month.
    date_token = re.compile(rf"(?:{wpat}\s*,?\s*)?(?:(?P<mon>{mpat})\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*(?P<year>{PARSER_YEAR_REGEX}))?", flags=re.I)
    for m in pat_same_window_multi_dates.finditer(s):
        t1, t2, tail = m.group(1), m.group(2), m.group(3)
        date_matches = list(date_token.finditer(tail))
        # Require at least two date tokens with at least one explicit month in the list.
        if len(date_matches) < 2 or not any(dm.group('mon') for dm in date_matches):
            continue
        current_year = int(base_year)
        current_month = None
        prev_mn = None
        for dm in date_matches:
            mon = dm.group('mon') or current_month
            day = dm.group('day')
            y_txt = dm.group('year')
            if not mon:
                continue
            current_month = mon
            if y_txt:
                y = int(y_txt)
                prev_mn = MONTHS.get(str(mon).lower())
                current_year = y
            else:
                y, prev_mn = _infer_year_for_month_day(mon, prev_mn, current_year)
                current_year = y
            start_dt = _make_timestamp_extended(y, mon, day, t1)
            end_dt = _make_timestamp_extended(y, mon, day, t2)
            _append_interval_if_valid(intervals, start_dt, end_dt, method="explicit_weekday_date_time_range")


    # If a relative cue such as "close today at 7 p.m. until 7 a.m. on Tuesday,
    # March 31" was parsed by the relative-date pattern, suppress same-window
    # weekday/date artifacts that reinterpret the end date as a separate start
    # date. This was observed in Milk River March 30, 2026.
    if intervals and re.search(r"\b(?:today|tonight|tomorrow)\b", s, flags=re.I):
        has_relative_exact = any(str(iv.get("interval_method")) == "explicit_dated_natural_language" for iv in intervals)
        if has_relative_exact:
            intervals = [iv for iv in intervals if str(iv.get("interval_method")) != "explicit_weekday_date_time_range"]

    return dedupe_intervals_exact(intervals)



def _row_anchor_date_for_relative_text(row):
    """Best available source-observation anchor for relative words such as today/tomorrow.

    Episode-level rows produced by grouping often no longer carry snapshot_date; they
    carry first_seen_snapshot_date/last_seen_snapshot_date instead. Relative notice
    wording such as "today at 7 p.m." must anchor to the observed posting date, not
    fall through to weekday/date parsers that can create wrong-date artifacts.
    """
    if not hasattr(row, "get"):
        return pd.NaT
    for col in ["snapshot_date", "first_seen_snapshot_date", "last_seen_snapshot_date"]:
        dt = pd.to_datetime(row.get(col), errors="coerce")
        if pd.notna(dt):
            return dt.normalize()
    for col in ["start_date_parsed_clean", "start_date_text"]:
        val = row.get(col)
        dt = pd.to_datetime(val, errors="coerce") if col.endswith("parsed_clean") else parse_clean_date_field(val)
        if pd.notna(dt):
            return dt.normalize()
    return pd.NaT

def extract_explicit_dated_natural_language_intervals(row, base_year):

    """Parse explicit dated natural-language closure intervals found in AHS notices.

    This parser is deliberately fixed-date, not recurring. It covers AHS wording such as:
      - 5 p.m., Thursday, May 7 to 8 a.m., Friday, May 8
      - 6 a.m. to 6 p.m., Wednesday, April 8
      - April 16 at 7 p.m. to April 17 at 7 a.m.
      - 11 p.m. tonight, June 6, until 7a.m. June 7
      - temporarily close today at 7:15 a.m. until 1:00 p.m. - Friday, February 27
      - closed at 8 a.m. tomorrow, Thursday, May 29; reopen at 8 a.m., Friday, May 30
      - closed on Sunday, January 11 from 7 a.m. to 12 noon

    It should not turn singular weekday+date language into weekly recurrence.
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    # Normalize common AHS run-together time tokens: "7a.m." -> "7 a.m."
    s = re.sub(r"(?i)\b(\d{1,2})(a\.?m\.?|p\.?m\.?)\b", r"\1 \2", s)
    texpr = _time_expr_extended_regex()
    mpat = month_regex()
    wpat = weekday_regex()
    intervals = []

    def _row_snapshot_date():
        return _row_anchor_date_for_relative_text(row)

    def _date_re():
        return rf"(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?"

    def _date_re_no_on():
        return rf"(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?"

    def _ts(mon, day, year_txt, time_txt, default_year=None):
        y = int(year_txt) if year_txt else int(default_year if default_year is not None else base_year)
        return _make_timestamp_extended(y, mon, day, time_txt)

    def _append(start_dt, end_dt):
        _append_interval_if_valid(intervals, start_dt, end_dt, method="explicit_dated_natural_language")

    # 1) time, Weekday, Month Day -> time, Weekday, Month Day
    pat = re.compile(
        rf"({texpr})\s*,?\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|until|through)\s*({texpr})\s*,?\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1_txt, t2, mon2, d2, y2_txt = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        _append(_make_timestamp_extended(y1, mon1, d1, t1), _make_timestamp_extended(y2, mon2, d2, t2))

    # 2) time on/no-on Month Day -> time on/no-on Month Day, including no weekdays.
    pat = re.compile(
        rf"({texpr})\s+(?:on\s+)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|until|through)\s*({texpr})\s+(?:on\s+)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1_txt, t2, mon2, d2, y2_txt = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        _append(_make_timestamp_extended(y1, mon1, d1, t1), _make_timestamp_extended(y2, mon2, d2, t2))

    # 3) Month Day at/from time -> Month Day at time.
    pat = re.compile(
        rf"(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:at|from)\s+({texpr})\s*(?:-|–|—|to|until|through)\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:at\s+)?({texpr})",
        flags=re.I,
    )
    for m in pat.finditer(s):
        mon1, d1, y1_txt, t1, mon2, d2, y2_txt, t2 = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        _append(_make_timestamp_extended(y1, mon1, d1, t1), _make_timestamp_extended(y2, mon2, d2, t2))

    # 3b) Month Day time -> Month Day time, no explicit "at/from".
    # This is the critical ambiguous-token case: "June 23 2030 - June 24 0700"
    # means 20:30/07:00 in time slots. A 4-digit year is only accepted here
    # when it is preceded by a comma as Month Day, Year.
    pat = re.compile(
        rf"(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+({texpr})\s*(?:-|–|—|to|until|through)\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+({texpr})",
        flags=re.I,
    )
    for m in pat.finditer(s):
        mon1, d1, y1_txt, t1, mon2, d2, y2_txt, t2 = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        _append(_make_timestamp_extended(y1, mon1, d1, t1), _make_timestamp_extended(y2, mon2, d2, t2))

    # 4) Same-day: time to time, Weekday, Month Day OR time to time on Month Day.
    pat = re.compile(
        rf"({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s*,?\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, t2, mon, day, y_txt = m.groups()
        # Avoid double-counting forms already handled as "Month Day from time to time on Month Day",
        # e.g. "January 18 from 7 p.m. to 8 a.m. on January 19".
        # The same-day pattern would otherwise also create a spurious Jan 19->Jan 20 interval.
        pre = s[max(0, m.start() - 80):m.start()]
        around_start = s[max(0, m.start() - 80):m.start() + 15]
        if re.search(rf"(?:{mpat})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s+(?:from|at)\s*$", pre, flags=re.I) or re.search(rf"(?:{mpat})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s+(?:from|at)\s+{texpr}", around_start, flags=re.I):
            continue
        # If this time-window/date phrase is preceded by a relative anchor such
        # as "today at 7 p.m. until 7 a.m. on Tuesday, March 31", pattern 7 below
        # should anchor the start to the notice/snapshot date. Do not also
        # reinterpret the end date as the start date and create a future extra night.
        if re.search(r"\b(?:today|tonight|tomorrow)\b[^.;]{0,80}$", pre, flags=re.I):
            continue
        y = int(y_txt) if y_txt else _event_year_for_unyear_month(mon, base_year, row)
        _append(_make_timestamp_extended(y, mon, day, t1), _make_timestamp_extended(y, mon, day, t2))

    # 5) Same-day: Month Day from/at time to time; also "closed on Sunday, January 11 from 7 a.m. to 12 noon".
    pat = re.compile(
        rf"(?:closed\s+)?(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?,?\s+(?:from|between|at)\s+({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})",
        flags=re.I,
    )
    for m in pat.finditer(s):
        mon, day, y_txt, t1, t2 = m.groups()
        y = int(y_txt) if y_txt else _event_year_for_unyear_month(mon, base_year, row)
        _append(_make_timestamp_extended(y, mon, day, t1), _make_timestamp_extended(y, mon, day, t2))

    # 6) time tonight/today, Month Day until time Month Day (Lacombe-style).
    pat = re.compile(
        rf"({texpr})\s*(?:tonight|today|tomorrow)?\s*,?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*(?:-|–|—|to|until|through)\s*({texpr})\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1_txt, t2, mon2, d2, y2_txt = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        _append(_make_timestamp_extended(y1, mon1, d1, t1), _make_timestamp_extended(y2, mon2, d2, t2))

    # 7) today/tonight/tomorrow at time until time plus optional explicit date.
    snap = _row_snapshot_date()
    pat = re.compile(
        rf"(?:close|closed|closure)[^.;]{{0,120}}?\b(today|tonight|tomorrow)\b\s+at\s+({texpr})[^.;]{{0,80}}?(?:until|to|-|through)\s*({texpr})(?:\s*,?\s*(?:-|–|—)?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?)?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        rel, t1, t2, mon, day, y_txt = m.groups()
        if pd.isna(snap):
            continue
        start_date = snap + (pd.Timedelta(days=1) if str(rel).lower() == "tomorrow" else pd.Timedelta(0))
        start_dt = start_date + pd.Timedelta(minutes=parse_time_token_extended(t1) or 0)
        if mon and day:
            y = int(y_txt) if y_txt else int(start_date.year)
            end_dt = _make_timestamp_extended(y, mon, day, t2)
        else:
            end_dt = start_date + pd.Timedelta(minutes=parse_time_token_extended(t2) or 0)
        _append(start_dt, end_dt)

    # 8) closed at time [today/tomorrow/date]. ... reopen at time/date OR reopen Month Day at time.
    pat = re.compile(
        rf"(?:close|closed)[^.;]{{0,120}}?\bat\s+({texpr})\s*(?:(today|tonight|tomorrow)\s*)?(?:,?\s*\(?\s*(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?\)?(?:,\s*({PARSER_YEAR_REGEX}))?)?[^.]*?\.\s*[^.]*?reopen[^.]*?(?:at\s+({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?|({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+at\s+({texpr}))",
        flags=re.I,
    )
    for m in pat.finditer(s):
        groups = m.groups()
        t_start, rel, mon_s, day_s, y_s = groups[0], groups[1], groups[2], groups[3], groups[4]
        t_end1, mon_e1, day_e1, y_e1, mon_e2, day_e2, y_e2, t_end2 = groups[5:]
        if mon_s and day_s:
            y_start = int(y_s) if y_s else int(base_year)
            start_dt = _make_timestamp_extended(y_start, mon_s, day_s, t_start)
        else:
            snap_dt = snap
            if pd.isna(snap_dt):
                continue
            if rel and str(rel).lower() == "tomorrow":
                snap_dt = snap_dt + pd.Timedelta(days=1)
            start_dt = snap_dt + pd.Timedelta(minutes=parse_time_token_extended(t_start) or 0)
        if mon_e1 and day_e1:
            y_end = int(y_e1) if y_e1 else int(start_dt.year)
            end_dt = _make_timestamp_extended(y_end, mon_e1, day_e1, t_end1)
        else:
            y_end = int(y_e2) if y_e2 else int(start_dt.year)
            end_dt = _make_timestamp_extended(y_end, mon_e2, day_e2, t_end2)
        if pd.notna(start_dt) and pd.notna(end_dt) and end_dt < start_dt and (end_dt.month < start_dt.month):
            end_dt = _make_timestamp_extended(start_dt.year + 1, mon_e1 or mon_e2, day_e1 or day_e2, t_end1 or t_end2)
        _append(start_dt, end_dt)

    return dedupe_intervals_exact(intervals)



def extract_explicit_dated_natural_language_intervals_v2(row, base_year):
    """Additional AHS fixed-date natural-language interval parser.

    Covers remaining notice variants found in the current corpus, including comma-before-connector
    date ranges, today/tomorrow parenthetical dates, close/reopen split sentences, date-only until
    a same-day reopen time, and paired same-day windows sharing one date.
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    s = re.sub(r"(?i)\b(\d{1,2})(a\.?m\.?|p\.?m\.?)\b", r"\1 \2", s)
    texpr = _time_expr_extended_regex()
    mpat = month_regex()
    wpat = weekday_regex()
    intervals = []

    def snap_date():
        return _row_anchor_date_for_relative_text(row)

    def append(start_dt, end_dt):
        _append_interval_if_valid(intervals, start_dt, end_dt, method="explicit_dated_natural_language")

    def y_for(mon, y_txt=None, default=None):
        return int(y_txt) if y_txt else int(default if default is not None else base_year)

    def ts(mon, day, time_txt, y_txt=None, default=None):
        return _make_timestamp_extended(y_for(mon, y_txt, default), mon, day, time_txt)

    # A. time on date, to/until time on date. Allows comma after start date before connector.
    pat = re.compile(
        rf"({texpr})\s*,?\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*(?:-|–|—|to|until|through)\s*({texpr})\s*,?\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1, t2, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # A2. Month Day time -> Month Day time, no explicit at/from.
    # 2030 is time here unless it appears after a comma as Month Day, 2030.
    pat = re.compile(
        rf"(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+({texpr})\s*(?:-|–|—|to|until|through)\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+({texpr})",
        flags=re.I,
    )
    for m in pat.finditer(s):
        mon1, d1, y1, t1, mon2, d2, y2, t2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # B. time today/tomorrow (Month Day) to/until time tomorrow/today (Month Day).
    pat = re.compile(
        rf"({texpr})\s+(today|tonight|tomorrow)\s*\(?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?\)?\s*(?:-|–|—|to|until|through)\s*({texpr})\s+(today|tonight|tomorrow)?\s*\(?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?\)?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, _rel1, mon1, d1, t2, _rel2, mon2, d2 = m.groups()
        yy1 = int(base_year)
        yy2 = yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # C. closed/close at time today/tomorrow (Month Day). Reopen Month Day at time OR reopen at time, Month Day.
    pat = re.compile(
        rf"(?:close|closed)[^.;]{{0,120}}?\bat\s+({texpr})\s*(?:today|tonight|tomorrow)?\s*\(?\s*(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?\)?(?:,\s*({PARSER_YEAR_REGEX}))?[^.]*?\.\s*[^.]*?re-?open[^.]*?(?:(?:at\s+({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?)|(?:(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*at\s+({texpr})))",
        flags=re.I,
    )
    for m in pat.finditer(s):
        g = m.groups()
        t1, mon1, d1, y1 = g[0], g[1], g[2], g[3]
        if g[4] is not None:
            t2, mon2, d2, y2 = g[4], g[5], g[6], g[7]
        else:
            mon2, d2, y2, t2 = g[8], g[9], g[10], g[11]
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # D. beginning Date at time. Reopen Date at time.
    pat = re.compile(
        rf"beginning\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*(?:at\s+)?({texpr})[^.]*?\.\s*[^.]*?re-?open[^.]*?(?:(?:at\s+({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?)|(?:(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*at\s+({texpr})))",
        flags=re.I,
    )
    for m in pat.finditer(s):
        g = m.groups()
        mon1, d1, y1, t1 = g[0], g[1], g[2], g[3]
        if g[4] is not None:
            t2, mon2, d2, y2 = g[4], g[5], g[6], g[7]
        else:
            mon2, d2, y2, t2 = g[8], g[9], g[10], g[11]
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # E. began today at time ... extended until time on Date.
    pat = re.compile(
        rf"began\s+today\s+at\s+({texpr})[^.]*?extended\s+until\s+({texpr})\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    sd = snap_date()
    for m in pat.finditer(s):
        if pd.isna(sd):
            continue
        t1, t2, mon, day, y = m.groups()
        start_dt = sd + pd.Timedelta(minutes=parse_time_token_extended(t1) or 0)
        end_dt = _make_timestamp_extended(y_for(mon, y, sd.year), mon, day, t2)
        append(start_dt, end_dt)

    # F. temporarily closed Date until time, same-day date-only start. Conservative start at 00:00.
    pat = re.compile(
        rf"(?:close|closed)[^.;]{{0,80}}?(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+until\s+({texpr})\b",
        flags=re.I,
    )
    for m in pat.finditer(s):
        mon, day, y, t2 = m.groups()
        yy = y_for(mon, y)
        start_dt = pd.Timestamp(year=yy, month=MONTHS.get(str(mon).lower()), day=int(day))
        end_dt = _make_timestamp_extended(yy, mon, day, t2)
        append(start_dt, end_dt)

    # G. two same-day windows sharing one date: "12 a.m. to 8 a.m. and 8 p.m. to 11:59 p.m. Friday, December 12".
    pat = re.compile(
        rf"({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+and\s+({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, t2, t3, t4, mon, day, y = m.groups()
        yy = y_for(mon, y)
        append(_make_timestamp_extended(yy, mon, day, t1), _make_timestamp_extended(yy, mon, day, t2))
        append(_make_timestamp_extended(yy, mon, day, t3), _make_timestamp_extended(yy, mon, day, t4))

    # H. fixed range recurring window: "closed from 8 p.m. to 8 a.m. starting Friday, Oct. 10 through Monday, Oct. 13".
    pat = re.compile(
        rf"closed\s+from\s+({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+starting\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:through|until|to)\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, t2, mon1, d1, y1, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        start_day = pd.Timestamp(year=yy1, month=MONTHS.get(str(mon1).lower()), day=int(d1))
        end_day = pd.Timestamp(year=yy2, month=MONTHS.get(str(mon2).lower()), day=int(d2))
        m1 = parse_time_token_extended(t1)
        m2 = parse_time_token_extended(t2)
        if m1 is None or m2 is None:
            continue
        cur = start_day
        # Overnight windows ending on the final through-date morning.
        last_start_day = end_day - (pd.Timedelta(days=1) if m2 <= m1 else pd.Timedelta(days=0))
        while cur <= last_start_day:
            st = cur + pd.Timedelta(minutes=m1)
            en = cur + pd.Timedelta(minutes=m2)
            if en <= st:
                en += pd.Timedelta(days=1)
            append(st, en)
            cur += pd.Timedelta(days=1)


    # I. from time today/tomorrow, Date, until/to time Date (relative cue before explicit date).
    pat = re.compile(
        rf"(?:from\s+)?({texpr})\s+(today|tonight|tomorrow)\s*,?\s*(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*(?:-|–|—|to|until|through)\s*({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, _rel, mon1, d1, y1, t2, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # J. close at time on Date, and re-open on Date at time (same sentence).
    pat = re.compile(
        rf"(?:close|closed)[^.;]{{0,160}}?\bat\s+({texpr})\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?[^.;]{{0,160}}?re-?open[^.;]{{0,80}}?(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,?\s*at\s+({texpr})",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1, mon2, d2, y2, t2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # K. fixed range recurring window with close/closed from time to time starting Date through Date.
    pat = re.compile(
        rf"(?:close|closed)\s+from\s+({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+starting\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:through|until|to)\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, t2, mon1, d1, y1, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        start_day = pd.Timestamp(year=yy1, month=MONTHS.get(str(mon1).lower()), day=int(d1))
        end_day = pd.Timestamp(year=yy2, month=MONTHS.get(str(mon2).lower()), day=int(d2))
        m1 = parse_time_token_extended(t1)
        m2 = parse_time_token_extended(t2)
        if m1 is None or m2 is None:
            continue
        cur = start_day
        last_start_day = end_day - (pd.Timedelta(days=1) if m2 <= m1 else pd.Timedelta(days=0))
        while cur <= last_start_day:
            st = cur + pd.Timedelta(minutes=m1)
            en = cur + pd.Timedelta(minutes=m2)
            if en <= st:
                en += pd.Timedelta(days=1)
            append(st, en)
            cur += pd.Timedelta(days=1)


    # L. from time on Date and reopen/re-open at time on Date.
    pat = re.compile(
        rf"from\s+({texpr})\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:and\s+)?re-?open\s+at\s+({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1, t2, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # M. from time today to time tomorrow (Date); start is snapshot date, end date from parenthetical/date text.
    pat = re.compile(
        rf"from\s+({texpr})\s+today\s*(?:\(?\s*({mpat})?\s*(\d{{1,2}})?\s*\)?)?\s*(?:-|–|—|to|until|through)\s*({texpr})\s+tomorrow\s*\(?\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?\)?",
        flags=re.I,
    )
    sd = snap_date()
    for m in pat.finditer(s):
        if pd.isna(sd):
            continue
        t1, _mon_s, _d_s, t2, mon2, d2 = m.groups()
        start_dt = sd + pd.Timedelta(minutes=parse_time_token_extended(t1) or 0)
        yy2 = int(sd.year) + (1 if MONTHS.get(str(mon2).lower()) < sd.month else 0)
        end_dt = _make_timestamp_extended(yy2, mon2, d2, t2)
        append(start_dt, end_dt)

    # N. closed on Date at time. Reopen at time, Date.
    pat = re.compile(
        rf"(?:close|closed)[^.;]{{0,120}}?(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+at\s+({texpr})[^.]*?\.\s*[^.]*?re-?open\s+at\s+({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        mon1, d1, y1, t1, t2, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        append(_make_timestamp_extended(yy1, mon1, d1, t1), _make_timestamp_extended(yy2, mon2, d2, t2))

    # O. Daily fixed date-range: close from time to time daily beginning Date ... return/reopen on Date.
    pat = re.compile(
        rf"close\s+from\s+({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+daily\s+beginning\s+({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?[^.]*?(?:return|re-?open)[^.]*?(?:on\s+)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, t2, mon1, d1, y1, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        start_day = pd.Timestamp(year=yy1, month=MONTHS.get(str(mon1).lower()), day=int(d1))
        end_day = pd.Timestamp(year=yy2, month=MONTHS.get(str(mon2).lower()), day=int(d2))
        m1 = parse_time_token_extended(t1); m2 = parse_time_token_extended(t2)
        if m1 is None or m2 is None: continue
        cur = start_day
        last_start_day = end_day - (pd.Timedelta(days=1) if m2 <= m1 else pd.Timedelta(days=0))
        while cur <= last_start_day:
            st = cur + pd.Timedelta(minutes=m1); en = cur + pd.Timedelta(minutes=m2)
            if en <= st: en += pd.Timedelta(days=1)
            append(st, en); cur += pd.Timedelta(days=1)


    # P. Cross-sentence daily fixed range: close from time to time daily beginning Date. ... return/reopen ... on Date.
    pat = re.compile(
        rf"(?is)(?:close|closed)\s+from\s+({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s+daily\s+beginning\s+(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?.{{0,350}}?(?:return|re-?open).{{0,120}}?(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?"
    )
    for m in pat.finditer(s):
        t1, t2, mon1, d1, y1, mon2, d2, y2 = m.groups()
        yy1 = y_for(mon1, y1)
        yy2 = y_for(mon2, y2, yy1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0))
        start_day = pd.Timestamp(year=yy1, month=MONTHS.get(str(mon1).lower()), day=int(d1))
        end_day = pd.Timestamp(year=yy2, month=MONTHS.get(str(mon2).lower()), day=int(d2))
        m1 = parse_time_token_extended(t1); m2 = parse_time_token_extended(t2)
        if m1 is None or m2 is None: continue
        cur = start_day
        last_start_day = end_day - pd.Timedelta(days=1)
        while cur <= last_start_day:
            st = cur + pd.Timedelta(minutes=m1); en = cur + pd.Timedelta(minutes=m2)
            if en <= st: en += pd.Timedelta(days=1)
            append(st, en); cur += pd.Timedelta(days=1)

    return dedupe_intervals_exact(intervals)

def extract_multi_intervals(text, base_year):
    s = clean_text(text) or ""
    s = s.replace("–", "-").replace("—", "-")
    month_pat = month_regex()
    s = re.sub(rf"(\))\s+(?={month_pat}\s+\d{{1,2}}(?:\s*\(|\s+\d{{3,4}}|\s+[AaPp][Mm]))", r"\1; ", s, flags=re.I)
    s = re.sub(rf"((?:am|pm)|\d{{3,4}})\s+(?={month_pat}\s+\d{{1,2}}(?:\s*\(|\s+\d{{3,4}}))", r"\1; ", s, flags=re.I)

    patterns = [
        re.compile(
            rf"({month_regex()})\s+(\d{{1,2}})\s*\(([^)]+)\)\s*(?:-|to|until)\s*"
            rf"({month_regex()})\s+(\d{{1,2}})\s*\(([^)]+)\)",
            flags=re.I,
        ),
        re.compile(
            rf"({month_regex()})\s+(\d{{1,2}})\s*\(([^)]+)\)\s*(?:-|to|until)\s*"
            rf"(\d{{1,2}})\s*\(([^)]+)\)",
            flags=re.I,
        ),
        re.compile(
            rf"({month_regex()})\s+(\d{{1,2}})\s*\(([^)]+)\)\s*(?:-|to|until)\s*"
            rf"({month_regex()})?\s*(\d{{1,2}})\s+({time_expr_regex()})",
            flags=re.I,
        ),
    ]

    intervals = []

    # Malformed cross-date parenthetical lists observed in Redwater/Manning-style
    # rows, e.g. "October 22 (0800 - October 23 (0800)" or
    # "December 16 (2300), to December 17 (0700)".  The standard parser
    # requires the first parenthesis to close before the connector, so recover
    # these variants explicitly.
    pat_malformed_cross_parenthetical = re.compile(
        rf"({month_regex()})\s+(\d{{1,2}})\s*\(\s*({time_expr_regex()})\s*\)?\s*,?\s*(?:-|to|until)\s*"
        rf"({month_regex()})\s+(\d{{1,2}})\s*\(\s*({time_expr_regex()})\s*\)?",
        flags=re.I,
    )
    malformed_matches = list(pat_malformed_cross_parenthetical.finditer(s))
    if malformed_matches:
        years = infer_year_progression([m.group(1) for m in malformed_matches], base_year)
        for m, y1 in zip(malformed_matches, years):
            m1, d1, t1, m2, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
            y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
            start_dt = make_timestamp(y1, m1, d1, t1)
            end_dt = make_timestamp(y2, m2, d2, t2)
            if not pd.isna(start_dt) and not pd.isna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })

    matches = list(patterns[0].finditer(s))
    if matches:
        years = infer_year_progression([m.group(1) for m in matches], base_year)
        for m, y1 in zip(matches, years):
            m1, d1, t1, m2, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
            y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
            start_dt = make_timestamp(y1, m1, d1, t1)
            end_dt = make_timestamp(y2, m2, d2, t2)
            if not pd.isna(start_dt) and not pd.isna(end_dt) and end_dt > start_dt:
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })

    matches = list(patterns[1].finditer(s))
    if matches:
        years = infer_year_progression([m.group(1) for m in matches], base_year)
        for m, y1 in zip(matches, years):
            m1, d1, t1, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            start_dt = make_timestamp(y1, m1, d1, t1)
            end_dt = make_timestamp(y1, m1, d2, t2)
            if not pd.isna(start_dt) and not pd.isna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })

    matches = list(patterns[2].finditer(s))
    if matches:
        years = infer_year_progression([m.group(1) for m in matches], base_year)
        for m, y1 in zip(matches, years):
            m1, d1, t1, m2_opt, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
            m2 = m2_opt if m2_opt else m1
            y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
            start_dt = make_timestamp(y1, m1, d1, t1)
            end_dt = make_timestamp(y2, m2, d2, t2)
            if not pd.isna(start_dt) and not pd.isna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })

    # Non-parenthetical format: 1900 July 4 to 0700 July 5
    pat4 = re.compile(
        rf"({time_expr_regex()})\s+({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|to|until)\s*({time_expr_regex()})\s+({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?",
        flags=re.I,
    )
    for m in pat4.finditer(s):
        t1, m1, d1, t2, m2, d2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
        y1 = base_year
        y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
        start_dt = make_timestamp(y1, m1, d1, t1)
        end_dt = make_timestamp(y2, m2, d2, t2)
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
            intervals.append({
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "explicit_multi_interval",
                "interval_quality": "exact_text_interval",
            })

    # Same-date non-parenthetical format: August 15th 1900-0700 / Emergency Department closed August 15th 1900-0700
    low = s.lower()
    if not re.search(weekday_regex(), low, flags=re.I) and not any(tok in low for tok in ["daily", "each day", "weekend", "operating hours", "open "]):
        pat5 = re.compile(
            rf"(?:^|[\s;,.])(?:closed\s*)?({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:at|from)?\s*({time_expr_regex()})\s*(?:-|to|until)\s*({time_expr_regex()})(?=$|[;,.\s])",
            flags=re.I,
        )
        for m in pat5.finditer(s):
            m1, d1, t1, t2 = m.group(1), m.group(2), m.group(3), m.group(4)
            start_dt = make_timestamp(base_year, m1, d1, t1)
            end_dt = make_timestamp(base_year, m1, d1, t2)
            if not pd.isna(start_dt) and not pd.isna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })

    # Time weekday, Month day to time weekday, Month day
    pat6 = re.compile(
        rf"({time_expr_regex()})\s+({weekday_regex()})?,?\s*({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|to|until)\s*({time_expr_regex()})\s+({weekday_regex()})?,?\s*({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?",
        flags=re.I,
    )
    for m in pat6.finditer(s):
        t1, _wd1, m1, d1, t2, _wd2, m2, d2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7), m.group(8)
        if is_calendar_year_token(t1) or is_calendar_year_token(t2):
            continue
        y1 = base_year
        y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
        start_dt = make_timestamp(y1, m1, d1, t1)
        end_dt = make_timestamp(y2, m2, d2, t2)
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
            intervals.append({
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "explicit_multi_interval",
                "interval_quality": "exact_text_interval",
            })

    # Month day from time to Month day time, e.g.
    # "August 6 from 1700 to August 7 0800".
    # This must run before date-after-time same-day parsers; otherwise the next
    # segment's month/day can be mistaken as the date for a previous "from X to Y" window.
    pat_month_day_from_time_to_month_day_time = re.compile(
        rf"({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+from\s+({time_expr_regex()})\s*(?:-|–|—|to|until)\s*(?:{weekday_regex()})?,?\s*({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+({time_expr_regex()})",
        flags=re.I,
    )
    for m in pat_month_day_from_time_to_month_day_time.finditer(s):
        m1, d1, y1_txt, t1, m2, d2, y2_txt, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7), m.group(8)
        y1 = int(y1_txt) if y1_txt else base_year
        y2 = int(y2_txt) if y2_txt else (y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0))
        start_dt = make_timestamp(y1, m1, d1, t1)
        end_dt = make_timestamp(y2, m2, d2, t2)
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
            intervals.append({
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "explicit_multi_interval",
                "interval_quality": "exact_text_interval",
            })

    # Month day from time to time, e.g. "August 4 from 0800 to 1700".
    pat_month_day_from_time_to_time = re.compile(
        rf"({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+from\s+({time_expr_regex()})\s*(?:-|–|—|to|until)\s*({time_expr_regex()})",
        flags=re.I,
    )
    for m in pat_month_day_from_time_to_time.finditer(s):
        mon, day, year_txt, t1, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        year = int(year_txt) if year_txt else base_year
        start_dt = make_timestamp(year, mon, day, t1)
        end_dt = make_timestamp(year, mon, day, t2)
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
            intervals.append({
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "explicit_multi_interval",
                "interval_quality": "exact_text_interval",
            })

    # Month day (time to time) already same-date explicit but some notices omit dash inside parentheses

    # Mixed explicit rows can contain both cross-date parenthetical intervals and
    # same-day parenthetical intervals, e.g.
    # "April 4 (6 pm) - April 5 (6 am), April 10 (6 am - 6 pm)".
    # extract_multi_intervals() is the first explicit parser in build_episode_intervals();
    # when it finds any cross-date interval, later explicit parsers are not called.
    # Fold in the existing same-day parser here so these explicit intervals are not dropped.
    intervals.extend(extract_single_day_interval(s, base_year))

    return dedupe_intervals_exact(intervals)


def extract_single_day_interval(text, base_year):
    s = clean_text(text) or ""
    s = s.replace("–", "-").replace("—", "-")
    month_pat = month_regex()
    texpr = time_expr_regex()
    patterns = [
        re.compile(
            rf"({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*(?:-|to|until)\s*({texpr})\s*\)",
            flags=re.I,
        ),
        re.compile(
            rf"({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)\s*(?:-|to|until)\s*\(\s*({texpr})\s*\)",
            flags=re.I,
        ),
    ]

    intervals = []
    prev_month_num = None
    current_year = base_year
    for pat in patterns:
        for m in pat.finditer(s):
            mon, day, t1, t2 = m.group(1), m.group(2), m.group(3), m.group(4)
            mn = MONTHS.get(mon.lower())
            if prev_month_num is not None and mn < prev_month_num:
                current_year += 1
            prev_month_num = mn

            start_dt = make_timestamp(current_year, mon, day, t1)
            end_dt = make_timestamp(current_year, mon, day, t2)
            if not pd.isna(start_dt) and not pd.isna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append(
                    {
                        "interval_start": start_dt,
                        "interval_end": end_dt,
                        "interval_method": "single_day_parenthetical",
                        "interval_quality": "exact_text_interval",
                    }
                )

    # Same-day explicit wording with the date after the time window, e.g.
    # "closed from 11 a.m. to 7 p.m., Monday, April 20".
    # Use the row/notice base year when no year is given.
    pat_named_same_day = re.compile(
        rf"(?:closed\s+)?from\s+({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*,?\s*(?:{weekday_regex()})?,?\s*({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat_named_same_day.finditer(s):
        # Guard against mixed list wording such as
        # "August 4 from 0800 to 1700, August 6 from 1700 to August 7 0800".
        # In that text, a date-after-time regex starting at "from 0800" can
        # incorrectly consume the next segment's "August 6" as the date for
        # the 0800-1700 window. If a Month Day phrase immediately precedes
        # the matched "from", this parser is the wrong parser; the
        # Month-day-from-time parser above should handle it instead.
        prior = s[max(0, m.start() - 40):m.start()]
        if re.search(rf"({month_pat})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s*$", prior, flags=re.I):
            continue
        t1, t2, mon, day, year_txt = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        year = int(year_txt) if year_txt else base_year
        start_dt = make_timestamp(year, mon, day, t1)
        end_dt = make_timestamp(year, mon, day, t2)
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
            intervals.append({
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "same_day_named_date_window",
                "interval_quality": "exact_text_interval",
            })

    # Malformed same-day parenthetical variant observed in AHS text, e.g.
    # "March 27 (0700 - March 27 (1700)". Treat as a same-day fixed interval
    # when the repeated endpoint date matches the start date.
    pat_malformed_same_day_parenthetical = re.compile(
        rf"({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*(?:-|–|—|to|until)\s*({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)?",
        flags=re.I,
    )
    for m in pat_malformed_same_day_parenthetical.finditer(s):
        mon1, day1, t1, mon2, day2, t2 = m.groups()
        if MONTHS.get(mon1.lower()) != MONTHS.get(mon2.lower()) or int(day1) != int(day2):
            continue
        start_dt = make_timestamp(base_year, mon1, day1, t1)
        end_dt = make_timestamp(base_year, mon1, day1, t2)
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
            intervals.append({
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "single_day_parenthetical",
                "interval_quality": "exact_text_interval",
            })

    return dedupe_intervals_exact(intervals)



def extract_explicit_closure_variants_v74(row, base_year):
    text_parts = [
        row.get("bed_or_space_reduction_text"),
        row.get("reason_text"),
        row.get("raw_block_text"),
    ]
    s = normalize_interval_parse_text(" ".join(str(x or "") for x in text_parts)) or ""
    if not s:
        return []

    month_pat = month_regex()
    texpr = time_expr_regex()
    intervals = []

    def is_calendar_year_token(tok) -> bool:
        """True when a candidate time token is actually a calendar year.

        This guards against strings like "June 18, 2025 - 8 a.m." being
        parsed as a start time of 20:25. Calendar years can appear between a
        date and a dash in full-date cross-day notices and must not be treated
        as military times.
        """
        s_tok = str(tok or "").strip().lower().replace("h", "")
        return bool(re.fullmatch(r"\d{4}", s_tok) and PARSER_YEAR_MIN <= int(s_tok) <= PARSER_YEAR_MAX)

    def _add_interval(start_dt, end_dt):
        if pd.isna(start_dt) or pd.isna(end_dt):
            return
        if not (is_valid_calendar_year_value(start_dt.year) and is_valid_calendar_year_value(end_dt.year)):
            return
        if abs(int(end_dt.year) - int(start_dt.year)) > 1:
            return
        if end_dt <= start_dt:
            end_dt = end_dt + pd.Timedelta(days=1)
        if end_dt <= start_dt:
            return
        intervals.append({
            "interval_start": start_dt,
            "interval_end": end_dt,
            "interval_method": "explicit_parenthetical_variants",
            "interval_quality": "exact_text_interval",
        })

    # Time + optional weekday + full date to time + optional weekday + full date, e.g.
    # "9 p.m., Wednesday, June 18, 2025 - 8 a.m., Thursday, June 19, 2025".
    # This must run before date-then-times fallbacks, otherwise the year can be
    # mistaken for a military-time token.
    pat_time_weekday_full_date = re.compile(
        rf"({texpr})\s*,?\s*(?:{weekday_regex()})?,?\s*({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|until)\s*({texpr})\s*,?\s*(?:{weekday_regex()})?,?\s*({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat_time_weekday_full_date.finditer(s):
        t1, m1, d1, y1_txt, t2, m2, d2, y2_txt = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7), m.group(8)
        if is_calendar_year_token(t1) or is_calendar_year_token(t2):
            continue
        y1 = int(y1_txt) if y1_txt else base_year
        y2 = int(y2_txt) if y2_txt else (y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0))
        _add_interval(make_timestamp(y1, m1, d1, t1), make_timestamp(y2, m2, d2, t2))

    pat_cross_full = re.compile(
        rf"({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until)\s*({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)",
        flags=re.I,
    )
    for m in pat_cross_full.finditer(s):
        m1, d1, t1, m2, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
        y1 = base_year
        y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
        _add_interval(make_timestamp(y1, m1, d1, t1), make_timestamp(y2, m2, d2, t2))

    pat_cross_same_month = re.compile(
        rf"({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until)\s*(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)",
        flags=re.I,
    )
    for m in pat_cross_same_month.finditer(s):
        m1, d1, t1, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        _add_interval(make_timestamp(base_year, m1, d1, t1), make_timestamp(base_year, m1, d2, t2))

    pat_same_day_parenthetical = re.compile(
        rf"({month_pat})\s+(\d{{1,2}}),?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until)\s*\(\s*({texpr})\s*\)",
        flags=re.I,
    )
    for m in pat_same_day_parenthetical.finditer(s):
        mon, day, t1, t2 = m.group(1), m.group(2), m.group(3), m.group(4)
        _add_interval(make_timestamp(base_year, mon, day, t1), make_timestamp(base_year, mon, day, t2))

    pat_full_date_time = re.compile(
        rf"({month_pat})\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?\s*({texpr})\s*(?:-|–|—|to|until)\s*({month_pat})\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?\s*({texpr})",
        flags=re.I,
    )
    for m in pat_full_date_time.finditer(s):
        m1, d1, y1_txt, t1, m2, d2, y2_txt, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7), m.group(8)
        # Do not let an explicit calendar year after a month/day be consumed as
        # the time token (e.g., "June 23, 2050 - 0700" should not become
        # June 23 20:50 to June 24 07:00).
        if is_calendar_year_token(t1) or is_calendar_year_token(t2):
            continue
        y1 = int(y1_txt) if y1_txt else base_year
        y2 = int(y2_txt) if y2_txt else (y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0))
        _add_interval(make_timestamp(y1, m1, d1, t1), make_timestamp(y2, m2, d2, t2))

    pat_time_on_date = re.compile(
        rf"({texpr})\s+on\s+({month_pat})\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|until)\s*({texpr})\s+on\s+({month_pat})\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat_time_on_date.finditer(s):
        t1, m1, d1, y1_txt, t2, m2, d2, y2_txt = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7), m.group(8)
        y1 = int(y1_txt) if y1_txt else base_year
        y2 = int(y2_txt) if y2_txt else (y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0))
        _add_interval(make_timestamp(y1, m1, d1, t1), make_timestamp(y2, m2, d2, t2))

    pat_time_range_then_date = re.compile(
        rf"(?:closed\s*:?\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?",
        flags=re.I,
    )
    for m in pat_time_range_then_date.finditer(s):
        t1, t2, mon, day = m.group(1), m.group(2), m.group(3), m.group(4)
        if is_calendar_year_token(t1) or is_calendar_year_token(t2):
            continue
        _add_interval(make_timestamp(base_year, mon, day, t1), make_timestamp(base_year, mon, day, t2))

    pat_date_then_times = re.compile(
        rf"(?:({month_pat})\s+)?(\d{{1,2}}),\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})",
        flags=re.I,
    )
    current_month = None
    current_year = base_year
    prev_month_num = None
    for m in pat_date_then_times.finditer(s):
        mon = m.group(1) or current_month
        day = m.group(2)
        t1 = m.group(3)
        t2 = m.group(4)
        if is_calendar_year_token(t1) or is_calendar_year_token(t2):
            continue
        if not mon:
            continue
        mn = MONTHS.get(mon.lower())
        if mn is None:
            continue
        if prev_month_num is not None and mn < prev_month_num:
            current_year += 1
        prev_month_num = mn
        current_month = mon
        _add_interval(make_timestamp(current_year, mon, day, t1), make_timestamp(current_year, mon, day, t2))

    start_dt = parse_boundary_datetime_field(row.get("start_date_parsed_clean"))
    if pd.isna(start_dt):
        start_dt = parse_boundary_datetime_field(row.get("start_date_text"))
    end_dt = parse_boundary_datetime_field(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(end_dt):
        end_dt = parse_boundary_datetime_field(row.get("anticipated_end_date_text"))
    if pd.notna(start_dt) and pd.notna(end_dt) and start_dt.normalize() == end_dt.normalize():
        pat_single_day_anchor = re.search(
            rf"({texpr})\s*(?:-|–|—|to|until)\s*({texpr})",
            s,
            flags=re.I,
        )
        if pat_single_day_anchor:
            t1, t2 = pat_single_day_anchor.group(1), pat_single_day_anchor.group(2)
            mins1 = parse_time_token(t1)
            mins2 = parse_time_token(t2)
            if mins1 is not None and mins2 is not None:
                hh1, mm1 = divmod(mins1, 60)
                hh2, mm2 = divmod(mins2, 60)
                start_anchor = pd.Timestamp(year=start_dt.year, month=start_dt.month, day=start_dt.day, hour=hh1, minute=mm1)
                end_anchor = pd.Timestamp(year=start_dt.year, month=start_dt.month, day=start_dt.day, hour=hh2, minute=mm2)
                _add_interval(start_anchor, end_anchor)


    pat_date_comma_from_times = re.compile(
        rf"(?:[A-Za-z]+,\s+)?({month_pat})\.?\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?,?\s*from\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})",
        flags=re.I,
    )
    for m in pat_date_comma_from_times.finditer(s):
        mon, day, year_txt, t1, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        year = int(year_txt) if year_txt else base_year
        _add_interval(make_timestamp(year, mon, day, t1), make_timestamp(year, mon, day, t2))

    return dedupe_intervals_exact(intervals)


def extract_begin_resume_interval(text, base_year):
    s = clean_text(text) or ""
    s = s.replace("–", "-").replace("—", "-")
    texpr = time_expr_regex()
    pattern = re.compile(
        rf"beginning\s+({month_regex()})\s+(\d{{1,2}})\s+at\s+({texpr})\s*,?\s*"
        rf".*?resume\s+({month_regex()})\s+(\d{{1,2}})\s+at\s+({texpr})",
        flags=re.I,
    )
    m = pattern.search(s)

    if not m:
        return []

    m1, d1, t1, m2, d2, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    y1 = base_year
    y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
    start_dt = make_timestamp(y1, m1, d1, t1)
    end_dt = make_timestamp(y2, m2, d2, t2)

    if pd.isna(start_dt) or pd.isna(end_dt) or end_dt <= start_dt:
        return []
    return [
        {
            "interval_start": start_dt,
            "interval_end": end_dt,
            "interval_method": "begin_resume",
            "interval_quality": "exact_text_interval",
        }
    ]


def _parse_month_day_year(month_name, day_token, year):
    try:
        return pd.Timestamp(year=year, month=MONTHS[month_name.lower()], day=int(day_token))
    except Exception:
        return pd.NaT

def _parse_month_day_tokens(day_text, start_month_name, year):
    """Parse comma/and-separated day tokens like '30, December 1, 2, 3' using month carry-forward."""
    month_pat = month_regex()
    token_pat = re.compile(rf"(?:({month_pat})\s+)?(\d{{1,2}})", flags=re.I)
    tokens = []
    current_month = start_month_name
    current_year = year
    prev_month_num = MONTHS.get(start_month_name.lower()) if start_month_name else None
    for m in token_pat.finditer(clean_text(day_text) or ""):
        mon = m.group(1) or current_month
        day = m.group(2)
        if not mon:
            continue
        mn = MONTHS.get(mon.lower())
        if mn is None:
            continue
        if prev_month_num is not None and mn < prev_month_num:
            current_year += 1
        prev_month_num = mn
        current_month = mon
        tokens.append((current_year, mon, day))
    return tokens


def extract_shared_window_date_pair_intervals(row, base_year):
    text = row.get("bed_or_space_reduction_text")
    s = normalize_interval_parse_text(text) or ""
    texpr = time_expr_regex()
    m = re.search(rf"(?:closed|from)?\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*:\s*(.+)$", s, flags=re.I)
    if not m:
        return []
    start_minutes = parse_time_token(m.group(1))
    end_minutes = parse_time_token(m.group(2))
    if start_minutes is None or end_minutes is None:
        return []
    tail = m.group(3)
    pair_pat = re.compile(rf"(?:({month_regex()})\s+)?(\d{{1,2}})\s*(?:-|–|—|to|until)\s*(?:({month_regex()})\s+)?(\d{{1,2}})", flags=re.I)
    intervals = []
    current_month = None
    current_year = base_year
    prev_month_num = None
    for pm in pair_pat.finditer(tail):
        m1 = pm.group(1) or current_month
        d1 = pm.group(2)
        m2 = pm.group(3) or m1
        d2 = pm.group(4)
        if not m1 or not m2:
            continue
        mn1 = MONTHS.get(m1.lower())
        mn2 = MONTHS.get(m2.lower())
        if prev_month_num is not None and mn1 is not None and mn1 < prev_month_num:
            current_year += 1
        y1 = current_year
        y2 = y1 + (1 if mn2 is not None and mn1 is not None and mn2 < mn1 else 0)
        prev_month_num = mn1
        current_month = m2
        hh1, mm1 = divmod(start_minutes, 60)
        hh2, mm2 = divmod(end_minutes, 60)
        try:
            start_dt = pd.Timestamp(year=y1, month=mn1, day=int(d1), hour=hh1, minute=mm1)
            end_dt = pd.Timestamp(year=y2, month=mn2, day=int(d2), hour=hh2, minute=mm2)
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
        except Exception:
            continue
        intervals.append({"interval_start": start_dt, "interval_end": end_dt, "interval_method": "narrative_overnight_explicit", "interval_quality": "exact_text_interval"})
    return dedupe_intervals_exact(intervals)




def extract_weekday_time_date_list_intervals(row, base_year):
    text = row.get("bed_or_space_reduction_text")
    s = normalize_interval_parse_text(text) or ""
    texpr = time_expr_regex()

    patterns = [
        re.compile(
            rf"on\s+({weekday_regex()})s?\s*\(\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*\)\s*(.+)$",
            flags=re.I,
        ),
        re.compile(
            rf"({weekday_regex()})s?\s*\(\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*\)\s*(.+)$",
            flags=re.I,
        ),
    ]

    m = None
    for pat in patterns:
        m = pat.search(s)
        if m:
            break
    if not m:
        return []

    start_minutes = parse_time_token(m.group(2))
    end_minutes = parse_time_token(m.group(3))
    if start_minutes is None or end_minutes is None:
        return []

    tail = m.group(4)
    date_pat = re.compile(rf"({month_regex()})\s+(\d{{1,2}})", flags=re.I)
    intervals = []
    current_year = base_year
    prev_month_num = None
    for dm in date_pat.finditer(tail):
        mon = dm.group(1)
        day = dm.group(2)
        mn = MONTHS.get(mon.lower())
        if mn is None:
            continue
        if prev_month_num is not None and mn < prev_month_num:
            current_year += 1
        prev_month_num = mn
        hh1, mm1 = divmod(start_minutes, 60)
        hh2, mm2 = divmod(end_minutes, 60)
        try:
            start_dt = pd.Timestamp(year=current_year, month=mn, day=int(day), hour=hh1, minute=mm1)
            end_dt = pd.Timestamp(year=current_year, month=mn, day=int(day), hour=hh2, minute=mm2)
            if end_dt <= start_dt:
                end_dt += pd.Timedelta(days=1)
        except Exception:
            continue
        intervals.append({
            "interval_start": start_dt,
            "interval_end": end_dt,
            "interval_method": "narrative_overnight_explicit",
            "interval_quality": "exact_text_interval",
        })

    return dedupe_intervals_exact(intervals)


def _parse_phase_start_from_text(s, base_year):
    """Parse the first explicit phase-change date after from/beginning/effective/as of."""
    if not s:
        return pd.NaT
    pat = re.search(
        rf"(?i)\b(?:from|beginning|starting|effective|as of)\s+(?:on\s+)?(?:{weekday_regex()}\s*,?\s*)?({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        s,
    )
    if not pat:
        return pd.NaT
    year = int(pat.group(3)) if pat.group(3) else int(base_year)
    return _parse_month_day_year(pat.group(1), pat.group(2), year)


def _daily_complement_intervals_from_open_window(start_date, end_date, open_start_mins, open_end_mins, method="daily_window_schedule"):
    """Return daily closure intervals outside a daily open window."""
    intervals = []
    if pd.isna(start_date) or pd.isna(end_date):
        return intervals
    if open_start_mins is None or open_end_mins is None or open_end_mins <= open_start_mins:
        return intervals
    current = pd.Timestamp(start_date).normalize()
    end_day = pd.Timestamp(end_date).normalize()
    while current <= end_day:
        day_open_start = current + pd.Timedelta(minutes=open_start_mins)
        day_open_end = current + pd.Timedelta(minutes=open_end_mins)
        if day_open_start > current:
            intervals.append({"interval_start": current, "interval_end": day_open_start, "interval_method": method, "interval_quality": "schedule_estimate"})
        next_day = current + pd.Timedelta(days=1)
        if day_open_end < next_day:
            intervals.append({"interval_start": day_open_end, "interval_end": next_day, "interval_method": method, "interval_quality": "schedule_estimate"})
        current += pd.Timedelta(days=1)
    return intervals


def _daily_closed_window_intervals(start_date, end_date, close_start_mins, close_end_mins, method="daily_window_schedule"):
    """Return repeated daily closure windows."""
    intervals = []
    if pd.isna(start_date) or pd.isna(end_date):
        return intervals
    if close_start_mins is None or close_end_mins is None:
        return intervals
    current = pd.Timestamp(start_date).normalize()
    end_day = pd.Timestamp(end_date).normalize()
    while current <= end_day:
        sdt = current + pd.Timedelta(minutes=close_start_mins)
        edt = current + pd.Timedelta(minutes=close_end_mins)
        if edt <= sdt:
            edt += pd.Timedelta(days=1)
        intervals.append({"interval_start": sdt, "interval_end": edt, "interval_method": method, "interval_quality": "schedule_estimate"})
        current += pd.Timedelta(days=1)
    return intervals



def _open_hours_weekly_complement_intervals(start_dt, end_dt, open_intervals, method="regular_hours_complement"):
    """Return closure intervals that are the complement of weekly open intervals.

    open_intervals are tuples of (start_weekday, start_minutes, end_weekday, end_minutes).
    Endpoints are clipped to the row's date span. This is used for AHS wording that
    explicitly lists adjusted OPEN hours rather than closure hours.
    """
    start_dt = pd.to_datetime(start_dt, errors="coerce")
    end_dt = pd.to_datetime(end_dt, errors="coerce")
    if pd.isna(start_dt) or pd.isna(end_dt) or not open_intervals:
        return []
    lower = start_dt.normalize()
    upper = end_dt.normalize() + pd.Timedelta(days=1)
    candidate_opens = []
    # Back up one week so continuous Fri-Mon open intervals can cover a Monday
    # row-start correctly.
    cur = lower - pd.Timedelta(days=7)
    final = upper + pd.Timedelta(days=7)
    while cur <= final:
        week_anchor = cur - pd.Timedelta(days=cur.weekday())
        for swd, smins, ewd, emins in open_intervals:
            if smins is None or emins is None:
                continue
            start = week_anchor + pd.Timedelta(days=int(swd), minutes=int(smins))
            day_delta = (int(ewd) - int(swd)) % 7
            end = week_anchor + pd.Timedelta(days=int(swd) + day_delta, minutes=int(emins))
            if end <= start:
                end += pd.Timedelta(days=7)
            if end <= lower or start >= upper:
                continue
            candidate_opens.append((max(start, lower), min(end, upper)))
        cur += pd.Timedelta(days=7)
    if not candidate_opens:
        return [{"interval_start": lower, "interval_end": upper, "interval_method": method, "interval_quality": "schedule_estimate"}]
    candidate_opens.sort()
    merged = []
    for a, b in candidate_opens:
        if b <= a:
            continue
        if not merged or a > merged[-1][1]:
            merged.append([a, b])
        else:
            if b > merged[-1][1]:
                merged[-1][1] = b
    intervals = []
    cursor = lower
    for a, b in merged:
        if a > cursor:
            intervals.append({"interval_start": cursor, "interval_end": a, "interval_method": method, "interval_quality": "schedule_estimate"})
        if b > cursor:
            cursor = b
    if cursor < upper:
        intervals.append({"interval_start": cursor, "interval_end": upper, "interval_method": method, "interval_quality": "schedule_estimate"})
    return [iv for iv in intervals if iv["interval_end"] > iv["interval_start"]]


def is_adjusted_open_hours_complement_text(text):
    s = clean_text(text) or ""
    if not s:
        return False
    return bool(
        re.search(r"\badjusted\s+hours\s+of\s+operation\b", s, flags=re.I)
        and re.search(r"\bopen\b", s, flags=re.I)
        and re.search(r"\b24\s*hours?\s+per\s+day\s+starting\s+fridays?\b", s, flags=re.I)
    )


def extract_adjusted_open_hours_complement_intervals(row):
    """Parse adjusted OPEN-hours rows as closed-time complements.

    Bow Island-style wording lists when the ED is open, e.g.:
      Adjusted hours of operation: Open Tuesday 0800-2000 ...
      Open 24 hours per day starting Fridays at 0700 through to Mondays at 2000.
    The closure burden is the complement of those open windows.
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = clean_text(text) or ""
    if not is_adjusted_open_hours_complement_text(s):
        return []
    start_dt = parse_dt_any(row.get("start_date_parsed_clean"))
    end_dt = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(start_dt) or pd.isna(end_dt):
        return []
    texpr = time_expr_regex()
    open_windows = []
    # Single-day open windows such as "Open Tuesday 0800 - 2000".
    single_pat = re.compile(rf"\bopen\s+({weekday_regex()})\s+({texpr})\s*(?:-|–|—|to|until)\s*({texpr})", flags=re.I)
    for m in single_pat.finditer(s):
        wd = WEEKDAY_NAME_TO_NUM[str(m.group(1)).lower().rstrip("s")]
        open_windows.append((wd, parse_time_token(m.group(2)), wd, parse_time_token(m.group(3))))
    # Continuous open range: "Open 24 hours per day starting Fridays at 0700 through to Mondays at 2000".
    range_pat = re.compile(rf"\bopen\s+24\s*hours?\s+per\s+day\s+starting\s+({weekday_regex()})s?\s+at\s+({texpr})\s+through\s+(?:to\s+)?({weekday_regex()})s?\s+at\s+({texpr})", flags=re.I)
    for m in range_pat.finditer(s):
        swd = WEEKDAY_NAME_TO_NUM[str(m.group(1)).lower().rstrip("s")]
        ewd = WEEKDAY_NAME_TO_NUM[str(m.group(3)).lower().rstrip("s")]
        open_windows.append((swd, parse_time_token(m.group(2)), ewd, parse_time_token(m.group(4))))
    if not open_windows:
        return []
    return dedupe_intervals_exact(_open_hours_weekly_complement_intervals(start_dt, end_dt, open_windows, method="regular_hours_complement"))


def extract_midnight_preamble_daily_list_intervals(row):
    """Recover daily midnight-0800 rows whose listed dates are cross-date malformed.

    Example: source preamble says "closed midnight to 8:00am" but the list is
    written "May 29 (0000) - May 30 (0800), May 30 (0000) - May 31 (0800)".
    The intended closures are same-start-date 0000-0800 daily windows, not 32-hour
    overlapping cross-date closures.
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not re.search(r"\bclosed\s+midnight\s*(?:-|to|until)\s*8(?::00)?\s*a\.?m\.?", s, flags=re.I):
        return []
    mpat = month_regex()
    texpr = time_expr_regex()
    pat = re.compile(rf"({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*\(\s*(0?000|12\s*midnight|midnight)\s*\)\s*(?:-|–|—|to|until)\s*(?:({mpat})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*\(\s*(0?800|8(?::00)?\s*a\.?m\.?)\s*\)", flags=re.I)
    base_year = base_year_for_row(row)
    intervals = []
    for m in pat.finditer(s):
        mon1, d1, y1_txt, _t1, _mon2, _d2, _y2_txt, _t2 = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        start = _make_timestamp_extended(y1, mon1, d1, "0000")
        end = _make_timestamp_extended(y1, mon1, d1, "0800")
        _append_interval_if_valid(intervals, start, end, method="daily_window_schedule")

    # Same-day parenthetical list variant observed in Fort Vermilion review rows:
    # "May 29 (0000 - 0800), May 30 (0000 - 0800), ...". Because
    # build_episode_intervals() suppresses broad explicit parsers for midnight
    # preamble rows, this schedule parser must recover these same-day entries.
    pat_same_day = re.compile(
        rf"({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*\(\s*(0?000|12\s*midnight|midnight)\s*(?:-|–|—|to|until)\s*(0?800|8(?::00)?\s*a\.?m\.?)\s*\)",
        flags=re.I,
    )
    for m in pat_same_day.finditer(s):
        mon1, d1, y1_txt, _t1, _t2 = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        start = _make_timestamp_extended(y1, mon1, d1, "0000")
        end = _make_timestamp_extended(y1, mon1, d1, "0800")
        _append_interval_if_valid(intervals, start, end, method="daily_window_schedule")
    return dedupe_intervals_exact(intervals)


def _drop_midnight_preamble_crossdate_artifacts(row, intervals):
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else ""
    s = normalize_interval_parse_text(text) or ""
    if not intervals or not re.search(r"\bclosed\s+midnight\s*(?:-|to|until)\s*8(?::00)?\s*a\.?m\.?", s, flags=re.I):
        return intervals
    out = []
    for iv in intervals:
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        en = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.notna(st) and pd.notna(en) and st.hour == 0 and st.minute == 0 and en.hour == 8 and (en - st).total_seconds() / 3600.0 > 24:
            continue
        out.append(iv)
    return out


def _parse_service_resume_date_for_terminal_drop(text, base_year, row=None):
    s = clean_text(text) or ""
    if not re.search(r"\b(?:service\s+)?resum(?:e|es|ing)|re-?open|return\s+to\s+(?:regular|normal|24-hour)", s, flags=re.I):
        return pd.NaT
    m = re.search(rf"\b(?:service\s+)?resum(?:e|es|ing)[^.;]{{0,80}}?(?:on\s+)?(?:{weekday_regex()}\s*,?\s*)?({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?", s, flags=re.I)
    if not m:
        m = re.search(rf"\bre-?open[^.;]{{0,80}}?(?:on\s+)?(?:{weekday_regex()}\s*,?\s*)?({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?", s, flags=re.I)
    if not m:
        return pd.NaT
    y = int(m.group(3)) if m.group(3) else _event_year_for_unyear_month(m.group(1), base_year, row)
    return _parse_month_day_year(m.group(1), m.group(2), y)


def _drop_service_resume_terminal_schedule_artifacts(row, intervals):
    if not intervals:
        return intervals
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else ""
    base_year = base_year_for_row(row)
    resume_date = _parse_service_resume_date_for_terminal_drop(text, base_year, row)
    if pd.isna(resume_date):
        return intervals
    resume_day = pd.Timestamp(resume_date).normalize()
    out = []
    for iv in intervals:
        method = str(iv.get("interval_method", ""))
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        if method in ESTIMATED_METHODS and pd.notna(st) and st.normalize() >= resume_day:
            continue
        out.append(iv)
    return out

def extract_phase_change_day_open_intervals(row, base_year):
    """Parse phase-change rows where a full closure is followed by changed hours.

    Handles broad future variants, not only the Swan Hills wording:
      - From April 13, ED open during the day only, from 7am to 7pm (closed overnights)
      - Beginning May 5, ED open daily from 8 a.m. to 8 p.m.
      - Effective June 3, service unavailable/closed overnight from 1900 to 0700 daily
      - Starting July 2, ED will be open from 0900 to 1700 and closed outside those hours
    """
    text = row.get("bed_or_space_reduction_text")
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    if not _has_schedule_recurrence_cue(s):
        return []

    row_start = parse_dt_any(row.get("start_date_parsed_clean"))
    row_end = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(row_start) or pd.isna(row_end):
        return []

    # Prefer the phase-change date nearest the schedule/change cue, not the first
    # date in the row. Rows may begin with a separate full-closure interval
    # (e.g., "closed from April 1 ... From April 13, open during the day only").
    cue_match = re.search(
        r"(?is)\b(open during the day only|day only|open daily from|open from|will be open|closed overnights?|closed overnight|unavailable overnight|overnight closures?|closed outside (?:those|these) hours)\b",
        s,
    )
    phase_start = pd.NaT
    if cue_match:
        context_start = max(0, cue_match.start() - 120)
        phase_start = _parse_phase_start_from_text(s[context_start:cue_match.start()+80], base_year)
    if pd.isna(phase_start):
        phase_start = _parse_phase_start_from_text(s, base_year)
    if pd.isna(phase_start):
        phase_start = row_start
    phase_start = max(pd.Timestamp(phase_start), pd.Timestamp(row_start))

    texpr = _time_expr_extended_regex()
    intervals = []

    # Open-hours complement: open/day-only/daily from T to T => closed outside those hours.
    open_patterns = [
        rf"(?is)\bopen\s+during\s+the\s+day\s+only,?\s*from\s*({texpr})\s*(?:to|-|until)\s*({texpr})",
        rf"(?is)\bopen\s+(?:daily\s+)?from\s*({texpr})\s*(?:to|-|until)\s*({texpr})",
        rf"(?is)\bwill\s+(?:be\s+)?open\s+(?:daily\s+)?from\s*({texpr})\s*(?:to|-|until)\s*({texpr})",
        rf"(?is)\bopen\s+between\s+(?:the\s+hours\s+of\s+)?({texpr})\s*(?:and|to|-|until)\s*({texpr})",
    ]
    for pat in open_patterns:
        m = re.search(pat, s, flags=re.I)
        if not m:
            continue
        open_start = parse_time_token_extended(m.group(1))
        open_end = parse_time_token_extended(m.group(2))
        intervals.extend(_daily_complement_intervals_from_open_window(phase_start, row_end, open_start, open_end))
        if intervals:
            return dedupe_intervals_exact(intervals)

    # Direct repeated closure window: unavailable/closed overnight from T to T daily.
    closed_patterns = [
        rf"(?is)\b(?:closed|unavailable)\s+overnights?\s*(?:from\s*)?({texpr})\s*(?:to|-|until)\s*({texpr})",
        rf"(?is)\bovernight\s+closures?\s*(?:from\s*)?({texpr})\s*(?:to|-|until)\s*({texpr})",
        rf"(?is)\bclosed\s+(?:daily\s+)?from\s*({texpr})\s*(?:to|-|until)\s*({texpr})\s*(?:daily|each\s+day|seven\s+days\s+a\s+week|7\s+days\s+a\s+week)",
        rf"(?is)\bfrom\s*({texpr})\s*(?:to|-|until)\s*({texpr})\s*(?:daily|each\s+day|overnights?|overnight)",
    ]
    for pat in closed_patterns:
        m = re.search(pat, s, flags=re.I)
        if not m:
            continue
        close_start = parse_time_token_extended(m.group(1))
        close_end = parse_time_token_extended(m.group(2))
        intervals.extend(_daily_closed_window_intervals(phase_start, row_end, close_start, close_end))
        if intervals:
            return dedupe_intervals_exact(intervals)

    return []

def extract_generic_timed_window_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = normalize_interval_parse_text(text) or ""
    prog = safe_lower(row.get("program_or_service"))
    if not re.search(r"emergency", prog, flags=re.I):
        return []
    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    end_date = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(start_date) or pd.isna(end_date):
        return []
    span_days = (end_date.normalize() - start_date.normalize()).days
    if span_days < 0 or span_days > 14:
        return []
    texpr = time_expr_regex()
    m = re.search(rf"closed\s*(?:overnight\s*)?(?:from\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})(?:\s*the following day|\s*the next day|\s*$)", s, flags=re.I)
    method = "daily_window_schedule"
    if not m:
        m = re.search(rf"between the hours of\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})", s, flags=re.I)
    else:
        method = "overnight_date_range_schedule"
    if not m:
        return []
    start_minutes = parse_time_token(m.group(1))
    end_minutes = parse_time_token(m.group(2))
    if start_minutes is None or end_minutes is None:
        return []
    return build_daily_repeated_intervals(start_date, end_date, start_minutes, end_minutes, method, "schedule_estimate")


def extract_compound_weekly_schedule_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = normalize_interval_parse_text(text) or ""
    texpr = time_expr_regex()
    pat = re.compile(rf"(?:closed\s+from\s+)?({weekday_regex()})s?\s*\(\s*({texpr})\s*\)\s*(?:to|through|until|-|–|—)\s*({weekday_regex()})s?\s*\(\s*({texpr})\s*\)", flags=re.I)
    matches = list(pat.finditer(s))
    if len(matches) < 2:
        return []
    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    end_date = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(start_date) or pd.isna(end_date):
        return []
    intervals = []
    for m in matches:
        start_wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
        end_wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
        start_minutes = parse_time_token(m.group(2))
        end_minutes = parse_time_token(m.group(4))
        if start_minutes is None or end_minutes is None:
            continue
        intervals.extend(generate_weekly_intervals(start_date, end_date, start_wd, end_wd, start_minutes, end_minutes, "weekly_named_range_schedule"))
    return dedupe_intervals_exact(intervals)

def _weekday_range_nums(start_name, end_name):
    start_key = str(start_name).lower().rstrip('s')
    end_key = str(end_name).lower().rstrip('s')
    if start_key not in WEEKDAY_NAME_TO_NUM or end_key not in WEEKDAY_NAME_TO_NUM:
        return []
    a = WEEKDAY_NAME_TO_NUM[start_key]
    b = WEEKDAY_NAME_TO_NUM[end_key]
    if a <= b:
        return list(range(a, b + 1))
    return list(range(a, 7)) + list(range(0, b + 1))


def extract_weekday_range_window_between_intervals(row):
    """Parse bounded weekday-range time windows, including AHS forms such as:
    "closed Monday to Thursday from 1700-0800 between October 14 to November 10".
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    texpr = time_expr_regex()
    wpat = weekday_regex()
    mpat = month_regex()
    patterns = [
        re.compile(
            rf"(?:closed|closure|without\s+coverage|no\s+physician[^.;]*?)\s+({wpat})\s*(?:-|–|—|to|through|thru)\s*({wpat})\s+from\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+between\s+({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|through|thru|until)\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
            flags=re.I,
        ),
        re.compile(
            rf"({wpat})\s*(?:-|–|—|to|through|thru)\s*({wpat})\s+from\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+between\s+({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*(?:-|–|—|to|through|thru|until)\s*({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
            flags=re.I,
        ),
    ]
    intervals = []
    for pat in patterns:
        for m in pat.finditer(s):
            wd1, wd2, t1, t2, mon1, d1, y1_txt, mon2, d2, y2_txt = m.groups()
            y1 = int(y1_txt) if y1_txt else int(base_year_for_row(row))
            y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
            start_bound = pd.Timestamp(year=y1, month=MONTHS[str(mon1).lower()], day=int(d1))
            end_bound = pd.Timestamp(year=y2, month=MONTHS[str(mon2).lower()], day=int(d2))
            weekdays = set(_weekday_range_nums(wd1, wd2))
            start_minutes = parse_time_token(t1); end_minutes = parse_time_token(t2)
            if start_minutes is None or end_minutes is None or not weekdays:
                continue
            cur = start_bound.normalize()
            while cur <= end_bound.normalize():
                if cur.weekday() in weekdays:
                    hh1, mm1 = divmod(start_minutes, 60); hh2, mm2 = divmod(end_minutes, 60)
                    st = cur + pd.Timedelta(hours=hh1, minutes=mm1)
                    en = cur + pd.Timedelta(hours=hh2, minutes=mm2)
                    if en <= st:
                        en += pd.Timedelta(days=1)
                    intervals.append({"interval_start": st, "interval_end": en, "interval_method": "daily_window_schedule", "interval_quality": "schedule_estimate"})
                cur += pd.Timedelta(days=1)
    return dedupe_intervals_exact(intervals)


def extract_overnight_hours_bounded_by_begin_resume(row, base_year):
    """Parse bounded recurring overnight window described by begin/resume brackets.

    Handles forms like: "closed overnight for the hours of 1700 to 0700 the following
    morning (beginning August 19 at 1700, regular hours to resume August 24 at 0700)".
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    low = s.lower()
    if "overnight" not in low or "following morning" not in low or not re.search(r"\b(beginning|starting|effective)\b", low):
        return []
    texpr = time_expr_regex(); mpat = month_regex()
    # Main overnight window.
    win = re.search(rf"hours?\s+of\s+({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+the\s+following\s+morning", s, flags=re.I)
    if not win:
        win = re.search(rf"overnight[^.;()]*?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+the\s+following\s+morning", s, flags=re.I)
    if not win:
        return []
    t1, t2 = win.group(1), win.group(2)
    b = re.search(rf"(?:beginning|starting|effective)\s+({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:at\s+)?({texpr})", s, flags=re.I)
    e = re.search(rf"(?:resume|resumes|re-?open|return\s+to\s+regular\s+hours|regular\s+hours\s+to\s+resume)[^.;()]*?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:at\s+)?({texpr})", s, flags=re.I)
    if not b or not e:
        return []
    mon1, d1, y1_txt, _bt = b.groups()
    mon2, d2, y2_txt, _et = e.groups()
    y1 = int(y1_txt) if y1_txt else int(base_year)
    y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
    start_bound = pd.Timestamp(year=y1, month=MONTHS[str(mon1).lower()], day=int(d1))
    end_bound = pd.Timestamp(year=y2, month=MONTHS[str(mon2).lower()], day=int(d2))
    start_minutes = parse_time_token(t1); end_minutes = parse_time_token(t2)
    if start_minutes is None or end_minutes is None or end_bound <= start_bound:
        return []
    intervals = []
    cur = start_bound.normalize()
    while cur < end_bound.normalize():
        hh1, mm1 = divmod(start_minutes, 60); hh2, mm2 = divmod(end_minutes, 60)
        st = cur + pd.Timedelta(hours=hh1, minutes=mm1)
        en = cur + pd.Timedelta(hours=hh2, minutes=mm2)
        if en <= st:
            en += pd.Timedelta(days=1)
        # Do not extend past the stated resume boundary.
        en = min(en, end_bound + pd.Timedelta(hours=end_minutes//60, minutes=end_minutes%60))
        if en > st:
            intervals.append({"interval_start": st, "interval_end": en, "interval_method": "daily_window_schedule", "interval_quality": "schedule_estimate"})
        cur += pd.Timedelta(days=1)
    return dedupe_intervals_exact(intervals)


def extract_narrative_overnight_intervals(row, base_year):
    """
    Handles natural-language overnight phrasing such as:
      - closed overnight on May 13 and 14, from 7:00 p.m. until 7:00 a.m. the following morning
      - closed overnight from 7:00pm until 7:00am the following morning, on May 9, 10, 11
      - closed overnight (between 1900 and 0800 the next day) on the following nights: December 4, 5, 6
      - closed overnight at 7:00pm until 7:00am the following morning
    Returns explicit intervals when specific dates are named, otherwise a short schedule estimate
    over the row's start/end date span.
    """
    text = row.get("bed_or_space_reduction_text")
    s = normalize_interval_parse_text(text) or ""
    low = s.lower()
    if (
        "overnight" not in low
        and "following nights" not in low
        and "following morning on" not in low
        and "the next day on" not in low
    ):
        return []

    texpr = time_expr_regex()

    def build_from_days(month_name, day_tokens, t1, t2):
        intervals = []
        year = base_year
        start_minutes = parse_time_token(t1)
        end_minutes = parse_time_token(t2)
        # AHS source typo guard: rows saying "closed overnight" with "1900 to 2000 the next day"
        # are internally contradictory and match adjacent rows that use 1900-0800. Treat exact
        # 20:00 overnight-next-day endings as 08:00 rather than a 25-hour closure.
        if start_minutes is not None and end_minutes == 20 * 60 and "overnight" in low and re.search(r"\b2000\b|8\s*p\.?m\.?", str(t2), flags=re.I):
            end_minutes = 8 * 60
        if start_minutes is None or end_minutes is None:
            return []
        for tok in day_tokens:
            tok = clean_text(tok)
            if not tok:
                continue
            m = re.match(r"(\d{1,2})", tok)
            if not m:
                continue
            day = int(m.group(1))
            hh1, mm1 = divmod(start_minutes, 60)
            hh2, mm2 = divmod(end_minutes, 60)
            try:
                interval_start = pd.Timestamp(year=year, month=MONTHS[month_name.lower()], day=day, hour=hh1, minute=mm1)
                interval_end = pd.Timestamp(year=year, month=MONTHS[month_name.lower()], day=day, hour=hh2, minute=mm2) + pd.Timedelta(days=1)
            except Exception:
                continue
            intervals.append({
                "interval_start": interval_start,
                "interval_end": interval_end,
                "interval_method": "narrative_overnight_explicit",
                "interval_quality": "exact_text_interval",
            })
        return intervals

    shared_pair_intervals = extract_shared_window_date_pair_intervals(row, base_year)
    if shared_pair_intervals:
        return shared_pair_intervals

    pat_a = re.compile(
        rf"overnight on\s+({month_regex()})\s+([\d,\sand]+),\s*from\s*({texpr})\s*(?:until|to|-)\s*({texpr}).*?following morning",
        flags=re.I,
    )
    m = pat_a.search(s)
    if m:
        month_name = m.group(1)
        days_text = m.group(2)
        day_tokens = re.split(r",|and", days_text)
        out = build_from_days(month_name, day_tokens, m.group(3), m.group(4))
        if out:
            return out

    pat_b = re.compile(
        rf"overnight\s*(?:at|from)?\s*({texpr})\s*(?:until|to|-)\s*({texpr}).*?following morning.*?on\s+({month_regex()})\s+([\d,\sand]+)",
        flags=re.I,
    )
    m = pat_b.search(s)
    if m:
        month_name = m.group(3)
        days_text = m.group(4)
        day_tokens = re.split(r",|and", days_text)
        out = build_from_days(month_name, day_tokens, m.group(1), m.group(2))
        if out:
            return out

    # Handles repeated month-name lists such as:
    # "closed overnight (between 1900 and 2000 the next day) on the following nights:
    # September 1, September 2, September 3, September 4".
    pat_c_repeated_months = re.compile(
        rf"overnight\s*\(\s*between\s*({texpr})\s*(?:and|to|-)\s*({texpr})\s*the next day\s*\)\s*on the following nights:\s*(.+)$",
        flags=re.I,
    )
    m = pat_c_repeated_months.search(s)
    if m:
        start_minutes = parse_time_token(m.group(1))
        end_minutes = parse_time_token(m.group(2))
        if start_minutes is not None and end_minutes == 20 * 60 and "overnight" in low and re.search(r"\b2000\b|8\s*p\.?m\.?", str(m.group(2)), flags=re.I):
            end_minutes = 8 * 60
        if start_minutes is not None and end_minutes is not None:
            hh1, mm1 = divmod(start_minutes, 60)
            hh2, mm2 = divmod(end_minutes, 60)
            tail = m.group(3)
            date_tokens = []
            cur_month = None
            cur_year = base_year
            prev_month_num = None
            for dm in re.finditer(rf"(?:({month_regex()})\s+)?(\d{{1,2}})", tail, flags=re.I):
                mon = dm.group(1) or cur_month
                day = dm.group(2)
                if not mon:
                    continue
                mn = MONTHS.get(mon.lower())
                if mn is None:
                    continue
                if prev_month_num is not None and mn < prev_month_num:
                    cur_year += 1
                prev_month_num = mn
                cur_month = mon
                date_tokens.append((cur_year, mn, int(day)))
            out = []
            for y, mn, day in date_tokens:
                try:
                    st = pd.Timestamp(year=y, month=mn, day=day, hour=hh1, minute=mm1)
                    en = pd.Timestamp(year=y, month=mn, day=day, hour=hh2, minute=mm2) + pd.Timedelta(days=1)
                except Exception:
                    continue
                out.append({"interval_start": st, "interval_end": en, "interval_method": "narrative_overnight_explicit", "interval_quality": "exact_text_interval"})
            out = dedupe_intervals_exact(out)
            if out:
                return out

    pat_c = re.compile(
        rf"overnight\s*\(\s*between\s*({texpr})\s*(?:and|to|-)\s*({texpr})\s*the next day\s*\)\s*on the following nights:\s*({month_regex()})\s+([\d,\sand]+)",
        flags=re.I,
    )
    m = pat_c.search(s)
    if m:
        month_name = m.group(3)
        days_text = m.group(4)
        day_tokens = re.split(r",|and", days_text)
        out = build_from_days(month_name, day_tokens, m.group(1), m.group(2))
        if out:
            return out

    pat_c2 = re.compile(
        rf"from\s*({texpr})\s*(?:until|to|-)\s*({texpr})\s*the following morning\s*on\s*({month_regex()})\s+([\d,\sand]+)",
        flags=re.I,
    )
    m = pat_c2.search(s)
    if m:
        month_name = m.group(3)
        days_text = m.group(4)
        day_tokens = re.split(r",|and", days_text)
        out = build_from_days(month_name, day_tokens, m.group(1), m.group(2))
        if out:
            return out

    pat_c3 = re.compile(
        rf"between\s*({texpr})\s*(?:and|to|-)\s*({texpr})\s*the next day\s*on\s*({month_regex()})\s+([\d,\sand]+)",
        flags=re.I,
    )
    m = pat_c3.search(s)
    if m:
        month_name = m.group(3)
        days_text = m.group(4)
        day_tokens = re.split(r",|and", days_text)
        out = build_from_days(month_name, day_tokens, m.group(1), m.group(2))
        if out:
            return out

    # closed overnight from 1700-0800 on: April 12, 13, 17, 18
    pat_c4 = re.compile(
        rf"overnight\s+from\s*({texpr})\s*(?:and|to|-)\s*({texpr})\s*on:\s*({month_regex()})\s+([\d,\sand]+)",
        flags=re.I,
    )
    m = pat_c4.search(s)
    if m:
        month_name = m.group(3)
        day_tokens = re.split(r",|and", m.group(4))
        out = build_from_days(month_name, day_tokens, m.group(1), m.group(2))
        if out:
            return out

    # closed at 1600 on the following dates (re-opening at 0800 the following day): October 30, November 1, November 2
    pat_c5 = re.compile(
        rf"closed\s+(?:overnight\s+)?at\s*({texpr})\s*on\s+the\s+following\s+dates.*?re-?opening\s+at\s*({texpr}).*?:\s*({month_regex()})\s+([\d,\sand]+(?:\s*,\s*{month_regex()}\s+[\d,\sand]+)*)",
        flags=re.I,
    )
    m = pat_c5.search(s)
    if m:
        month_name = m.group(3)
        day_items = _parse_month_day_tokens(m.group(4), month_name, base_year)
        intervals = []
        start_minutes = parse_time_token(m.group(1)); end_minutes = parse_time_token(m.group(2))
        if start_minutes is not None and end_minutes is not None:
            for year_i, mon_i, day_i in day_items:
                hh1, mm1 = divmod(start_minutes, 60); hh2, mm2 = divmod(end_minutes, 60)
                try:
                    st = pd.Timestamp(year=year_i, month=MONTHS[mon_i.lower()], day=int(day_i), hour=hh1, minute=mm1)
                    en = pd.Timestamp(year=year_i, month=MONTHS[mon_i.lower()], day=int(day_i), hour=hh2, minute=mm2) + pd.Timedelta(days=1)
                except Exception:
                    continue
                intervals.append({"interval_start": st, "interval_end": en, "interval_method": "narrative_overnight_explicit", "interval_quality": "exact_text_interval"})
        out = dedupe_intervals_exact(intervals)
        if out:
            return out

    # closed temporarily from 1900-0700: November 30, December 1, 2, 3
    pat_c6 = re.compile(
        rf"(?:closed\s+temporarily\s+)?from\s*({texpr})\s*(?:and|to|-)\s*({texpr})\s*:\s*({month_regex()})\s+([\d,\sand]+(?:\s*,\s*{month_regex()}\s+[\d,\sand]+)*)",
        flags=re.I,
    )
    m = pat_c6.search(s)
    if m:
        month_name = m.group(3)
        day_items = _parse_month_day_tokens(m.group(4), month_name, base_year)
        intervals = []
        start_minutes = parse_time_token(m.group(1)); end_minutes = parse_time_token(m.group(2))
        if start_minutes is not None and end_minutes is not None:
            for year_i, mon_i, day_i in day_items:
                hh1, mm1 = divmod(start_minutes, 60); hh2, mm2 = divmod(end_minutes, 60)
                try:
                    st = pd.Timestamp(year=year_i, month=MONTHS[mon_i.lower()], day=int(day_i), hour=hh1, minute=mm1)
                    en = pd.Timestamp(year=year_i, month=MONTHS[mon_i.lower()], day=int(day_i), hour=hh2, minute=mm2) + pd.Timedelta(days=1)
                except Exception:
                    continue
                intervals.append({"interval_start": st, "interval_end": en, "interval_method": "narrative_overnight_explicit", "interval_quality": "exact_text_interval"})
        out = dedupe_intervals_exact(intervals)
        if out:
            return out

    pat_d = re.compile(
        rf"overnight\s*(?:at|from)?\s*({texpr})\s*(?:until|to|-)\s*({texpr}).*?following morning",
        flags=re.I,
    )
    m = pat_d.search(s)
    if m:
        start_minutes = parse_time_token(m.group(1))
        end_minutes = parse_time_token(m.group(2))
        start_date = row.get("start_date_parsed_clean")
        end_date = row.get("anticipated_end_date_parsed_clean")
        if (
            start_minutes is not None and end_minutes is not None
            and not pd.isna(start_date) and not pd.isna(end_date)
        ):
            span_days = (end_date.normalize() - start_date.normalize()).days
            if 0 <= span_days <= 7:
                intervals = []
                current = start_date.normalize()
                while current < end_date.normalize():
                    hh1, mm1 = divmod(start_minutes, 60)
                    hh2, mm2 = divmod(end_minutes, 60)
                    interval_start = current + pd.Timedelta(hours=hh1, minutes=mm1)
                    interval_end = current + pd.Timedelta(days=1, hours=hh2, minutes=mm2)
                    intervals.append({
                        "interval_start": interval_start,
                        "interval_end": interval_end,
                        "interval_method": "overnight_date_range_schedule",
                        "interval_quality": "schedule_estimate",
                    })
                    current += pd.Timedelta(days=1)
                if intervals:
                    return intervals

    return []


def extract_full_closure_addon_intervals(row, base_year):
    """
    Parse add-on full-closure clauses embedded alongside a daily schedule baseline, for example:
    "Plus full closure of the department on the following dates: July 16 - July 17 (24 hours),
     July 18 - July 22 (3 days), July 25 - July 29 (3 1/2 days)"

    These closures are explicit about dates but often omit clock times. We treat them as
    estimated intervals whose duration is governed primarily by the stated duration token
    when present. When only a date range is given, we fall back to a full-day span estimate.
    """
    s = normalize_interval_parse_text(row.get("bed_or_space_reduction_text")) or ""
    low = s.lower()
    if "full closure" not in low:
        return []

    tail_match = re.search(r"full closure.*?:\s*(.+)$", s, flags=re.I)
    if not tail_match:
        return []

    tail = tail_match.group(1)
    parts = [clean_text(p) for p in re.split(r";|,(?=\s*"+month_regex()+r")", tail, flags=re.I) if clean_text(p)]
    intervals = []

    patt = re.compile(
        rf"({month_regex()})\s+(\d{{1,2}})\s*-\s*(?:({month_regex()})\s+)?(\d{{1,2}})\s*\(([^)]+)\)",
        flags=re.I,
    )

    for part in parts:
        m = patt.search(part)
        if not m:
            continue
        m1, d1, m2_opt, d2, dur_txt = m.group(1), m.group(2), m.group(3), m.group(4), clean_text(m.group(5))
        m2 = m2_opt if m2_opt else m1
        y1 = base_year
        y2 = y1 + (1 if MONTHS.get(m2.lower()) < MONTHS.get(m1.lower()) else 0)
        try:
            start_dt = pd.Timestamp(year=y1, month=MONTHS[m1.lower()], day=int(d1))
        except Exception:
            continue

        dur_low = safe_lower(dur_txt)
        duration_hours = None
        # Explicit hour token
        mh = re.search(r"(\d+(?:\.\d+)?)\s*hours?", dur_low)
        if mh:
            duration_hours = float(mh.group(1))
        else:
            md = re.search(r"(\d+(?:\.\d+)?)\s*days?", dur_low)
            if md:
                duration_hours = float(md.group(1)) * 24.0
            elif "1/2 day" in dur_low or "half day" in dur_low:
                duration_hours = 12.0

        if duration_hours is None:
            try:
                end_anchor = pd.Timestamp(year=y2, month=MONTHS[m2.lower()], day=int(d2))
            except Exception:
                continue
            duration_hours = max((end_anchor - start_dt).total_seconds() / 3600.0, 0.0)

        if duration_hours <= 0:
            continue

        end_dt = start_dt + pd.Timedelta(hours=duration_hours)
        intervals.append(
            {
                "interval_start": start_dt,
                "interval_end": end_dt,
                "interval_method": "full_closure_addon_schedule",
                "interval_quality": "schedule_estimate",
            }
        )

    return dedupe_intervals_exact(intervals)

def build_daily_repeated_intervals(start_date, end_date, start_minutes, end_minutes, method_name, quality):
    intervals = []
    current = start_date.normalize()
    end_day = end_date.normalize()
    while current <= end_day:
        hh1, mm1 = divmod(start_minutes, 60)
        hh2, mm2 = divmod(end_minutes, 60)
        interval_start = current + pd.Timedelta(hours=hh1, minutes=mm1)
        interval_end = current + pd.Timedelta(hours=hh2, minutes=mm2)
        if interval_end <= interval_start:
            interval_end += pd.Timedelta(days=1)

        intervals.append(
            {
                "interval_start": interval_start,
                "interval_end": interval_end,
                "interval_method": method_name,
                "interval_quality": quality,
            }
        )
        current += pd.Timedelta(days=1)
    return intervals


def _daily_repeated_intervals_exclusive_end(start_date, end_date, start_minutes, end_minutes, method_name, quality):
    """Return repeated daily windows from start_date through the night before end_date.

    This is for AHS shorthand such as "February 3 - February 9 (2100 - 0700)",
    where the range normally denotes the overnight windows beginning on Feb. 3
    and ending the morning of Feb. 9, not an additional Feb. 9 overnight.
    Fixed same-day or cross-date intervals are still handled by explicit parsers.
    """
    intervals = []
    if pd.isna(start_date) or pd.isna(end_date):
        return intervals
    current = pd.Timestamp(start_date).normalize()
    end_day = pd.Timestamp(end_date).normalize()
    if end_day <= current:
        return intervals
    while current < end_day:
        hh1, mm1 = divmod(start_minutes, 60)
        hh2, mm2 = divmod(end_minutes, 60)
        interval_start = current + pd.Timedelta(hours=hh1, minutes=mm1)
        interval_end = current + pd.Timedelta(hours=hh2, minutes=mm2)
        if interval_end <= interval_start:
            interval_end += pd.Timedelta(days=1)
        intervals.append({
            "interval_start": interval_start,
            "interval_end": interval_end,
            "interval_method": method_name,
            "interval_quality": quality,
        })
        current += pd.Timedelta(days=1)
    return intervals


def _date_from_month_day_token(month_name, day_text, anchor_year, previous_month_num=None):
    """Parse a month/day token using anchor_year and roll forward across year boundary."""
    if not month_name or day_text is None:
        return pd.NaT
    try:
        month_num = MONTHS[str(month_name).lower()]
        year = int(anchor_year)
        if previous_month_num is not None and month_num < int(previous_month_num):
            year += 1
        return pd.Timestamp(year=year, month=month_num, day=int(day_text))
    except Exception:
        return pd.NaT


def _parenthetical_date_range_specs(row):
    """Find Month Day - [Month] Day (time-time) shorthand ranges in a row.

    These ranges are schedule-like, not a single continuous closure. The parser is
    deliberately conservative: the ending calendar date is treated as the reopen
    morning for overnight windows, so Feb 3-Feb 9 (2100-0700) yields nights
    beginning Feb 3 through Feb 8.
    """
    text = row.get("bed_or_space_reduction_text") or row.get("raw_block_text") or ""
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    month_pat = month_regex()
    texpr = time_expr_regex()
    pat = re.compile(
        rf"({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*"
        rf"(?:-|–|—|to|through|thru|until)\s*"
        rf"(?:({month_pat})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*"
        rf"\(\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*\)",
        flags=re.I,
    )
    # Avoid treating explicit cross-date "Feb 9 (1300) - Feb 10 (0700)" as a
    # recurring range; this parser only handles a single parenthetical window
    # after the date range.
    specs = []
    anchor = parse_dt_any(row.get("start_date_parsed_clean"))
    if pd.isna(anchor):
        anchor = parse_dt_any(row.get("snapshot_date"))
    if pd.isna(anchor):
        anchor = pd.Timestamp.now().normalize()
    for m in pat.finditer(s):
        start_month, start_day, start_year_txt = m.group(1), m.group(2), m.group(3)
        end_month, end_day, end_year_txt = m.group(4), m.group(5), m.group(6)
        t1, t2 = m.group(7), m.group(8)
        start_year = int(start_year_txt) if start_year_txt else int(anchor.year)
        start_dt = _date_from_month_day_token(start_month, start_day, start_year)
        if pd.isna(start_dt):
            continue
        end_month_final = end_month or start_month
        end_year = int(end_year_txt) if end_year_txt else int(start_dt.year)
        previous_month_num = MONTHS.get(str(start_month).lower()) if not end_year_txt else None
        end_dt = _date_from_month_day_token(end_month_final, end_day, end_year, previous_month_num=previous_month_num)
        if pd.isna(end_dt) or end_dt <= start_dt:
            continue
        start_minutes = parse_time_token(t1)
        end_minutes = parse_time_token(t2)
        if start_minutes is None or end_minutes is None:
            continue
        # This parser is specifically for repeated daily/overnight windows. If the
        # two times produce a non-overnight daytime window, leave it to explicit or
        # daily-window parsers rather than guessing.
        if end_minutes > start_minutes:
            continue
        specs.append({
            "range_start_date": start_dt,
            "range_end_date": end_dt,
            "start_minutes": start_minutes,
            "end_minutes": end_minutes,
            "source_text": m.group(0),
        })
    return specs


def extract_parenthetical_date_range_window_intervals(row):
    """Expand AHS shorthand like "Feb 3 - Feb 9 (2100 - 0700)" into nightly windows."""
    intervals = []
    for spec in _parenthetical_date_range_specs(row):
        intervals.extend(_daily_repeated_intervals_exclusive_end(
            spec["range_start_date"],
            spec["range_end_date"],
            spec["start_minutes"],
            spec["end_minutes"],
            "overnight_date_range_schedule",
            "schedule_estimate",
        ))
    return dedupe_intervals_exact(intervals)


def _drop_parenthetical_range_endpoint_artifacts(row, explicit_intervals, schedule_intervals):
    """Drop endpoint artifacts created by same-day parenthetical explicit parsers.

    When a range such as "Feb 10 - Feb 14 (2100-0700)" is expanded by the new
    overnight-date-range parser, older same-day parenthetical logic can also emit
    a spurious Feb 14 21:00-Feb 15 07:00 interval by reading only the terminal
    date plus parenthetical times. That would turn an exclusive shorthand range
    into an inclusive range. Remove only those exact endpoint artifacts, preserving
    true fixed intervals such as "Feb 9 (1300) - Feb 10 (0700)".
    """
    if not explicit_intervals or not schedule_intervals:
        return explicit_intervals
    if not any(iv.get("interval_method") == "overnight_date_range_schedule" for iv in schedule_intervals):
        return explicit_intervals
    artifact_pairs = set()
    for spec in _parenthetical_date_range_specs(row):
        end_day = pd.Timestamp(spec["range_end_date"]).normalize()
        hh1, mm1 = divmod(spec["start_minutes"], 60)
        hh2, mm2 = divmod(spec["end_minutes"], 60)
        art_start = end_day + pd.Timedelta(hours=hh1, minutes=mm1)
        art_end = end_day + pd.Timedelta(hours=hh2, minutes=mm2)
        if art_end <= art_start:
            art_end += pd.Timedelta(days=1)
        artifact_pairs.add((pd.Timestamp(art_start), pd.Timestamp(art_end)))
    if not artifact_pairs:
        return explicit_intervals
    kept = []
    for iv in explicit_intervals:
        key = (
            pd.to_datetime(iv.get("interval_start"), errors="coerce"),
            pd.to_datetime(iv.get("interval_end"), errors="coerce"),
        )
        method = str(iv.get("interval_method", ""))
        if key in artifact_pairs and method in {"single_day_parenthetical", "explicit_parenthetical_variants", "explicit_multi_interval"}:
            continue
        kept.append(iv)
    return kept


def _subtract_covering_intervals(candidate_intervals, covering_intervals):
    """Return residual pieces of candidate intervals after removing covering intervals."""
    if not candidate_intervals or not covering_intervals:
        return candidate_intervals or []
    return subtract_intervals(candidate_intervals, covering_intervals)


def extract_closed_nightly_plus_weekly_range_intervals(row):
    """Handle compound rows: "closed nightly (T-T), Fridays (T)-Mondays (T)".

    The nightly window is expanded across the row date range, but any portions
    covered by an explicitly stated weekly continuous range are removed so the
    episode-level diagnostic hours do not double-count weekend closures.
    """
    text = row.get("bed_or_space_reduction_text") or ""
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    texpr = time_expr_regex()
    nightly_match = re.search(
        rf"\bclosed\s+(?:nightly|overnights?|overnight)\s*\(\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*\)",
        s,
        flags=re.I,
    )
    if not nightly_match:
        return []
    # Require an additional named weekly range. Simple "closed overnight (T-T)"
    # forms are handled elsewhere and should not be duplicated here.
    weekly_pat = re.compile(
        rf"({weekday_regex()})s?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|through|thru|until)\s*({weekday_regex()})s?\s*\(\s*({texpr})\s*\)",
        flags=re.I,
    )
    weekly_matches = list(weekly_pat.finditer(s))
    if not weekly_matches:
        return []

    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    end_date, method_name = resolve_schedule_end_date(row, "daily_window_schedule")
    if pd.isna(start_date) or pd.isna(end_date):
        return []
    nightly_start = parse_time_token(nightly_match.group(1))
    nightly_end = parse_time_token(nightly_match.group(2))
    if nightly_start is None or nightly_end is None:
        return []

    nightly = build_daily_repeated_intervals(start_date, end_date, nightly_start, nightly_end, method_name, "schedule_estimate")
    weekly = []
    for m in weekly_matches:
        start_wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
        end_wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
        start_minutes = parse_time_token(m.group(2))
        end_minutes = parse_time_token(m.group(4))
        if start_minutes is None or end_minutes is None:
            continue
        weekly.extend(generate_weekly_intervals(start_date, end_date, start_wd, end_wd, start_minutes, end_minutes, "weekly_named_range_schedule"))
    weekly = dedupe_intervals_exact(weekly)
    if not weekly:
        return dedupe_intervals_exact(nightly)
    residual_nightly = _subtract_covering_intervals(nightly, weekly)
    return dedupe_intervals_exact(weekly + residual_nightly)



def extract_text_anchored_bounded_daily_window_intervals(row):
    """Parse bounded daily/overnight schedules whose true dates are in the text.

    This is intentionally independent of structured Start/Anticipated End fields
    because AHS sometimes carries a broad/stale episode start while the burden
    wording states the actual schedule span. Example Grimshaw wording:
      "Emergency Department closed from 2100-0900 from September 17 through October 31, 2022."

    The start date must come from the text range. The structured row dates are
    used only as year/month context when the source omits a year or end month.
    """
    if row is None or not hasattr(row, "get"):
        return []
    # Prefer the burden field, but include raw_block_text because some cached rows
    # carry the same schedule phrase there while a structured field is shortened.
    text_parts = [row.get("bed_or_space_reduction_text") or "", row.get("raw_block_text") or ""]
    s = clean_text(" ".join(str(x) for x in text_parts if x)) or ""
    if not s:
        return []
    texpr = time_expr_regex()
    mpat = month_regex()
    patterns = [
        rf"\b(?:then\s+)?(?:ed\s+|emergency\s+department\s+)?(?:is\s+|will\s+be\s+)?closed\s+from\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+(?:daily\s+)?from\s+({mpat}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)\s*(?:-|–|—|to|through|thru|until)\s*((?:{mpat}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)",
        rf"\b(?:then\s+)?(?:ed\s+|emergency\s+department\s+)?(?:is\s+|will\s+be\s+)?closed\s+(?:daily\s+)?from\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s+between\s+({mpat}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)\s*(?:-|–|—|to|and|through|thru|until)\s*((?:{mpat}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)",
    ]
    intervals = []
    for pat in patterns:
        for m in re.finditer(pat, s, flags=re.I):
            start_minutes = parse_time_token(m.group(1))
            end_minutes = parse_time_token(m.group(2))
            if start_minutes is None or end_minutes is None:
                continue
            start_tok = m.group(3)
            end_tok = m.group(4)
            sd = _parse_unyeared_month_day_token(start_tok, row)
            prev_month = int(sd.month) if pd.notna(sd) else None
            ed = _parse_unyeared_month_day_token(
                end_tok,
                row,
                fallback_year=int(sd.year) if pd.notna(sd) else None,
                previous_month_num=prev_month,
            )
            if pd.isna(sd) or pd.isna(ed) or ed < sd:
                continue
            intervals.extend(build_daily_repeated_intervals(
                sd,
                ed,
                start_minutes,
                end_minutes,
                "daily_window_schedule",
                "schedule_estimate",
            ))
    return dedupe_intervals_exact(intervals)


def extract_daily_window_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = safe_lower(text)
    if not s:
        return []

    if "weekend" in s or "each weekend" in s:
        return []
    # Fixed daily ranges with an explicit return/reopen date are handled by the
    # explicit dated natural-language parser; do not project them to the row end.
    if re.search(rf"close(?:d)?\s+from\s+{time_expr_regex()}\s*(?:-|–|—|to|until|through)\s*{time_expr_regex()}\s+daily\s+beginning\s+{month_regex()}\s+\d{{1,2}}.*?(?:return|re-?open).*?{month_regex()}\s+\d{{1,2}}", s, flags=re.I):
        return []
    if re.search(rf"{weekday_regex()}.*?(through|until).*?{weekday_regex()}", s, flags=re.I):
        return []

    # A singular dated phrase such as "from 5 p.m. to 8 a.m. on Wednesday, May 20"
    # is an explicit fixed date/list, not a daily recurring schedule.
    if re.search(rf"\bon\s+{weekday_regex()}\s*,?\s*{month_regex()}\s+\d{{1,2}}", s, flags=re.I):
        recurrence_cues = ["daily", "each day", "every day", "until further notice", "ongoing", "recurring"]
        if not any(cue in s for cue in recurrence_cues):
            return []

    texpr = time_expr_regex()
    patterns = [
        rf"closed from\s*({texpr})\s*(?:-|–|to)\s*({texpr})(?:\s*daily|\s*each day|\s*$|\s*\.)",
        rf"closed daily from\s*({texpr})\s*(?:-|–|to)\s*({texpr})",
        rf"will be closed from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s*daily",
        rf"\bdaily\b[^.]*?\(\s*closed\s*(?:from\s*)?({texpr})\s*(?:-|–|to)\s*({texpr})\s*\)",
        rf"closed from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s*from\s*({month_regex()}\s+\d{{1,2}}(?:st|nd|rd|th)?)\s*(?:-|–|to|through|thru|until)\s*({month_regex()}\s+\d{{1,2}}(?:st|nd|rd|th)?)",
        rf"from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s*daily",
    ]

    m = None
    leading_date_range = None
    compact_month_day_range = None
    for i, pat in enumerate(patterns):
        m = re.search(pat, s, flags=re.I)
        if m:
            if i == 4:
                leading_date_range = (m.group(3), m.group(4))
            break

    if not m:
        m = re.search(
            rf"closed from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s*on\s*({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|to|through|until)\s*(\d{{1,2}})(?:st|nd|rd|th)?",
            s,
            flags=re.I,
        )
        if m:
            compact_month_day_range = (m.group(3), m.group(4), m.group(5))

    if not m:
        m = re.search(
            rf"(?:closed(?:\s+temporarily)?(?:\s+from)?|between)\s*({texpr})\s*(?:-|–|to|until)\s*({texpr})(?:\s*daily)?\s*from\s*({month_regex()}\s+\d{{1,2}}(?:,\s*{PARSER_YEAR_REGEX})?)\s*(?:-|–|to|through|until)\s*((?:{month_regex()}\s+)?\d{{1,2}}(?:,\s*{PARSER_YEAR_REGEX})?)",
            s,
            flags=re.I,
        )
        if m:
            leading_date_range = (m.group(3), m.group(4))

    if not m:
        return []

    start_token, end_token = m.group(1), m.group(2)
    start_minutes = parse_time_token(start_token)
    end_minutes = parse_time_token(end_token)
    if start_minutes is None or end_minutes is None:
        return []

    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    end_date, method_name = resolve_schedule_end_date(row, "daily_window_schedule")
    if leading_date_range:
        sd = parse_boundary_datetime_field(leading_date_range[0])
        ed = parse_boundary_datetime_field(leading_date_range[1])
        if pd.isna(ed):
            start_anchor = parse_boundary_datetime_field(leading_date_range[0])
            if pd.isna(start_anchor):
                start_anchor = start_date
            if pd.notna(start_anchor):
                m_end = re.match(rf"(?:({month_regex()})\s+)?(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?$", clean_text(leading_date_range[1]) or "", flags=re.I)
                if m_end:
                    end_month = m_end.group(1) or start_anchor.strftime("%B")
                    end_day = int(m_end.group(2))
                    end_year = int(m_end.group(3)) if m_end.group(3) else int(start_anchor.year)
                    try:
                        end_month_num = MONTHS[end_month.lower()]
                        if end_month_num < int(start_anchor.month):
                            end_year += 1
                        ed = pd.Timestamp(year=end_year, month=end_month_num, day=end_day)
                    except Exception:
                        ed = pd.NaT
        if not pd.isna(sd):
            start_date = sd
        if not pd.isna(ed):
            end_date = ed
    elif compact_month_day_range:
        month_name, start_day_txt, end_day_txt = compact_month_day_range
        anchor_year = None
        anchor_start = parse_dt_any(row.get("start_date_parsed_clean"))
        if not pd.isna(anchor_start):
            anchor_year = int(anchor_start.year)
        else:
            anchor_end = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))
            if not pd.isna(anchor_end):
                anchor_year = int(anchor_end.year)
        try:
            month_num = MONTHS[month_name.lower()]
            if anchor_year is not None:
                start_date = pd.Timestamp(year=anchor_year, month=month_num, day=int(start_day_txt))
                end_date = pd.Timestamp(year=anchor_year, month=month_num, day=int(end_day_txt))
        except Exception:
            return []
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    return build_daily_repeated_intervals(
        start_date,
        end_date,
        start_minutes,
        end_minutes,
        method_name,
        "schedule_estimate",
    )


def extract_weeknight_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = safe_lower(text)
    if not s:
        return []

    texpr = time_expr_regex()
    parsed = None

    # Existing "each night" phrasing
    m = re.search(
        rf"({weekday_regex()})\s+to\s+({weekday_regex()}).*?({texpr})\s*to\s*({texpr})",
        s,
        flags=re.I,
    )
    if m and "each night" in s:
        parsed = (m.group(1), m.group(2), m.group(3), m.group(4))

    if not parsed:
        # "closed from 2200 to 0700 Monday to Thursday each week"
        m = re.search(
            rf"closed\s+from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s+({weekday_regex()})\s*(?:to|through|until)\s*({weekday_regex()})(?:\s+each week)?",
            s,
            flags=re.I,
        )
        if m:
            parsed = (m.group(3), m.group(4), m.group(1), m.group(2))

    if not parsed:
        # "Monday to Thursday closed from 2200 to 0700"
        m = re.search(
            rf"({weekday_regex()})\s*(?:to|through|until)\s*({weekday_regex()}).*?closed\s+from\s*({texpr})\s*(?:-|–|to)\s*({texpr})",
            s,
            flags=re.I,
        )
        if m:
            parsed = (m.group(1), m.group(2), m.group(3), m.group(4))

    if not parsed:
        # "between the hours of 1900 to 0800 the next day, Mondays through Thursdays"
        m = re.search(
            rf"between\s+the\s+hours\s+of\s*({texpr})\s*(?:-|–|to|until)\s*({texpr})\s*(?:the\s+next\s+day|the\s+following\s+morning)?\s*,?\s*({weekday_regex()})\s*(?:to|through|until|-|–|—)\s*({weekday_regex()})",
            s,
            flags=re.I,
        )
        if m:
            parsed = (m.group(3), m.group(4), m.group(1), m.group(2))

    if not parsed:
        # "overnight from 8pm to 8am Monday to Thursday each week"
        m = re.search(
            rf"overnight\s+from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s+({weekday_regex()})\s*(?:to|through|until)\s*({weekday_regex()})(?:\s+each week)?",
            s,
            flags=re.I,
        )
        if m:
            parsed = (m.group(3), m.group(4), m.group(1), m.group(2))

    if not parsed:
        # "on Mondays to Thursdays overnight (from 2000-0800)"
        m = re.search(
            rf"on\s+({weekday_regex()})\s*(?:to|through|until)\s*({weekday_regex()})\s+overnight.*?\(?\s*from\s*({texpr})\s*(?:-|–|to)\s*({texpr})\s*\)?",
            s,
            flags=re.I,
        )
        if m:
            parsed = (m.group(1), m.group(2), m.group(3), m.group(4))

    if not parsed:
        return []

    wd1, wd2, t1, t2 = parsed
    start_wd = WEEKDAY_NAME_TO_NUM[wd1.lower().rstrip("s")]
    end_wd = WEEKDAY_NAME_TO_NUM[wd2.lower().rstrip("s")]
    start_minutes = parse_time_token(t1)
    end_minutes = parse_time_token(t2)
    if start_minutes is None or end_minutes is None:
        return []

    overnight_cues = any(tok in s for tok in ["overnight", "each night", "night shift", "next day", "following morning"])
    if end_minutes > start_minutes and not overnight_cues:
        return []

    allowed = set()
    wd = start_wd
    while True:
        allowed.add(wd)
        if wd == end_wd:
            break
        wd = (wd + 1) % 7

    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    end_date, method_name = resolve_schedule_end_date(row, "weekday_night_schedule")
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    intervals = []
    current = start_date.normalize()
    end_day = end_date.normalize()
    while current <= end_day:
        if current.weekday() in allowed:
            hh1, mm1 = divmod(start_minutes, 60)
            hh2, mm2 = divmod(end_minutes, 60)
            interval_start = current + pd.Timedelta(hours=hh1, minutes=mm1)
            interval_end = current + pd.Timedelta(hours=hh2, minutes=mm2)
            if interval_end <= interval_start:
                interval_end += pd.Timedelta(days=1)

            intervals.append({
                "interval_start": interval_start,
                "interval_end": interval_end,
                "interval_method": method_name,
                "interval_quality": "schedule_estimate",
            })
        current += pd.Timedelta(days=1)

    return intervals

def generate_weekly_intervals(start_date, end_date, start_wd, end_wd, start_minutes, end_minutes, method_name):
    """Return weekly named-range intervals, including partial first-cycle overlap.

    Earlier v84 logic began scanning at the row start date and waited for the
    next occurrence of the start weekday. That undercounted rows whose start date
    fell inside an already-active weekly range, e.g., a Monday 1900-Thursday 0700
    schedule with a Tuesday start date. We now back up to the most recent start
    weekday and rely on row-context clipping to keep only the in-window residual.
    """
    intervals = []
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    start_day = start_date.normalize()
    end_day = end_date.normalize()
    days_since_start_wd = (start_day.weekday() - start_wd) % 7
    current = start_day - pd.Timedelta(days=days_since_start_wd)
    while current <= end_day:
        hh1, mm1 = divmod(start_minutes, 60)
        hh2, mm2 = divmod(end_minutes, 60)
        interval_start = current + pd.Timedelta(hours=hh1, minutes=mm1)

        days_ahead = (end_wd - start_wd) % 7
        interval_end = current + pd.Timedelta(days=days_ahead, hours=hh2, minutes=mm2)
        if days_ahead == 0 and interval_end <= interval_start:
            interval_end += pd.Timedelta(days=7)
        elif days_ahead > 0 and interval_end <= interval_start:
            interval_end += pd.Timedelta(days=7)

        # Keep intervals that could overlap the row; context clipping in
        # build_episode_intervals() will trim the first/last interval precisely.
        if interval_end > start_date and interval_start <= (end_day + pd.Timedelta(days=2)):
            intervals.append(
                {
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                    "interval_method": method_name,
                    "interval_quality": "schedule_estimate",
                }
            )
        current += pd.Timedelta(days=7)
    return intervals


def _ordered_unique_weekday_numbers(fragment: str):
    names = re.findall(weekday_regex(), safe_lower(fragment), flags=re.I)
    ordered = []
    for name in names:
        wd = WEEKDAY_NAME_TO_NUM[name.lower().rstrip("s")]
        if wd not in ordered:
            ordered.append(wd)
    return ordered


def _is_contiguous_weekday_sequence(days):
    if not days or len(days) < 2:
        return False
    return all(((days[i - 1] + 1) % 7) == days[i] for i in range(1, len(days)))



def extract_duration_shorthand_intervals(row, base_year):
    text_parts = [
        row.get("bed_or_space_reduction_text"),
        row.get("raw_block_text"),
        row.get("reason_text"),
    ]
    s = normalize_interval_parse_text(" ".join(str(x or "") for x in text_parts)) or ""
    if not s or " x " not in s.lower():
        return []

    texpr = time_expr_regex()
    pat = re.compile(
        rf"(?:({month_regex()})\.?\s+)?(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?\s+({texpr})\s*x\s*(\d{{1,3}})\s*h(?:ou)?rs?\b",
        flags=re.I,
    )
    intervals = []
    current_month = None
    current_year = base_year
    prev_month_num = None
    for m in pat.finditer(s):
        mon = m.group(1) or current_month
        day = m.group(2)
        explicit_year = m.group(3)
        t1 = m.group(4)
        hours_txt = m.group(5)
        if not mon:
            continue
        mn = MONTHS.get(mon.lower())
        if mn is None:
            continue
        if explicit_year:
            current_year = int(explicit_year)
        elif prev_month_num is not None and mn < prev_month_num:
            current_year += 1
        prev_month_num = mn
        current_month = mon
        start_dt = make_timestamp(current_year, mon, day, t1)
        try:
            dur_hours = float(hours_txt)
        except Exception:
            continue
        if pd.isna(start_dt) or dur_hours <= 0:
            continue
        end_dt = start_dt + pd.Timedelta(hours=dur_hours)
        intervals.append({
            "interval_start": start_dt,
            "interval_end": end_dt,
            "interval_method": "explicit_multi_interval",
            "interval_quality": "exact_text_interval",
        })
    return dedupe_intervals_exact(intervals)


def extract_timed_date_list_intervals_v75(row, base_year):
    text_parts = [
        row.get("bed_or_space_reduction_text"),
        row.get("raw_block_text"),
        row.get("reason_text"),
    ]
    s = normalize_interval_parse_text(" ".join(str(x or "") for x in text_parts)) or ""
    if not s:
        return []

    texpr = time_expr_regex()
    intervals = []

    def _parse_date_list(tail, default_month=None, default_year=None):
        token_pat = re.compile(rf"(?:({month_regex()})\.?\s+)?(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?", flags=re.I)
        current_month = default_month
        current_year = default_year if default_year is not None else base_year
        prev_month_num = MONTHS.get(default_month.lower()) if default_month else None
        out = []
        for m in token_pat.finditer(clean_text(tail) or ""):
            mon = m.group(1) or current_month
            day = m.group(2)
            year_txt = m.group(3)
            if not mon:
                continue
            mn = MONTHS.get(mon.lower())
            if mn is None:
                continue
            if year_txt:
                current_year = int(year_txt)
            elif prev_month_num is not None and mn < prev_month_num:
                current_year += 1
            prev_month_num = mn
            current_month = mon
            out.append((current_year, mon, day))
        return out

    # closed at 1600 on the following dates ... re-opening at 0800 the following day: Oct 30, Nov 1, Nov 2
    m = re.search(
        rf"closed\s+at\s+({texpr}).*?re-?open(?:ing)?\s+at\s+({texpr}).*?following day\)?\s*:\s*(.+?)(?:\.\s|$)",
        s,
        flags=re.I,
    )
    if m:
        start_tok, end_tok, tail = m.group(1), m.group(2), m.group(3)
        for year, mon, day in _parse_date_list(tail):
            start_dt = make_timestamp(year, mon, day, start_tok)
            end_dt = make_timestamp(year, mon, day, end_tok)
            if pd.notna(start_dt) and pd.notna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })
        if intervals:
            return dedupe_intervals_exact(intervals)

    # closed from 1900-0700: November 30, December 1, 2, 3 (re-opening at 0700 December 4)
    m = re.search(
        rf"(?:closed(?:\s+temporarily)?(?:\s+from)?|closure(?:s)?(?:\s+from)?)\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*:\s*(.+?)(?:\(\s*re-?open|\.\s|$)",
        s,
        flags=re.I,
    )
    if m:
        start_tok, end_tok, tail = m.group(1), m.group(2), m.group(3)
        for year, mon, day in _parse_date_list(tail):
            start_dt = make_timestamp(year, mon, day, start_tok)
            end_dt = make_timestamp(year, mon, day, end_tok)
            if pd.notna(start_dt) and pd.notna(end_dt):
                if end_dt <= start_dt:
                    end_dt += pd.Timedelta(days=1)
                intervals.append({
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "interval_method": "explicit_multi_interval",
                    "interval_quality": "exact_text_interval",
                })
        if intervals:
            return dedupe_intervals_exact(intervals)

    return []



def _weekday_range_to_numbers(fragment):
    if not fragment:
        return []
    frag = str(fragment).lower()
    # normalize plural weekday names
    for name in list(WEEKDAY_NAME_TO_NUM.keys()):
        frag = re.sub(rf"\b{name}s\b", name, frag, flags=re.I)
    m = re.search(rf"\b({weekday_regex()})\b\s*(?:-|–|—|to|through|thru)\s*\b({weekday_regex()})\b", frag, flags=re.I)
    if m:
        a = WEEKDAY_NAME_TO_NUM.get(m.group(1).lower().rstrip('s'))
        b = WEEKDAY_NAME_TO_NUM.get(m.group(2).lower().rstrip('s'))
        if a is None or b is None:
            return []
        out = []
        cur = a
        while True:
            out.append(cur)
            if cur == b:
                break
            cur = (cur + 1) % 7
            if len(out) > 7:
                break
        return out
    return _ordered_unique_weekday_numbers(frag)


def extract_listed_weekday_multi_window_intervals(row):
    """Parse bounded snapshot schedule rows with multiple weekday windows.

    Handles real AHS forms such as:
      - Emergency Department closed from 0900-1700h Monday to Thursday,
        and 0900-1200h on Fridays
      - closed 0900-1700 Monday to Thursday and 0900-1200 Friday

    This is schedule-derived and bounded by the episode start/end context.
    """
    text_parts = [row.get("bed_or_space_reduction_text"), row.get("reason_text"), row.get("raw_block_text")]
    s = clean_text(" ".join(str(x or "") for x in text_parts)) or ""
    low = s.lower()
    if "closed" not in low or not re.search(weekday_regex(), low, flags=re.I):
        return []

    start_date = parse_boundary_datetime_field(row.get("start_date_parsed_clean"))
    if pd.isna(start_date):
        start_date = parse_boundary_datetime_field(row.get("start_date_text"))
    end_date, method_name = resolve_schedule_end_date(row, "listed_weekday_schedule")
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    day_re = r"(?:mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)"
    day_span_re = rf"{day_re}(?:\s*(?:-|–|—|to|through|thru)\s*{day_re}|(?:\s*,\s*|\s+and\s+){day_re})*"
    texpr = time_expr_regex()
    pat = re.compile(
        rf"(?:from\s+)?(?P<t1>{texpr})\s*(?:-|–|—|to|until)\s*(?P<t2>{texpr})\s*(?:on\s+)?(?P<days>{day_span_re})",
        flags=re.I,
    )

    intervals = []
    for m in pat.finditer(low):
        t1, t2 = m.group("t1"), m.group("t2")
        days = (m.group("days") or "").strip()
        if re.search(month_regex(), days, flags=re.I):
            continue
        start_minutes = parse_time_token(t1)
        end_minutes = parse_time_token(t2)
        if start_minutes is None or end_minutes is None:
            continue
        day_nums = _weekday_range_to_numbers(days)
        if not day_nums:
            continue
        for wd in day_nums:
            end_wd = wd if end_minutes > start_minutes else (wd + 1) % 7
            intervals.extend(generate_weekly_intervals(start_date, end_date, wd, end_wd, start_minutes, end_minutes, method_name))
    return dedupe_intervals_exact(intervals)


def extract_between_cross_date_intervals(row, base_year):
    """Parse explicit cross-date 'between time Month Day and time Month Day' notices."""
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    s = re.sub(r"(?i)\b(\d{1,2})(a\.?m\.?|p\.?m\.?)\b", r"\1 \2", s)
    texpr = _time_expr_extended_regex()
    mpat = month_regex()
    wpat = weekday_regex()
    intervals = []
    pat = re.compile(
        rf"\bbetween\s+({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:and|to|until|through|-|–|—)\s*({texpr})\s*,?\s*(?:on\s+)?(?:{wpat}\s*,?\s*)?({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?",
        flags=re.I,
    )
    for m in pat.finditer(s):
        t1, mon1, d1, y1_txt, t2, mon2, d2, y2_txt = m.groups()
        y1 = int(y1_txt) if y1_txt else _event_year_for_unyear_month(mon1, base_year, row)
        y2 = int(y2_txt) if y2_txt else y1 + (1 if MONTHS.get(str(mon2).lower()) < MONTHS.get(str(mon1).lower()) else 0)
        start_dt = _make_timestamp_extended(y1, mon1, d1, t1)
        end_dt = _make_timestamp_extended(y2, mon2, d2, t2)
        _append_interval_if_valid(intervals, start_dt, end_dt, method="explicit_dated_natural_language")
    return dedupe_intervals_exact(intervals)


def extract_listed_weekday_same_window_intervals(row):
    text_parts = [
        row.get("bed_or_space_reduction_text"),
        row.get("reason_text"),
        row.get("raw_block_text"),
    ]
    s = clean_text(" ".join(str(x or "") for x in text_parts)) or ""
    low = s.lower()
    if not low or not re.search(weekday_regex(), low, flags=re.I):
        return []
    if "following day" not in low and "next day" not in low and "following morning" not in low:
        return []

    texpr = time_expr_regex()
    m = re.search(
        rf"((?:{weekday_regex()}s?(?:,\s*|\s+and\s+|\s*,\s*and\s*)){{2,}}).*?(?:from\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr}).*?(?:following day|next day|following morning)",
        low,
        flags=re.I,
    )
    if not m:
        return []

    listed_days = _ordered_unique_weekday_numbers(m.group(1))
    if not listed_days:
        return []

    start_minutes = parse_time_token(m.group(2))
    end_minutes = parse_time_token(m.group(3))
    if start_minutes is None or end_minutes is None:
        return []

    start_date = parse_boundary_datetime_field(row.get("start_date_parsed_clean"))
    if pd.isna(start_date):
        start_date = parse_boundary_datetime_field(row.get("start_date_text"))
    end_date, method_name = resolve_schedule_end_date(row, "listed_weekday_schedule")
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    intervals = []
    for wd in listed_days:
        intervals.extend(
            generate_weekly_intervals(
                start_date,
                end_date,
                wd,
                (wd + 1) % 7,
                start_minutes,
                end_minutes,
                method_name,
            )
        )
    return dedupe_intervals_exact(intervals)


def extract_listed_weekday_closure_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = clean_text(text) or ""
    low = s.lower()
    if "closed" not in low:
        return []

    anchor_match = re.search(
        rf"\bstarting\s+({month_regex()})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*\(\s*({time_expr_regex()})\s*\)|\s+at\s+({time_expr_regex()}))?",
        low,
        flags=re.I,
    )
    if not anchor_match:
        return []

    prefix = low[:anchor_match.start()]
    if "," not in prefix and " and " not in prefix:
        return []

    listed_days = _ordered_unique_weekday_numbers(prefix)
    if not _is_contiguous_weekday_sequence(listed_days):
        return []

    first_wd = listed_days[0]
    last_wd = listed_days[-1]
    next_wd = (last_wd + 1) % 7

    anchor_year = None
    start_clean = parse_dt_any(row.get("start_date_parsed_clean"))
    if not pd.isna(start_clean):
        anchor_year = int(start_clean.year)
    elif not pd.isna(parse_dt_any(row.get("anticipated_end_date_parsed_clean"))):
        anchor_year = int(parse_dt_any(row.get("anticipated_end_date_parsed_clean")).year)
    if anchor_year is None:
        return []

    try:
        anchor_date = pd.Timestamp(year=anchor_year, month=MONTHS[anchor_match.group(1).lower()], day=int(anchor_match.group(2)))
    except Exception:
        return []

    anchor_time_token = anchor_match.group(3) or anchor_match.group(4)
    anchor_minutes = parse_time_token(anchor_time_token) if anchor_time_token else 0
    if anchor_minutes is None:
        return []

    if anchor_date.weekday() != first_wd:
        return []

    end_date, method_name = resolve_schedule_end_date(row, "listed_weekday_schedule")
    if pd.isna(end_date):
        return []

    return generate_weekly_intervals(
        anchor_date,
        end_date,
        first_wd,
        next_wd,
        anchor_minutes,
        anchor_minutes,
        method_name,
    )



def extract_listed_weekday_full_day_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = clean_text(text) or ""
    low = s.lower()

    if "closed" not in low and "closure of emergency department on" not in low:
        return []

    days_fragment = None
    m = re.search(
        rf"(?:closure of emergency department on|closed on)\s+(.+?)\s+from\s+({month_regex()})\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?\s+(?:to|until|through|-|–|—)\s+({month_regex()})\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?",
        low,
        flags=re.I,
    )
    if not m:
        return []
    days_fragment = m.group(1)
    start_month, start_day, start_year_txt = m.group(2), m.group(3), m.group(4)
    end_month, end_day, end_year_txt = m.group(5), m.group(6), m.group(7)

    listed_days = _ordered_unique_weekday_numbers(days_fragment)
    if not listed_days:
        return []

    start_year = int(start_year_txt) if start_year_txt else base_year_for_row(row)
    end_year = int(end_year_txt) if end_year_txt else (start_year + (1 if MONTHS.get(end_month.lower()) < MONTHS.get(start_month.lower()) else 0))
    try:
        start_date = pd.Timestamp(year=start_year, month=MONTHS[start_month.lower()], day=int(start_day))
        end_date = pd.Timestamp(year=end_year, month=MONTHS[end_month.lower()], day=int(end_day))
    except Exception:
        return []

    intervals = []
    current = start_date.normalize()
    end_day_ts = end_date.normalize()
    while current <= end_day_ts:
        if current.weekday() in listed_days:
            intervals.append({
                "interval_start": current,
                "interval_end": current + pd.Timedelta(days=1),
                "interval_method": "listed_weekday_full_day_schedule",
                "interval_quality": "schedule_estimate",
            })
        current += pd.Timedelta(days=1)

    return dedupe_intervals_exact(intervals)


def extract_weekend_schedule_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = safe_lower(text)
    if "weekend" not in s:
        return []

    texpr = time_expr_regex()
    # Example: closed each weekend starting on Fridays at 5pm until Mondays at 8am
    pattern = re.compile(
        rf"(?:each weekend.*?starting(?: on)?\s+)?({weekday_regex()})\s+at\s+({texpr}).*?"
        rf"(?:until|through)\s+({weekday_regex()})\s+at\s+({texpr})",
        flags=re.I,
    )
    m = pattern.search(s)
    if not m:
        pattern2 = re.compile(
            rf"from\s*({texpr})\s+({weekday_regex()}).*?(?:until|through)\s*({texpr})\s+({weekday_regex()})",
            flags=re.I,
        )
        m = pattern2.search(s)
        if not m:
            # Two Hills-style wording: "Closed Fridays at 8pm through the weekend,
            # re-opening Mondays at 8am." This is a recurring weekend-continuous
            # closure but lacks the simple "through Monday at 8am" grammar.
            pattern3 = re.compile(
                rf"(?:closed\s+|and\s+)?(?:on\s+)?({weekday_regex()})\s+(?:at|from)\s+({texpr}).{{0,120}}?(?:through|thru|over)\s+the\s+weekend.{{0,120}}?(?:re-?open(?:ing)?s?|open(?:ing)?s?)\s+({weekday_regex()})\s+(?:at\s+)?({texpr})",
                flags=re.I,
            )
            m = pattern3.search(s)
            if not m:
                return []
            start_wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
            start_minutes = parse_time_token(m.group(2))
            end_wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
            end_minutes = parse_time_token(m.group(4))
        else:
            start_minutes = parse_time_token(m.group(1))
            start_wd = WEEKDAY_NAME_TO_NUM[m.group(2).lower().rstrip("s")]
            end_minutes = parse_time_token(m.group(3))
            end_wd = WEEKDAY_NAME_TO_NUM[m.group(4).lower().rstrip("s")]
    else:
        start_wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
        end_wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
        start_minutes = parse_time_token(m.group(2))
        end_minutes = parse_time_token(m.group(4))
    if start_minutes is None or end_minutes is None:
        return []

    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    end_date, method_name = resolve_schedule_end_date(row, "weekend_schedule")
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    return generate_weekly_intervals(
        start_date,
        end_date,
        start_wd,
        end_wd,
        start_minutes,
        end_minutes,
        method_name,
    )


def extract_weekday_named_range_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    s = clean_text(text) or ""
    low = s.lower()
    if not re.search(rf"{weekday_regex()}.*?(?:through|until|to|-|–|—|at).*?{weekday_regex()}", low, flags=re.I):
        return []

    # Exclude the "each night" variant because it has its own parser
    if "each night" in low:
        return []

    # Exclude weekend notices because they are handled by the dedicated weekend parser.
    # Without this guard, the same Friday-to-Monday wording can be parsed twice
    # (once as weekend_schedule and مرة as weekly_named_range_schedule), inflating
    # restricted/broad hours for the same notice.
    if "weekend" in low:
        return []

    # Fixed calendar-date weekday language (e.g., "5 p.m. on Sunday, May 17 to
    # 8 a.m. on Tuesday, May 19") is explicit, not a recurring weekly schedule.
    if re.search(rf"{weekday_regex()}\s*,?\s*{month_regex()}\s+\d{{1,2}}", s, flags=re.I):
        recurrence_cues = ["every", "each", "weekly", "weekdays", "weekends", "until further notice", "ongoing", "recurring"]
        if not any(cue in low for cue in recurrence_cues):
            return []

    start_date = row.get("start_date_parsed_clean")
    end_date = row.get("anticipated_end_date_parsed_clean")
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    texpr = time_expr_regex()

    def parse_single_clause(clause_text):
        clause = clean_text(clause_text) or ""

        # Example: closed Mondays (0700) through Thursdays (0700)
        pattern1 = re.compile(
            rf"({weekday_regex()})\s*\(\s*({texpr})\s*\)\s*(?:through|until|to|-|–|—)\s*({weekday_regex()})\s*\(\s*({texpr})\s*\)",
            flags=re.I,
        )
        m = pattern1.search(clause)
        if m:
            start_wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
            end_wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
            start_minutes = parse_time_token(m.group(2))
            end_minutes = parse_time_token(m.group(4))
            if start_minutes is not None and end_minutes is not None:
                return generate_weekly_intervals(
                    start_date,
                    end_date,
                    start_wd,
                    end_wd,
                    start_minutes,
                    end_minutes,
                    "weekly_named_range_schedule",
                )

        # Example: closed Friday at 1700 until Monday at 0800
        pattern2 = re.compile(
            rf"({weekday_regex()})\s+at\s+({texpr}).*?(?:through|until|to)\s+({weekday_regex()})\s+at\s+({texpr})",
            flags=re.I,
        )
        m = pattern2.search(clause)
        if m:
            start_wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
            end_wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
            start_minutes = parse_time_token(m.group(2))
            end_minutes = parse_time_token(m.group(4))
            if start_minutes is not None and end_minutes is not None:
                return generate_weekly_intervals(
                    start_date,
                    end_date,
                    start_wd,
                    end_wd,
                    start_minutes,
                    end_minutes,
                    "weekly_named_range_schedule",
                )

        # Example: from 8 a.m. on Mondays to 8 a.m. on Tuesdays
        pattern3 = re.compile(
            rf"(?:from\s+)?({texpr})\s+on\s+({weekday_regex()})s?\b.*?(?:through|until|to|-|–|—)\s+({texpr})\s+on\s+({weekday_regex()})s?\b",
            flags=re.I,
        )
        m = pattern3.search(clause)
        if m:
            start_minutes = parse_time_token(m.group(1))
            start_wd = WEEKDAY_NAME_TO_NUM[m.group(2).lower().rstrip("s")]
            end_minutes = parse_time_token(m.group(3))
            end_wd = WEEKDAY_NAME_TO_NUM[m.group(4).lower().rstrip("s")]
            if start_minutes is not None and end_minutes is not None:
                return generate_weekly_intervals(
                    start_date,
                    end_date,
                    start_wd,
                    end_wd,
                    start_minutes,
                    end_minutes,
                    "weekly_named_range_schedule",
                )

        # Example: from 2000 Friday through 0800 Monday
        pattern4 = re.compile(
            rf"(?:from\s+)?({texpr})\s+({weekday_regex()})s?\b.*?(?:through|until|to|-|–|—)\s+({texpr})\s+({weekday_regex()})s?\b",
            flags=re.I,
        )
        m = pattern4.search(clause)
        if m:
            start_minutes = parse_time_token(m.group(1))
            start_wd = WEEKDAY_NAME_TO_NUM[m.group(2).lower().rstrip("s")]
            end_minutes = parse_time_token(m.group(3))
            end_wd = WEEKDAY_NAME_TO_NUM[m.group(4).lower().rstrip("s")]
            if start_minutes is not None and end_minutes is not None:
                return generate_weekly_intervals(
                    start_date,
                    end_date,
                    start_wd,
                    end_wd,
                    start_minutes,
                    end_minutes,
                    "weekly_named_range_schedule",
                )

        return []

    normalized = re.sub(r",\s*and\s+from\s+", "; and from ", s, flags=re.I)
    clauses = [c.strip(" ;") for c in re.split(r"\s*;\s*and\s*", normalized, flags=re.I) if c.strip(" ;")]
    intervals = []
    for clause in clauses:
        intervals.extend(parse_single_clause(clause))
    return intervals


def parse_open_hours_schedule(text):
    s = clean_text(text) or ""
    s = s.replace("–", "-").replace("—", "-")
    low = s.lower()
    trigger_tokens = [
        "regular operating hours are",
        "operating hours are",
        "will be open",
        "during regular operating hours",
        "closed:",
    ]
    if not any(tok in low for tok in trigger_tokens):
        return {}

    schedules = {}

    def _add_block(wd1, wd2, t1, t2):
        if t1 is None or t2 is None:
            return
        wd = wd1
        while True:
            schedules.setdefault(wd, [])
            block = (t1, t2)
            if block not in schedules[wd]:
                schedules[wd].append(block)
            if wd == wd2:
                break
            wd = (wd + 1) % 7

    def _add_continuous_open_span(start_wd, start_min, end_wd, end_min):
        """Split a cross-day open span into per-day open blocks for complement logic."""
        if start_min is None or end_min is None:
            return
        if start_wd == end_wd:
            schedules.setdefault(start_wd, [])
            block = (start_min, end_min) if end_min > start_min else (0, 24 * 60)
            if block not in schedules[start_wd]:
                schedules[start_wd].append(block)
            return
        wd = start_wd
        first = True
        while True:
            schedules.setdefault(wd, [])
            if first:
                block = (start_min, 24 * 60)
            elif wd == end_wd:
                block = (0, end_min)
            else:
                block = (0, 24 * 60)
            if block[1] > block[0] and block not in schedules[wd]:
                schedules[wd].append(block)
            if wd == end_wd:
                break
            first = False
            wd = (wd + 1) % 7

    # Open-hours complement rows may state weekend open coverage as a continuous
    # span, e.g. "24-hours throughout the weekends: Friday at 8:00 AM to Mondays
    # at 8:00 AM". Split that open span before computing closed-hour complements.
    continuous_open_pat = re.compile(
        rf"({weekday_regex()})s?\s+at\s+({time_expr_regex()})\s*(?:-|to|through|until)\s*({weekday_regex()})s?\s+at\s+({time_expr_regex()})",
        flags=re.I,
    )
    for m in continuous_open_pat.finditer(s):
        context = s[max(0, m.start()-80):m.end()+80].lower()
        if "weekend" in context and ("open" in context or "24-hour" in context or "24 hours" in context or "throughout the weekend" in context or "throughout the weekends" in context):
            _add_continuous_open_span(
                WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")],
                parse_time_token(m.group(2)),
                WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")],
                parse_time_token(m.group(4)),
            )

    range_pat = re.compile(
        rf"({weekday_regex()})\s*(?:-|to|through|until)\s*({weekday_regex()})\s+\(?\s*({time_expr_regex()})\s*(?:-|to|until)\s*({time_expr_regex()})\s*\)?",
        flags=re.I,
    )
    for m in range_pat.finditer(s):
        _add_block(
            WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")],
            WEEKDAY_NAME_TO_NUM[m.group(2).lower().rstrip("s")],
            parse_time_token(m.group(3)),
            parse_time_token(m.group(4)),
        )

    time_then_range_pat = re.compile(
        rf"({time_expr_regex()})\s*(?:-|to|through|until)\s*({time_expr_regex()})\s+(?:on\s+)?({weekday_regex()})\s*(?:-|to|through|until)\s*({weekday_regex()})",
        flags=re.I,
    )
    for m in time_then_range_pat.finditer(s):
        _add_block(
            WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")],
            WEEKDAY_NAME_TO_NUM[m.group(4).lower().rstrip("s")],
            parse_time_token(m.group(1)),
            parse_time_token(m.group(2)),
        )

    single_pat = re.compile(
        rf"({weekday_regex()})\s+\(?\s*({time_expr_regex()})\s*(?:-|to|until)\s*({time_expr_regex()})\s*\)?",
        flags=re.I,
    )
    for m in single_pat.finditer(s):
        wd = WEEKDAY_NAME_TO_NUM[m.group(1).lower().rstrip("s")]
        _add_block(wd, wd, parse_time_token(m.group(2)), parse_time_token(m.group(3)))

    time_then_single_pat = re.compile(
        rf"({time_expr_regex()})\s*(?:-|to|through|until)\s*({time_expr_regex()})\s+(?:on\s+)?({weekday_regex()})\b",
        flags=re.I,
    )
    for m in time_then_single_pat.finditer(s):
        wd = WEEKDAY_NAME_TO_NUM[m.group(3).lower().rstrip("s")]
        _add_block(wd, wd, parse_time_token(m.group(1)), parse_time_token(m.group(2)))

    return schedules


def text_has_explicit_24h_closure_cue(text):
    s = safe_lower(text)
    if not s:
        return False
    patterns = [
        r"\b24\s*hours?(?:\s+a\s+day)?\b",
        r"\b24/7\b",
        r"\btwenty[- ]four\s+hours?\b",
        r"\bseven\s+days?\s+a\s+week\b",
        r"\b7\s+days?\s+a\s+week\b",
        r"\bclosed\s+24\s*hours?\b",
    ]
    return any(re.search(p, s, flags=re.I) for p in patterns)


def should_treat_regular_hours_as_closure_hours(text):
    s = safe_lower(text)
    if not s:
        return False
    if text_has_explicit_24h_closure_cue(s):
        return False

    has_regular_hours = (
        ("regular operating hours are" in s)
        or ("operating hours are" in s)
        or ("during regular operating hours" in s)
    )

    # "Closed overnights and weekends; operating hours are Monday-Friday 0800-1600"
    # lists the OPEN hours. The burden is the complement: nights plus omitted
    # weekend days. Do not treat the listed operating hours as closure hours.
    if has_regular_hours and re.search(r"\bclosed\s+overnights?\s+and\s+weekends?\b", s, flags=re.I):
        return False

    texpr = time_expr_regex()
    explicit_closed_hours = bool(re.search(
        rf"closed\s+from\s*({texpr})\s*(?:-|to|through|until)\s*({texpr})\s+(?:on\s+)?{weekday_regex()}",
        s,
        flags=re.I,
    )) or bool(re.search(
        rf"closed\s*:\s*{weekday_regex()}\s*(?:-|to|through|until)\s*{weekday_regex()}\s*\(\s*({texpr})\s*(?:-|to|through|until)\s*({texpr})\s*\)",
        s,
        flags=re.I,
    ))

    if not has_regular_hours and not explicit_closed_hours:
        return False

    closure_cues = [
        "emergency department closed",
        "ed closed",
        "closed temporarily",
        "temporarily closed",
        "temporary closure",
        "will be temporarily closed",
        "will be closed",
        "closure remains in place",
        "patients presenting to the ed during regular operating hours",
        "patients presenting during regular operating hours",
        "patients presenting to the emergency department during regular operating hours",
        "will be referred to emergency departments in surrounding communities",
        "will be referred to eds in surrounding communities",
        "ems will divert patients",
    ]
    return explicit_closed_hours or any(cue in s for cue in closure_cues)


def complement_closure_intervals_for_day(day_ts, open_blocks):
    if not open_blocks:
        return []

    open_blocks = sorted(open_blocks)
    closures = []
    current = 0
    for start_min, end_min in open_blocks:
        if start_min > current:
            closures.append(
                (
                    day_ts + pd.Timedelta(minutes=current),
                    day_ts + pd.Timedelta(minutes=start_min),
                )
            )
        current = max(current, end_min)

    if current < 24 * 60:
        closures.append(
            (
                day_ts + pd.Timedelta(minutes=current),
                day_ts + pd.Timedelta(days=1),
            )
        )

    return [(a, b) for a, b in closures if b > a]


def extract_regular_hours_closure_intervals(row):
    text = row.get("bed_or_space_reduction_text")
    schedules = parse_open_hours_schedule(text)
    if not schedules:
        return []

    # Some ED closure notices list the site's regular operating hours only to describe
    # when patients would normally have been seen locally. For those rows, the stated
    # weekday/time blocks represent the closure burden itself, not the hours the ED
    # remains open. Explicit 24-hour closure wording is left to other parsers/fallbacks.
    if text_has_explicit_24h_closure_cue(text):
        return []

    closure_hours_mode = should_treat_regular_hours_as_closure_hours(text)

    start_date = parse_dt_any(row.get("start_date_parsed_clean"))
    method_base = "regular_hours_only_closure" if closure_hours_mode else "regular_hours_complement"
    end_date, method_name = resolve_schedule_end_date(row, method_base)
    if pd.isna(start_date) or pd.isna(end_date):
        return []

    text_low = safe_lower(text)
    infer_closed_omitted_days = (not closure_hours_mode) and (("overnights and weekends" in text_low) or ("overnight and weekends" in text_low))

    intervals = []
    current = start_date.normalize()
    end_day = end_date.normalize()
    explicit_weekdays = set(schedules.keys())
    while current <= end_day:
        wd = current.weekday()
        if wd in explicit_weekdays:
            open_blocks = schedules.get(wd, [])
            if closure_hours_mode:
                for start_min, end_min in sorted(open_blocks):
                    a = current + pd.Timedelta(minutes=start_min)
                    b = current + pd.Timedelta(minutes=end_min)
                    if b > a:
                        intervals.append({
                            "interval_start": a,
                            "interval_end": b,
                            "interval_method": method_name,
                            "interval_quality": "schedule_estimate",
                        })
            else:
                closures = complement_closure_intervals_for_day(current, open_blocks)
                for a, b in closures:
                    intervals.append({
                        "interval_start": a,
                        "interval_end": b,
                        "interval_method": method_name,
                        "interval_quality": "schedule_estimate",
                    })
        elif infer_closed_omitted_days:
            intervals.append({
                "interval_start": current,
                "interval_end": current + pd.Timedelta(days=1),
                "interval_method": method_name,
                "interval_quality": "schedule_estimate",
            })
        current += pd.Timedelta(days=1)
    return intervals



def is_full_ed_capacity_closure_row(program_or_service=None, text=None, raw_block_text=None):
    """
    Detect rows like Hardisty where the ED is effectively fully closed but the archive expresses
    it as a full care-space reduction rather than with plain "ED closed" wording.

    Example:
      - program_or_service: Emergency Department
      - bed/reduction text: 6 out of 6 emergency care spaces
      - reason/raw block: Percentage of Beds or Spaces in Operation: 0%
    """
    prog = safe_lower(program_or_service)
    s = safe_lower(text)
    raw = safe_lower(raw_block_text)
    combined = f"{prog} {s} {raw}"

    true_ed_service = bool(re.fullmatch(r"emergency(?: department| dept)?", prog.strip()))
    if not true_ed_service:
        return False

    zero_pct = bool(re.search(r"percentage of beds or spaces in operation:\s*0%", combined, flags=re.I))
    if not zero_pct and " 0% " not in f" {combined} ":
        zero_pct = False

    full_ratio = False
    for m in re.finditer(r"(\d+)\s*out of\s*(\d+)\s*(?:emergency\s+)?care spaces?", combined, flags=re.I):
        try:
            num = int(m.group(1))
            den = int(m.group(2))
        except Exception:
            continue
        if den > 0 and num == den:
            full_ratio = True
            break

    return full_ratio and (zero_pct or "closed" in combined or true_ed_service)

def fallback_allowed_for_text(text, program_or_service=None, raw_block_text=None):
    """
    Conservative broad fallback:
    allow a continuous closure when the text clearly describes a closure with no better
    parsable interval structure, or when a known scrape-truncation/disaster pattern leaves
    an ED notice with dates but no usable timing structure.

    v27 tightening:
    - do not treat ED capacity/space-reduction rows as ED closure episodes merely because they
      contain dates (for example "Number of Closures: 6 out of 56 Care Spaces")
    - only allow the blank/stub broad fallback for true ED service labels, not arbitrary program
      strings that happen to mention emergency wording
    """
    s = safe_lower(text)
    raw = safe_lower(raw_block_text)
    prog = safe_lower(program_or_service)
    combined = f"{prog} {s} {raw}"

    full_capacity_closure = is_full_ed_capacity_closure_row(
        program_or_service=program_or_service,
        text=text,
        raw_block_text=raw_block_text,
    )

    short_stub = s in {"the ed will", "the emergency department will", ""}
    evacuation_like = any(tok in combined for tok in ["evacuation", "wildfire", "natural disaster"])
    true_ed_service = bool(re.fullmatch(r"emergency(?: department| dept)?", prog.strip()))
    hybrid_acute_ed = ("acute care" in prog and "emergency" in prog and any(tok in combined for tok in ["closed", "overnight", "temporarily closed", "will remain closed", "will"]))
    hybrid_truncated = bool(hybrid_acute_ed and re.search(r"emergency department will\s*$", s, flags=re.I))

    capacity_reduction_like = any(tok in combined for tok in [
        "number of closures",
        "care spaces",
        "care space",
        "out of",
        "bed spaces",
        "bed space",
        "reduced from",
    ])
    if capacity_reduction_like and not full_capacity_closure and not hybrid_truncated:
        return False

    if full_capacity_closure:
        return True

    # For clearly truncated ED rows (for example "The ED will") or disaster/TBD rows,
    # let the broader fallback use the stated date span / inferred archive span.
    if (true_ed_service or hybrid_acute_ed) and (short_stub or evacuation_like or hybrid_truncated):
        return True

    if not s:
        return False

    if not re.search(r"\b(closed|closure|temporarily closed|will remain closed|service suspended|ed closed)\b", s):
        return False

    if re.search(rf"{month_regex()}\s+\d{{1,2}}\s*\(", s, flags=re.I):
        return False
    if "beginning" in s and "resume" in s:
        return False
    if "overnight" in s or "following morning" in s:
        return False

    if "regular operating hours are" in s or "operating hours are" in s or "will be open" in s:
        return False
    if "closed from" in s:
        return False
    if "closed daily from" in s:
        return False
    if "each night" in s:
        return False
    if "weekend" in s:
        return False
    if "daily" in s:
        return False
    if re.search(weekday_regex(), s, flags=re.I):
        return False

    if re.search(time_expr_regex(), s, flags=re.I):
        return False

    return True





def is_continuous_ed_closure_phrase_notice(row, max_days=21):
    program = clean_text(row.get("program_or_service"))
    if not program:
        return False
    prog = safe_lower(program)
    true_ed_service = bool(re.fullmatch(r"emergency(?: department| dept)?", prog.strip()))
    hybrid_acute_ed = ("acute care" in prog and "emergency" in prog)

    reduction = safe_lower(row.get("bed_or_space_reduction_text"))
    reason = safe_lower(row.get("reason_text"))
    raw = safe_lower(row.get("raw_block_text"))
    combined = f"{reduction} {reason} {raw}"
    reduction_plus_raw = reduction

    if not (true_ed_service or hybrid_acute_ed):
        return False

    explicit_continuous_cues = [
        "no emergency department services",
        "pause emergency department services",
        "temporary service disruption in emergency department",
        "emergency department disruption",
        "will be without physician coverage",
        "temporarily without on-site physician coverage",
        "temporarily without physician coverage",
        "without on-site physician coverage",
        "no on-site physician coverage",
        "no on site physician coverage",
        "no on-site physician in emergency department",
        "no physician in emergency department",
        "no physician in the emergency department",
        "no physician coverage",
        "no on-site physician",
        "no on site physician",
    ]
    if not any(tok in combined for tok in explicit_continuous_cues):
        return False

    schedule_like = any(tok in reduction_plus_raw for tok in [
        "closed from",
        "closed daily",
        "reduction in hours of operation",
        "daily from",
        "each night",
        "night shift",
        "following day",
        "following morning",
        "weekend",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "multiple shifts",
        "operating hours",
        "virtual emergency physician",
        "vep coverage",
    ])
    if schedule_like:
        return False

    if re.search(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b|\b\d{3,4}h?\b|\b\d{1,2}\s*(?:am|pm)\b", reduction_plus_raw, flags=re.I):
        return False

    start_dt = parse_boundary_datetime_field(row.get("start_date_parsed_clean"))
    if pd.isna(start_dt):
        start_dt = parse_boundary_datetime_field(row.get("start_date_text"))
    end_dt = parse_boundary_datetime_field(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(end_dt):
        end_dt = parse_boundary_datetime_field(row.get("anticipated_end_date_text"))
    if pd.isna(start_dt) or pd.isna(end_dt) or end_dt <= start_dt:
        return False

    span_days = (end_dt - start_dt).total_seconds() / 86400.0
    if span_days > max_days:
        return False

    return True


def is_short_physician_coverage_ed_notice(row, max_days=14):
    """
    Narrow v45 recovery for terse ED physician-coverage notices.

    Keep only short, explicit, ED-only outages that were scraped and deduplicated
    but dropped because they lacked stronger "closed" wording. Exclude mixed-service
    rows and generic disruption wording so we do not recreate the v43/v44 broad drift.
    """
    program = clean_text(row.get("program_or_service"))
    if not program:
        return False
    if not re.fullmatch(r"(?i)emergency departments?", program.strip()):
        return False

    reduction = safe_lower(row.get("bed_or_space_reduction_text"))
    reason = safe_lower(row.get("reason_text"))
    raw = safe_lower(row.get("raw_block_text"))
    combined = f"{reduction} {reason} {raw}"

    # Accept only explicit physician-coverage outage wording in the reduction text,
    # or N/A reduction text paired with clear physician-coverage wording elsewhere.
    explicit_reduction_phrase = (
        any(tok in reduction for tok in [
            "no on-site physician coverage",
            "no physician coverage on-site",
            "no on-site physician",
            "temporarily without on-site physician coverage",
            "temporarily without physician coverage",
            "without on-site physician coverage",
            "lack of physician coverage",
            "temporary lack of physician coverage",
            "no physician available for emergency department services",
            "no on-site physician in emergency department",
            "no physician in the emergency department",
        ])
    )
    n_a_with_physician_phrase = (
        reduction in {"n / a", "n/a", "na"} and
        any(tok in combined for tok in [
            "physician coverage",
            "without on-site physician coverage",
            "no on-site physician",
            "no physician coverage on-site",
            "no physician available for emergency department services",
            "no physician in the emergency department",
        ])
    )
    if not (explicit_reduction_phrase or n_a_with_physician_phrase):
        return False

    # Exclude mixed-service or clearly non-ED service wording.
    if any(tok in combined for tok in [
        "acute care",
        "admissions",
        "detox",
        "obstetric",
        "obstetrical",
        "birthing",
        "operating room",
        "surgery",
        "surgical",
        "inpatient",
        "clinic",
        "ambulatory",
    ]):
        return False

    # Exclude schedule-based, partial-service, or generic disruption wording.
    if any(tok in combined for tok in [
        "virtual physician",
        "virtual care",
        "high acuity",
        "operating hours",
        "will be open",
        "closed from",
        "each night",
        "weekend",
        "daily from",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "emergency department disruption",
        "multiple shifts",
    ]):
        return False

    start_dt = parse_dt_any(row.get("start_date_parsed_clean"))
    end_dt = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))
    if pd.isna(start_dt) or pd.isna(end_dt) or end_dt <= start_dt:
        return False

    span_days = (end_dt - start_dt).total_seconds() / 86400.0
    if span_days > max_days:
        return False

    return True


def extract_fallback_datetime_range(row):

    reduction_text = row.get("bed_or_space_reduction_text")

    start_dt = parse_dt_any(row.get("start_date_parsed_clean"))
    end_dt = parse_dt_any(row.get("anticipated_end_date_parsed_clean"))

    if pd.isna(end_dt):
        inferred_end = parse_dt_any(row.get("inferred_end_date_from_snapshots"))
        if not pd.isna(inferred_end):
            end_dt = inferred_end

    if pd.isna(start_dt) or pd.isna(end_dt) or end_dt <= start_dt:
        return []

    allowed = fallback_allowed_for_text(
        reduction_text,
        program_or_service=row.get("program_or_service"),
        raw_block_text=row.get("raw_block_text"),
    )

    # v44 narrow recovery for terse, short-span ED physician-coverage notices.
    if not allowed and is_short_physician_coverage_ed_notice(row, max_days=14):
        allowed = True

    # v75 targeted recovery for clear continuous ED closure phrasing without schedule cues.
    if not allowed and is_continuous_ed_closure_phrase_notice(row):
        allowed = True

    if not allowed:
        return []

    return [
        {
            "interval_start": start_dt,
            "interval_end": end_dt,
            "interval_method": "fallback_datetime_range",
            "interval_quality": "coarse_range_estimate",
        }
    ]


# ============================================================
# PARSER SANITY GUARDS
# ============================================================

def _config_years_for_parser_sanity():
    """Return the parser year-capture band for artifact sanity checks.

    This is not the analysis window. It intentionally mirrors PARSER_YEAR_MIN /
    PARSER_YEAR_MAX so 4-digit tokens are only accepted as years in 2020-2050.
    Outside that band, tokens such as 2300 and 0700 are not calendar years.
    """
    return [PARSER_YEAR_MIN, PARSER_YEAR_MAX]


def parser_sanity_min_year():
    return PARSER_YEAR_MIN


def parser_sanity_max_year():
    return PARSER_YEAR_MAX


def _record_interval_sanity_rejection(iv, reason):
    try:
        PARSER_EXCEPTION_RECORDS.append({
            "parser_stage": "dedupe_intervals_exact.interval_sanity_reject",
            "exception_type": "IntervalSanityReject",
            "exception_text": str(reason),
            "snapshot_url": None,
            "snapshot_date": None,
            "site_best": None,
            "facility_name": None,
            "community_heading": None,
            "program_or_service": None,
            "start_date_text": None,
            "anticipated_end_date_text": None,
            "bed_or_space_reduction_text": None,
            "reason_text": None,
            "raw_block_text": None,
            "interval_method": (iv or {}).get("interval_method"),
            "interval_start": (iv or {}).get("interval_start"),
            "interval_end": (iv or {}).get("interval_end"),
        })
    except Exception:
        pass


def _interval_candidate_is_plausible(iv, start, end):
    method = str((iv or {}).get("interval_method") or "")
    if pd.isna(start) or pd.isna(end) or end <= start:
        return False, "invalid_or_nonpositive_interval"

    min_year = parser_sanity_min_year()
    max_year = parser_sanity_max_year()
    if int(start.year) < min_year or int(end.year) < min_year or int(start.year) > max_year or int(end.year) > max_year:
        return False, f"interval_year_outside_parser_sanity_envelope_{min_year}_{max_year}"

    # Do not reject on duration here. Real AHS disruption/capacity episodes can span
    # years and are clipped downstream to the analysis window. This sanity gate is
    # intentionally limited to impossible dates/years and invalid intervals so that
    # legitimate long-running fallback/capacity rows (e.g., full ED space closures)
    # are preserved. Grossly long explicit candidates should be reviewed in QA outputs,
    # not silently dropped from analytic interval construction.

    return True, "ok"

def _default_interval_quality(interval_method):
    method = str(interval_method or "").strip()
    if method in HIGH_CONF_METHODS or method == "manual_fixed_interval":
        return "exact_text_interval"
    if method in ESTIMATED_METHODS:
        return "schedule_estimate"
    if method in COARSE_METHODS:
        return "coarse_range_estimate"
    if method.startswith("notice_custom"):
        return "notice_custom"
    return "derived_interval"


def _interval_method_priority(method):
    method = str(method or "")
    if method == "manual_fixed_interval":
        return 0
    if method in {"explicit_weekday_date_time_range", "explicit_dated_natural_language"}:
        return 1
    if method.startswith("notice_custom"):
        return 3
    if method in HIGH_CONF_METHODS:
        return 2
    if method in ESTIMATED_METHODS:
        return 4
    if method in COARSE_METHODS:
        return 5
    return 9


def dedupe_intervals_exact(intervals):
    """Collapse exact duplicate time ranges, preferring the strongest method label.

    Also applies parser-artifact sanity checks. Impossible years are rejected globally,
    but duration is not used as a drop criterion because real disruptions can span
    years and are clipped downstream.
    """
    best_by_range = {}
    for iv in intervals or []:
        start = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        end = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        plausible, reason = _interval_candidate_is_plausible(iv, start, end)
        if not plausible:
            _record_interval_sanity_rejection(iv, reason)
            continue
        method = iv.get("interval_method")
        quality = iv.get("interval_quality") or _default_interval_quality(method)
        clean_iv = dict(iv)
        clean_iv["interval_start"] = start
        clean_iv["interval_end"] = end
        clean_iv["interval_method"] = method
        clean_iv["interval_quality"] = quality
        key = (start.isoformat(), end.isoformat())
        old = best_by_range.get(key)
        if old is None or _interval_method_priority(method) < _interval_method_priority(old.get("interval_method")):
            best_by_range[key] = clean_iv
    out = list(best_by_range.values())
    out.sort(key=lambda x: (x["interval_start"], x["interval_end"], _interval_method_priority(x.get("interval_method")), str(x.get("interval_method") or "")))

    # Suppress zero-novel nested intervals emitted from the same source text.
    # This prevents patterns like "2300 July 12 to 1500 July 13, 0700 to 1500 July 14"
    # from keeping a spurious July 13 0700-1500 interval that is fully contained
    # in the cross-date July 12-13 closure. Fully contained intervals add no
    # unioned burden and only inflate raw/episode diagnostic surfaces.
    filtered = []
    for i, iv in enumerate(out):
        s = iv["interval_start"]
        e = iv["interval_end"]
        contained = False
        for j, other in enumerate(out):
            if i == j:
                continue
            os = other["interval_start"]
            oe = other["interval_end"]
            if os <= s and e <= oe and (os < s or e < oe):
                contained = True
                break
        if not contained:
            filtered.append(iv)
    return filtered


def subtract_intervals(intervals, blockers):
    """
    Subtract blocker intervals from intervals while preserving the original method/quality
    on any remaining non-overlapping schedule-derived pieces.

    This is used when a notice contains both an explicit closure interval and a broader
    repeating schedule. In that case, the explicit interval stays in the high-confidence
    layer and the schedule-derived baseline is retained only for the residual time not
    already covered by the explicit interval.
    """
    blocker_ranges = []
    for b in blockers or []:
        b_start = pd.to_datetime(b.get("interval_start"), errors="coerce")
        b_end = pd.to_datetime(b.get("interval_end"), errors="coerce")
        if pd.isna(b_start) or pd.isna(b_end) or b_end <= b_start:
            continue
        blocker_ranges.append((b_start, b_end))

    if not blocker_ranges:
        return dedupe_intervals_exact(intervals)

    blocker_ranges.sort()

    out = []
    for iv in intervals or []:
        start = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        end = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(start) or pd.isna(end) or end <= start:
            continue

        segments = [(start, end)]
        for b_start, b_end in blocker_ranges:
            new_segments = []
            for seg_start, seg_end in segments:
                if b_end <= seg_start or b_start >= seg_end:
                    new_segments.append((seg_start, seg_end))
                    continue
                if b_start > seg_start:
                    new_segments.append((seg_start, min(b_start, seg_end)))
                if b_end < seg_end:
                    new_segments.append((max(b_end, seg_start), seg_end))
            segments = [(a, b) for a, b in new_segments if b > a]
            if not segments:
                break

        for seg_start, seg_end in segments:
            new_iv = dict(iv)
            new_iv["interval_start"] = seg_start
            new_iv["interval_end"] = seg_end
            out.append(new_iv)

    return dedupe_intervals_exact(out)



def _month_num_from_name_v51(month_name: str):
    if not month_name:
        return None
    key = str(month_name).lower().replace('.', '')
    for k, v in MONTHS.items():
        if k.replace('.', '') == key:
            return v
    return None


def _parse_date_phrase_v51(phrase: str, default_year: int):
    if not phrase:
        return pd.NaT
    s = clean_text(phrase) or ''
    m = re.search(
        rf"(?i)(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{{1,2}})(?:,\s*({PARSER_YEAR_REGEX}))?",
        s,
    )
    if not m:
        return pd.NaT
    month_num = _month_num_from_name_v51(m.group(1))
    if not month_num:
        return pd.NaT
    year = int(m.group(3)) if m.group(3) else int(default_year)
    try:
        return pd.Timestamp(year=year, month=int(month_num), day=int(m.group(2)))
    except Exception:
        return pd.NaT


def _parse_time_phrase_v51(phrase: str):
    if not phrase:
        return None
    m = re.search(r"(?i)(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)|\d{3,4}|noon|midnight)", str(phrase))
    if not m:
        return None
    return parse_time_token(m.group(1))


def _combine_date_and_time_v51(date_phrase: str, time_phrase: str, default_year: int):
    d = _parse_date_phrase_v51(date_phrase, default_year)
    mins = _parse_time_phrase_v51(time_phrase)
    if pd.isna(d) or mins is None:
        return pd.NaT
    hh, mm = divmod(mins, 60)
    try:
        return pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hh, minute=mm)
    except Exception:
        return pd.NaT


def extract_notice_custom_intervals_v51(row, base_year):
    text_parts = [
        row.get('bed_or_space_reduction_text'),
        row.get('raw_block_text'),
        row.get('reason_text'),
    ]
    text = clean_text(' '.join(str(x or '') for x in text_parts)) or ''
    if not text:
        return []
    low = text.lower()
    intervals = []

    time_pat = r"(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)|\d{3,4}|noon|midnight)"
    date_pat = r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:,\s*\d{4})?)"

    # explicit date-at-time to date-at-time
    pat = re.search(rf"(?is)(?:from\s+)?{date_pat}\s+at\s+{time_pat}\s+(?:to|until|-)\s+{date_pat}\s+at\s+{time_pat}", text)
    if pat:
        d1, t1, d2, t2 = pat.group(1), pat.group(2), pat.group(3), pat.group(4)
        s = _combine_date_and_time_v51(d1, t1, base_year)
        e = _combine_date_and_time_v51(d2, t2, base_year)
        if pd.notna(s) and pd.notna(e) and e > s:
            intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_date_at_time_range", "interval_quality": "notice_custom"})
            return intervals

    # from time to/until time on date
    pat = re.search(rf"(?is)\bfrom\s+{time_pat}\s+(?:to|until)\s+{time_pat}\s+on\s+(?:[A-Za-z]+,?\s+)?{date_pat}", text)
    if pat:
        t1, t2, d1 = pat.group(1), pat.group(2), pat.group(3)
        s = _combine_date_and_time_v51(d1, t1, base_year)
        e = _combine_date_and_time_v51(d1, t2, base_year)
        if pd.notna(s) and pd.notna(e):
            if e <= s:
                e = e + pd.Timedelta(days=1)
            intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_same_day_window", "interval_quality": "notice_custom"})
            return intervals

    # on date from time to time
    pat = re.search(rf"(?is)\bon\s+(?:[A-Za-z]+,?\s+)?{date_pat}\s+from\s+{time_pat}\s+(?:to|until)\s+{time_pat}", text)
    if pat:
        d1, t1, t2 = pat.group(1), pat.group(2), pat.group(3)
        s = _combine_date_and_time_v51(d1, t1, base_year)
        e = _combine_date_and_time_v51(d1, t2, base_year)
        if pd.notna(s) and pd.notna(e):
            if e <= s:
                e = e + pd.Timedelta(days=1)
            intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_same_day_window", "interval_quality": "notice_custom"})
            return intervals

    # from time to/until time <Month day> (without explicit 'on')
    pat = re.search(rf"(?is)\bfrom\s+{time_pat}\s+(?:to|until)\s+{time_pat}\s+(?:today,?\s+|tomorrow,?\s+)?(?:[A-Za-z]+,?\s+)?{date_pat}", text)
    if pat:
        # Same guard as extract_single_day_interval(): do not allow this
        # date-after-time parser to cross a prior Month Day from-time segment.
        prior = text[max(0, pat.start() - 40):pat.start()]
        if not re.search(rf"({month_regex()})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s*$", prior, flags=re.I):
            t1, t2, d1 = pat.group(1), pat.group(2), pat.group(3)
            s = _combine_date_and_time_v51(d1, t1, base_year)
            e = _combine_date_and_time_v51(d1, t2, base_year)
            if pd.notna(s) and pd.notna(e):
                if e <= s:
                    e = e + pd.Timedelta(days=1)
                intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_same_day_window", "interval_quality": "notice_custom"})
                return intervals

    # from time to time on date1 and date2 ...
    multi = re.search(rf"(?is)\bfrom\s+{time_pat}\s+(?:to|until)\s+{time_pat}\s+on\s+([^\.]+)", text)
    if multi:
        t1, t2, tail = multi.group(1), multi.group(2), multi.group(3)
        date_matches = list(re.finditer(date_pat, tail, flags=re.I))
        if len(date_matches) >= 2:
            for m in date_matches:
                d = m.group(1)
                s = _combine_date_and_time_v51(d, t1, base_year)
                e = _combine_date_and_time_v51(d, t2, base_year)
                if pd.notna(s) and pd.notna(e):
                    if e <= s:
                        e = e + pd.Timedelta(days=1)
                    intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_multi_date_window", "interval_quality": "notice_custom"})
            if intervals:
                return dedupe_intervals_exact(intervals)

    # closed at time DATE ... reopen at time DATE
    pat = re.search(rf"(?is)(?:closed|closure|temporarily closed|will be closed).*?at\s+{time_pat}\s+(?:today,?\s+|tomorrow,?\s+)?([^\.]*?{date_pat}).*?re-?open(?:ed)?\s+at\s+{time_pat}\s+(?:today,?\s+|tomorrow,?\s+)?([^\.]*?{date_pat})", text)
    if pat:
        t1, d1, _, t2, d2, _ = pat.group(1), pat.group(2), pat.group(3), pat.group(4), pat.group(5), pat.group(6)
        s = _combine_date_and_time_v51(d1, t1, base_year)
        e = _combine_date_and_time_v51(d2, t2, base_year)
        if pd.notna(s) and pd.notna(e) and e > s:
            intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_closed_reopen", "interval_quality": "notice_custom"})
            return intervals

    # closed for 24/12 hours beginning at time DATE ... reopen at time DATE
    pat = re.search(rf"(?is)closed\s+for\s+(24|12)\s+hours?.*?beginning\s+at\s+{time_pat}\s+(?:today,?\s+|tomorrow,?\s+)?([^\.]*?{date_pat}).*?re-?open(?:ed)?\s+at\s+{time_pat}\s+(?:today,?\s+|tomorrow,?\s+)?([^\.]*?{date_pat})", text)
    if pat:
        _dur, t1, d1, _, t2, d2, _ = pat.group(1), pat.group(2), pat.group(3), pat.group(4), pat.group(5), pat.group(6), pat.group(7)
        s = _combine_date_and_time_v51(d1, t1, base_year)
        e = _combine_date_and_time_v51(d2, t2, base_year)
        if pd.notna(s) and pd.notna(e) and e > s:
            intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_duration_begin_reopen", "interval_quality": "notice_custom"})
            return intervals

    # today/tomorrow from time until time, using notice/snapshot year with explicit month/day in text
    pat = re.search(rf"(?is)\bfrom\s+{time_pat}\s+(?:today|tomorrow),?\s+([^\.]*?{date_pat})\s*,?\s*(?:until|to)\s+{time_pat}\s+(?:today|tomorrow),?\s+([^\.]*?{date_pat})", text)
    if pat:
        t1, d1, _, t2, d2, _ = pat.group(1), pat.group(2), pat.group(3), pat.group(4), pat.group(5), pat.group(6)
        s = _combine_date_and_time_v51(d1, t1, base_year)
        e = _combine_date_and_time_v51(d2, t2, base_year)
        if pd.notna(s) and pd.notna(e) and e > s:
            intervals.append({"interval_start": s, "interval_end": e, "interval_method": "notice_custom_explicit_range", "interval_quality": "notice_custom"})
            return intervals

    return []

def extract_manual_fixed_interval(row):
    """
    Force manual additions to enter the pipeline as exact fixed intervals rather than
    being reinterpreted by the schedule parsers.
    """
    is_manual = bool(row.get("is_manual_add"))
    page_label = clean_text(row.get("snapshot_page_label")) or ""
    snapshot_url = clean_text(row.get("snapshot_url")) or ""
    if not is_manual and page_label.lower() != "manual_add" and "/page_manual_" not in snapshot_url.lower():
        return []

    start_raw = row.get("manual_interval_start") if row.get("manual_interval_start") is not None else row.get("start_date_text")
    end_raw = row.get("manual_interval_end") if row.get("manual_interval_end") is not None else row.get("anticipated_end_date_text")
    start_dt = parse_dt_any(start_raw)
    end_dt = parse_dt_any(end_raw)
    if pd.notna(start_dt) and pd.notna(end_dt) and end_dt > start_dt:
        return [{
            "interval_start": start_dt,
            "interval_end": end_dt,
            "interval_method": "manual_fixed_interval",
            "interval_quality": "exact_text_interval",
        }]
    return []


def _has_schedule_recurrence_cue(text):
    """Return True when text contains recurrence/phase-change cues.

    This intentionally errs toward running schedule parsers when a row combines
    an explicit fixed closure with a later change in operating hours. It prevents
    the unsafe assumption that one successful explicit parser means the row is
    complete. Singular weekday+calendar-date wording alone is still not enough;
    cues must imply recurrence, changed hours, day-only operation, or open-hours
    complement logic.
    """
    low = safe_lower(text)
    if not low:
        return False

    recurrence_patterns = [
        r"\b(daily|weekly|every|each|weekdays?|weekends?|seven days a week|7 days a week)\b",
        r"\b(mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)\b",
        rf"\b{weekday_regex()}\s*(?:-|–|—|to|through|thru)\s*{weekday_regex()}\b",
    ]
    phase_change_patterns = [
        r"\b(open during the day only|day only|open day(?:time)? only)\b",
        r"\b(closed overnights?|closed overnight|overnight closures?|overnights?)\b",
        r"\b(open daily from|open from|open between|will be open|will remain open)\b",
        r"\b(closed daily|unavailable overnight|unavailable overnights?|closed outside (?:those|these) hours)\b",
        r"\b(reduced hours|temporary hours|modified hours|changed hours|limited hours|reduced service hours)\b",
        r"\b(as of|effective|beginning|starting)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b",
        r"\b(return to 24-hour service|resume 24-hour service|resume full service|reopen for regular hours)\b",
        r"\b(until further notice|until further updates?)\b",
    ]
    compact_month_day_window = re.search(
        rf"\bclosed\s+from\s+{time_expr_regex()}\s*(?:-|–|—|to|until)\s*{time_expr_regex()}\s+on\s+{month_regex()}\s+\d{{1,2}}(?:st|nd|rd|th)?\s*(?:-|–|—|to|through|thru|until)\s*\d{{1,2}}(?:st|nd|rd|th)?",
        low,
        flags=re.I,
    )
    if compact_month_day_window:
        return True

    parenthetical_date_range_window = re.search(
        rf"{month_regex()}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s*(?:-|–|—|to|through|thru|until)\s*(?:{month_regex()}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s*\(\s*{time_expr_regex()}\s*(?:-|–|—|to|until)\s*{time_expr_regex()}\s*\)",
        low,
        flags=re.I,
    )
    if parenthetical_date_range_window:
        return True

    bounded_time_date_range = re.search(
        rf"\bclosed\s+from\s+{time_expr_regex()}\s*(?:-|–|—|to|until)\s*{time_expr_regex()}\s+from\s+{month_regex()}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?\s*(?:-|–|—|to|through|thru|until)\s*(?:{month_regex()}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?",
        low,
        flags=re.I,
    )
    if bounded_time_date_range:
        return True

    for pat in recurrence_patterns + phase_change_patterns:
        if re.search(pat, low, flags=re.I):
            return True
    return False


def _roll_forward_early_year_intervals_for_late_year_notice(row, intervals):
    """Correct unyear-qualified upcoming Jan/Feb/Mar dates in late-year notices.

    AHS notices published in Nov/Dec often announce January closures without writing
    the year. Parsers that default to the publication year can therefore create dates
    one year too early. For late-year source rows, roll early-year intervals forward
    one year before deduplication.
    """
    if not intervals:
        return intervals
    snap = pd.NaT
    if hasattr(row, "get"):
        snap = pd.to_datetime(row.get("snapshot_date"), errors="coerce")
    if pd.isna(snap) or int(snap.month) < 10:
        return intervals
    out = []
    for iv in intervals:
        nd = dict(iv)
        st = pd.to_datetime(nd.get("interval_start"), errors="coerce")
        en = pd.to_datetime(nd.get("interval_end"), errors="coerce")
        if pd.notna(st) and pd.notna(en) and int(st.year) == int(snap.year) and int(st.month) <= 3:
            try:
                nd["interval_start"] = st + pd.DateOffset(years=1)
                nd["interval_end"] = en + pd.DateOffset(years=1)
            except Exception:
                pass
        out.append(nd)
    return out



def _first_available_row_date(row, cols):
    if row is None or not hasattr(row, "get"):
        return pd.NaT
    for c in cols:
        dt = pd.to_datetime(row.get(c), errors="coerce")
        if pd.notna(dt):
            return pd.Timestamp(dt)
    return pd.NaT


def _realign_stale_unyear_explicit_intervals(row, intervals):
    """Move unyear-qualified explicit month/day intervals to the source-observed year.

    Some long-running rows keep a stale Start Date while their inline explicit
    add-on intervals are current wording without written years, e.g. a 2025
    Westlock row with Start Date: Oct. 14, 2024 and text "October 14
    (1700) - October 15 (0800)". Parsers defaulting to the stale start-year
    misallocate those intervals one or more years early. This post-parser guard
    only shifts high-confidence explicit intervals forward when the shifted
    interval overlaps the row's observed/end-date context.

    v8 fix: decide whether the *bed/space reduction wording* has an explicit
    calendar year. The previous v7 guard scanned raw_block_text as well, which
    contains labels such as "Start Date: October 14, 2024" and
    "Anticipated End Date: October 15, 2025". That made the real grouped
    Westlock/Hinton rows bypass this guard even though their actual interval
    clauses were unyear-qualified.
    """
    if not intervals or row is None or not hasattr(row, "get"):
        return intervals
    bed_text = str(row.get("bed_or_space_reduction_text") or "")
    # If the interval source wording itself writes a calendar year, leave it
    # alone. Raw labeled blocks may contain start/end-field years and must not
    # disable realignment for unyear-qualified inline intervals.
    if re.search(PARSER_YEAR_REGEX, bed_text):
        return intervals

    obs = _first_available_row_date(row, [
        "snapshot_date", "first_seen_snapshot_date", "notice_date",
        "notice_effective_date", "cluster_first_notice_date", "last_seen_snapshot_date",
    ])
    end_dt = _first_available_row_date(row, [
        "anticipated_end_date_parsed_clean", "anticipated_end_date_text",
        "end_date_parsed", "notice_end_date", "tbd_proxy_end_date",
    ])
    start_dt = _first_available_row_date(row, [
        "start_date_parsed_clean", "start_date_text", "start_date_parsed",
    ])
    target_years = []
    for dt in [obs, end_dt]:
        if pd.notna(dt):
            target_years.append(int(pd.Timestamp(dt).year))
    target_years = sorted(set(target_years))
    if not target_years:
        return intervals

    lower_candidates = []
    if pd.notna(obs):
        lower_candidates.append(pd.Timestamp(obs).normalize() - pd.Timedelta(days=21))
    if pd.notna(start_dt) and any(int(pd.Timestamp(start_dt).year) == y for y in target_years):
        lower_candidates.append(pd.Timestamp(start_dt).normalize() - pd.Timedelta(days=1))
    upper_candidates = []
    if pd.notna(end_dt):
        # When an anticipated end date exists, it is the safest upper bound for
        # deciding whether a projected unyear-qualified interval belongs to the
        # current row. Do not let obs+370 pull Dec. intervals into the following
        # December for Dec-Jan rows.
        upper_candidates.append(pd.Timestamp(end_dt).normalize() + pd.Timedelta(days=2))
    elif pd.notna(obs):
        upper_candidates.append(pd.Timestamp(obs).normalize() + pd.Timedelta(days=370))
    if not lower_candidates or not upper_candidates:
        return intervals
    lower = min(lower_candidates)
    upper = max(upper_candidates)

    out = []
    for iv in intervals:
        nd = dict(iv)
        method = str(nd.get("interval_method", ""))
        if method not in HIGH_CONF_METHODS:
            out.append(nd)
            continue
        st = pd.to_datetime(nd.get("interval_start"), errors="coerce")
        en = pd.to_datetime(nd.get("interval_end"), errors="coerce")
        if pd.isna(st) or pd.isna(en):
            out.append(nd)
            continue
        best = None
        for y in target_years:
            if y <= int(st.year) or y - int(st.year) > 5:
                continue
            try:
                pst = st + pd.DateOffset(years=y - int(st.year))
                pen = en + pd.DateOffset(years=y - int(st.year))
            except Exception:
                continue
            # Accept only if the shifted interval falls into the source-observed
            # or anticipated-end context. This catches Westlock/Beaverlodge/
            # Smoky Lake/Boyle add-ons and avoids shifting Dec. 2024 intervals
            # to Dec. 2025 in Dec-Jan rows.
            if pen > lower and pst < upper:
                # Prefer the shift closest to the observed/source date when possible.
                score_anchor = obs if pd.notna(obs) else end_dt
                score = abs((pd.Timestamp(pst).normalize() - pd.Timestamp(score_anchor).normalize()).days) if pd.notna(score_anchor) else 0
                if best is None or score < best[0]:
                    best = (score, pst, pen, y)
        if best is not None:
            _, pst, pen, y = best
            nd["interval_start"] = pst
            nd["interval_end"] = pen
            nd["year_anchor_adjustment"] = f"shifted_to_{y}"
        out.append(nd)
    return out


def _drop_explicit_intervals_outside_declared_episode_bounds(row, intervals):
    """Drop high-confidence explicit intervals falling outside declared episode dates.

    This guards against stale/no-year re-anchoring creating phantom active years
    for a closed historical episode (for example a 2024 Lac La Biche row being
    shifted into 2025).  The bounds are deliberately date-level and slightly
    padded to retain legitimate overnight tails.
    """
    if not intervals or row is None or not hasattr(row, "get"):
        return intervals
    start_dt = _first_available_row_date(row, ["start_date_parsed_clean", "start_date_text", "start_date_parsed"])
    end_dt = _first_available_row_date(row, ["anticipated_end_date_parsed_clean", "anticipated_end_date_text", "end_date_parsed", "notice_end_date"])
    if pd.isna(start_dt) or pd.isna(end_dt):
        return intervals
    start_dt = pd.Timestamp(start_dt).normalize() - pd.Timedelta(days=1)
    end_dt = pd.Timestamp(end_dt).normalize() + pd.Timedelta(days=2)
    if end_dt <= start_dt:
        return intervals

    declared_years = set(range(int(start_dt.year), int(end_dt.year) + 1))
    out = []
    for iv in intervals:
        method = str(iv.get("interval_method") or "")
        if method not in HIGH_CONF_METHODS:
            out.append(iv)
            continue
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        en = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(st) or pd.isna(en):
            continue
        outside_declared_years = int(st.year) not in declared_years and int(en.year) not in declared_years
        if outside_declared_years:
            continue
        out.append(iv)
    return dedupe_intervals_exact(out)


def suppress_relative_today_weekday_artifacts(row, intervals):
    """Remove extra weekday/date artifacts when a relative today/tomorrow parse succeeded."""
    if not intervals:
        return intervals
    text = " ".join(str(x or "") for x in [
        row.get("bed_or_space_reduction_text") if hasattr(row, "get") else "",
        row.get("raw_block_text") if hasattr(row, "get") else "",
    ])
    if not re.search(r"\b(?:today|tonight|tomorrow)\b", text, flags=re.I):
        return intervals
    has_relative_exact = any(str(iv.get("interval_method")) == "explicit_dated_natural_language" for iv in intervals)
    if not has_relative_exact:
        return intervals
    return [iv for iv in intervals if str(iv.get("interval_method")) != "explicit_weekday_date_time_range"]



def _anchor_year_for_unyeared_interval(row, fallback_year=None):
    """Return a row-context year for unyeared month/day phrases."""
    for c in ["first_seen_snapshot_date", "snapshot_date", "start_date_parsed_clean", "anticipated_end_date_parsed_clean", "last_seen_snapshot_date"]:
        dt = parse_dt_any(row.get(c)) if row is not None and hasattr(row, "get") else pd.NaT
        if pd.notna(dt):
            return int(pd.Timestamp(dt).year)
    return int(fallback_year if fallback_year is not None else base_year_for_row(row))


def _parse_unyeared_month_day_token(token, row, fallback_year=None, previous_month_num=None):
    """Parse 'Month Day[, Year]' or 'Day' with a context month/year."""
    txt = clean_text(token) or ""
    m = re.match(rf"(?:({month_regex()})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?$", txt, flags=re.I)
    if not m:
        return pd.NaT
    mon, day, year_txt = m.group(1), m.group(2), m.group(3)
    if not mon:
        # Fall back to the month of row start if only a day is supplied.
        st = parse_dt_any(row.get("start_date_parsed_clean")) if row is not None and hasattr(row, "get") else pd.NaT
        if pd.isna(st):
            return pd.NaT
        mon = pd.Timestamp(st).strftime("%B")
    year = int(year_txt) if year_txt else _anchor_year_for_unyeared_interval(row, fallback_year)
    try:
        mn = MONTHS[str(mon).lower()]
        if previous_month_num is not None and mn < int(previous_month_num) and not year_txt:
            year += 1
        return pd.Timestamp(year=year, month=mn, day=int(day))
    except Exception:
        return pd.NaT


def _parse_month_day_token_with_context(token, row, fallback_year=None, fallback_month=None, previous_month_num=None):
    """Parse Month Day or bare Day while preserving date-range carry-forward.

    This is stricter than _parse_unyeared_month_day_token for multi-range schedule
    text because fallback_year/fallback_month must be respected across paired
    ranges such as "August 20 to 28, and August 30 to September 2".
    """
    txt = clean_text(token) or ""
    m = re.match(rf"(?:({month_regex()})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?$", txt, flags=re.I)
    if not m:
        return pd.NaT
    mon, day, year_txt = m.group(1), m.group(2), m.group(3)
    if not mon:
        mon = fallback_month
    if not mon:
        st = parse_dt_any(row.get("start_date_parsed_clean")) if row is not None and hasattr(row, "get") else pd.NaT
        if pd.isna(st):
            return pd.NaT
        mon = pd.Timestamp(st).strftime("%B")
    if year_txt:
        year = int(year_txt)
    elif fallback_year is not None:
        year = int(fallback_year)
    else:
        year = _anchor_year_for_unyeared_interval(row)
    try:
        mn = MONTHS[str(mon).lower()]
        if previous_month_num is not None and mn < int(previous_month_num) and not year_txt:
            year += 1
        return pd.Timestamp(year=year, month=mn, day=int(day))
    except Exception:
        return pd.NaT


def _multi_range_daily_window_specs(row):
    """Return daily-window date-range specs sharing one closure time window.

    Handles AHS wording such as:
      "closed from 1900 - 0700 daily between August 20 to 28, and August 30 to September 2"

    Existing daily-window parsing only reads the first date span and the later
    source-specific bounds guard then clips away any additional span. This helper
    deliberately activates only when two or more month/day ranges are present.
    """
    text = row.get("bed_or_space_reduction_text") if row is not None and hasattr(row, "get") else ""
    s = clean_text(text) or ""
    if not s:
        return []
    texpr = time_expr_regex()
    mpat = month_regex()
    time_patterns = [
        rf"(?:closed|will\s+be\s+closed|temporarily\s+closed|unavailable)[^.;:]*?(?:from\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})[^.;:]*?\b(?:daily|each\s+day|each\s+night|overnights?|overnight)\b[^.;:]*?\b(?:between|from)\s+(.+)",
        rf"(?:closed|will\s+be\s+closed|temporarily\s+closed|unavailable)[^.;:]*?\b(?:daily|each\s+day|each\s+night|overnights?|overnight)\b[^.;:]*?(?:from\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})[^.;:]*?\b(?:between|from)\s+(.+)",
    ]
    m = None
    for pat in time_patterns:
        m = re.search(pat, s, flags=re.I)
        if m:
            break
    if not m:
        return []
    start_minutes = parse_time_token(m.group(1))
    end_minutes = parse_time_token(m.group(2))
    if start_minutes is None or end_minutes is None:
        return []
    tail = m.group(3)
    range_pat = re.compile(
        rf"({mpat}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)\s*(?:-|–|—|to|through|thru|until|and)\s*((?:{mpat}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)",
        flags=re.I,
    )
    matches = list(range_pat.finditer(tail))
    if len(matches) < 2:
        return []

    anchor_year = _anchor_year_for_unyeared_interval(row)
    specs = []
    previous_range_month = None
    previous_range_year = anchor_year
    for rm in matches:
        start_tok = clean_text(rm.group(1))
        end_tok = clean_text(rm.group(2))
        start_month_match = re.match(rf"({mpat})\s+", start_tok or "", flags=re.I)
        start_month = start_month_match.group(1) if start_month_match else None
        start_dt = _parse_month_day_token_with_context(
            start_tok,
            row,
            fallback_year=previous_range_year,
            fallback_month=None,
            previous_month_num=previous_range_month,
        )
        if pd.isna(start_dt):
            continue
        end_dt = _parse_month_day_token_with_context(
            end_tok,
            row,
            fallback_year=int(pd.Timestamp(start_dt).year),
            fallback_month=start_month,
            previous_month_num=int(pd.Timestamp(start_dt).month),
        )
        if pd.isna(end_dt) or end_dt < start_dt:
            continue
        specs.append({
            "range_start_date": pd.Timestamp(start_dt).normalize(),
            "range_end_date": pd.Timestamp(end_dt).normalize(),
            "start_minutes": start_minutes,
            "end_minutes": end_minutes,
            "source_text": rm.group(0),
        })
        previous_range_month = int(pd.Timestamp(start_dt).month)
        previous_range_year = int(pd.Timestamp(start_dt).year)
    return specs if len(specs) >= 2 else []


def extract_multi_range_daily_window_intervals(row):
    """Expand one daily closure time window across multiple explicit date ranges."""
    intervals = []
    for spec in _multi_range_daily_window_specs(row):
        intervals.extend(build_daily_repeated_intervals(
            spec["range_start_date"],
            spec["range_end_date"],
            spec["start_minutes"],
            spec["end_minutes"],
            "daily_window_schedule",
            "schedule_estimate",
        ))
    for iv in intervals:
        iv["schedule_bound_source"] = "multi_date_range_daily_window"
    return dedupe_intervals_exact(intervals)


def _apply_multi_range_daily_window_bounds(row, intervals):
    """Keep schedule intervals only when their start date is inside a listed range."""
    specs = _multi_range_daily_window_specs(row)
    if not specs:
        return intervals
    ranges = [(pd.Timestamp(s["range_start_date"]).normalize(), pd.Timestamp(s["range_end_date"]).normalize()) for s in specs]
    out = []
    for iv in intervals:
        method = str(iv.get("interval_method") or "")
        if classify_interval_confidence(method) not in {"estimated", "coarse"}:
            out.append(iv)
            continue
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        en = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(st) or pd.isna(en) or en <= st:
            continue
        st_day = pd.Timestamp(st).normalize()
        if any(lo <= st_day <= hi for lo, hi in ranges):
            out.append(iv)
    return dedupe_intervals_exact(out)


def _source_specific_schedule_bounds_from_text(row):
    """Extract explicit schedule validity bounds embedded in burden text.

    Examples:
      - closed from 2100-0900 from September 17 through October 31, 2022
      - closed from 1700-0800 Monday to Thursday between June 1 to October 31
    """
    text = row.get("bed_or_space_reduction_text") if row is not None and hasattr(row, "get") else ""
    s = clean_text(text) or ""
    if not s:
        return pd.NaT, pd.NaT
    texpr = time_expr_regex()
    mpat = month_regex()
    patterns = [
        rf"(?:closed|will be closed|no onsite physician coverage)[^.;:]*?{texpr}\s*(?:-|–|—|to|until)\s*{texpr}[^.;:]*?\bfrom\s+({mpat}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)\s*(?:-|–|—|to|through|thru|until)\s*((?:{mpat}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)",
        rf"(?:closed|will be closed|no onsite physician coverage)[^.;:]*?{texpr}\s*(?:-|–|—|to|until)\s*{texpr}[^.;:]*?\bbetween\s+({mpat}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)\s*(?:-|–|—|to|and|through|thru|until)\s*((?:{mpat}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?)",
    ]
    for pat in patterns:
        m = re.search(pat, s, flags=re.I)
        if not m:
            continue
        start_tok, end_tok = m.group(1), m.group(2)
        sd = _parse_unyeared_month_day_token(start_tok, row)
        prev_month = int(sd.month) if pd.notna(sd) else None
        ed = _parse_unyeared_month_day_token(end_tok, row, fallback_year=int(sd.year) if pd.notna(sd) else None, previous_month_num=prev_month)
        if pd.notna(sd) and pd.notna(ed) and ed >= sd:
            return sd.normalize(), ed.normalize()
    return pd.NaT, pd.NaT


def _schedule_first_closure_start_minutes(row):
    text = row.get("bed_or_space_reduction_text") if row is not None and hasattr(row, "get") else ""
    s = clean_text(text) or ""
    texpr = time_expr_regex()
    pats = [
        rf"closed\s+from\s*({texpr})\s*(?:-|–|—|to|until)\s*{texpr}",
        rf"closed\s+each\s+night[^.;]*?from\s*({texpr})\s*(?:-|–|—|to|until)\s*{texpr}",
        rf"between\s*({texpr})\s*(?:-|–|—|to|until)\s*{texpr}\s*(?:daily|each\s+night|overnight)",
        rf"(?:starting\s+on\s+)?{weekday_regex()}s?\s+at\s+({texpr})\b[^.;]*(?:until|through|thru|over)\b",
        rf"closed\s+{weekday_regex()}s?\s+(?:at|from)\s+({texpr})\b[^.;]*(?:until|through|thru|over)\b",
        rf"from\s*({texpr})\s+{weekday_regex()}s?\b[^.;]*(?:through|until|to)\s*{texpr}\s+{weekday_regex()}s?",
        rf"from\s*({texpr})\s*(?:-|–|—|to|until)\s*{texpr}\s+(?:daily|monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        rf"({texpr})\s*(?:-|–|—|to|until)\s*{texpr}\s+from\s+{month_regex()}\s+\d{{1,2}}",
    ]
    for pat in pats:
        m = re.search(pat, s, flags=re.I)
        if m:
            mins = parse_time_token(m.group(1))
            if mins is not None:
                return mins
    return None

def _apply_source_specific_schedule_bounds(row, intervals):
    """Constrain schedule-derived intervals to explicit bounds embedded in the burden text."""
    if not intervals:
        return intervals
    # Multi-range daily-window rows have discontinuous source bounds. Applying the
    # older single-span guard would keep only the first range and drop later ranges.
    if _multi_range_daily_window_specs(row):
        return _apply_multi_range_daily_window_bounds(row, intervals)
    sd, ed = _source_specific_schedule_bounds_from_text(row)
    if pd.isna(sd) and pd.isna(ed):
        return intervals
    lower = pd.Timestamp(sd).normalize() if pd.notna(sd) else pd.NaT
    # Inclusive source-bound end: allow a closure that STARTS on the final named
    # date to carry overnight, but do not allow new schedule starts after that date.
    upper = pd.Timestamp(ed).normalize() + pd.Timedelta(days=2) if pd.notna(ed) else pd.NaT
    source_end_day = pd.Timestamp(ed).normalize() if pd.notna(ed) else pd.NaT
    out = []
    for iv in intervals:
        method = str(iv.get("interval_method") or "")
        if classify_interval_confidence(method) not in {"estimated", "coarse"}:
            out.append(iv); continue
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        en = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(st) or pd.isna(en) or en <= st:
            continue
        if pd.notna(source_end_day) and st.normalize() > source_end_day:
            continue
        if pd.notna(lower) and en <= lower:
            continue
        if pd.notna(upper) and st >= upper:
            continue
        nst = max(st, lower) if pd.notna(lower) else st
        nen = min(en, upper) if pd.notna(upper) else en
        if nen <= nst:
            continue
        nd = dict(iv); nd["interval_start"] = nst; nd["interval_end"] = nen
        out.append(nd)
    return dedupe_intervals_exact(out)


def _trim_initial_overnight_carry_in(row, intervals):
    """Remove/trim artificial midnight-to-morning carry-in on the first schedule date.

    Weekly-range generators back up to the start weekday and then row-context clipping
    can create a partial interval from 00:00 to the morning on the declared start date,
    even when the posted schedule begins later that same day (e.g., 1700-0800).
    """
    if not intervals:
        return intervals
    source_start, _source_end = _source_specific_schedule_bounds_from_text(row)
    if pd.notna(source_start):
        start_dt = source_start
    else:
        start_dt = parse_boundary_datetime_field(row.get("start_date_parsed_clean") if hasattr(row, "get") else None)
        if pd.isna(start_dt):
            start_dt = parse_boundary_datetime_field(row.get("start_date_text") if hasattr(row, "get") else None)
    if pd.isna(start_dt):
        return intervals
    start_day = pd.Timestamp(start_dt).normalize()
    first_mins = _schedule_first_closure_start_minutes(row)
    if first_mins is None or first_mins <= 0:
        return intervals
    first_closure_start = start_day + pd.Timedelta(minutes=int(first_mins))
    out = []
    for iv in intervals:
        method = str(iv.get("interval_method") or "")
        if method.startswith("regular_hours_complement") or method.startswith("regular_hours_only_closure"):
            out.append(iv); continue
        if classify_interval_confidence(method) not in {"estimated", "coarse"}:
            out.append(iv); continue
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        en = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(st) or pd.isna(en) or en <= st:
            continue
        if st == start_day and en > first_closure_start and en <= start_day + pd.Timedelta(days=2):
            nd = dict(iv); nd["interval_start"] = first_closure_start
            out.append(nd)
        elif st == start_day and en <= first_closure_start and en <= start_day + pd.Timedelta(hours=12):
            continue
        else:
            out.append(iv)
    return dedupe_intervals_exact(out)


def _drop_weekly_named_terminal_endpoint_artifacts(row, intervals):
    """Drop weekly-range intervals beginning on/after a date-only anticipated end date."""
    if not intervals:
        return intervals
    end_dt = parse_boundary_datetime_field(row.get("anticipated_end_date_parsed_clean") if hasattr(row, "get") else None)
    if pd.isna(end_dt):
        end_dt = parse_boundary_datetime_field(row.get("anticipated_end_date_text") if hasattr(row, "get") else None)
    if pd.isna(end_dt):
        return intervals
    end_day = pd.Timestamp(end_dt).normalize()
    out = []
    for iv in intervals:
        method = str(iv.get("interval_method") or "")
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce")
        if method.startswith("weekly_named_range_schedule") and pd.notna(st) and st.normalize() >= end_day:
            continue
        out.append(iv)
    return dedupe_intervals_exact(out)


def _subtract_functional_day_shift(row, intervals):
    """For rows saying ED remains functional during day shift, count only residual closed hours."""
    if not intervals:
        return intervals
    text = " ".join(str(x or "") for x in [row.get("bed_or_space_reduction_text"), row.get("raw_block_text")]) if row is not None and hasattr(row, "get") else ""
    if not re.search(r"ED\s+will\s+remain\s+functional\s+over\s+the\s+day\s+shift|emergency department\s+will\s+remain\s+functional\s+over\s+the\s+day\s+shift", text, flags=re.I):
        return intervals
    m = re.search(r"day\s+shift\s*\(\s*([^)\s]+)\s*(?:-|–|—|to)\s*([^)\s]+)\s*\)", text, flags=re.I)
    if not m:
        return intervals
    open_start = parse_time_token(m.group(1)); open_end = parse_time_token(m.group(2))
    if open_start is None or open_end is None or open_end <= open_start:
        return intervals
    adjusted = []
    for iv in intervals:
        st = pd.to_datetime(iv.get("interval_start"), errors="coerce"); en = pd.to_datetime(iv.get("interval_end"), errors="coerce")
        if pd.isna(st) or pd.isna(en) or en <= st:
            continue
        # Only split broad multi-day continuous intervals; leave already-nightly intervals alone.
        if (en - st).total_seconds() / 3600.0 < 24:
            adjusted.append(iv); continue
        open_blocks = []
        cur = st.normalize()
        while cur < en.normalize() + pd.Timedelta(days=1):
            os = cur + pd.Timedelta(minutes=open_start)
            oe = cur + pd.Timedelta(minutes=open_end)
            if oe > st and os < en:
                open_blocks.append({"interval_start": os, "interval_end": oe})
            cur += pd.Timedelta(days=1)
        residuals = subtract_intervals([iv], open_blocks)
        adjusted.extend(residuals)
    return dedupe_intervals_exact(adjusted)


def extract_colon_window_date_pair_intervals(row, base_year):
    """Parse 'closed 1600 - 0800: Jan 31 - Feb 1, Feb 2 - Feb 3' as listed overnight pairs."""
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    texpr = time_expr_regex(); mpat = month_regex()
    m = re.search(rf"(?:closed|emergency department closed)\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*:\s*(.+)$", s, flags=re.I)
    if not m:
        return []
    start_min = parse_time_token(m.group(1)); end_min = parse_time_token(m.group(2))
    if start_min is None or end_min is None:
        return []
    tail = m.group(3)
    pair_pat = re.compile(rf"({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|—|to|until)\s*(?:({mpat})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?", flags=re.I)
    out = []
    cur_year = _anchor_year_for_unyeared_interval(row, base_year)
    prev_month = None
    for pm in pair_pat.finditer(tail):
        mon1, d1, mon2, d2 = pm.group(1), pm.group(2), pm.group(3) or pm.group(1), pm.group(4)
        mn1 = MONTHS.get(mon1.lower()); mn2 = MONTHS.get(mon2.lower())
        if mn1 is None or mn2 is None:
            continue
        if prev_month is not None and mn1 < prev_month:
            cur_year += 1
        y1 = cur_year
        y2 = y1 + (1 if mn2 < mn1 else 0)
        prev_month = mn1
        try:
            st = pd.Timestamp(year=y1, month=mn1, day=int(d1)) + pd.Timedelta(minutes=start_min)
            en = pd.Timestamp(year=y2, month=mn2, day=int(d2)) + pd.Timedelta(minutes=end_min)
            if en <= st:
                en += pd.Timedelta(days=1)
        except Exception:
            continue
        out.append({"interval_start": st, "interval_end": en, "interval_method": "explicit_multi_interval", "interval_quality": "exact_text_interval"})
    return dedupe_intervals_exact(out)


def extract_comma_date_time_list_intervals(row, base_year):
    """Parse 'February 7, 0900-1600, ... February 11, 0900-1200'."""
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    texpr = time_expr_regex(); mpat = month_regex()
    pat = re.compile(rf"({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*,\s*({texpr})\s*(?:-|–|—|to|until)\s*({texpr})", flags=re.I)
    out = []
    cur_year = _anchor_year_for_unyeared_interval(row, base_year)
    prev_month = None
    for m in pat.finditer(s):
        mon, day, year_txt, t1, t2 = m.groups()
        mn = MONTHS.get(mon.lower())
        if mn is None:
            continue
        if year_txt:
            cur_year = int(year_txt)
        elif prev_month is not None and mn < prev_month:
            cur_year += 1
        prev_month = mn
        st = _make_timestamp_extended(cur_year, mon, day, t1)
        en = _make_timestamp_extended(cur_year, mon, day, t2)
        if pd.notna(st) and pd.notna(en):
            if en <= st:
                en += pd.Timedelta(days=1)
            out.append({"interval_start": st, "interval_end": en, "interval_method": "explicit_multi_interval", "interval_quality": "exact_text_interval"})
    return dedupe_intervals_exact(out)


def extract_ambiguous_1201_to_morning_intervals(row, base_year):
    """Recover AHS same-day entries like 'August 19 (12:01 - 0700 am)' as 00:01-07:00."""
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return []
    mpat = month_regex()
    pat = re.compile(rf"({mpat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?\s*\(\s*12:01\s*(?:-|–|—|to|until)\s*(\d{{3,4}}|\d{{1,2}}(?::\d{{2}})?)\s*a\.?m\.?\s*\)", flags=re.I)
    out = []
    cur_year = _anchor_year_for_unyeared_interval(row, base_year)
    prev_month = None
    for m in pat.finditer(s):
        mon, day, year_txt, end_tok = m.groups()
        mn = MONTHS.get(mon.lower())
        if mn is None:
            continue
        if year_txt:
            cur_year = int(year_txt)
        elif prev_month is not None and mn < prev_month:
            cur_year += 1
        prev_month = mn
        end_min = parse_time_token(end_tok)
        if end_min is None:
            continue
        try:
            st = pd.Timestamp(year=cur_year, month=mn, day=int(day), hour=0, minute=1)
            en = pd.Timestamp(year=cur_year, month=mn, day=int(day)) + pd.Timedelta(minutes=end_min)
            if en <= st:
                en += pd.Timedelta(days=1)
        except Exception:
            continue
        out.append({"interval_start": st, "interval_end": en, "interval_method": "explicit_multi_interval", "interval_quality": "exact_text_interval"})
    return dedupe_intervals_exact(out)



def _is_fixed_overnight_date_list_text(text):
    """True for finite listed-date overnight rows, not recurring daily schedules."""
    s = normalize_interval_parse_text(text) or ""
    if not s:
        return False
    texpr = time_expr_regex()
    mpat = month_regex()
    m = re.search(
        rf"\bovernight\b[^.;]*?(?:from\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*(?:on|on\s+the\s+following\s+dates?)\s*:?\s*(.+?)(?:\.|$)",
        s,
        flags=re.I,
    )
    if not m:
        return False
    tail = m.group(3)
    if re.search(r"\b(daily|each\s+night|every\s+night|from\s+%s\s+\d{1,2}\s*(?:-|to|through|until))\b" % mpat, tail, flags=re.I):
        return False
    date_tokens = list(re.finditer(rf"(?:{mpat}\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*{PARSER_YEAR_REGEX})?", tail, flags=re.I))
    return len(date_tokens) >= 2


def extract_overnight_on_date_list_intervals(row, base_year):
    """Parse finite overnight date lists such as 'closed overnight from 1700-0800 on: April 12, 13, 17, 18'.

    These are explicit listed closure-start dates, not a continuous daily schedule
    from the first to last listed date.
    """
    text = row.get("bed_or_space_reduction_text") if hasattr(row, "get") else row
    s = normalize_interval_parse_text(text) or ""
    if not s or not _is_fixed_overnight_date_list_text(s):
        return []
    texpr = time_expr_regex()
    mpat = month_regex()
    m = re.search(
        rf"\bovernight\b[^.;]*?(?:from\s*)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})\s*(?:on|on\s+the\s+following\s+dates?)\s*:?\s*(.+?)(?:\.|$)",
        s,
        flags=re.I,
    )
    if not m:
        return []
    start_minutes = parse_time_token(m.group(1))
    end_minutes = parse_time_token(m.group(2))
    if start_minutes is None or end_minutes is None:
        return []
    tail = m.group(3)
    current_month = None
    current_year = _anchor_year_for_unyeared_interval(row, base_year)
    prev_month_num = None
    out = []
    for dm in re.finditer(rf"(?:({mpat})\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*({PARSER_YEAR_REGEX}))?", tail, flags=re.I):
        mon = dm.group(1) or current_month
        day = dm.group(2)
        year_txt = dm.group(3)
        if not mon:
            continue
        mn = MONTHS.get(str(mon).lower())
        if mn is None:
            continue
        if year_txt:
            current_year = int(year_txt)
        elif prev_month_num is not None and mn < prev_month_num:
            current_year += 1
        prev_month_num = mn
        current_month = mon
        hh1, mm1 = divmod(start_minutes, 60)
        hh2, mm2 = divmod(end_minutes, 60)
        try:
            st = pd.Timestamp(year=int(current_year), month=int(mn), day=int(day), hour=hh1, minute=mm1)
            en = pd.Timestamp(year=int(current_year), month=int(mn), day=int(day), hour=hh2, minute=mm2)
            if en <= st:
                en += pd.Timedelta(days=1)
        except Exception:
            continue
        out.append({
            "interval_start": st,
            "interval_end": en,
            "interval_method": "narrative_overnight_explicit",
            "interval_quality": "exact_text_interval",
        })
    return dedupe_intervals_exact(out)


def build_episode_intervals(row):
    """
    Build intervals for one episode using a tiered-but-combinable approach.

    Explicit intervals remain primary/high-confidence. Unlike earlier v84 drafts, all
    explicit parsers are allowed to contribute because AHS notices can mix formats in the
    same text block (for example beginning/resume plus parenthetical intervals, or natural
    language weekday-date intervals plus same-day closures). Exact duplicate start/end
    intervals are collapsed after all explicit parsers run. Schedule-derived intervals are
    retained only for residual time not already covered by explicit intervals.
    """
    manual_intervals = extract_manual_fixed_interval(row)
    if manual_intervals:
        return dedupe_intervals_exact(manual_intervals)

    base_year = base_year_for_row(row)
    text = row.get("bed_or_space_reduction_text") or ""
    midnight_preamble_daily_list = bool(re.search(r"\bclosed\s+midnight\s*(?:-|to|until)\s*8(?::00)?\s*a\.?m\.?", str(text), flags=re.I))

    explicit_parsers = [
        ("extract_colon_window_date_pair_intervals", lambda: extract_colon_window_date_pair_intervals(row, base_year)),
        ("extract_comma_date_time_list_intervals", lambda: extract_comma_date_time_list_intervals(row, base_year)),
        ("extract_ambiguous_1201_to_morning_intervals", lambda: extract_ambiguous_1201_to_morning_intervals(row, base_year)),
        ("extract_overnight_on_date_list_intervals", lambda: extract_overnight_on_date_list_intervals(row, base_year)),
        ("extract_between_cross_date_intervals", lambda: extract_between_cross_date_intervals(row, base_year)),
        ("extract_explicit_dated_natural_language_intervals_v2", lambda: extract_explicit_dated_natural_language_intervals_v2(row, base_year)),
        ("extract_explicit_dated_natural_language_intervals", lambda: extract_explicit_dated_natural_language_intervals(row, base_year)),
        ("extract_weekday_dated_explicit_intervals", lambda: extract_weekday_dated_explicit_intervals(row, base_year)),
        ("extract_multi_intervals", lambda: extract_multi_intervals(text, base_year)),
        ("extract_single_day_interval", lambda: extract_single_day_interval(text, base_year)),
        ("extract_explicit_closure_variants_v74", lambda: extract_explicit_closure_variants_v74(row, base_year)),
        ("extract_duration_shorthand_intervals", lambda: extract_duration_shorthand_intervals(row, base_year)),
        ("extract_timed_date_list_intervals_v75", lambda: extract_timed_date_list_intervals_v75(row, base_year)),
        ("extract_begin_resume_interval", lambda: extract_begin_resume_interval(text, base_year)),
        ("extract_shared_window_date_pair_intervals", lambda: extract_shared_window_date_pair_intervals(row, base_year)),
        ("extract_weekday_time_date_list_intervals", lambda: extract_weekday_time_date_list_intervals(row, base_year)),
        ("extract_narrative_overnight_intervals", lambda: extract_narrative_overnight_intervals(row, base_year)),
        ("extract_full_closure_addon_intervals", lambda: extract_full_closure_addon_intervals(row, base_year)),
        ("extract_notice_custom_intervals_v51", lambda: extract_notice_custom_intervals_v51(row, base_year)),
    ]

    adjusted_open_hours_complement_only = is_adjusted_open_hours_complement_text(text)
    schedule_parsers = [
        ("extract_adjusted_open_hours_complement_intervals", lambda: extract_adjusted_open_hours_complement_intervals(row)),
        ("extract_midnight_preamble_daily_list_intervals", lambda: extract_midnight_preamble_daily_list_intervals(row)),
        ("extract_overnight_hours_bounded_by_begin_resume", lambda: extract_overnight_hours_bounded_by_begin_resume(row, base_year)),
        ("extract_parenthetical_date_range_window_intervals", lambda: extract_parenthetical_date_range_window_intervals(row)),
        ("extract_closed_nightly_plus_weekly_range_intervals", lambda: extract_closed_nightly_plus_weekly_range_intervals(row)),
        ("extract_weekday_range_window_between_intervals", lambda: extract_weekday_range_window_between_intervals(row)),
        ("extract_listed_weekday_multi_window_intervals", lambda: extract_listed_weekday_multi_window_intervals(row)),
        ("extract_listed_weekday_closure_intervals", lambda: extract_listed_weekday_closure_intervals(row)),
        ("extract_listed_weekday_full_day_intervals", lambda: extract_listed_weekday_full_day_intervals(row)),
        ("extract_weekend_schedule_intervals", lambda: extract_weekend_schedule_intervals(row)),
        ("extract_compound_weekly_schedule_intervals", lambda: extract_compound_weekly_schedule_intervals(row)),
        ("extract_listed_weekday_same_window_intervals", lambda: extract_listed_weekday_same_window_intervals(row)),
        ("extract_weekday_named_range_intervals", lambda: extract_weekday_named_range_intervals(row)),
        ("extract_text_anchored_bounded_daily_window_intervals", lambda: extract_text_anchored_bounded_daily_window_intervals(row)),
        ("extract_multi_range_daily_window_intervals", lambda: extract_multi_range_daily_window_intervals(row)),
        ("extract_daily_window_intervals", lambda: extract_daily_window_intervals(row)),
        ("extract_weeknight_intervals", lambda: extract_weeknight_intervals(row)),
        ("extract_phase_change_day_open_intervals", lambda: extract_phase_change_day_open_intervals(row, base_year)),
        ("extract_generic_timed_window_intervals", lambda: extract_generic_timed_window_intervals(row)),
        ("extract_regular_hours_closure_intervals", lambda: extract_regular_hours_closure_intervals(row)),
        ("extract_narrative_overnight_intervals", lambda: extract_narrative_overnight_intervals(row, base_year)),
        ("extract_full_closure_addon_intervals", lambda: extract_full_closure_addon_intervals(row, base_year)),
    ]

    explicit_intervals = []
    for parser_name, parser in explicit_parsers:
        try:
            candidate = parser() or []
        except Exception as e:
            log_parser_exception(row, f"build_episode_intervals.{parser_name}", e)
            candidate = []
        candidate = [iv for iv in candidate if iv.get("interval_method") in HIGH_CONF_METHODS]
        explicit_intervals.extend(candidate)
    # Prefer dedicated list parsers for source patterns where broader regexes can overexpand.
    colon_pair_intervals = extract_colon_window_date_pair_intervals(row, base_year)
    if colon_pair_intervals:
        explicit_intervals = colon_pair_intervals
    else:
        comma_list_intervals = extract_comma_date_time_list_intervals(row, base_year)
        if comma_list_intervals and re.search(r"no\s+on[- ]?site\s+physician\s+coverage", str(text), flags=re.I):
            explicit_intervals = comma_list_intervals
    explicit_intervals = _subtract_functional_day_shift(row, explicit_intervals)
    explicit_intervals = _roll_forward_early_year_intervals_for_late_year_notice(row, explicit_intervals)
    explicit_intervals = _realign_stale_unyear_explicit_intervals(row, explicit_intervals)
    explicit_intervals = _drop_explicit_intervals_outside_declared_episode_bounds(row, explicit_intervals)
    explicit_intervals = dedupe_intervals_exact(explicit_intervals)
    explicit_intervals = suppress_relative_today_weekday_artifacts(row, explicit_intervals)
    explicit_intervals = _drop_midnight_preamble_crossdate_artifacts(row, explicit_intervals)
    if midnight_preamble_daily_list:
        # In these rows, broad explicit date/time regexes can misread the preamble
        # and list dates. Use the dedicated daily-list schedule parser instead.
        explicit_intervals = []
    # Notice-custom regexes are fallback helpers. If standard explicit parsers found any
    # fixed intervals, do not let broad notice_custom patterns add extra intervals that may
    # simply be reinterpreting a reopen date/time as a new closure.
    if any(not str(iv.get("interval_method", "")).startswith("notice_custom_") for iv in explicit_intervals):
        explicit_intervals = [iv for iv in explicit_intervals if not str(iv.get("interval_method", "")).startswith("notice_custom_")]

    # Do not treat begin/resume bracket dates as one continuous closure when the same
    # source says the actual closure is a recurring overnight-hours window.
    if explicit_intervals and re.search(r"closed\s+overnight[^.;()]*hours?\s+of", str(text), flags=re.I):
        explicit_intervals = [iv for iv in explicit_intervals if iv.get("interval_method") != "begin_resume"]

    # If fixed explicit intervals were found and the text does not contain recurrence
    # cues, do not let schedule parsers reinterpret singular weekday+date wording as
    # recurring weekly closures. This prevents extra Friday/Wednesday schedule rows
    # from natural-language notices such as "Noon to 5 p.m. on Friday, May 29".
    if explicit_intervals and _is_fixed_overnight_date_list_text(text):
        return explicit_intervals

    if explicit_intervals and not _has_schedule_recurrence_cue(text) and not midnight_preamble_daily_list and not adjusted_open_hours_complement_only:
        return explicit_intervals

    schedule_intervals = []
    for parser_name, parser in schedule_parsers:
        if adjusted_open_hours_complement_only and parser_name != "extract_adjusted_open_hours_complement_intervals":
            continue
        try:
            schedule_intervals.extend(parser() or [])
        except Exception as e:
            log_parser_exception(row, f"build_episode_intervals.{parser_name}", e)
            continue
    schedule_intervals = [iv for iv in schedule_intervals if iv.get("interval_method") in ESTIMATED_METHODS]
    schedule_intervals = constrain_intervals_to_row_context(row, schedule_intervals)
    schedule_intervals = _apply_source_specific_schedule_bounds(row, schedule_intervals)
    schedule_intervals = _trim_initial_overnight_carry_in(row, schedule_intervals)
    schedule_intervals = _drop_weekly_named_terminal_endpoint_artifacts(row, schedule_intervals)
    schedule_intervals = _drop_service_resume_terminal_schedule_artifacts(row, schedule_intervals)
    schedule_intervals = dedupe_intervals_exact(schedule_intervals)
    explicit_intervals = constrain_intervals_to_row_context(row, explicit_intervals)
    explicit_intervals = _drop_parenthetical_range_endpoint_artifacts(row, explicit_intervals, schedule_intervals)

    if explicit_intervals and schedule_intervals:
        schedule_residual = subtract_intervals(schedule_intervals, explicit_intervals)
        combined = dedupe_intervals_exact(explicit_intervals + schedule_residual)
        if combined:
            return combined

    if explicit_intervals:
        return explicit_intervals

    if schedule_intervals:
        return schedule_intervals

    try:
        fallback_intervals = extract_fallback_datetime_range(row)
        if fallback_intervals:
            return dedupe_intervals_exact(fallback_intervals)
    except Exception as e:
        log_parser_exception(row, "build_episode_intervals.extract_fallback_datetime_range", e)

    return []


# ============================================================
# INTERVAL UNION
# ============================================================

def merge_intervals_for_group(df, start_col="interval_start_clipped", end_col="interval_end_clipped"):
    if df.empty:
        return []

    temp = df[[start_col, end_col]].copy()
    temp[start_col] = pd.to_datetime(temp[start_col], errors="coerce")
    temp[end_col] = pd.to_datetime(temp[end_col], errors="coerce")
    temp = temp.dropna().sort_values(start_col)

    merged = []
    for _, r in temp.iterrows():
        start = r[start_col]
        end = r[end_col]
        if end <= start:
            continue
        if not merged:
            merged.append([start, end])
        else:
            last_start, last_end = merged[-1]
            if start <= last_end:
                if end > last_end:
                    merged[-1][1] = end
            else:
                merged.append([start, end])
    return [(a, b) for a, b in merged]


def build_site_union_tables(active_intervals_df, suffix="all_methods"):
    if active_intervals_df.empty:
        merged_cols = [
            "site_id", "site_best", "merged_interval_index", "merged_interval_start",
            "merged_interval_end", "merged_interval_hours", "raw_interval_count_for_site",
            "raw_interval_hours_for_site", "summary_variant",
        ]
        summary_cols = [
            "site_id", "site_best", "merged_interval_count", "unioned_closure_hours",
            "raw_interval_count", "raw_interval_hours", "overlap_inflation_hours",
            "overlap_inflation_pct", "summary_variant",
        ]
        return pd.DataFrame(columns=merged_cols), pd.DataFrame(columns=summary_cols)

    merged_rows = []
    for (site_id, site_best), group in active_intervals_df.groupby(["site_id", "site_best"], dropna=False):
        merged = merge_intervals_for_group(group)
        raw_hours = group["hours_in_analysis_window"].sum()
        for i, (a, b) in enumerate(merged, start=1):
            merged_rows.append(
                {
                    "site_id": site_id,
                    "site_best": site_best,
                    "merged_interval_index": i,
                    "merged_interval_start": a,
                    "merged_interval_end": b,
                    "merged_interval_hours": (b - a).total_seconds() / 3600.0,
                    "raw_interval_count_for_site": len(group),
                    "raw_interval_hours_for_site": raw_hours,
                    "summary_variant": suffix,
                }
            )

    merged_df = pd.DataFrame(merged_rows)
    site_union_summary = (
        merged_df.groupby(["site_id", "site_best"], dropna=False)
        .agg(
            merged_interval_count=("merged_interval_index", "size"),
            unioned_closure_hours=("merged_interval_hours", "sum"),
        )
        .reset_index()
    )

    raw_summary = (
        active_intervals_df.groupby(["site_id", "site_best"], dropna=False)
        .agg(
            raw_interval_count=("hours_in_analysis_window", "size"),
            raw_interval_hours=("hours_in_analysis_window", "sum"),
        )
        .reset_index()
    )

    site_union_summary = site_union_summary.merge(raw_summary, on=["site_id", "site_best"], how="left")
    site_union_summary["overlap_inflation_hours"] = (
        site_union_summary["raw_interval_hours"] - site_union_summary["unioned_closure_hours"]
    )
    site_union_summary["overlap_inflation_pct"] = (
        site_union_summary["overlap_inflation_hours"] / site_union_summary["raw_interval_hours"] * 100.0
    )
    site_union_summary["summary_variant"] = suffix
    return merged_df, site_union_summary



UNCERTAIN_SCHEDULE_METHODS = {
    "regular_hours_complement",
    "regular_hours_only_closure",
    "listed_weekday_schedule",
    "listed_weekday_full_day_schedule",
    "daily_window_schedule",
    "weekday_night_schedule",
    "weekend_schedule",
    "overnight_date_range_schedule",
    "fallback_datetime_range",
}

def has_explicit_interval_cues(text):
    s = safe_lower(text)
    if not s:
        return False
    if re.search(rf"{month_regex()}\s+\d{{1,2}}\s*\(", s, flags=re.I):
        return True
    if "beginning" in s and "resume" in s:
        return True
    if "overnight on" in s or re.search(r"on\s+" + month_regex() + r"\s+\d", s, flags=re.I):
        return True
    return False

def assign_uncertainty_flags(ed_episodes):
    ed_episodes = ed_episodes.copy()
    flags = []
    reasons = []

    for _, r in ed_episodes.iterrows():
        method_str = str(r.get("interval_methods") or "")
        methods = {m.strip() for m in method_str.split(";") if m.strip()}
        text = r.get("bed_or_space_reduction_text")
        start_dt = parse_dt_any(r.get("start_date_parsed_clean"))
        end_dt = parse_dt_any(r.get("anticipated_end_date_parsed_clean"))
        span_days = None
        if not pd.isna(start_dt) and not pd.isna(end_dt):
            span_days = int((end_dt.normalize() - start_dt.normalize()).days)

        flag = False
        reason = None

        if any(m.endswith("_tbd_proxy") for m in methods):
            flag = True
            reason = "tbd_end_proxy"
        elif "fallback_datetime_range" in methods:
            flag = True
            reason = "fallback_method_used"
        elif methods and methods.issubset(UNCERTAIN_SCHEDULE_METHODS):
            if span_days is not None and span_days >= 180 and not has_explicit_interval_cues(text):
                flag = True
                reason = "long_running_schedule_only"
            elif ({"regular_hours_complement", "regular_hours_only_closure"} & methods) and span_days is not None and span_days >= 90:
                flag = True
                reason = "regular_hours_schedule_long_span"

        flags.append(flag)
        reasons.append(reason)

    ed_episodes["uncertainty_flag"] = flags
    ed_episodes["uncertainty_reason"] = reasons
    return ed_episodes


# ============================================================
# TABLE BUILDING
# ============================================================


# ============================================================
# SCHEDULE-STATE RECONCILIATION QA
# ============================================================

# Records are accumulated across per-year build_tables() calls and written by
# the wrapper. This layer is intentionally post-parser and pre-aggregation: the
# regex parsers still recover intervals normally, then this layer constrains
# long-running recurring schedule variants so later changed wording is not
# applied retroactively to the original historical start date.
SCHEDULE_STATE_RECONCILIATION_RECORDS = []


def reset_schedule_state_reconciliation_records():
    global SCHEDULE_STATE_RECONCILIATION_RECORDS
    SCHEDULE_STATE_RECONCILIATION_RECORDS = []


def get_schedule_state_reconciliation_records():
    return list(SCHEDULE_STATE_RECONCILIATION_RECORDS)


SCHEDULE_STATE_ELIGIBLE_METHODS = {
    "daily_window_schedule",
    "weekday_night_schedule",
    "regular_hours_complement",
    "regular_hours_only_closure",
    "weekend_schedule",
    "weekly_named_range_schedule",
    "overnight_date_range_schedule",
    "listed_weekday_schedule",
    "listed_weekday_full_day_schedule",
    "daily_window_schedule_tbd_proxy",
    "weekday_night_schedule_tbd_proxy",
    "regular_hours_complement_tbd_proxy",
    "regular_hours_only_closure_tbd_proxy",
    "weekend_schedule_tbd_proxy",
    "weekly_named_range_schedule_tbd_proxy",
    "overnight_date_range_schedule_tbd_proxy",
    "listed_weekday_schedule_tbd_proxy",
    "listed_weekday_full_day_schedule_tbd_proxy",
}


def _schedule_state_time_label(tok):
    mins = parse_time_token(tok)
    if mins is None:
        return None
    hh, mm = divmod(mins, 60)
    return f"{hh:02d}{mm:02d}"


def _schedule_state_first_time_range(s: str):
    texpr = time_expr_regex()
    m = re.search(rf"(?:from\s+)?({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})", s, flags=re.I)
    if not m:
        m = re.search(rf"\(\s*({texpr})\s*(?:-|–|—|to|until|through)\s*({texpr})\s*\)", s, flags=re.I)
    if not m:
        return None, None
    return _schedule_state_time_label(m.group(1)), _schedule_state_time_label(m.group(2))


def _schedule_state_signature_from_text(text):
    s = safe_lower(text)
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    texpr = time_expr_regex()

    # Open-hours complement wording: the listed hours are open hours; closures are
    # the complement. This catches Two Hills Jan-Mar 2024 and similar rows.
    if re.search(r"\bclosed\s+overnights?\s+and\s+weekends?\b", s, flags=re.I) and "operating hours" in s:
        m = re.search(rf"monday\s+to\s+friday\s+({texpr})\s*(?:-|to|–|—)\s*({texpr})", s, flags=re.I)
        if m:
            t1 = _schedule_state_time_label(m.group(1))
            t2 = _schedule_state_time_label(m.group(2))
            if t1 and t2:
                return f"open_complement:mon-fri:{t1}-{t2}:weekends_closed"

    # Combined weekday-night plus weekend-continuous schedule, including:
    # "Monday to Thursday, from 8pm to 8am ... Closed Fridays at 8pm through the weekend"
    # and "... and on Fridays from 5pm through the weekend (opening Mondays at 8am)".
    if ("weekend" in s and re.search(r"monday\s+to\s+thursday", s, flags=re.I)
            and re.search(r"fridays?", s, flags=re.I) and re.search(r"mondays?", s, flags=re.I)):
        prefix = s[: re.search(r"fridays?", s, flags=re.I).start()]
        nt1, nt2 = _schedule_state_first_time_range(prefix)
        m = re.search(
            rf"(?:closed\s+|and\s+)?(?:on\s+)?fridays?\s+(?:at|from)\s+({texpr}).{{0,120}}?(?:through|thru|over)\s+the\s+weekend.{{0,120}}?(?:re-?open(?:ing)?s?|open(?:ing)?s?)\s+mondays?\s+(?:at\s+)?({texpr})",
            s,
            flags=re.I,
        )
        if not m:
            m = re.search(
                rf"fridays?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until|through)\s*mondays?\s*\(\s*({texpr})\s*\)",
                s,
                flags=re.I,
            )
        if m and nt1 and nt2:
            ft = _schedule_state_time_label(m.group(1))
            mt = _schedule_state_time_label(m.group(2))
            if ft and mt:
                return f"mon-thu-night:{nt1}-{nt2}|weekend:fri-{ft}-mon-{mt}"

    # Weekend-only recurring closure.
    if "weekend" in s and re.search(r"fridays?", s, flags=re.I) and re.search(r"mondays?", s, flags=re.I):
        m = re.search(
            rf"(?:closed\s+|and\s+)?(?:on\s+)?fridays?\s+(?:at|from)\s+({texpr}).{{0,120}}?(?:through|thru|over)\s+the\s+weekend.{{0,120}}?(?:re-?open(?:ing)?s?|open(?:ing)?s?)\s+mondays?\s+(?:at\s+)?({texpr})",
            s,
            flags=re.I,
        )
        if not m:
            m = re.search(
                rf"fridays?\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until|through)\s*mondays?\s*\(\s*({texpr})\s*\)",
                s,
                flags=re.I,
            )
        if m:
            ft = _schedule_state_time_label(m.group(1))
            mt = _schedule_state_time_label(m.group(2))
            if ft and mt:
                return f"weekend:fri-{ft}-mon-{mt}"

    # Weekday night/range recurring schedule.
    m = re.search(
        rf"monday\s+(?:to|through|until|-)\s+thursday.*?(?:from\s+)?({texpr})\s*(?:-|–|—|to|until)\s*({texpr})",
        s,
        flags=re.I,
    )
    if m:
        t1 = _schedule_state_time_label(m.group(1))
        t2 = _schedule_state_time_label(m.group(2))
        if t1 and t2:
            return f"mon-thu-night:{t1}-{t2}"

    m = re.search(
        rf"monday\s*\(\s*({texpr})\s*\)\s*(?:-|–|—|to|until|through)\s*thursday\s*\(\s*({texpr})\s*\)",
        s,
        flags=re.I,
    )
    if m:
        t1 = _schedule_state_time_label(m.group(1))
        t2 = _schedule_state_time_label(m.group(2))
        if t1 and t2:
            return f"mon-thu-weekly-range:{t1}-{t2}"

    # Stable daily overnight schedules. These signatures mainly prevent false-positive
    # clipping for rows with wording extensions around the same base schedule.
    if "daily" in s or "each night" in s or "overnight" in s:
        t1, t2 = _schedule_state_first_time_range(s)
        if t1 and t2:
            return f"daily-or-overnight:{t1}-{t2}"

    return None


def _schedule_state_episode_key(row):
    key_cols = [
        "site_id",
        "program_or_service",
        "closure_mode",
        "reason_group",
        "start_date_text",
        "anticipated_end_date_text",
        "bed_or_space_reduction_text",
    ]
    return "||".join(str(row.get(c, "")) for c in key_cols)


def _schedule_state_family_key(row):
    # Use site/service rather than exact start date. The Two Hills failure mode
    # included overlapping schedule states with different start dates (Jan 2 vs
    # Mar 18) as well as later variants that retained an old start date. Grouping
    # by site/service lets the state layer clip both forms while still requiring
    # contradictory normalized schedule signatures before any adjustment occurs.
    return "||".join([
        str(row.get("site_id", "")),
        str(row.get("program_or_service", "")),
    ])


def _schedule_state_parsed_start(start_text):
    dt = parse_clean_date_field(start_text)
    return dt.normalize() if pd.notna(dt) else pd.NaT


def _schedule_state_dates_for_episode(row):
    parsed_start = _schedule_state_parsed_start(row.get("start_date_text"))
    first_seen = pd.to_datetime(row.get("first_seen_snapshot_date"), errors="coerce")
    first_seen = first_seen.normalize() if pd.notna(first_seen) else pd.NaT
    start_gap_days = pd.NA
    if pd.notna(parsed_start) and pd.notna(first_seen):
        start_gap_days = int((first_seen - parsed_start).days)
    return parsed_start, first_seen, start_gap_days


def _schedule_state_effective_start(start_text, first_seen, is_first_state=False, previous_state_start=None):
    """Choose the effective date for a recurring schedule state.

    Ordering of states is based on first observation, but the effective start is
    not always the first observation. The first schedule state in a family should
    usually trust the source's stated start date even if the first archived
    snapshot is later (Swan Hills 2022). Later contradictory states should avoid
    retroactively applying stale historical start dates (Two Hills 2024/2025),
    but can trust recent explicit starts close to the first observation.
    """
    parsed_start = _schedule_state_parsed_start(start_text)
    first_seen_ts = pd.to_datetime(first_seen, errors="coerce")
    first_day = first_seen_ts.normalize() if pd.notna(first_seen_ts) else pd.NaT
    if pd.isna(parsed_start):
        return first_day, "no_parsed_start_use_first_seen", pd.NA
    if pd.isna(first_day):
        return parsed_start, "no_first_seen_use_parsed_start", pd.NA
    gap_days = int((first_day - parsed_start).days)
    if is_first_state:
        return parsed_start, "first_state_use_parsed_start", gap_days
    if gap_days <= 21 and (previous_state_start is None or pd.isna(previous_state_start) or parsed_start > pd.Timestamp(previous_state_start)):
        return parsed_start, "later_state_recent_explicit_start", gap_days
    return first_day, "later_state_stale_start_use_first_seen", gap_days


def apply_schedule_state_reconciliation(intervals_df, analysis_start, analysis_end_excl):
    """Clip recurring schedule intervals when long-running schedule wording changes.

    This is intentionally conservative. It only adjusts estimated recurring schedule
    intervals with a recognized normalized schedule signature, and only inside a
    same-site/same-service/same-start family where more than one contradictory base
    schedule signature appears over time. Explicit fixed intervals and manual fixed
    intervals are not clipped by this layer.
    """
    if intervals_df is None or intervals_df.empty:
        return intervals_df

    df = intervals_df.copy()
    analysis_year = int(pd.Timestamp(analysis_start).year)
    for col in ["interval_start", "interval_end", "first_seen_snapshot_date", "last_seen_snapshot_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    df["schedule_state_signature"] = df.apply(lambda r: _schedule_state_signature_from_text(r.get("bed_or_space_reduction_text")), axis=1)
    df["schedule_state_episode_key"] = df.apply(_schedule_state_episode_key, axis=1)
    df["schedule_state_family_key"] = df.apply(_schedule_state_family_key, axis=1)
    method_series = df.get("interval_method", pd.Series(index=df.index, dtype="object")).astype(str)
    confidence_series = df.get("interval_confidence", pd.Series(index=df.index, dtype="object")).astype(str)
    manual_series = df.get("is_manual_add", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    eligible = (
        method_series.isin(SCHEDULE_STATE_ELIGIBLE_METHODS)
        & confidence_series.eq("estimated")
        & df["schedule_state_signature"].notna()
        & (~manual_series)
    )
    df["schedule_state_eligible"] = eligible

    if not bool(eligible.any()):
        return df

    ep_cols = [
        "schedule_state_family_key",
        "schedule_state_episode_key",
        "site_id",
        "site_best",
        "program_or_service",
        "start_date_text",
        "anticipated_end_date_text",
        "bed_or_space_reduction_text",
        "schedule_state_signature",
    ]
    eps = (
        df.loc[eligible, ep_cols + ["first_seen_snapshot_date", "last_seen_snapshot_date", "interval_method"]]
        .drop_duplicates()
        .groupby(ep_cols, dropna=False, as_index=False)
        .agg(
            first_seen_snapshot_date=("first_seen_snapshot_date", "min"),
            last_seen_snapshot_date=("last_seen_snapshot_date", "max"),
            interval_methods=("interval_method", lambda s: "; ".join(sorted(set(str(x) for x in s if pd.notna(x))))),
        )
    )
    if eps.empty:
        return df

    eps["parsed_start_date"] = eps.apply(lambda r: _schedule_state_parsed_start(r.get("start_date_text")), axis=1)
    eps["state_order_date"] = pd.to_datetime(eps["first_seen_snapshot_date"], errors="coerce")
    eps.loc[eps["state_order_date"].isna(), "state_order_date"] = eps.loc[eps["state_order_date"].isna(), "parsed_start_date"]
    eps = eps[pd.notna(eps["state_order_date"])].copy()
    if eps.empty:
        return df

    episode_window = {}
    for family_key, fam in eps.groupby("schedule_state_family_key", dropna=False):
        distinct_sigs = sorted(set(str(x) for x in fam["schedule_state_signature"].dropna()))
        if len(distinct_sigs) <= 1:
            continue
        fam = fam.sort_values(["state_order_date", "parsed_start_date", "schedule_state_signature"]).reset_index(drop=True)
        obs_span_days = (fam["state_order_date"].max() - fam["state_order_date"].min()).days
        if obs_span_days < 7:
            continue

        segments = []
        previous_state_start = pd.NaT
        for fam_pos, er in fam.iterrows():
            sig = str(er["schedule_state_signature"])
            is_first_state = len(segments) == 0
            st, start_rule, start_gap_days = _schedule_state_effective_start(
                er.get("start_date_text"),
                er.get("first_seen_snapshot_date"),
                is_first_state=is_first_state,
                previous_state_start=previous_state_start,
            )
            if pd.isna(st):
                continue
            st = pd.Timestamp(st)
            if segments and segments[-1]["signature"] == sig:
                segments[-1]["episode_keys"].append(er["schedule_state_episode_key"])
                segments[-1]["episode_count"] += 1
                segments[-1]["last_seen_snapshot_date"] = max(segments[-1]["last_seen_snapshot_date"], pd.Timestamp(er["last_seen_snapshot_date"]) if pd.notna(er["last_seen_snapshot_date"]) else segments[-1]["last_seen_snapshot_date"])
                # Preserve the earliest effective start for repeated observations of the same state.
                if st < segments[-1]["state_start"]:
                    segments[-1]["state_start"] = st
                    segments[-1]["state_effective_start_rule"] = start_rule
                    segments[-1]["start_gap_days"] = start_gap_days
            else:
                segments.append({
                    "signature": sig,
                    "state_start": st,
                    "state_order_date": er.get("state_order_date"),
                    "parsed_start_date": er.get("parsed_start_date"),
                    "state_effective_start_rule": start_rule,
                    "start_gap_days": start_gap_days,
                    "episode_keys": [er["schedule_state_episode_key"]],
                    "episode_count": 1,
                    "site_id": er.get("site_id"),
                    "site_best": er.get("site_best"),
                    "program_or_service": er.get("program_or_service"),
                    "first_seen_snapshot_date": er.get("first_seen_snapshot_date"),
                    "last_seen_snapshot_date": er.get("last_seen_snapshot_date"),
                })
                previous_state_start = st
        if len(segments) <= 1:
            continue

        for i, seg in enumerate(segments):
            state_start = pd.Timestamp(seg["state_start"])
            state_end = pd.Timestamp(segments[i + 1]["state_start"]) if i + 1 < len(segments) else pd.NaT
            for ep_key in seg["episode_keys"]:
                episode_window[ep_key] = {
                    "state_start": state_start,
                    "state_end": state_end,
                    "signature": seg["signature"],
                    "family_key": family_key,
                    "state_order_date": seg.get("state_order_date"),
                    "parsed_start_date": seg.get("parsed_start_date"),
                    "start_gap_days": seg.get("start_gap_days"),
                    "state_effective_start_rule": seg.get("state_effective_start_rule"),
                }
            SCHEDULE_STATE_RECONCILIATION_RECORDS.append({
                "record_type": "family_state",
                "analysis_year": analysis_year,
                "schedule_state_family_key": family_key,
                "site_id": seg.get("site_id"),
                "site_best": seg.get("site_best"),
                "program_or_service": seg.get("program_or_service"),
                "state_sequence": i + 1,
                "schedule_state_signature": seg["signature"],
                "state_effective_start": state_start,
                "state_effective_end": state_end,
                "state_order_date": seg.get("state_order_date"),
                "parsed_start_date": seg.get("parsed_start_date"),
                "start_gap_days": seg.get("start_gap_days"),
                "state_effective_start_rule": seg.get("state_effective_start_rule"),
                "episode_count_in_state": seg.get("episode_count"),
                "distinct_signatures_in_family": " | ".join(distinct_sigs),
                "observation_span_days": obs_span_days,
            })

    if not episode_window:
        return df

    changed_any = False
    for idx, r in df.loc[eligible].iterrows():
        ep_key = r.get("schedule_state_episode_key")
        if ep_key not in episode_window:
            continue
        win = episode_window[ep_key]
        state_start = win.get("state_start")
        state_end = win.get("state_end")
        state_sig = win.get("signature")
        family_key = win.get("family_key")
        old_start = pd.to_datetime(r.get("interval_start"), errors="coerce")
        old_end = pd.to_datetime(r.get("interval_end"), errors="coerce")
        if pd.isna(old_start) or pd.isna(old_end):
            continue
        new_start = max(old_start, state_start) if pd.notna(state_start) else old_start
        new_end = min(old_end, state_end) if pd.notna(state_end) else old_end
        if new_end <= new_start:
            new_start = pd.NaT
            new_end = pd.NaT
        if (pd.isna(new_start) or pd.isna(new_end) or new_start != old_start or new_end != old_end):
            changed_any = True
            old_overlap = overlap_hours(old_start, old_end, analysis_start, analysis_end_excl)
            new_overlap = 0.0 if pd.isna(new_start) or pd.isna(new_end) else overlap_hours(new_start, new_end, analysis_start, analysis_end_excl)
            df.at[idx, "schedule_state_original_interval_start"] = old_start
            df.at[idx, "schedule_state_original_interval_end"] = old_end
            df.at[idx, "schedule_state_effective_start"] = state_start
            df.at[idx, "schedule_state_effective_end"] = state_end
            df.at[idx, "schedule_state_adjustment"] = "removed" if new_overlap <= 0 else "trimmed"
            df.at[idx, "schedule_state_hours_removed_in_analysis_window"] = float(old_overlap - new_overlap)
            df.at[idx, "schedule_state_order_date"] = win.get("state_order_date")
            df.at[idx, "schedule_state_parsed_start_date"] = win.get("parsed_start_date")
            df.at[idx, "schedule_state_start_gap_days"] = win.get("start_gap_days")
            df.at[idx, "schedule_state_effective_start_rule"] = win.get("state_effective_start_rule")
            SCHEDULE_STATE_RECONCILIATION_RECORDS.append({
                "record_type": "interval_adjustment",
                "analysis_year": analysis_year,
                "schedule_state_family_key": family_key,
                "schedule_state_episode_key": ep_key,
                "site_id": r.get("site_id"),
                "site_best": r.get("site_best"),
                "program_or_service": r.get("program_or_service"),
                "start_date_text": r.get("start_date_text"),
                "anticipated_end_date_text": r.get("anticipated_end_date_text"),
                "bed_or_space_reduction_text": r.get("bed_or_space_reduction_text"),
                "interval_method": r.get("interval_method"),
                "schedule_state_signature": state_sig,
                "state_effective_start": state_start,
                "state_effective_end": state_end,
                "state_order_date": win.get("state_order_date"),
                "parsed_start_date": win.get("parsed_start_date"),
                "start_gap_days": win.get("start_gap_days"),
                "state_effective_start_rule": win.get("state_effective_start_rule"),
                "original_interval_start": old_start,
                "original_interval_end": old_end,
                "adjusted_interval_start": new_start,
                "adjusted_interval_end": new_end,
                "original_hours_in_analysis_window": old_overlap,
                "adjusted_hours_in_analysis_window": new_overlap,
                "hours_removed_in_analysis_window": float(old_overlap - new_overlap),
                "adjustment": "removed" if new_overlap <= 0 else "trimmed",
            })
            df.at[idx, "interval_start"] = new_start
            df.at[idx, "interval_end"] = new_end

    if not changed_any:
        return df

    df = df[pd.notna(df["interval_start"]) & pd.notna(df["interval_end"]) & (df["interval_end"] > df["interval_start"])].copy()
    if df.empty:
        df["interval_hours_total"] = pd.Series(dtype="float64")
        df["hours_in_analysis_window"] = pd.Series(dtype="float64")
        df["active_in_analysis_window"] = pd.Series(dtype="bool")
        df["interval_start_clipped"] = pd.Series(dtype="datetime64[ns]")
        df["interval_end_clipped"] = pd.Series(dtype="datetime64[ns]")
        return df
    df["interval_hours_total"] = (df["interval_end"] - df["interval_start"]).dt.total_seconds() / 3600.0
    df["hours_in_analysis_window"] = df.apply(
        lambda r: overlap_hours(r["interval_start"], r["interval_end"], analysis_start, analysis_end_excl),
        axis=1,
    )
    df["active_in_analysis_window"] = df["hours_in_analysis_window"] > 0
    df["interval_start_clipped"] = df["interval_start"].apply(lambda x: max(x, analysis_start))
    df["interval_end_clipped"] = df["interval_end"].apply(lambda x: min(x, analysis_end_excl))
    return df

def build_tables(raw_df, analysis_start=None, analysis_end=None, alias_map=None, write_alias_template=True):
    raw_df = raw_df.copy()
    if "is_manual_add" not in raw_df.columns:
        raw_df["is_manual_add"] = False
    raw_df["is_manual_add"] = raw_df["is_manual_add"].fillna(False).astype(bool)
    if "manual_add_id" not in raw_df.columns:
        raw_df["manual_add_id"] = pd.NA
    if "manual_interval_start" not in raw_df.columns:
        raw_df["manual_interval_start"] = pd.NA
    if "manual_interval_end" not in raw_df.columns:
        raw_df["manual_interval_end"] = pd.NA
    analysis_start_str = analysis_start or ANALYSIS_START
    analysis_end_str = analysis_end or ANALYSIS_END

    for col in [
        "community_heading",
        "facility_name",
        "program_or_service",
        "bed_or_space_reduction_text",
        "reason_text",
        "start_date_text",
        "anticipated_end_date_text",
        "raw_block_text",
    ]:
        raw_df[col] = raw_df[col].map(clean_text)

    raw_df = explode_embedded_notice_rows(raw_df)
    raw_df = raw_df[~raw_df.apply(is_empty_notice_stub_row, axis=1)].copy()

    raw_df["raw_block_text"] = raw_df["raw_block_text"].map(sanitize_block_text)
    raw_df["start_date_parsed_clean"] = raw_df["start_date_text"].map(parse_clean_date_field)
    raw_df["anticipated_end_date_parsed_clean"] = raw_df["anticipated_end_date_text"].map(parse_clean_date_field)

    raw_df["is_emergency_department"] = raw_df["program_or_service"].map(is_emergency_department_service)

    ed_rows = raw_df[raw_df["is_emergency_department"]].copy()
    if not ed_rows.empty:
        ed_rows = ed_rows[~ed_rows.apply(is_scope_excluded_non_ed_burden_row, axis=1)].copy()

    ed_rows = apply_site_resolution(ed_rows)

    if alias_map is None:
        if write_alias_template:
            write_or_update_site_alias_template(ed_rows["site_id_raw"].dropna().unique())
        alias_map = load_site_aliases()

    ed_rows["site_id"] = ed_rows["site_id_raw"].map(lambda x: alias_map.get(x, x))
    ed_rows["site_best"] = ed_rows["site_id"]
    drop_cols = [c for c in ["site_resolution_fp", "has_any_usable_site_heading"] if c in ed_rows.columns]
    if drop_cols:
        ed_rows = ed_rows.drop(columns=drop_cols)

    ed_rows["reason_group"] = ed_rows["reason_text"].map(normalize_reason)
    ed_rows["closure_mode"] = ed_rows["bed_or_space_reduction_text"].map(infer_closure_mode)

    ed_rows["mentions_nursing_remain_on_site"] = ed_rows["raw_block_text"].fillna("").str.contains(
        "nursing staff remain on site|nurses will remain|nursing remain on site",
        case=False,
        regex=True,
    )
    ed_rows["mentions_virtual_physician"] = ed_rows["raw_block_text"].fillna("").str.contains(
        "virtual", case=False, regex=False
    )
    ed_rows["mentions_ems_rerouted"] = ed_rows["raw_block_text"].fillna("").str.contains(
        "ems", case=False, regex=False
    )

    episode_keys = [
        "site_id",
        "program_or_service",
        "closure_mode",
        "reason_group",
        "start_date_text",
        "anticipated_end_date_text",
        "bed_or_space_reduction_text",
    ]

    ed_episodes = (
        ed_rows.groupby(episode_keys, dropna=False)
        .agg(
            first_seen_snapshot_date=("snapshot_date", "min"),
            last_seen_snapshot_date=("snapshot_date", "max"),
            n_snapshots_seen=("snapshot_date", "nunique"),
            site_best=("site_best", "first"),
            site_best_raw_label=("site_best_raw_label", "first"),
            facility_name=("facility_name", "first"),
            community_heading=("community_heading", "first"),
            reason_text=("reason_text", "first"),
            raw_block_text=("raw_block_text", "first"),
            mentions_nursing_remain_on_site=("mentions_nursing_remain_on_site", "max"),
            mentions_virtual_physician=("mentions_virtual_physician", "max"),
            mentions_ems_rerouted=("mentions_ems_rerouted", "max"),
            start_date_parsed_clean=("start_date_parsed_clean", "first"),
            anticipated_end_date_parsed_clean=("anticipated_end_date_parsed_clean", "first"),
            is_manual_add=("is_manual_add", "max"),
            manual_add_id=("manual_add_id", "first"),
            manual_interval_start=("manual_interval_start", "first"),
            manual_interval_end=("manual_interval_end", "first"),
            snapshot_page_label=("snapshot_page_label", "first"),
            snapshot_url=("snapshot_url", "first"),
        )
        .reset_index()
    )

    snapshot_dates_sorted = sorted(pd.to_datetime(raw_df["snapshot_date"]).dropna().unique().tolist())
    next_snapshot_map = {}
    for i, dt in enumerate(snapshot_dates_sorted):
        ts = pd.Timestamp(dt)
        next_snapshot_map[ts] = pd.Timestamp(snapshot_dates_sorted[i + 1]) if i + 1 < len(snapshot_dates_sorted) else ts + pd.Timedelta(days=1)
    ed_episodes["inferred_end_date_from_snapshots"] = pd.to_datetime(ed_episodes["last_seen_snapshot_date"]).map(next_snapshot_map)
    ed_episodes["tbd_proxy_end_date"] = pd.to_datetime(ed_episodes["last_seen_snapshot_date"]) + pd.Timedelta(days=14)

    analysis_start_ts = pd.Timestamp(analysis_start_str)
    analysis_end_excl = pd.Timestamp(analysis_end_str) + pd.Timedelta(days=1)
    ed_episodes["_analysis_base_year"] = int(analysis_start_ts.year)

    interval_rows = []
    for _, row in ed_episodes.iterrows():
        intervals = build_episode_intervals(row)
        for iv in intervals:
            interval_rows.append(
                {
                    "site_id": row["site_id"],
                    "site_best": row["site_best"],
                    "site_best_raw_label": row["site_best_raw_label"],
                    "facility_name": row["facility_name"],
                    "community_heading": row["community_heading"],
                    "program_or_service": row["program_or_service"],
                    "closure_mode": row["closure_mode"],
                    "reason_group": row["reason_group"],
                    "reason_text": row["reason_text"],
                    "start_date_text": row["start_date_text"],
                    "anticipated_end_date_text": row["anticipated_end_date_text"],
                    "bed_or_space_reduction_text": row["bed_or_space_reduction_text"],
                    "first_seen_snapshot_date": row["first_seen_snapshot_date"],
                    "last_seen_snapshot_date": row["last_seen_snapshot_date"],
                    "n_snapshots_seen": row["n_snapshots_seen"],
                    "is_manual_add": row.get("is_manual_add"),
                    "manual_add_id": row.get("manual_add_id"),
                    "mentions_nursing_remain_on_site": row["mentions_nursing_remain_on_site"],
                    "mentions_virtual_physician": row["mentions_virtual_physician"],
                    "mentions_ems_rerouted": row["mentions_ems_rerouted"],
                    "interval_start": iv["interval_start"],
                    "interval_end": iv["interval_end"],
                    "interval_method": iv.get("interval_method"),
                    "interval_quality": iv.get("interval_quality") or _default_interval_quality(iv.get("interval_method")),
                    "interval_confidence": classify_interval_confidence(iv.get("interval_method")),
                }
            )

    intervals_df = pd.DataFrame(interval_rows)

    analysis_start = analysis_start_ts
    analysis_end_excl = analysis_end_excl

    if not intervals_df.empty:
        intervals_df["interval_start"] = pd.to_datetime(intervals_df["interval_start"])
        intervals_df["interval_end"] = pd.to_datetime(intervals_df["interval_end"])
        intervals_df["interval_hours_total"] = (
            intervals_df["interval_end"] - intervals_df["interval_start"]
        ).dt.total_seconds() / 3600.0

        intervals_df["hours_in_analysis_window"] = intervals_df.apply(
            lambda r: overlap_hours(
                r["interval_start"],
                r["interval_end"],
                analysis_start,
                analysis_end_excl,
            ),
            axis=1,
        )
        intervals_df["active_in_analysis_window"] = intervals_df["hours_in_analysis_window"] > 0
        intervals_df["interval_start_clipped"] = intervals_df["interval_start"].apply(lambda x: max(x, analysis_start))
        intervals_df["interval_end_clipped"] = intervals_df["interval_end"].apply(lambda x: min(x, analysis_end_excl))

        intervals_df = apply_schedule_state_reconciliation(intervals_df, analysis_start, analysis_end_excl)

        active_intervals_df = intervals_df[intervals_df["active_in_analysis_window"]].copy()
        active_intervals_df["analysis_year"] = analysis_start.year
        high_conf_intervals_df = active_intervals_df[active_intervals_df["interval_confidence"] == "high"].copy()
    else:
        active_intervals_df = pd.DataFrame()
        high_conf_intervals_df = pd.DataFrame()

    if not active_intervals_df.empty:
        episode_active_summary = (
            active_intervals_df.groupby(
                [
                    "site_id",
                    "program_or_service",
                    "closure_mode",
                    "reason_group",
                    "start_date_text",
                    "anticipated_end_date_text",
                    "bed_or_space_reduction_text",
                ],
                dropna=False,
            )
            .agg(
                active_interval_count=("hours_in_analysis_window", "size"),
                estimated_hours_in_analysis_window=("hours_in_analysis_window", "sum"),
                interval_methods=("interval_method", lambda s: "; ".join(sorted(set(s)))),
                interval_qualities=("interval_quality", lambda s: "; ".join(sorted(set(s)))),
                interval_confidences=("interval_confidence", lambda s: "; ".join(sorted(set(s)))),
                first_interval_in_window=("interval_start_clipped", "min"),
                last_interval_in_window=("interval_end_clipped", "max"),
            )
            .reset_index()
        )

        ed_episodes = ed_episodes.merge(
            episode_active_summary,
            on=[
                "site_id",
                "program_or_service",
                "closure_mode",
                "reason_group",
                "start_date_text",
                "anticipated_end_date_text",
                "bed_or_space_reduction_text",
            ],
            how="left",
        )
        ed_episodes["active_in_analysis_window"] = ed_episodes["estimated_hours_in_analysis_window"].fillna(0) > 0
    else:
        ed_episodes["active_in_analysis_window"] = False
        ed_episodes["active_interval_count"] = 0
        ed_episodes["estimated_hours_in_analysis_window"] = pd.NA
        ed_episodes["interval_methods"] = pd.NA
        ed_episodes["interval_qualities"] = pd.NA
        ed_episodes["interval_confidences"] = pd.NA
        ed_episodes["first_interval_in_window"] = pd.NaT
        ed_episodes["last_interval_in_window"] = pd.NaT

    ed_episodes_active = ed_episodes[ed_episodes["active_in_analysis_window"]].copy()
    ed_episodes_active = assign_uncertainty_flags(ed_episodes_active)

    # propagate uncertainty flags down to intervals for optional restricted sensitivity analyses
    uncertainty_keys = [
        "site_id",
        "program_or_service",
        "closure_mode",
        "reason_group",
        "start_date_text",
        "anticipated_end_date_text",
        "bed_or_space_reduction_text",
    ]
    uncertainty_map = ed_episodes_active[uncertainty_keys + ["uncertainty_flag", "uncertainty_reason"]].drop_duplicates()
    if not active_intervals_df.empty:
        active_intervals_df = active_intervals_df.merge(uncertainty_map, on=uncertainty_keys, how="left")
    if not high_conf_intervals_df.empty:
        high_conf_intervals_df = high_conf_intervals_df.merge(uncertainty_map, on=uncertainty_keys, how="left")

    active_intervals_restricted_df = active_intervals_df[~active_intervals_df["uncertainty_flag"].fillna(False)].copy() if not active_intervals_df.empty else pd.DataFrame()

    site_union_intervals_all, site_union_summary_all = build_site_union_tables(active_intervals_df, suffix="all_methods")
    site_union_intervals_restricted, site_union_summary_restricted = build_site_union_tables(active_intervals_restricted_df, suffix="restricted_all_methods")
    site_union_intervals_high, site_union_summary_high = build_site_union_tables(high_conf_intervals_df, suffix="high_confidence_only")

    if not active_intervals_df.empty:
        raw_site_summary = (
            active_intervals_df.groupby(["site_id", "site_best"], dropna=False)
            .agg(
                raw_interval_count=("hours_in_analysis_window", "size"),
                raw_interval_hours=("hours_in_analysis_window", "sum"),
                median_interval_hours=("hours_in_analysis_window", "median"),
                mean_interval_hours=("hours_in_analysis_window", "mean"),
                interval_methods=("interval_method", lambda s: "; ".join(sorted(set(s)))),
                interval_confidences=("interval_confidence", lambda s: "; ".join(sorted(set(s)))),
                any_virtual_support=("mentions_virtual_physician", "max"),
                any_ems_rerouted=("mentions_ems_rerouted", "max"),
                any_nursing_remained=("mentions_nursing_remain_on_site", "max"),
            )
            .reset_index()
        )

        high_site_summary = (
            high_conf_intervals_df.groupby(["site_id", "site_best"], dropna=False)
            .agg(
                high_conf_interval_count=("hours_in_analysis_window", "size"),
                high_conf_interval_hours=("hours_in_analysis_window", "sum"),
            )
            .reset_index()
        ) if not high_conf_intervals_df.empty else pd.DataFrame(
            columns=["site_id", "site_best", "high_conf_interval_count", "high_conf_interval_hours"]
        )

        active_episode_counts = (
            ed_episodes_active.groupby(["site_id", "site_best"], dropna=False)
            .size()
            .reset_index(name="active_episode_count")
        )

        site_year_summary = raw_site_summary.merge(active_episode_counts, on=["site_id", "site_best"], how="left")
        site_year_summary = site_year_summary.merge(
            site_union_summary_all[
                [
                    "site_id",
                    "site_best",
                    "merged_interval_count",
                    "unioned_closure_hours",
                    "overlap_inflation_hours",
                    "overlap_inflation_pct",
                ]
            ],
            on=["site_id", "site_best"],
            how="left",
        )
        site_year_summary = site_year_summary.merge(
            site_union_summary_high[
                [
                    "site_id",
                    "site_best",
                    "merged_interval_count",
                    "unioned_closure_hours",
                ]
            ].rename(
                columns={
                    "merged_interval_count": "high_conf_merged_interval_count",
                    "unioned_closure_hours": "high_conf_unioned_closure_hours",
                }
            ),
            on=["site_id", "site_best"],
            how="left",
        )
        site_year_summary = site_year_summary.merge(
            site_union_summary_restricted[
                [
                    "site_id",
                    "site_best",
                    "merged_interval_count",
                    "unioned_closure_hours",
                ]
            ].rename(
                columns={
                    "merged_interval_count": "restricted_merged_interval_count",
                    "unioned_closure_hours": "restricted_unioned_closure_hours",
                }
            ),
            on=["site_id", "site_best"],
            how="left",
        )
        site_year_summary = site_year_summary.merge(high_site_summary, on=["site_id", "site_best"], how="left")
        site_year_summary["analysis_year"] = analysis_start.year
    else:
        site_year_summary = pd.DataFrame()

    if not ed_episodes_active.empty:
        site_cause_summary = (
            ed_episodes_active.groupby(["site_id", "site_best", "reason_group"], dropna=False)
            .agg(
                active_episode_count=("site_id", "size"),
                notice_based_hours=("estimated_hours_in_analysis_window", "sum"),
            )
            .reset_index()
            .sort_values(["site_best", "active_episode_count"], ascending=[True, False])
        )
    else:
        site_cause_summary = pd.DataFrame()

    for _df in [ed_episodes, ed_episodes_active]:
        if isinstance(_df, pd.DataFrame):
            _drop_cols = [c for c in ["_analysis_base_year", "has_any_usable_site_heading"] if c in _df.columns]
            if _drop_cols:
                _df.drop(columns=_drop_cols, inplace=True)

    _episode_drop_cols = [c for c in ["_analysis_base_year", "has_any_usable_site_heading"] if c in ed_episodes.columns]
    if _episode_drop_cols:
        ed_episodes = ed_episodes.drop(columns=_episode_drop_cols)

    return (
        raw_df,
        ed_rows,
        ed_episodes,
        intervals_df,
        active_intervals_df,
        active_intervals_restricted_df,
        high_conf_intervals_df,
        ed_episodes_active,
        site_union_intervals_all,
        site_union_summary_all,
        site_union_intervals_restricted,
        site_union_summary_restricted,
        site_union_intervals_high,
        site_union_summary_high,
        site_year_summary,
        site_cause_summary,
    )


# ============================================================
# SITE METADATA
# ============================================================

def write_or_update_site_metadata_template(active_site_df):
    new_template = active_site_df[["site_id", "site_best", "facility_name", "community_heading"]].drop_duplicates().copy()
    new_template = new_template.sort_values(["site_best", "site_id"]).reset_index(drop=True)
    new_template["community_population"] = pd.NA
    new_template["rurality_auto"] = new_template["site_best"].map(guess_rurality_from_site)
    new_template["rurality"] = new_template["rurality_auto"]
    new_template["nearest_alternate_ed"] = pd.NA
    new_template["alternate_ed_drive_minutes"] = pd.NA
    new_template["notes"] = pd.NA

    if os.path.exists(SITE_METADATA_FILE):
        try:
            old = pd.read_csv(SITE_METADATA_FILE)
            old["site_id"] = old["site_id"].map(clean_text)

            keep_cols = [
                "site_id",
                "community_population",
                "rurality",
                "nearest_alternate_ed",
                "alternate_ed_drive_minutes",
                "notes",
            ]
            for col in keep_cols:
                if col not in old.columns:
                    old[col] = pd.NA

            merged = new_template.merge(
                old[keep_cols],
                on="site_id",
                how="left",
                suffixes=("", "_old"),
            )

            for col in ["community_population", "rurality", "nearest_alternate_ed", "alternate_ed_drive_minutes", "notes"]:
                if f"{col}_old" in merged.columns:
                    merged[col] = merged[f"{col}_old"].fillna(merged[col])
                    merged = merged.drop(columns=[f"{col}_old"])

            merged["community_population"] = pd.to_numeric(merged["community_population"], errors="coerce")
            merged["rurality_auto_from_population"] = merged["community_population"].map(guess_rurality_from_population)
            merged["rurality"] = merged["rurality"].fillna(merged["rurality_auto_from_population"])
            merged["rurality"] = merged["rurality"].fillna(merged["rurality_auto"])

            merged.to_csv(SITE_METADATA_FILE, index=False)
            return
        except Exception:
            pass

    new_template.to_csv(SITE_METADATA_FILE, index=False)


def enrich_with_site_metadata(ed_episodes_active, site_year_summary, site_union_summary_all, site_union_summary_high):
    if not os.path.exists(SITE_METADATA_FILE):
        return None, None, None, None, None

    meta = pd.read_csv(SITE_METADATA_FILE)
    if "site_id" not in meta.columns:
        return None, None, None, None, None

    meta["site_id"] = meta["site_id"].map(clean_text)
    if "community_population" not in meta.columns:
        meta["community_population"] = pd.NA
    if "rurality" not in meta.columns:
        meta["rurality"] = pd.NA
    if "site_best" not in meta.columns:
        meta["site_best"] = pd.NA

    meta["community_population"] = pd.to_numeric(meta["community_population"], errors="coerce")
    meta["rurality_auto_from_population"] = meta["community_population"].map(guess_rurality_from_population)
    meta["rurality_auto_from_site"] = meta["site_best"].map(guess_rurality_from_site)
    meta["rurality"] = meta["rurality"].fillna(meta["rurality_auto_from_population"])
    meta["rurality"] = meta["rurality"].fillna(meta["rurality_auto_from_site"])

    enriched_episodes = ed_episodes_active.merge(meta, on="site_id", how="left")
    site_year_enriched = site_year_summary.merge(meta, on="site_id", how="left")
    site_union_all_enriched = site_union_summary_all.merge(meta, on="site_id", how="left")
    site_union_high_enriched = site_union_summary_high.merge(meta, on="site_id", how="left")

    if not enriched_episodes.empty:
        rurality_summary = (
            enriched_episodes.groupby(["rurality"], dropna=False)
            .agg(
                active_episode_count=("site_id", "size"),
                notice_based_hours=("estimated_hours_in_analysis_window", "sum"),
                unique_sites=("site_id", "nunique"),
                episode_population_proxy_sum=("community_population", "sum"),
            )
            .reset_index()
        )

        rurality_union_all = (
            site_union_all_enriched.groupby(["rurality"], dropna=False)
            .agg(
                unique_sites=("site_id", "nunique"),
                unioned_closure_hours=("unioned_closure_hours", "sum"),
                raw_interval_hours=("raw_interval_hours", "sum"),
                unique_population_proxy_affected=("community_population", "sum"),
            )
            .reset_index()
        )

        rurality_union_high = (
            site_union_high_enriched.groupby(["rurality"], dropna=False)
            .agg(
                unique_sites=("site_id", "nunique"),
                high_conf_unioned_closure_hours=("unioned_closure_hours", "sum"),
                high_conf_raw_interval_hours=("raw_interval_hours", "sum"),
                unique_population_proxy_affected=("community_population", "sum"),
            )
            .reset_index()
        )
    else:
        rurality_summary = pd.DataFrame()
        rurality_union_all = pd.DataFrame()
        rurality_union_high = pd.DataFrame()

    return (
        enriched_episodes,
        site_year_enriched,
        rurality_summary,
        rurality_union_all,
        rurality_union_high,
    )


# ============================================================
# QA / VALIDATION OUTPUTS
# ============================================================

def run_qa_checks(
    ed_rows,
    active_intervals_df,
    high_conf_intervals_df,
    ed_episodes_active,
    site_union_summary_all,
    site_union_summary_high,
):
    checks = []

    raw_all = float(site_union_summary_all["raw_interval_hours"].sum()) if not site_union_summary_all.empty else 0.0
    union_all = float(site_union_summary_all["unioned_closure_hours"].sum()) if not site_union_summary_all.empty else 0.0

    raw_high = float(site_union_summary_high["raw_interval_hours"].sum()) if not site_union_summary_high.empty else 0.0
    union_high = float(site_union_summary_high["unioned_closure_hours"].sum()) if not site_union_summary_high.empty else 0.0

    notice_hours = float(ed_episodes_active["estimated_hours_in_analysis_window"].sum()) if not ed_episodes_active.empty else 0.0
    raw_active_interval_hours = float(active_intervals_df["hours_in_analysis_window"].sum()) if not active_intervals_df.empty else 0.0

    bad_site_rows = int(ed_rows["site_best_raw_label"].map(looks_like_date_string).sum()) if not ed_rows.empty else 0

    checks.append({"check": "all_union_le_raw", "pass": union_all <= raw_all + 1e-6, "value": union_all, "reference": raw_all})
    checks.append({"check": "high_union_le_raw", "pass": union_high <= raw_high + 1e-6, "value": union_high, "reference": raw_high})
    checks.append({"check": "high_union_le_all_union", "pass": union_high <= union_all + 1e-6, "value": union_high, "reference": union_all})
    checks.append({"check": "date_like_site_rows_zero", "pass": bad_site_rows == 0, "value": bad_site_rows, "reference": 0})
    checks.append({
        "check": "notice_hours_close_to_raw_active_interval_hours",
        "pass": abs(notice_hours - raw_active_interval_hours) < 1e-6,
        "value": notice_hours,
        "reference": raw_active_interval_hours,
    })

    qa_df = pd.DataFrame(checks)
    qa_df.to_csv(f"{OUTPUT_PREFIX}_qa_checks.csv", index=False)

    suspicious_patterns = re.compile(
        r"\b(william|george|theresa|complex|community|municipal|desmarais|big|district|berwyn)\b",
        flags=re.I,
    )

    unresolved = (
        ed_rows[["site_id", "site_best_raw_label", "facility_name", "community_heading"]]
        .drop_duplicates()
        .copy()
    )
    unresolved["suspicious"] = unresolved["site_id"].fillna("").str.contains(suspicious_patterns)
    unresolved = unresolved[unresolved["suspicious"]].sort_values("site_id")
    unresolved.to_csv(f"{OUTPUT_PREFIX}_qa_unresolved_sites.csv", index=False)

    if not active_intervals_df.empty:
        samples = []
        for method, group in active_intervals_df.groupby("interval_method"):
            n = min(20, len(group))
            samples.append(group.sample(n=n, random_state=42))
        audit_df = pd.concat(samples, ignore_index=True)
        audit_cols = [
            "site_id",
            "site_best",
            "interval_method",
            "interval_confidence",
            "bed_or_space_reduction_text",
            "start_date_text",
            "anticipated_end_date_text",
            "interval_start",
            "interval_end",
            "hours_in_analysis_window",
        ]
        audit_cols = [c for c in audit_cols if c in audit_df.columns]
        audit_df[audit_cols].to_csv(f"{OUTPUT_PREFIX}_qa_interval_audit_sample.csv", index=False)


# ============================================================
# MAIN
# ============================================================

def main():
    print("Finding archive pages...")
    archive_links = parse_archive_links()
    if archive_links.empty:
        print("No archive pages found in the requested scrape range.")
        sys.exit(1)

    print(f"Found {len(archive_links)} snapshot pages.")
    print(f"Scrape window:   {SCRAPE_START} to {SCRAPE_END}")
    print(f"Analysis window: {ANALYSIS_START} to {ANALYSIS_END}")

    all_rows = []
    failures = []

    for idx, row in archive_links.iterrows():
        snapshot_date = row["snapshot_date"]
        snapshot_url = row["snapshot_url"]
        try:
            df = scrape_snapshot(snapshot_date, snapshot_url)
            all_rows.append(df)
            print(f"[{idx + 1}/{len(archive_links)}] OK   {snapshot_date.date()}   rows={len(df)}")
        except Exception as e:
            failures.append(
                {
                    "snapshot_date": snapshot_date,
                    "snapshot_url": snapshot_url,
                    "error": str(e),
                }
            )
            print(f"[{idx + 1}/{len(archive_links)}] FAIL {snapshot_date.date()}   {e}")

    if not all_rows:
        print("No rows scraped.")
        sys.exit(1)

    raw_df = pd.concat(all_rows, ignore_index=True)
    if raw_df.empty:
        print("Scrape ran but returned no rows.")
        sys.exit(1)

    (
        raw_all_services,
        ed_rows,
        ed_episodes,
        intervals_df,
        active_intervals_df,
        active_intervals_restricted_df,
        high_conf_intervals_df,
        ed_episodes_active,
        site_union_intervals_all,
        site_union_summary_all,
        site_union_intervals_restricted,
        site_union_summary_restricted,
        site_union_intervals_high,
        site_union_summary_high,
        site_year_summary,
        site_cause_summary,
    ) = build_tables(raw_df)

    archive_links.to_csv(f"{OUTPUT_PREFIX}_archive_links.csv", index=False)
    raw_all_services.to_csv(f"{OUTPUT_PREFIX}_raw_all_services.csv", index=False)
    ed_rows.to_csv(f"{OUTPUT_PREFIX}_ed_rows.csv", index=False)
    ed_episodes.to_csv(f"{OUTPUT_PREFIX}_ed_episodes.csv", index=False)
    intervals_df.to_csv(f"{OUTPUT_PREFIX}_ed_intervals.csv", index=False)
    active_intervals_df.to_csv(f"{OUTPUT_PREFIX}_ed_intervals_active_window.csv", index=False)
    high_conf_intervals_df.to_csv(f"{OUTPUT_PREFIX}_ed_intervals_active_window_high_conf.csv", index=False)
    ed_episodes_active.to_csv(f"{OUTPUT_PREFIX}_ed_episodes_active_window.csv", index=False)

    uncertain_episodes_df = ed_episodes_active[ed_episodes_active["uncertainty_flag"].fillna(False)].copy() if "uncertainty_flag" in ed_episodes_active.columns else pd.DataFrame()
    uncertain_intervals_df = active_intervals_df[active_intervals_df["uncertainty_flag"].fillna(False)].copy() if (not active_intervals_df.empty and "uncertainty_flag" in active_intervals_df.columns) else pd.DataFrame()

    uncertain_episodes_df.to_csv(f"{OUTPUT_PREFIX}_ed_episodes_uncertain.csv", index=False)
    uncertain_intervals_df.to_csv(f"{OUTPUT_PREFIX}_ed_intervals_uncertain.csv", index=False)

    excluded_raw_hours = float(uncertain_intervals_df["hours_in_analysis_window"].sum()) if not uncertain_intervals_df.empty else 0.0
    all_union_hours = float(site_union_summary_all["unioned_closure_hours"].sum()) if not site_union_summary_all.empty else 0.0
    restricted_union_hours = float(site_union_summary_restricted["unioned_closure_hours"].sum()) if not site_union_summary_restricted.empty else 0.0
    excluded_union_hours = all_union_hours - restricted_union_hours

    uncertainty_summary_rows = [
        {
            "summary_level": "overall",
            "uncertainty_reason": "ALL",
            "active_episode_count": int(len(uncertain_episodes_df)),
            "active_interval_count": int(len(uncertain_intervals_df)),
            "excluded_notice_based_episode_hours": float(uncertain_episodes_df["estimated_hours_in_analysis_window"].sum()) if not uncertain_episodes_df.empty else 0.0,
            "excluded_raw_active_interval_hours": excluded_raw_hours,
            "excluded_unioned_site_level_hours": excluded_union_hours,
        }
    ]

    if not uncertain_episodes_df.empty:
        ep_by_reason = (
            uncertain_episodes_df.groupby("uncertainty_reason", dropna=False)
            .agg(
                active_episode_count=("site_id", "size"),
                excluded_notice_based_episode_hours=("estimated_hours_in_analysis_window", "sum"),
            )
            .reset_index()
        )
    else:
        ep_by_reason = pd.DataFrame(columns=["uncertainty_reason", "active_episode_count", "excluded_notice_based_episode_hours"])

    if not uncertain_intervals_df.empty:
        int_by_reason = (
            uncertain_intervals_df.groupby("uncertainty_reason", dropna=False)
            .agg(
                active_interval_count=("hours_in_analysis_window", "size"),
                excluded_raw_active_interval_hours=("hours_in_analysis_window", "sum"),
            )
            .reset_index()
        )
    else:
        int_by_reason = pd.DataFrame(columns=["uncertainty_reason", "active_interval_count", "excluded_raw_active_interval_hours"])

    by_reason = ep_by_reason.merge(int_by_reason, on="uncertainty_reason", how="outer").fillna(0)
    for _, r in by_reason.iterrows():
        uncertainty_summary_rows.append(
            {
                "summary_level": "uncertainty_reason",
                "uncertainty_reason": r["uncertainty_reason"],
                "active_episode_count": int(r.get("active_episode_count", 0)),
                "active_interval_count": int(r.get("active_interval_count", 0)),
                "excluded_notice_based_episode_hours": float(r.get("excluded_notice_based_episode_hours", 0.0)),
                "excluded_raw_active_interval_hours": float(r.get("excluded_raw_active_interval_hours", 0.0)),
                "excluded_unioned_site_level_hours": pd.NA,
            }
        )

    pd.DataFrame(uncertainty_summary_rows).to_csv(f"{OUTPUT_PREFIX}_uncertainty_summary.csv", index=False)
    site_union_intervals_all.to_csv(f"{OUTPUT_PREFIX}_ed_site_union_intervals.csv", index=False)
    site_union_summary_all.to_csv(f"{OUTPUT_PREFIX}_ed_site_union_summary.csv", index=False)
    site_union_intervals_restricted.to_csv(f"{OUTPUT_PREFIX}_ed_site_union_intervals_restricted.csv", index=False)
    site_union_summary_restricted.to_csv(f"{OUTPUT_PREFIX}_ed_site_union_summary_restricted.csv", index=False)
    site_union_intervals_high.to_csv(f"{OUTPUT_PREFIX}_ed_site_union_intervals_high_conf.csv", index=False)
    site_union_summary_high.to_csv(f"{OUTPUT_PREFIX}_ed_site_union_summary_high_conf.csv", index=False)
    site_year_summary.to_csv(f"{OUTPUT_PREFIX}_ed_site_year_summary.csv", index=False)
    site_cause_summary.to_csv(f"{OUTPUT_PREFIX}_ed_site_cause_summary.csv", index=False)

    if failures:
        pd.DataFrame(failures).to_csv(f"{OUTPUT_PREFIX}_failures.csv", index=False)

    write_or_update_site_metadata_template(ed_episodes_active)
    enriched = enrich_with_site_metadata(
        ed_episodes_active,
        site_year_summary,
        site_union_summary_all,
        site_union_summary_high,
    )

    if enriched[0] is not None:
        (
            enriched_episodes,
            site_year_enriched,
            rurality_summary,
            rurality_union_all,
            rurality_union_high,
        ) = enriched
        enriched_episodes.to_csv(f"{OUTPUT_PREFIX}_ed_episodes_active_window_enriched.csv", index=False)
        site_year_enriched.to_csv(f"{OUTPUT_PREFIX}_ed_site_year_summary_enriched.csv", index=False)
        rurality_summary.to_csv(f"{OUTPUT_PREFIX}_ed_rurality_summary.csv", index=False)
        rurality_union_all.to_csv(f"{OUTPUT_PREFIX}_ed_rurality_union_summary.csv", index=False)
        rurality_union_high.to_csv(f"{OUTPUT_PREFIX}_ed_rurality_union_summary_high_conf.csv", index=False)

    run_qa_checks(
        ed_rows,
        active_intervals_df,
        high_conf_intervals_df,
        ed_episodes_active,
        site_union_summary_all,
        site_union_summary_high,
    )

    print("\n=== SUMMARY ===")
    print(f"Snapshot pages scraped: {len(archive_links)}")
    print(f"Raw service blocks: {len(raw_all_services)}")
    print(f"ED rows: {len(ed_rows)}")
    print(f"Deduplicated ED episodes: {len(ed_episodes)}")
    print(f"ED episodes active in analysis window: {len(ed_episodes_active)}")
    if "uncertainty_flag" in ed_episodes_active.columns:
        uncertain_count = int(ed_episodes_active["uncertainty_flag"].fillna(False).sum())
        print(f"Uncertain active episodes flagged: {uncertain_count}")
        excluded_raw_hours = float(uncertain_intervals_df["hours_in_analysis_window"].sum()) if not uncertain_intervals_df.empty else 0.0
        print(f"Excluded raw active interval hours due to uncertainty: {excluded_raw_hours:.1f}")
    print(f"ED intervals total: {len(intervals_df)}")
    print(f"ED intervals active in analysis window: {len(active_intervals_df)}")
    print(f"High-confidence active intervals: {len(high_conf_intervals_df)}")

    if not ed_episodes_active.empty:
        notice_hours = ed_episodes_active["estimated_hours_in_analysis_window"].sum(skipna=True)
        print(f"Notice-based episode hours in analysis window: {notice_hours:.1f}")

    if not site_union_summary_all.empty:
        raw_hours = site_union_summary_all["raw_interval_hours"].sum(skipna=True)
        union_hours = site_union_summary_all["unioned_closure_hours"].sum(skipna=True)
        print(f"Raw active interval hours in analysis window: {raw_hours:.1f}")
        print(f"Unioned site-level closure hours in analysis window: {union_hours:.1f}")

    if not site_union_summary_restricted.empty:
        restricted_raw_hours = site_union_summary_restricted["raw_interval_hours"].sum(skipna=True)
        restricted_union_hours = site_union_summary_restricted["unioned_closure_hours"].sum(skipna=True)
        print(f"Restricted all-method raw active interval hours in analysis window: {restricted_raw_hours:.1f}")
        print(f"Restricted all-method unioned site-level closure hours in analysis window: {restricted_union_hours:.1f}")

    if not site_union_summary_high.empty:
        high_raw_hours = site_union_summary_high["raw_interval_hours"].sum(skipna=True)
        high_union_hours = site_union_summary_high["unioned_closure_hours"].sum(skipna=True)
        print(f"High-confidence raw active interval hours in analysis window: {high_raw_hours:.1f}")
        print(f"High-confidence unioned site-level closure hours in analysis window: {high_union_hours:.1f}")

    bad_site_rows = ed_rows["site_best_raw_label"].map(looks_like_date_string).sum()
    print(f"Rows where raw chosen site still looked date-like before aliasing: {int(bad_site_rows)}")

    if not site_year_summary.empty:
        print("\nTop sites by all-method unioned closure hours:")
        print(
            site_year_summary.sort_values("unioned_closure_hours", ascending=False)
            [["site_best", "active_episode_count", "unioned_closure_hours", "high_conf_unioned_closure_hours"]]
            .head(15)
            .to_string(index=False)
        )

    if not active_intervals_df.empty:
        print("\nInterval confidence counts:")
        print(active_intervals_df["interval_confidence"].value_counts(dropna=False).to_string())

        print("\nInterval method counts:")
        print(active_intervals_df["interval_method"].value_counts(dropna=False).to_string())

    print("\nSaved files:")
    print(f"- {OUTPUT_PREFIX}_archive_links.csv")
    print(f"- {OUTPUT_PREFIX}_raw_all_services.csv")
    print(f"- {OUTPUT_PREFIX}_ed_rows.csv")
    print(f"- {OUTPUT_PREFIX}_ed_episodes.csv")
    print(f"- {OUTPUT_PREFIX}_ed_intervals.csv")
    print(f"- {OUTPUT_PREFIX}_ed_intervals_active_window.csv")
    print(f"- {OUTPUT_PREFIX}_ed_intervals_active_window_high_conf.csv")
    print(f"- {OUTPUT_PREFIX}_ed_episodes_active_window.csv")
    print(f"- {OUTPUT_PREFIX}_ed_episodes_uncertain.csv")
    print(f"- {OUTPUT_PREFIX}_ed_intervals_uncertain.csv")
    print(f"- {OUTPUT_PREFIX}_uncertainty_summary.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_union_intervals.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_union_summary.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_union_intervals_restricted.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_union_summary_restricted.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_union_intervals_high_conf.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_union_summary_high_conf.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_year_summary.csv")
    print(f"- {OUTPUT_PREFIX}_ed_site_cause_summary.csv")
    print(f"- {OUTPUT_PREFIX}_qa_checks.csv")
    print(f"- {OUTPUT_PREFIX}_qa_unresolved_sites.csv")
    print(f"- {OUTPUT_PREFIX}_qa_interval_audit_sample.csv")
    print(f"- {SITE_ALIASES_FILE}")
    print(f"- {SITE_METADATA_FILE}")
    if enriched[0] is not None:
        print(f"- {OUTPUT_PREFIX}_ed_episodes_active_window_enriched.csv")
        print(f"- {OUTPUT_PREFIX}_ed_site_year_summary_enriched.csv")
        print(f"- {OUTPUT_PREFIX}_ed_rurality_summary.csv")
        print(f"- {OUTPUT_PREFIX}_ed_rurality_union_summary.csv")
        print(f"- {OUTPUT_PREFIX}_ed_rurality_union_summary_high_conf.csv")
    if failures:
        print(f"- {OUTPUT_PREFIX}_failures.csv")

    print("\nNotes:")
    print("- Primary closure-burden metric: ahs_archive_ed_site_union_summary_high_conf.csv")
    print("- Secondary / sensitivity metric: ahs_archive_ed_site_union_summary_restricted.csv")
    print("- Broad sensitivity metric: ahs_archive_ed_site_union_summary.csv")
    print("- Explicit exports now list which episodes and intervals were excluded from the restricted analysis due to uncertainty.")
    print("- See ahs_archive_ed_episodes_uncertain.csv, ahs_archive_ed_intervals_uncertain.csv, and ahs_archive_uncertainty_summary.csv.")
    print("- Fallback continuous-range parsing is now much more conservative.")
    print("- New weekly named-range schedule parsing handles weekend/weekday recurrence better.")


if __name__ == "__main__":
    main()

