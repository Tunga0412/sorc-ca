"""Daily retry gate: scheduled retries skip a fresh successful Alberta-day snapshot."""
from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from public_data_reader import public_constant
def main():
    now = datetime.now(ZoneInfo("America/Edmonton"))
    due = True
    try:
        path = Path(__file__).resolve().parent.parent / "sorctracks_live.html"
        data = public_constant(path.read_text(encoding="utf-8"), "LIVE_DATA")
        observed = datetime.fromisoformat(data["generated_at"])
        age = (now - observed).total_seconds()/3600
        due = data.get("as_of") != now.date().isoformat() or not (0 <= age < 20) or bool(data.get("demo_fixture"))
    except (ValueError, KeyError, OSError, TypeError):
        due = True
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        due = True
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("due=" + str(due).lower() + "\n")
    print("Daily Live collection is due" if due else "Today's successful Live snapshot is already fresh")
if __name__ == "__main__":
    main()

