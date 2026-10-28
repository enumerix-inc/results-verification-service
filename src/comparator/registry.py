"""
registry.py — comparison strategy registry.

Maps comparison type strings (as declared in the context manifest)
to {"fn", "scope"} entries. To add a new comparison type, add one entry
here and implement the corresponding strategy in strategies/.

scope (ADR-021):
  "sample"     fn(actual_samples, gs_samples, params) -> per-(sample, metric)
               result dicts; compared against the registered gold standard,
               stored in sample_results.
  "aggregate"  fn(observation_set, params) -> per-(criterion, channel) result
               dicts computed over groups of annotated observations; needs no
               gold standard, stored in aggregate_results (ADR-026).

See ADR-016 for the reasoning behind this pattern.
"""

from src.comparator.strategies.acceptance_criteria import run_acceptance_criteria
from src.comparator.strategies.count_tolerance import run_count_tolerance
from src.comparator.strategies.hybrid_tolerance import run_hybrid_tolerance

SAMPLE = "sample"
AGGREGATE = "aggregate"

COMPARISON_REGISTRY = {
    "count_tolerance":     {"fn": run_count_tolerance,     "scope": SAMPLE},
    "hybrid_tolerance":    {"fn": run_hybrid_tolerance,    "scope": SAMPLE},
    "acceptance_criteria": {"fn": run_acceptance_criteria, "scope": AGGREGATE},
    # future entries:
    # "file_exists":       ...,
    # "correlation":       ...,
    # "curve_fit":         ...,
    # "per_channel_count": ...,
    # "flag_detection":    ...,
}


def comparison_scope(comparison_type: str) -> str | None:
    """Return 'sample' or 'aggregate', or None for an unknown type."""
    entry = COMPARISON_REGISTRY.get(comparison_type)
    return entry["scope"] if entry else None
