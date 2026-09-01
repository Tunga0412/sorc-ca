"""Hosted historical SORCTracks publisher using a versioned input bundle."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path


def _portable_config(bundle: Path, repo_root: Path) -> dict:
    config = json.loads((bundle / "config.sanitized.json").read_text(encoding="utf-8"))
    paths = {
        "v84_wrapper_script": bundle / "pipelines" / "ed" / "ahs_scrape_all_years_v84_patched.py",
        "v84_base_script": bundle / "pipelines" / "ed" / "ahs_scrape_v84_patched.py",
        "previous_output_dir": bundle / "ahs_all_years_outputs_v84_explicit_preappend_postappend",
        "ob_output_dir": bundle / "ob_maternity_first_pass_outputs",
        "ob_pipeline_script": bundle / "pipelines" / "ob" / "ob_maternity_first_pass.py",
        "acute_output_dir": bundle / "ac_v6_xyr",
        "acute_pipeline_script": bundle / "pipelines" / "acute_inpatient" / "acute_inpatient_first_pass.py",
        "surgery_output_dir": bundle / "surg_v10_split_windows_fix_cleanqa",
        "surgery_pipeline_script": bundle / "pipelines" / "surgery" / "surgery_first_pass.py",
        "other_output_dir": bundle / "other_services_outputs",
        "other_pipeline_script": bundle / "pipelines" / "other_services" / "other_services_first_pass.py",
        "cache_dir": repo_root / ".historical_cache",
        "historical_html_path": repo_root / "sorctracks_tool.html",
    }
    for key, value in paths.items():
        config[key] = str(value)
    config.update({
        "github_owner": "Tunga0412",
        "github_repo": "sorc-ca",
        "github_branch": "main",
        "github_path": "sorctracks_tool.html",
        "github_token": os.environ.get("GITHUB_TOKEN", ""),
        "service_layers_to_update": ["ed", "ob", "acute", "surgery", "other"],
        "run_ob_pipeline": True,
        "run_acute_pipeline": True,
        "run_surgery_pipeline": True,
        "run_other_pipeline": True,
    })
    raw_source = bundle / "ahs_all_years_outputs_v84_explicit_preappend_postappend"
    for key in ("ob_raw_source", "acute_raw_source", "surgery_raw_source", "other_raw_source"):
        config[key] = str(raw_source)
    routing = dict(config.get("nearest_access_routing") or {})
    routing.update({
        "rank_by": "road",
        "road_matrix_csv": str(bundle / "nearest_access" / "nearest_access_road_distance_matrix.csv"),
        "service_directory_csv": str(bundle / "nearest_access" / "ahs_service_directory_capability_master_long.csv"),
    })
    config["nearest_access_routing"] = routing
    config["ed_access_routing"] = dict(routing)
    if not config["github_token"]:
        raise RuntimeError("GITHUB_TOKEN is not available")
    return config


def _repair_bundle_compatibility(bundle: Path) -> None:
    """Repair known omissions in older pinned bundles before importing them."""
    repaired = []
    for path in bundle.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            continue
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module.split(".")[0])
        if "os" in imported_names:
            continue
        if "os." not in source and "os[" not in source and "os(" not in source:
            continue
        lines = source.splitlines(keepends=True)
        insert_line = 0
        for node in tree.body:
            is_docstring = (
                isinstance(node, ast.Expr)
                and isinstance(getattr(node, "value", None), ast.Constant)
                and isinstance(node.value.value, str)
            )
            is_future_import = isinstance(node, ast.ImportFrom) and node.module == "__future__"
            if is_docstring or is_future_import:
                insert_line = node.end_lineno or insert_line
                continue
            break
        lines.insert(insert_line, "import os\n")
        path.write_text("".join(lines), encoding="utf-8")
        repaired.append(str(path.relative_to(bundle)))
    if repaired:
        print(f"Applied compatibility imports to: {', '.join(repaired)}")


def _seed_baseline_from_checkout(repo_root: Path, html_patcher) -> None:
    """Seed the runner-local baseline from the checked-out known-good publication."""
    source = repo_root / "sorctracks_tool.html"
    if not source.exists() or source.stat().st_size < 1000:
        raise RuntimeError(f"Checked-out historical HTML is missing or unexpectedly small: {source}")
    try:
        data = html_patcher.parse_existing_data(source)
        range_info = html_patcher.compute_data_range_info(data)
    except Exception as exc:
        raise RuntimeError(f"Checked-out historical HTML could not be parsed: {exc}") from exc
    if not range_info.get("latest_ym"):
        raise RuntimeError("Checked-out historical HTML has no recognized data range")
    baseline = repo_root / ".historical_cache" / "_last_known_good_baseline.html"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(source.read_bytes())
    print(f"Seeded historical baseline from checkout: {baseline} ({range_info.get('latest_ym')})")

def _ensure_zero_hour_parser(bundle: Path, repo_root: Path) -> None:
    """Provide the parser omitted from older pinned bundles."""
    bundled = bundle / "zero_hour_parser.py"
    fallback = repo_root / "automation" / "zero_hour_parser.py"
    if fallback.exists() and fallback.stat().st_size >= 1000:
        fallback_bytes = fallback.read_bytes()
        if not bundled.exists() or bundled.read_bytes() != fallback_bytes:
            bundled.write_bytes(fallback_bytes)
            print(f"Loaded current zero_hour_parser fallback into bundle: {bundled}")
            return
    if bundled.exists():
        return
    raise RuntimeError(f"Missing zero_hour_parser fallback: {fallback}")

def _patch_pipeline_release_gate(bundle: Path) -> None:
    """Allow notices without explicit hours to remain audit-only."""
    pipeline_path = bundle / "pipeline.py"
    if not pipeline_path.exists():
        return
    source = pipeline_path.read_text(encoding="utf-8")
    old = 'if entry.get("status") in {"rescued", "future_interval_not_counted"}:'
    new = 'if entry.get("status") in {"rescued", "future_interval_not_counted", "no_explicit_hours"}:'
    if old in source and new not in source:
        pipeline_path.write_text(source.replace(old, new, 1), encoding="utf-8")
        print("Patched historical release gate for notices without explicit hours.")


def _patch_html_patcher_rescue_counts(bundle: Path) -> None:
    """Keep rescued zero-hour rows from inflating already-merged episode denominators."""
    patch_path = bundle / "html_patcher.py"
    if not patch_path.exists():
        return
    source = patch_path.read_text(encoding="utf-8")
    old_total = "                total_episodes = int(distinct_episode_count_by_site.get(site_id) or 0) + rescued_eps"
    new_total = "                total_episodes = int(distinct_episode_count_by_site.get(site_id) or 0)"
    if old_total in source:
        source = source.replace(old_total, new_total, 1)
    old_years = "            for y, n in (rescued_episode_year_by_site.get(site_id) or {}).items():\n                year_breakdown[str(y)] = int(year_breakdown.get(str(y), 0)) + int(n)"
    new_years = "            if site_id not in distinct_episode_count_by_site_year:\n                for y, n in (rescued_episode_year_by_site.get(site_id) or {}).items():\n                    year_breakdown[str(y)] = int(year_breakdown.get(str(y), 0)) + int(n)"
    if old_years in source:
        source = source.replace(old_years, new_years, 1)
    if source != patch_path.read_text(encoding="utf-8"):
        patch_path.write_text(source, encoding="utf-8")
        print("Patched HTML builder to preserve merged ED episode denominators.")


def _patch_pipeline_framework_rescue_inputs(bundle: Path) -> None:
    """Feed recovered ED intervals into the independent hour-framework rebuild."""
    patch_path = bundle / "pipeline.py"
    if not patch_path.exists():
        return
    source = patch_path.read_text(encoding="utf-8")
    helper_marker = "def _augment_hour_framework_inputs_for_rescues("
    if helper_marker not in source:
        helper = '''
def _augment_hour_framework_inputs_for_rescues(v84_output_dir, rescue_summary, log_fn):
    """Append recovered zero-hour intervals to the framework's validated inputs."""
    if not rescue_summary:
        return
    import re as _re
    import pandas as _pd
    root = Path(v84_output_dir)
    rows = []
    for entry_index, entry in enumerate(rescue_summary):
        if entry.get("status") != "rescued":
            continue
        site = str(entry.get("site") or "").strip().lower()
        intervals = entry.get("rescued_intervals") or []
        for interval_index, interval in enumerate(intervals):
            start = _pd.to_datetime(interval.get("start"), errors="coerce")
            end = _pd.to_datetime(interval.get("end"), errors="coerce")
            if _pd.isna(start) or _pd.isna(end) or end <= start:
                continue
            year = int(start.year)
            source_key = f"zero_hour_rescue|{site}|{year}|{entry_index}"
            rows.append({
                "analysis_year": year,
                "site_best": site,
                "interval_start_clipped": start.isoformat(),
                "interval_end_clipped": end.isoformat(),
                "interval_method": "zero_hour_rescue_explicit_interval",
                "bed_or_space_reduction_text": "Recovered explicit interval from zero-hour notice.",
                "schedule_state_episode_key": source_key,
                "is_manual_add": False,
                "manual_add_id": "",
            })
    if not rows:
        return

    rescue_frame = _pd.DataFrame(rows)
    added = 0
    for path in sorted(root.glob("v84_*_ahs_archive_ed_intervals_active.csv")):
        match = _re.search(r"v84_(\d{4})_ahs_archive_ed_intervals_active\.csv$", path.name)
        if not match:
            continue
        year = int(match.group(1))
        additions = rescue_frame[rescue_frame["analysis_year"].eq(year)].copy()
        if additions.empty:
            continue
        existing = _pd.read_csv(path)
        for column in additions.columns:
            if column not in existing.columns:
                existing[column] = "" if column not in {"analysis_year", "is_manual_add"} else (False if column == "is_manual_add" else year)
        key_columns = ["site_best", "interval_start_clipped", "interval_end_clipped"]
        existing_keys = set(map(tuple, existing[key_columns].astype(str).itertuples(index=False, name=None)))
        additions = additions[
            ~additions[key_columns].astype(str).apply(tuple, axis=1).isin(existing_keys)
        ]
        if additions.empty:
            continue
        existing = _pd.concat([existing, additions.reindex(columns=existing.columns)], ignore_index=True)
        existing.to_csv(path, index=False)
        added += len(additions)

    year_summary_path = root / "v84_ahs_archive_year_summary.csv"
    if year_summary_path.exists():
        summary = _pd.read_csv(year_summary_path)
        hour_column = next(
            (column for column in ("all_method_unioned_closure_hours", "unioned_ed_disruption_hours")
             if column in summary.columns),
            None,
        )
        if hour_column is not None and "analysis_year" in summary.columns:
            by_year = rescue_frame.assign(hours=( _pd.to_datetime(rescue_frame["interval_end_clipped"]) - _pd.to_datetime(rescue_frame["interval_start_clipped"]) ).dt.total_seconds() / 3600).groupby("analysis_year")["hours"].sum()
            for year, hours in by_year.items():
                mask = summary["analysis_year"].eq(int(year))
                if mask.any():
                    summary.loc[mask, hour_column] = summary.loc[mask, hour_column].astype(float) + float(hours)
            summary.to_csv(year_summary_path, index=False)

    site_year_path = root / "v84_ahs_archive_site_year_panel.csv"
    if site_year_path.exists():
        panel = _pd.read_csv(site_year_path)
        if {"analysis_year", "site_best", "unioned_closure_hours"}.issubset(panel.columns):
            rescue_frame["hours"] = (
                _pd.to_datetime(rescue_frame["interval_end_clipped"])
                - _pd.to_datetime(rescue_frame["interval_start_clipped"])
            ).dt.total_seconds() / 3600
            by_site_year = rescue_frame.groupby(["analysis_year", "site_best"])["hours"].sum()
            for (year, site), hours in by_site_year.items():
                mask = panel["analysis_year"].eq(int(year)) & panel["site_best"].astype(str).str.lower().eq(str(site).lower())
                if mask.any():
                    panel.loc[mask, "unioned_closure_hours"] = panel.loc[mask, "unioned_closure_hours"].astype(float) + float(hours)
                else:
                    new_row = {column: "" for column in panel.columns}
                    new_row.update({"analysis_year": int(year), "site_best": site, "unioned_closure_hours": float(hours)})
                    panel = _pd.concat([panel, _pd.DataFrame([new_row])], ignore_index=True)
            panel.to_csv(site_year_path, index=False)

    log_fn(f"  hour-framework inputs: added {added} recovered ED interval(s) and reconciled official ED totals.")
