"""Bind legacy current-page rows to exact own modal sections and full label census."""
from __future__ import annotations
import collections
import re
import unicodedata
from urllib.parse import urldefrag
from bs4 import BeautifulSoup
FIELDS = {
    "Program or Service": "program_or_service",
    "Bed or Space Reduction": "bed_or_space_reduction_text",
    "Reason": "reason_text",
    "Start Date": "start_date_text",
    "Anticipated End Date": "anticipated_end_date_text",
}
KEYS = ("facility_name",) + tuple(FIELDS.values())
def _clean(value):
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()
def _key(row):
    if "raw_block_text" in row:
        expected_raw = " | ".join(label + ": " + _clean(row.get(field)) for label, field in FIELDS.items())
        if _clean(row["raw_block_text"]) != expected_raw:
            raise RuntimeError("Current raw-block context differs from its own source fields")
    return tuple(_clean(row.get(name)) for name in KEYS)
def require_own_current_census(rows, source_pages):
    expected = []
    seen_pages = set()
    for text in source_pages:
        if text in seen_pages:
            continue
        seen_pages.add(text)
        soup = BeautifulSoup(text, "lxml")
        program_labels = [tag for tag in soup.select("strong,b") if _clean(tag.get_text()).rstrip(":") == "Program or Service"]
        page_expected = []
        for modal in soup.select("div.modal[id]"):
            headers = modal.select(".modal-header h4")
            if not headers:
                # Non-disruption informational modals are allowed only if they
                # have no Program label.
                if any(x in modal.descendants for x in program_labels):
                    raise RuntimeError("AHS current source unit has no facility heading")
                continue
            if len(headers) != 1:
                raise RuntimeError("AHS current source unit has multiple facility headings")
            bodies = modal.select(".modal-body")
            if len(bodies) != 1:
                raise RuntimeError("AHS current source unit body is ambiguous")
            section = None
            for tag in bodies[0].find_all(["strong", "b"]):
                label = _clean(tag.get_text()).rstrip(":")
                if label not in FIELDS:
                    continue
                paragraph = tag.find_parent("p")
                if paragraph is None or paragraph.find_parent("div", class_="modal-body") is not bodies[0]:
                    raise RuntimeError("AHS current source field is not owned by a supported section")
                known_in_paragraph = [x for x in paragraph.find_all(["strong","b"]) if _clean(x.get_text()).rstrip(":") in FIELDS]
                if len(known_in_paragraph) != 1:
                    raise RuntimeError("AHS current source paragraph has conflicting labels")
                full = _clean(paragraph.get_text(" ", strip=True))
                literal = _clean(tag.get_text(" ", strip=True))
                if not full.startswith(literal):
                    raise RuntimeError("AHS current source field has text preceding its own label")
                value = _clean(full[len(literal):])
                # Own field lists, such as Bow Island's explicitly adjusted
                # opening schedule, can follow the labelled paragraph. Accept
                # only direct UL/OL siblings before the next field or boundary.
                for sibling in paragraph.next_siblings:
                    if getattr(sibling, "name", None) in ("ul", "ol"):
                        if sibling.find(["strong", "b"]):
                            raise RuntimeError("AHS current field list contains a new label")
                        value = _clean(value + " " + sibling.get_text(" ", strip=True))
                    elif getattr(sibling, "name", None) is None and not str(sibling).strip():
                        continue
                    else:
                        break
                if label == "Program or Service":
                    if section is not None:
                        if set(section) != set(KEYS):
                            raise RuntimeError("Incomplete AHS current source section")
                        page_expected.append(section)
                    section = {"facility_name": _clean(headers[0].get_text(" ", strip=True))}
                if section is None or FIELDS[label] in section:
                    raise RuntimeError("Repeated or unowned current source field")
                section[FIELDS[label]] = value
            if section is not None:
                if set(section) != set(KEYS):
                    raise RuntimeError("Incomplete AHS current source section")
                page_expected.append(section)
        if len(page_expected) != len(program_labels):
            raise RuntimeError("Dropped or unsupported Program labels in current source census")
        expected.extend(page_expected)
    actual = [_key(row) for row in rows]
    wanted = [_key(row) for row in expected]
    if len(actual) != len(wanted) or collections.Counter(actual) != collections.Counter(wanted):
        raise RuntimeError("Current parsed rows differ from complete own-card field census")
    return {"source_sections": len(wanted), "parsed_rows": len(actual), "own_field_census_verified": True}



def require_advertised_pagination_closed(base, receipts, source_pages):
    if len(receipts) != len(source_pages):
        raise RuntimeError("Current source page provenance count differs")
    captured = {urldefrag(item["requested_url"])[0] for item in receipts}
    for receipt, text in zip(receipts, source_pages):
        for action in base._discover_snapshot_page_candidates(BeautifulSoup(text, "lxml"), receipt["requested_url"]):
            if action.get("kind") != "get":
                raise RuntimeError("Unsupported current source POST pagination; preserving prior Live")
            if urldefrag(action.get("url", ""))[0] not in captured:
                raise RuntimeError("Advertised current source GET page was not received; preserving prior Live")
    return {"captured_pages": len(receipts), "advertised_GET_pages_received": True, "unsupported_POST_pages": False}
