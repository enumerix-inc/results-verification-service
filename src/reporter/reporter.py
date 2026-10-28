"""
reporter.py — assembles and writes verification reports.

Produces two CSV files per run (detail and summary) and stores a JSON
blob in the reports table. CSVs are written to {reports_dir}/{run_id}/.

Detail report  — one row per (sample, metric, experiment)
Summary report — one row per (feature_set, comparison_type, metric),
                 aggregated across all samples

The 'notes' column is empty for the MVP. It is the designated
insertion point for LLM-generated narrative in Phase 3 (ADR-017).

Entry point: write_report()
"""

import csv
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.database.models import insert_report
from src.reporter.acceptance_report import write_acceptance_report


def determine_experiment_verdict(sample_results: list[dict]) -> str:
    """
    Roll up per-sample verdicts into an experiment-level verdict.
    Returns 'fail' if any sample fails, 'pass' otherwise.
    An empty sample list returns 'fail'.
    """
    if not sample_results:
        return "fail"
    return "pass" if all(r["verdict"] == "pass" for r in sample_results) else "fail"


def determine_run_verdict(experiment_results: list[dict],
                          build_verdict_policy: dict) -> str:
    """
    Roll up experiment-level verdicts into a run-level build verdict
    according to the build_verdict_policy from the manifest.

    Supported policy values:
      rule:                   "all_stability_experiments_must_pass"
      on_exploratory_failure: "warn_only"

    Raises ValueError for unrecognised policy values so that unsupported
    manifests fail loudly rather than silently applying wrong behaviour.
    """
    rule = build_verdict_policy.get("rule")
    on_exploratory_failure = build_verdict_policy.get("on_exploratory_failure")

    if rule != "all_stability_experiments_must_pass":
        raise ValueError(f"Unsupported build_verdict_policy rule: {rule!r}")
    if on_exploratory_failure != "warn_only":
        raise ValueError(f"Unsupported on_exploratory_failure policy: {on_exploratory_failure!r}")

    stability = [r for r in experiment_results if r["classification"] == "stability"]
    if any(r["verdict"] == "fail" for r in stability):
        return "fail"
    if any(r["verdict"] == "fail" for r in experiment_results):
        return "warn"
    return "pass"


def write_report(conn, config: dict, run_id: str,
                 experiment_results: list[dict], run_verdict: str,
                 run_info: dict | None = None) -> None:
    """
    Write the detail CSV, summary CSV, and report DB record for a completed run.

    experiment_results — list of dicts, one per experiment, each containing:
      feature_set    : str
      classification : str
      verdict        : str
      comparisons    : list of manifest comparison entries (for criteria lookup)
      sample_results : list of per-(sample, metric) result dicts from the comparator
      aggregate_results : (optional) per-(criterion, channel) dicts from aggregate-scope
                          comparisons; when present they also produce the acceptance
                          report files (see acceptance_report.py)

    run_info — optional {"pipeline_build", "scenario"} shown in the acceptance report.

    CSVs are written to {reports_dir}/{run_id}/.
    The JSON blob (detail + summary) is stored in the reports table.
    """
    reports_dir = Path(config["paths"]["reports_dir"]) / run_id
    reports_dir.mkdir(parents=True, exist_ok=True)

    detail_rows = []
    summary_agg = {}  # (feature_set, comparison_type, metric) → aggregation dict

    for exp_result in experiment_results:
        feature_set = exp_result["feature_set"]

        # Build criteria string per comparison_type from the manifest comparisons list
        criteria_by_type = {}
        for comp in exp_result.get("comparisons", []):
            ct = comp["type"]
            if ct == "count_tolerance":
                criteria_by_type[ct] = f"count_tolerance ±{comp['tolerance_percent']}%"
            else:
                criteria_by_type[ct] = ct

        for sample in exp_result.get("sample_results", []):
            comparison_type = sample["comparison_type"]
            metric = sample["metric"]
            criteria = criteria_by_type.get(comparison_type, comparison_type)

            detail_rows.append({
                "feature_set":       feature_set,
                "comparison_type":   comparison_type,
                "metric":            metric,
                "criteria":          criteria,
                "sample_id":         sample["sample_id"],
                "expected":          sample["expected_value"],
                "actual":            sample["actual_value"],
                "deviation_percent": sample["deviation_percent"],
                "verdict":           sample["verdict"],
                "notes":             sample.get("note", ""),
            })

            key = (feature_set, comparison_type, metric)
            if key not in summary_agg:
                summary_agg[key] = {
                    "feature_set":     feature_set,
                    "comparison_type": comparison_type,
                    "metric":          metric,
                    "criteria":        criteria,
                    "passed":          0,
                    "failed":          0,
                }
            if sample["verdict"] == "pass":
                summary_agg[key]["passed"] += 1
            else:
                summary_agg[key]["failed"] += 1

    detail_fieldnames = [
        "feature_set", "comparison_type", "metric", "criteria",
        "sample_id", "expected", "actual", "deviation_percent", "verdict", "notes",
    ]
    detail_path = reports_dir / "detail_report.csv"
    with open(detail_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=detail_fieldnames)
        writer.writeheader()
        writer.writerows(detail_rows)

    summary_rows = []
    for agg in summary_agg.values():
        summary_rows.append({
            "feature_set":     agg["feature_set"],
            "comparison_type": agg["comparison_type"],
            "metric":          agg["metric"],
            "criteria":        agg["criteria"],
            "samples_passed":  agg["passed"],
            "samples_failed":  agg["failed"],
            "verdict":         "pass" if agg["failed"] == 0 else "fail",
            "notes":           "",
        })

    summary_fieldnames = [
        "feature_set", "comparison_type", "metric", "criteria",
        "samples_passed", "samples_failed", "verdict", "notes",
    ]
    summary_path = reports_dir / "summary_report.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    report_blob = {
        "run_id":  run_id,
        "detail":  detail_rows,
        "summary": summary_rows,
    }

    acceptance_rows = write_acceptance_report(reports_dir, run_id, experiment_results, run_info)
    if acceptance_rows:
        report_blob["acceptance"] = acceptance_rows

    with conn:
        insert_report(conn, {
            "report_id":       str(uuid.uuid4()),
            "run_id":          run_id,
            "result_id":       None,
            "llm_narrative":   "",
            "overall_verdict": run_verdict,
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "report_json":     json.dumps(report_blob),
        })

    print(f"Report written: {reports_dir}")
