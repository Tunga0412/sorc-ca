"""Hosted historical SORCTracks publisher using a versioned input bundle."""

from __future__ import annotations

import argparse
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

    sys.path.insert(0, str(bundle))
    import github_api
    import html_patcher
    import historical_toggle
    import pipeline

    config = _portable_config(bundle, repo_root)
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

