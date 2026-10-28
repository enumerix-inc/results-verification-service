"""
orchestrator.py — coordinates the full verification flow for a run.

For each run detected by the listener:
  1. Load the run context manifest
  2. For each experiment in the manifest:
     a. Run the pre-verification gate
     b. Locate the result folder and CSV
     c. Run per-sample comparison (if gate passes)
  3. Determine experiment and run verdicts
  4. Persist results to the database
  5. Write CSV reports and store JSON blob

Entry points: load_manifest(), verify_run()
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.comparator.comparator import compare_experiment
from src.database.db import get_connection
from dataclasses import asdict

from src.database.models import (
    insert_aggregate_result,
    insert_experiment_result,
    insert_result_observations,
    insert_run,
    insert_sample_result,
)
from src.gate.gate import run_gate
from src.reporter.reporter import (
    determine_experiment_verdict,
    determine_run_verdict,
    write_report,
)


def load_manifest(run_id: str, config: dict) -> dict:
    """Load and return the context manifest for a run."""
    path = Path(config["paths"]["manifests_dir"]) / f"context_manifest_{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Manifest not found for run '{run_id}': expected at {path}"
        )
    with open(path) as f:
        return json.load(f)


def find_result_folder(name: str, config: dict) -> Path:
    """
    Locate the pipeline result folder for a given experiment execution.

    Globs for {name}* under results_dir, where `name` is the exact
    timestamped identifier the pipeline itself assigned to this execution
    (e.g. "JS221N_serial_titration_PSF_260925_1416") — the same string
    carried in the Reports table's Name column and in the "name" field of
    the pipeline's NOTIFY payload. This is the pipeline's own folder-naming
    convention; it has no notion of the verification service's run_id at
    all, so `name` must be supplied by the caller (collected from the live
    notification), not reconstructed from experiment_id + run_id.

    Always returns exactly one match — the pipeline's name is unique per
    experiment execution.

    NOTE: Currently assumes CSVs are manually placed in the results folder.
    Future implementation will include a password-protected unzip step
    before this lookup — see STATUS.md open design questions.
    """
    results_dir = Path(config["paths"]["results_dir"])
    matches = list(results_dir.glob(f"{name}*"))

    if len(matches) == 0:
        raise FileNotFoundError(
            f"No result folder found for '{name}' under {results_dir}. "
            f"Expected a folder matching '{name}*'."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple result folders found for '{name}' — "
            f"expected exactly one: {[str(m) for m in matches]}"
        )
    return matches[0]


def verify_run(config: dict, run_id: str, manifest: dict,
                experiment_names: dict[str, str],
                manifest_record_path: str) -> None:
    """
    Full verification flow for a single run.

    Opens its own DB connection so it is safe to call from a worker
    thread (e.g. via asyncio.to_thread) without hitting SQLite's
    check_same_thread restriction.

    experiment_names maps experiment_id -> the pipeline's own timestamped
    name for that execution (from the notification's "name" field),
    collected live as notifications arrive — the manifest itself has no
    way to know this ahead of time.

    manifest_record_path is the path to store in runs.manifest_path. The
    caller (main.py) is responsible for passing the manifest's *eventual*
    location, not its current one: this function is only invoked once all
    of a run's experiments are confirmed ready, immediately before the
    caller archives the manifest into processed/, so the caller passes
    the destination path it is about to move the file to. Recomputing the
    path from manifests_dir + run_id here would record a location that
    stops existing the moment archiving happens.

    Coordinates gate, comparator, and reporter modules. All results are
    written to the verification database and CSV reports are written to
    {reports_dir}/{run_id}/.
    """
    conn = get_connection(config["paths"]["database"])
    policy = manifest["build_verdict_policy"]
    now = datetime.now(timezone.utc).isoformat()
    manifest_path = manifest_record_path

    experiment_results = []

    for experiment in manifest["experiments"]:
        gate_passed, gate_status = run_gate(
            conn, experiment["experiment_id"], experiment.get("comparisons"))
        sample_results = []
        aggregate_results = []
        comparison = None

        if gate_passed:
            try:
                name = experiment_names.get(experiment["experiment_id"])
                if not name:
                    raise FileNotFoundError(
                        f"No pipeline report name recorded for experiment "
                        f"'{experiment['experiment_id']}' in run '{run_id}' — "
                        f"was a notification ever received for it?"
                    )
                result_folder = find_result_folder(name, config)

                # Different experiment types produce differently-named output
                # CSVs from the same result folder (e.g. a linkage experiment's
                # LinkageSummary.csv vs. the default CountableDataSummary.csv).
                # The manifest declares which one this experiment expects;
                # default to CountableDataSummary.csv for experiments that
                # don't set it, since that's the original/common case.
                file_suffix = experiment.get("result_file_suffix", "CountableDataSummary.csv")
                result_csvs = list(result_folder.glob(f"*{file_suffix}"))
                if not result_csvs:
                    raise FileNotFoundError(
                        f"No file matching '*{file_suffix}' found in {result_folder}"
                    )
            except (FileNotFoundError, ValueError) as e:
                print(f"Warning: {e}")
                gate_status = "result_not_found"
            else:
                comparison = compare_experiment(
                    conn, experiment, str(result_csvs[0]),
                    annotations_dir=config["paths"].get("annotations_dir"),
                )
                sample_results = comparison["comparison_results"]
                aggregate_results = comparison["aggregate_results"]

        experiment_results.append({
            "experiment_id":  experiment["experiment_id"],
            "feature_set":    experiment["feature_set"],
            "classification": experiment["classification"],
            "comparisons":    experiment.get("comparisons", []),
            "gate_status":    gate_status,
            "comparison":     comparison,
            "sample_results": sample_results,
            "aggregate_results": aggregate_results,
            "verdict":        determine_experiment_verdict(sample_results + aggregate_results),
        })

    run_verdict = determine_run_verdict(experiment_results, policy)

    with conn:
        insert_run(conn, {
            "run_id":         run_id,
            "triggered_at":   manifest["run"]["triggered_at"],
            "pipeline_build": manifest["run"]["pipeline_build"],
            "scenario":       manifest["run"]["scenario"],
            "manifest_path":  manifest_path,
            "verdict":        run_verdict,
        })

        for exp_result in experiment_results:
            result_id = str(uuid.uuid4())
            exp_result["result_id"] = result_id
            comparison = exp_result["comparison"]

            insert_experiment_result(conn, {
                "result_id":         result_id,
                "run_id":            run_id,
                "gs_exp_version_id": comparison["gs_exp_version_id"] if comparison else None,
                "experiment_id":     exp_result["experiment_id"],
                "feature_set":       exp_result["feature_set"],
                "classification":    exp_result["classification"],
                "pre_verify_status": exp_result["gate_status"],
                "verdict":           "aborted" if comparison is None else exp_result["verdict"],
                "verified_at":       now,
            })

            if comparison is None:
                continue

            gs_samples_by_id = comparison["gs_samples"]
            for sr in exp_result["sample_results"]:
                gs_sample = gs_samples_by_id.get(sr["sample_id"])
                insert_sample_result(conn, {
                    "sample_result_id": str(uuid.uuid4()),
                    "result_id":        result_id,
                    "gs_sample_id":     gs_sample["gs_sample_id"] if gs_sample else "",
                    "sample_id":        sr["sample_id"],
                    "metric":           sr["metric"],
                    "comparison_type":  sr["comparison_type"],
                    "actual_value":     sr["actual_value"],
                    "expected_value":   sr["expected_value"],
                    "deviation_percent": sr.get("deviation_percent"),
                    "verdict":          sr["verdict"],
                    "notes":            sr.get("note"),
                })

            for ar in exp_result["aggregate_results"]:
                insert_aggregate_result(conn, {
                    **ar,
                    "aggregate_result_id": str(uuid.uuid4()),
                    "result_id":           result_id,
                    "notes":               ar.get("note"),
                })
            if comparison["observation_set"] is not None:
                insert_result_observations(
                    conn, result_id,
                    [asdict(o) for o in comparison["observation_set"].observations])

    write_report(conn, config, run_id, experiment_results, run_verdict,
                 run_info={"pipeline_build": manifest["run"]["pipeline_build"],
                           "scenario": manifest["run"]["scenario"]})
    conn.close()
    print(f"Run {run_id} complete — verdict: {run_verdict}")
