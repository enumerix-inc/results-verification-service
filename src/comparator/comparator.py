"""
comparator.py — comparison orchestration for one experiment.

Dispatches each entry in the manifest's comparisons list through the
COMPARISON_REGISTRY by scope (ADR-021):

  sample     fetched gold standard + actual CSV rows -> per-sample results
  aggregate  sample annotation + annotated observations -> per-criterion,
             per-channel results; no gold standard needed (ADR-026)

Entry point: compare_experiment()
"""

from src.annotation.annotation import (AnnotationError, find_annotation,
                                       load_annotation, resolve_annotations_dir)
from src.comparator.acceptance_params import AcceptanceParamsError
from src.comparator.registry import AGGREGATE, COMPARISON_REGISTRY, SAMPLE
from src.database.models import get_active_gs_version, get_gs_samples
from src.observations import ObservationError, read_observations
from src.registration.registrar import read_gold_standard_csv


def _failed_aggregate(comparison: dict, reason: str) -> list[dict]:
    """One 'error' row standing in for a whole aggregate comparison that
    could not be set up (missing annotation, missing columns, bad params...)."""
    return [{
        "criterion_id": None, "group_key": "__ALL__", "channel": None,
        "metric": comparison["type"], "comparison_type": comparison["type"],
        "actual_value": None, "expected_value": None, "deviation_percent": None,
        "lower_bound": None, "lower_inclusive": None,
        "upper_bound": None, "upper_inclusive": None, "margin": None,
        "verdict": "error", "note": reason,
        "annotation_version": None, "annotation_sha256": None,
        "source_file_sha256": None, "filters": None,
    }]


def _run_aggregate(experiment: dict, comparison: dict, result_csv_path: str,
                   annotations_dir):
    """Returns (aggregate result dicts, ObservationSet or None). Setup failures
    become a recorded 'error' result instead of aborting the whole run."""
    try:
        annotation_id = comparison.get("annotation") or experiment["experiment_id"]
        annotation = load_annotation(
            find_annotation(resolve_annotations_dir(annotations_dir), annotation_id))
        obs_set = read_observations(result_csv_path, annotation,
                                    comparison.get("columns", {}))
        handler = COMPARISON_REGISTRY[comparison["type"]]["fn"]
        return handler(obs_set, comparison), obs_set
    except (AnnotationError, AcceptanceParamsError, ObservationError,
            FileNotFoundError) as e:
        return _failed_aggregate(comparison, f"{type(e).__name__}: {e}"), None


def compare_experiment(conn, experiment: dict, result_csv_path: str,
                       annotations_dir=None) -> dict:
    """
    Orchestrate comparison for one experiment.

    Dispatches to each handler declared in experiment["comparisons"] via
    the COMPARISON_REGISTRY. Raises ValueError for unrecognised types, and
    for more than one aggregate acceptance entry (put all criteria in one).
    The active gold standard is only fetched when a sample-scope comparison
    is declared.

    Returns a dict with:
      gs_exp_version_id  — FK needed by the reporter (None when no sample-scope comparison)
      gs_samples         — {sample_id: gs_sample_dict} for gs_sample_id lookup
      comparison_results — per-sample result dicts across all sample-scope comparisons
      aggregate_results  — per-(criterion, channel) result dicts from aggregate comparisons
      observation_set    — the ObservationSet an aggregate comparison used, or None
    """
    comparisons = experiment["comparisons"]
    for comparison in comparisons:
        if comparison["type"] not in COMPARISON_REGISTRY:
            raise ValueError(f"Unknown comparison type: '{comparison['type']}'")
    aggregates = [c for c in comparisons
                  if COMPARISON_REGISTRY[c["type"]]["scope"] == AGGREGATE]
    if len(aggregates) > 1:
        raise ValueError(
            f"Experiment '{experiment['experiment_id']}' declares {len(aggregates)} "
            f"aggregate comparisons; declare one entry holding all criteria")

    gs_exp_version_id, gs_samples_by_id = None, {}
    comparison_results, aggregate_results, observation_set = [], [], None

    if any(COMPARISON_REGISTRY[c["type"]]["scope"] == SAMPLE for c in comparisons):
        active = get_active_gs_version(conn, experiment["experiment_id"])
        gs_exp_version_id = active["gs_exp_version_id"]
        gs_samples = get_gs_samples(conn, gs_exp_version_id)
        gs_samples_by_id = {s["sample_id"]: s for s in gs_samples}
        actual_samples = read_gold_standard_csv(result_csv_path)
        for comparison in comparisons:
            entry = COMPARISON_REGISTRY[comparison["type"]]
            if entry["scope"] == SAMPLE:
                comparison_results.extend(entry["fn"](actual_samples, gs_samples, comparison))

    for comparison in aggregates:
        aggregate_results, observation_set = _run_aggregate(
            experiment, comparison, result_csv_path, annotations_dir)

    return {
        "gs_exp_version_id":  gs_exp_version_id,
        "gs_samples":         gs_samples_by_id,
        "comparison_results": comparison_results,
        "aggregate_results":  aggregate_results,
        "observation_set":    observation_set,
    }
