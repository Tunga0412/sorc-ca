"""Daily retry gate: scheduled retries skip a fresh successful Alberta-day snapshot."""
from __future__ import annotations
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from public_data_reader import public_constant

# The retry gate accepts only this received v7 rendering contract. A changed
# schema or renderer triggers recollection; it never approves older output.
_RUNTIME_STATUS = """function runtimeStatus(item, now) {
      const windows = (item.schedule_windows || []).map(window => ({start:new Date(window.start),end:new Date(window.end)}));
      if (windows.length) { if (windows.some(window => window.start <= now && now < window.end)) return 'active_now'; if (windows.some(window => window.start > now)) return 'scheduled'; return null; }
      const today = dateKey(now); if (item.start_date_key && today < item.start_date_key) return 'scheduled'; if (item.end_date_key && today >= item.end_date_key) return null; return 'active_now';
    }"""
_TIMING_NOTE_DISPLAY = "${item.timing_note ? `<div class=\"details\">${esc(item.timing_note)}</div>` : ''}"

def _accepted_v7_contract(html, data):
    if not isinstance(data, dict):
        return False
    fetched = data.get("source_fetched_at")
    if not isinstance(fetched, str) or datetime.fromisoformat(fetched).utcoffset() is None:
        return False
    if type(data.get("source_page_count")) is not int or data["source_page_count"] < 1:
        return False
    if not isinstance(data.get("sites"), list):
        return False
    for site in data["sites"]:
        if not isinstance(site, dict) or not isinstance(site.get("disruptions"), list):
            return False
        if any(not isinstance(item, dict) or not isinstance(item.get("timing_note"), str) for item in site["disruptions"]):
            return False
    functions = re.findall(r"(?ms)^[ \t]*function runtimeStatus\(item, now\) \{\n(.*?)^[ \t]*\}", html)
    expected = _RUNTIME_STATUS.split("{\n", 1)[1].rsplit("}", 1)[0]
    return (len(functions) == 1 and functions[0].rstrip() == expected.rstrip()
            and html.count(_TIMING_NOTE_DISPLAY) == 1)

def main():
    now = datetime.now(ZoneInfo("America/Edmonton"))
    due = True
    try:
        path = Path(__file__).resolve().parent.parent / "sorctracks_live.html"
        html = path.read_text(encoding="utf-8")
        data = public_constant(html, "LIVE_DATA")
        observed = datetime.fromisoformat(data["generated_at"])
        age = (now - observed).total_seconds()/3600
        due = not _accepted_v7_contract(html, data) or data.get("as_of") != now.date().isoformat() or not (0 <= age < 20) or bool(data.get("demo_fixture"))
    except (ValueError, KeyError, OSError, TypeError):
        due = True
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        due = True
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("due=" + str(due).lower() + "\n")
    print("Daily Live collection is due" if due else "Today's successful Live snapshot is already fresh")
if __name__ == "__main__":
    main()

