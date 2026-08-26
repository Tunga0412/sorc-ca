"""Portable, fail-closed Live publisher for a hosted runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent if ROOT.name == "automation" else ROOT
sys.path.insert(0, str(ROOT))

import live_tool


def _validate_candidate(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) < 1000:
        raise RuntimeError("Live candidate is unexpectedly small")
    if "const LIVE_DATA = " not in text:
        raise RuntimeError("Live candidate is missing LIVE_DATA")
    lowered = text.lower()
    for marker in ("api key required", "error loading map", "sample fixture data"):
        if marker in lowered:
            raise RuntimeError(f"Live candidate contains blocked marker: {marker}")
    generated_at = re.search(r'"generated_at":"([^"]+)"', text)
    if not generated_at:
        raise RuntimeError("Live candidate is missing generated_at")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=REPO_ROOT / "sorctracks_tool.html")
    parser.add_argument("--base-script", type=Path, default=ROOT / "ahs_scrape_v84_patched.py")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "sorctracks_live.html")
    parser.add_argument("--as-of")
    args = parser.parse_args()

    candidate = args.output.with_name(args.output.name + ".candidate")
    try:
        result = live_tool.build_live_page(args.template, args.base_script, candidate, as_of=args.as_of)
        _validate_candidate(candidate)
        os.replace(candidate, args.output)
        result["output"] = str(args.output)
        print(json.dumps({"status": "success", **result}, indent=2))
        return 0
    finally:
        if candidate.exists():
            candidate.unlink()


if __name__ == "__main__":
    raise SystemExit(main())

