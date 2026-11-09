"""
count_tolerance.py — count tolerance comparison strategy.

Compares actual pipeline count values against gold standard expected
values within a percentage tolerance defined in the manifest.

Iterates over the columns list declared in the manifest comparisons
entry, producing one result dict per (sample, column) pair.

Strategy signature: run_count_tolerance(actual_samples, gs_samples, params)
"""

import json

from src.comparator.derived_values import apply_derived_values
from src.comparator.sample_result import build_sample_result
from src.registration.registrar import SAMPLE_ID_COLUMN


def compare_sample(sample_id: str, comparison_type: str, metric: str,
                   actual_value: float, expected_value: float,
                   tolerance_percent: float) -> dict:
    """
    Compare one sample's actual count against its expected value for a
    single metric (column).

    Returns a result dict with sample_id, comparison_type, metric,
    actual_value, expected_value, deviation_percent, and verdict
    ('pass' | 'fail').

    If expected_value is zero, returns 'fail' with deviation_percent of
    None and a 'note' field — division by zero avoided.
    """
    if expected_value == 0:
        if actual_value == 0:
            return build_sample_result(
                sample_id=sample_id, comparison_type=comparison_type, metric=metric,
                actual_value=actual_value, expected_value=expected_value,
                deviation_percent=0.0, verdict="pass")
        return build_sample_result(
            sample_id=sample_id, comparison_type=comparison_type, metric=metric,
            actual_value=actual_value, expected_value=expected_value,
            deviation_percent=None, verdict="fail",
            note="expected_value is zero but actual is non-zero — division avoided")

    deviation = round(abs(actual_value - expected_value) / expected_value * 100, 4)
    return build_sample_result(
        sample_id=sample_id, comparison_type=comparison_type, metric=metric,
        actual_value=actual_value, expected_value=expected_value,
        deviation_percent=deviation,
        verdict="pass" if deviation <= tolerance_percent else "fail")


def run_count_tolerance(actual_samples: list[dict],
                        gs_samples: list[dict],
                        params: dict) -> list[dict]:
    """
    Run count tolerance comparison for all samples and all declared columns.

    actual_samples — raw CSV rows from read_gold_standard_csv() (actual results)
    gs_samples     — sample dicts from get_gs_samples() (gold standard from DB)
    params         — comparison entry from manifest, e.g.:
                     {
                       "type": "count_tolerance",
                       "tolerance_percent": 10.0,
                       "columns": ["UM-01_CountsPer50ul"]
                     }

    Returns one result dict per (sample, column) pair.
    """
    actual_by_id = {row[SAMPLE_ID_COLUMN]: row for row in actual_samples}
    comparison_type = params["type"]
    tolerance = params["tolerance_percent"]
    columns = params["columns"]
    reported_values = params.get("reported_values") or []

    results = []
    for gs_sample in gs_samples:
        sid = gs_sample["sample_id"]
        actual_row = actual_by_id.get(sid)
        gs_metrics = json.loads(gs_sample["full_metrics"])

        for col in columns:
            if actual_row is None:
                results.append(build_sample_result(
                    sample_id=sid, comparison_type=comparison_type, metric=col,
                    actual_value=None, expected_value=gs_metrics.get(col),
                    deviation_percent=None, verdict="fail",
                    note="sample not found in results"))
            else:
                results.append(compare_sample(
                    sid,
                    comparison_type,
                    col,
                    float(actual_row[col]),
                    float(gs_metrics[col]),
                    tolerance,
                ))

    for result in results:
        apply_derived_values(result, reported_values)
    return results
