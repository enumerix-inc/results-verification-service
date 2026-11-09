"""
hybrid_tolerance.py — hybrid percent/absolute tolerance comparison strategy.

Same shape as count_tolerance (sample-scope: one result per sample per
declared column), but switches between percent and absolute deviation
based on the gold standard's expected value:

  expected_value >= count_threshold  -> percent tolerance (as count_tolerance)
  expected_value <  count_threshold  -> absolute tolerance

This exists because percent deviation is noisy/meaningless near zero —
e.g. a count of 1 vs an expected 2 is a 50% deviation despite being a
negligible absolute difference. Below the threshold, an absolute
difference is the more meaningful check.

See ADR-021 (comparison registry extension) for the reasoning behind
generalizing this as a reusable sample-scope strategy rather than
one-off logic per feature. Sample-scope strategies share the
sample_results schema as-is — no schema change needed.

Strategy signature: run_hybrid_tolerance(actual_samples, gs_samples, params)
"""

import json

from src.comparator.derived_values import apply_derived_values
from src.comparator.sample_result import build_sample_result
from src.registration.registrar import SAMPLE_ID_COLUMN


def compare_sample_hybrid(sample_id: str, comparison_type: str, metric: str,
                          actual_value: float, expected_value: float,
                          tolerance_percent: float, absolute_tolerance: float,
                          count_threshold: float) -> dict:
    """
    Compare one sample's actual value against its expected value for a
    single metric, using absolute tolerance below count_threshold and
    percent tolerance at or above it.

    Returns a result dict with sample_id, comparison_type, metric,
    actual_value, expected_value, deviation_percent, and verdict
    ('pass' | 'fail'). deviation_percent is None when absolute tolerance
    was applied — the absolute deviation and which mode was used are
    recorded in 'note' instead, since sample_results has no dedicated
    absolute-deviation column.
    """
    if expected_value < count_threshold:
        deviation_absolute = abs(actual_value - expected_value)
        verdict = "pass" if deviation_absolute <= absolute_tolerance else "fail"
        return build_sample_result(
            sample_id=sample_id, comparison_type=comparison_type, metric=metric,
            actual_value=actual_value, expected_value=expected_value,
            deviation_percent=None, verdict=verdict,
            note=(
                f"expected_value {expected_value} below count_threshold "
                f"{count_threshold} — absolute tolerance applied "
                f"(deviation {deviation_absolute}, tolerance ±{absolute_tolerance})"
            ))

    deviation = round(abs(actual_value - expected_value) / expected_value * 100, 4)
    return build_sample_result(
        sample_id=sample_id, comparison_type=comparison_type, metric=metric,
        actual_value=actual_value, expected_value=expected_value,
        deviation_percent=deviation,
        verdict="pass" if deviation <= tolerance_percent else "fail")


def run_hybrid_tolerance(actual_samples: list[dict],
                         gs_samples: list[dict],
                         params: dict) -> list[dict]:
    """
    Run hybrid tolerance comparison for all samples and all declared columns.

    actual_samples — raw CSV rows from read_gold_standard_csv() (actual results)
    gs_samples     — sample dicts from get_gs_samples() (gold standard from DB)
    params         — comparison entry from manifest, e.g.:
                     {
                       "type": "hybrid_tolerance",
                       "tolerance_percent": 10.0,
                       "absolute_tolerance": 2,
                       "count_threshold": 100,
                       "columns": ["UM-01_UM-02"]
                     }

    Returns one result dict per (sample, column) pair.
    """
    actual_by_id = {row[SAMPLE_ID_COLUMN]: row for row in actual_samples}
    comparison_type = params["type"]
    tolerance_percent = params["tolerance_percent"]
    absolute_tolerance = params["absolute_tolerance"]
    count_threshold = params["count_threshold"]
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
                results.append(compare_sample_hybrid(
                    sid,
                    comparison_type,
                    col,
                    float(actual_row[col]),
                    float(gs_metrics[col]),
                    tolerance_percent,
                    absolute_tolerance,
                    count_threshold,
                ))

    for result in results:
        apply_derived_values(result, reported_values)
    return results
