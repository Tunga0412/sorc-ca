"""Read exact JSON constants or the validated five-object public export; never execute JS."""
from __future__ import annotations
import base64
import gzip
import hashlib
import io
import json
import re
FIVE = {"DATA", "HOUR_FRAMEWORKS", "YEAR_SUMMARY", "SERVICE_YEAR_SUMMARY", "SERVICE_LAYER_META"}
MAX_EXPORT_BYTES = 128 * 1024 * 1024
def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in public export")
        result[key] = value
    return result
def _bad(value):
    raise ValueError("Nonfinite JSON number")
DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_bad)
def _literal(html, name):
    matches = list(re.finditer(r"\bconst\s+" + re.escape(name) + r"\s*=\s*", html))
    if len(matches) != 1:
        raise ValueError("Missing or duplicate public constant: " + name)
    start = matches[0].end()
    value, length = DECODER.raw_decode(html[start:])
    if not html[start + length:].lstrip().startswith(";"):
        raise ValueError("Public constant is not a terminated JSON literal: " + name)
    return value
def public_constant(html, name):
    if name in FIVE and re.search(r"\bconst\s+SORC_PACKED_EXPORT\s*=", html):
        for field in FIVE:
            declarations = list(re.finditer(r"\bconst\s+" + re.escape(field) + r"\s*=\s*", html))
            expected = r'SORC_PACKED_DATA\["' + re.escape(field) + r'"\]\s*;'
            if len(declarations) != 1 or not re.match(expected, html[declarations[0].end():]):
                raise ValueError("Packed public consumer binding changed: " + field)
        envelope = _literal(html, "SORC_PACKED_EXPORT")
        if not isinstance(envelope, dict) or set(envelope) != {"encoding", "sha256", "data"}:
            raise ValueError("Unexpected packed public export envelope")
        if envelope["encoding"] != "gzip-base64" or not all(type(envelope[x]) is str for x in envelope):
            raise ValueError("Unsupported public export encoding")
        if not re.fullmatch(r"[a-f0-9]{64}", envelope["sha256"]):
            raise ValueError("Invalid public export digest")
        compressed = base64.b64decode(envelope["data"], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as source:
            raw = source.read(MAX_EXPORT_BYTES + 1)
        if len(raw) > MAX_EXPORT_BYTES:
            raise ValueError("Public export exceeds bound")
        if hashlib.sha256(raw).hexdigest() != envelope["sha256"]:
            raise ValueError("Public export digest mismatch")
        objects = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad)
        if not isinstance(objects, dict) or set(objects) != FIVE or any(type(v) is not dict for v in objects.values()):
            raise ValueError("Unexpected five-object public schema")
        return objects[name]
    return _literal(html, name)

