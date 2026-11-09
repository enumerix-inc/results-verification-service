"""
sample_result.py - the one place that defines the shape of a sample-scope result.

Every sample-scope strategy (count_tolerance, hybrid_tolerance, ...) builds its
per-(sample, column) result through build_sample_result(), so the fields of a
result are owned here and nowhere else. The orchestrator and reporter read the
result by key, so adding a value means changing this builder (and whichever
output wants to show it), not every strategy.

Fields
  sample_id, comparison_type
  metric           the measured column (e.g. "e14a2"), NOT a calculation
  actual_value, expected_value
  deviation_percent  absolute percent deviation used by the tolerance check, or None
  verdict          "pass" | "fail"
  note             free text explaining a verdict, or None
  derived          report-only calculated values by name, e.g.
                   {"absolute_difference": 180.0}. Never consulted for a verdict.
                   Always a dict (empty when nothing was requested). Output layers
                   carry it without naming its keys; the database insert ignores it
                   for now.
"""


def build_sample_result(*, sample_id, comparison_type, metric, actual_value,
                        expected_value, deviation_percent, verdict,
                        note=None, derived=None) -> dict:
    return {
        "sample_id":         sample_id,
        "comparison_type":   comparison_type,
        "metric":            metric,
        "actual_value":      actual_value,
        "expected_value":    expected_value,
        "deviation_percent": deviation_percent,
        "verdict":           verdict,
        "note":              note,
        "derived":           dict(derived) if derived else {},
    }
