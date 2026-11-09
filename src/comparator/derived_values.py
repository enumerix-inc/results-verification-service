"""
derived_values.py - report-only calculations shown next to each sample result (TES-3010).

Measure and judge are separate steps. A tolerance check judges (pass/fail); a
derived value only MEASURES, for a human to read. Derived values never feed a
verdict, and failing to compute one never fails a run: the cell is left blank
(and, when the reason is worth knowing, a note says why).

Each derived value is a small pure function of (actual, expected) registered by
name. New calculations are a new function plus one registry line, never a
formula in configuration. Both are signed (actual minus the gold standard):

  absolute_difference          actual - expected
  relative_difference_percent  (actual - expected) / expected * 100   (blank when expected is 0)

Declared in the manifest with `reported_values`, on a sample-scope comparison
entry or as an experiment-level default that its sample-scope comparisons inherit:

  {"experiment_id": "...", "reported_values": ["absolute_difference"], "comparisons": [...]}

Note: in this service a "metric" is the measured column (e.g. "e14a2"); these
calculations are called derived values to keep the two apart. For a percentage
column the absolute difference is in percentage points.

The column order in reports is the order of DERIVED_VALUES, not the manifest's.
"""

import logging

log = logging.getLogger(__name__)

_DECIMALS = 4


class DerivedValueError(ValueError):
    """A manifest names a derived value that does not exist or is declared wrongly."""


class Undefined(Exception):
    """The value has no meaningful result for these inputs (e.g. division by a zero gold value)."""


def absolute_difference(actual: float, expected: float) -> float:
    return round(actual - expected, _DECIMALS)


def relative_difference_percent(actual: float, expected: float) -> float:
    if expected == 0:
        raise Undefined("expected_value is 0")
    return round((actual - expected) / expected * 100, _DECIMALS)


# Order here is the order of the report columns.
DERIVED_VALUES = {
    "absolute_difference":         absolute_difference,
    "relative_difference_percent": relative_difference_percent,
}


def _check_names(value, where: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{where}: reported_values must be a list of names, got {type(value).__name__}"]
    problems = []
    for name in value:
        if not isinstance(name, str) or name not in DERIVED_VALUES:
            problems.append(f"{where}: unknown reported value {name!r} "
                            f"(known: {', '.join(DERIVED_VALUES)})")
    if len(set(n for n in value if isinstance(n, str))) != len([n for n in value if isinstance(n, str)]):
        problems.append(f"{where}: reported_values lists a name more than once")
    return problems


def validate_reported_values(experiments: list[dict]) -> list[str]:
    """Problems (empty = ok) with `reported_values` anywhere in these (already panel-expanded) experiments."""
    from src.comparator.registry import SAMPLE, comparison_scope  # lazy: registry imports the strategies
    problems = []
    for exp in experiments:
        if not isinstance(exp, dict):
            continue
        eid = exp.get("experiment_id")
        if "reported_values" in exp:
            problems += _check_names(exp["reported_values"], str(eid))
        for comp in exp.get("comparisons") or []:
            if not isinstance(comp, dict) or "reported_values" not in comp:
                continue
            where = f"{eid} ({comp.get('type')})"
            if comparison_scope(comp.get("type")) not in (SAMPLE, None):
                problems.append(f"{where}: reported_values applies to sample-scope comparisons only")
                continue
            problems += _check_names(comp["reported_values"], where)
    return problems


def resolve_reported_values(experiment: dict, comparison: dict) -> list[str]:
    """The comparison's own list if it has one, else the experiment's default, else none."""
    if "reported_values" in comparison:
        return list(comparison["reported_values"] or [])
    return list(experiment.get("reported_values") or [])


def apply_derived_values(result: dict, names) -> dict:
    """
    Fill result["derived"] for the requested names, in registry order. Never raises:
    a value that cannot be computed is recorded as None, with a note when the
    reason is the data (zero gold value), and the verdict is left untouched.
    """
    wanted = [n for n in DERIVED_VALUES if n in (names or [])]
    if not wanted:
        return result
    actual, expected = result.get("actual_value"), result.get("expected_value")
    notes = []
    for name in wanted:
        value = None
        if actual is not None and expected is not None:
            try:
                value = DERIVED_VALUES[name](float(actual), float(expected))
            except Undefined as e:
                notes.append(f"{name} not computed: {e}")
            except (TypeError, ValueError, ArithmeticError) as e:
                log.warning("derived value %s failed for %s/%s: %s",
                            name, result.get("sample_id"), result.get("metric"), e)
                notes.append(f"{name} not computed: {type(e).__name__}")
        result["derived"][name] = value
    if notes:
        result["note"] = "; ".join(filter(None, [result.get("note"), *notes]))
    return result