'''
        source = source.replace("\ndef _build_hour_frameworks(", "\n" + helper + "\ndef _build_hour_frameworks(", 1)
    call_marker = "    hour_frameworks = _build_hour_frameworks(v84_output_dir, new_output_dir, public_analysis_end, log_fn)"
    call = "    _augment_hour_framework_inputs_for_rescues(v84_output_dir, rescue_summary, log_fn)\n" + call_marker
    if call_marker in source and "    _augment_hour_framework_inputs_for_rescues(v84_output_dir, rescue_summary, log_fn)\n" not in source:
        source = source.replace(call_marker, call, 1)
    validation_helper_marker = "def _validate_hour_framework_totals("
    if validation_helper_marker not in source:
        validation_helper = '''def _validate_hour_framework_totals(staging_html, service_year_summary):
    """Fail closed when HOUR_FRAMEWORKS diverges from service-layer totals."""
    text = Path(staging_html).read_text(encoding="utf-8")
    marker = "const HOUR_FRAMEWORKS = "
    start = text.find(marker)
    if start < 0:
        raise RuntimeError("Missing HOUR_FRAMEWORKS block; staged HTML was not released.")
    try:
        actual, _ = json.JSONDecoder().raw_decode(text[start + len(marker):])
    except json.JSONDecodeError as exc:
        raise RuntimeError("HOUR_FRAMEWORKS block is invalid; staged HTML was not released.") from exc
    observed = {
        str(row.get("service_layer") or "").strip().lower(): float(row.get("total_hours") or 0)
        for row in (actual.get("time_overall_summary") or [])
    }
    expected_map = {
        "all": "all_analyzed_services",
        "ed": "ed",
        "ob": "obstetrics",
        "acute": "acute care",
        "surgery": "surgery/or",
        "other": "other services",
    }
    missing = []
    mismatches = []
    for service_id, framework_label in expected_map.items():
        expected_block = (service_year_summary.get(service_id) or {}).get("all") or {}
        if not expected_block:
            continue
        expected = float(expected_block.get("hours") or 0)
        observed_value = observed.get(framework_label)
        if observed_value is None:
            missing.append(framework_label)
        elif abs(observed_value - expected) >= 0.02:
            mismatches.append(f"{service_id}: expected {expected:.4f}, observed {observed_value:.4f}")
    if missing or mismatches:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if mismatches:
            details.append("mismatches " + "; ".join(mismatches))
        raise RuntimeError("Hour-framework totals do not match SERVICE_YEAR_SUMMARY: " + " | ".join(details))
'''
        source = source.replace("\ndef _build_hour_frameworks(", "\n" + validation_helper + "\ndef _build_hour_frameworks(", 1)
    validation_marker = "    _validate_staged_hour_framework_html(staging_html, hour_frameworks[\\"html_data\\\"], log_fn)"
    validation_call = "    _validate_hour_framework_totals(staging_html, service_year_summary)\n" + validation_marker
    if validation_marker in source and "    _validate_hour_framework_totals(staging_html, service_year_summary)\n" not in source:
        source = source.replace(validation_marker, validation_call, 1)
    if source != patch_path.read_text(encoding="utf-8"):
        patch_path.write_text(source, encoding="utf-8")
        print("Patched hour-framework inputs to include recovered ED intervals.")

def _patch_pipeline_rescue_hours_metadata(bundle: Path) -> None:
    """Expose recovered ED hours to the legacy cross-layer QA pass."""
    patch_path = bundle / "pipeline.py"
    if not patch_path.exists():
        return
    source = patch_path.read_text(encoding="utf-8")
    marker = "    qa_report = cross_layer_release_qa.run_qa(new_output_dir, updater_root=Path(__file__).resolve().parent)"
    replacement = "    rescue_hours_meta = new_output_dir / \"ed_rescue_hours.json\"\n"
    replacement += "    rescue_hours_meta.write_text(json.dumps({\"total_hours\": round(sum(sum(months.values()) for months in rescued_by_site.values()), 2)}), encoding=\"utf-8\")\n"
    replacement += marker
    if marker in source and "ed_rescue_hours.json" not in source:
        patch_path.write_text(source.replace(marker, replacement, 1), encoding="utf-8")
        print("Patched pipeline to expose recovered ED hours to release QA.")

def _patch_cross_layer_rescue_hours_qa(bundle: Path) -> None:
    """Include recovered ED hours in the legacy source-hour comparison."""
    patch_path = bundle / "cross_layer_release_qa.py"
    if not patch_path.exists():
        return
    source = patch_path.read_text(encoding="utf-8")
    old_start = "    baseline = updater_root / \"cache\" / \"_last_known_good_baseline.html\"\n\n    checks = []"
    new_start = "    baseline = updater_root / \"cache\" / \"_last_known_good_baseline.html\"\n    rescue_hours = 0.0\n    rescue_meta = run_dir / \"ed_rescue_hours.json\"\n    if rescue_meta.exists():\n        try:\n            rescue_hours = float((json.loads(rescue_meta.read_text(encoding=\"utf-8\")) or {}).get(\"total_hours\") or 0.0)\n        except (OSError, TypeError, ValueError, json.JSONDecodeError):\n            rescue_hours = 0.0\n\n    checks = []"
    old_check = "            source_hour_totals[service_id] = metrics.source_hours\n            _add(checks, f\"{service_id}_hours_match_year_summary\", abs(float(service_block[\"all\"][\"hours\"]) - metrics.source_hours) < 0.02, metrics.source_hours, service_block[\"all\"][\"hours\"])"
    new_check = "            expected_source_hours = metrics.source_hours + (rescue_hours if service_id == \"ed\" else 0.0)\n            source_hour_totals[service_id] = expected_source_hours\n            _add(checks, f\"{service_id}_hours_match_year_summary\", abs(float(service_block[\"all\"][\"hours\"]) - expected_source_hours) < 0.02, expected_source_hours, service_block[\"all\"][\"hours\"])"
    changed = False
    if old_start in source and "rescue_hours = 0.0" not in source:
        source = source.replace(old_start, new_start, 1)
        changed = True
    if old_check in source:
        source = source.replace(old_check, new_check, 1)
        changed = True
    if changed:
        patch_path.write_text(source, encoding="utf-8")
        print("Patched cross-layer QA to include recovered ED hours.")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--force", action="store_true", help="Publish even when the historical cutoff is current")
    args = parser.parse_args()
    bundle = args.bundle_dir.resolve()
    repo_root = args.repo_root.resolve()
    if not (bundle / "config.sanitized.json").exists():
        raise FileNotFoundError(f"Historical bundle is missing config.sanitized.json: {bundle}")

    _repair_bundle_compatibility(bundle)
    _ensure_zero_hour_parser(bundle, repo_root)
    _patch_pipeline_release_gate(bundle)
    _patch_html_patcher_rescue_counts(bundle)
    _patch_pipeline_rescue_hours_metadata(bundle)
    _patch_pipeline_framework_rescue_inputs(bundle)
    _patch_cross_layer_rescue_hours_qa(bundle)
    sys.path.insert(0, str(bundle))
    import github_api
    import html_patcher
    import historical_toggle
    import pipeline

    config = _portable_config(bundle, repo_root)
    _seed_baseline_from_checkout(repo_root, html_patcher)
    expected_cutoff = pipeline._public_analysis_end(config)
    current_data = html_patcher.parse_existing_data(repo_root / "sorctracks_tool.html")
    current_ym = html_patcher.compute_data_range_info(current_data).get("latest_ym")
    expected_ym = expected_cutoff[:7]
    if not args.force and current_ym and current_ym >= expected_ym:
        print(json.dumps({
            "status": "skipped",
            "reason": "historical_publication_already_current",
            "current_month": current_ym,
            "expected_month": expected_ym,
        }, indent=2))
        return 0
    result = pipeline.run_full_update(config, log_fn=print, stage_fn=lambda stage: print(f"stage={stage}"))
    staged = Path(result["staging_html"])
    historical_toggle.ensure_toggle(staged)
    if not staged.exists() or staged.stat().st_size < 1000:
        raise RuntimeError("Historical candidate is missing or unexpectedly small")
    response = github_api.put_file(
        owner=config["github_owner"],
        repo=config["github_repo"],
        path=config["github_path"],
        branch=config["github_branch"],
        token=config["github_token"],
        new_content_bytes=staged.read_bytes(),
        sha=result.get("current_sha"),
        commit_message="Automated SORCTracks historical update",
    )
    print(json.dumps({
        "status": "success",
        "public_analysis_end": result["report"].get("public_analysis_end"),
        "commit": response.get("commit", {}).get("sha"),
        "staging_html": str(staged),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

