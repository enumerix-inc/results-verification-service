"""
acceptance_params.py - parse and validate the `params` of an acceptance
comparison entry in the run context manifest (ADR-026, TES-2872).

A manifest entry looks like:

  {
    "type": "acceptance_criteria",
    "annotation": "MP37_V_V_Testcase_2_beta05",      # optional, default: the experiment_id
    "columns": {"Ch01": "UM-01_CountsPer50ul", ...},  # channel -> result CSV column
    "replicates": 8,                                 # optional: required samples per group
    "criteria": [
      {"id": "linearity_exponent", "metric": "loglog_slope",
       "test_case": "TC1.2",                           # optional label for reports
       "groups": ["FourPlex_1k", "FourPlex_10k", "FourPlex_100k"],
       "lower": 0.9, "lower_inclusive": false,
       "upper": 1.1, "upper_inclusive": false},
      ...
    ]
  }

`groups` name annotation groups (see src/annotation); a group is a label, so
many criteria can share the same samples. Every bound states whether it is
inclusive; there is no default. Ratios (PR, CV) are fractions: 0.20 = 20%.
Channel names are the keys of `columns`; a criterion may restrict itself to a
subset with `channels`.

This module only checks the SHAPE of the entry. Whether the groups exist in
the annotation, and the metric maths itself, are checked at evaluation time.
"""

from dataclasses import dataclass

TYPE = "acceptance_criteria"

# metric name -> (min number of groups, max number or None, extra required keys)
METRICS = {
    "loglog_slope":      (2, None, ()),             # linearity exponent n
    "geomean_recovery":  (1, None, ()),             # linear slope s
    "plateau_ratio":     (1, 1,    ("fit_groups",)),  # PR at one level, s fitted on fit_groups
    "cv":                (1, 1,    ()),             # CV at one level
    "mean_counts":       (1, 1,    ()),             # e.g. NTC mean
}

_ENTRY_KEYS = {"type", "annotation", "columns", "replicates", "criteria"}
_CRITERION_KEYS = {"id", "metric", "groups", "fit_groups", "channels", "test_case",
                   "lower", "lower_inclusive", "upper", "upper_inclusive"}


class AcceptanceParamsError(ValueError):
    """The acceptance comparison entry is malformed."""


@dataclass(frozen=True)
class Criterion:
    id: str
    metric: str
    groups: tuple
    fit_groups: tuple
    channels: tuple
    lower: float | None
    lower_inclusive: bool | None
    upper: float | None
    upper_inclusive: bool | None
    test_case: str | None = None     # label for reports, e.g. "TC1.2"

    def passes(self, value: float) -> bool:
        if self.lower is not None and not (
                value >= self.lower if self.lower_inclusive else value > self.lower):
            return False
        if self.upper is not None and not (
                value <= self.upper if self.upper_inclusive else value < self.upper):
            return False
        return True

    def margin(self, value: float) -> float:
        """Distance to the nearest bound; negative when the value is outside."""
        gaps = []
        if self.lower is not None:
            gaps.append(value - self.lower)
        if self.upper is not None:
            gaps.append(self.upper - value)
        return min(gaps)


@dataclass(frozen=True)
class AcceptanceParams:
    annotation: str | None
    columns: dict
    criteria: tuple
    replicates: int | None = None


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _str_list(where, value, minimum=1):
    if (not isinstance(value, list) or len(value) < minimum
            or not all(isinstance(v, str) and v for v in value)
            or len(set(value)) != len(value)):
        raise AcceptanceParamsError(
            f"{where} must be a list of at least {minimum} unique non-empty strings")
    return tuple(value)


def _parse_criterion(i, c, channels) -> Criterion:
    where = f"criteria[{i}]"
    if not isinstance(c, dict):
        raise AcceptanceParamsError(f"{where} must be an object")
    cid = c.get("id")
    if not isinstance(cid, str) or not cid:
        raise AcceptanceParamsError(f"{where} needs a non-empty string 'id'")
    where = f"criterion '{cid}'"
    unknown = set(c) - _CRITERION_KEYS
    if unknown:
        raise AcceptanceParamsError(f"{where}: unknown key(s) {sorted(unknown)}")
    metric = c.get("metric")
    if metric not in METRICS:
        raise AcceptanceParamsError(
            f"{where}: unknown metric {metric!r} (known: {', '.join(sorted(METRICS))})")
    lo, hi, extra = METRICS[metric]
    groups = _str_list(f"{where} groups", c.get("groups"), lo)
    if hi is not None and len(groups) > hi:
        raise AcceptanceParamsError(f"{where}: metric '{metric}' takes at most {hi} group(s)")
    fit = ()
    if "fit_groups" in extra:
        fit = _str_list(f"{where} fit_groups", c.get("fit_groups"))
    elif "fit_groups" in c:
        raise AcceptanceParamsError(f"{where}: 'fit_groups' is not used by metric '{metric}'")

    chans = tuple(channels)
    if "channels" in c:
        chans = _str_list(f"{where} channels", c["channels"])
        missing = [ch for ch in chans if ch not in channels]
        if missing:
            raise AcceptanceParamsError(f"{where}: channels {missing} are not in 'columns'")

    test_case = c.get("test_case")
    if test_case is not None and (not isinstance(test_case, str) or not test_case.strip()):
        raise AcceptanceParamsError(f"{where}: test_case must be a non-empty string")

    bounds = {}
    for side in ("lower", "upper"):
        val, flag = c.get(side), c.get(f"{side}_inclusive")
        if side not in c:
            if f"{side}_inclusive" in c:
                raise AcceptanceParamsError(f"{where}: '{side}_inclusive' given without '{side}'")
            bounds[side] = (None, None)
            continue
        if not _num(val):
            raise AcceptanceParamsError(f"{where}: '{side}' must be a number, got {val!r}")
        if not isinstance(flag, bool):
            raise AcceptanceParamsError(
                f"{where}: '{side}_inclusive' (true/false) is required with '{side}'")
        bounds[side] = (val, flag)
    (lv, lf), (uv, uf) = bounds["lower"], bounds["upper"]
    if lv is None and uv is None:
        raise AcceptanceParamsError(f"{where}: needs at least one of 'lower' or 'upper'")
    if lv is not None and uv is not None and (lv > uv or (lv == uv and not (lf and uf))):
        raise AcceptanceParamsError(f"{where}: bounds leave no passing values")
    return Criterion(cid, metric, groups, fit, chans, lv, lf, uv, uf, test_case)


def parse_acceptance_params(entry: dict) -> AcceptanceParams:
    """Validate one `comparisons` entry of type 'acceptance_criteria'."""
    if not isinstance(entry, dict):
        raise AcceptanceParamsError("comparison entry must be an object")
    if entry.get("type") != TYPE:
        raise AcceptanceParamsError(f"type must be '{TYPE}', got {entry.get('type')!r}")
    unknown = set(entry) - _ENTRY_KEYS
    if unknown:
        raise AcceptanceParamsError(f"unknown key(s) {sorted(unknown)}")
    ann = entry.get("annotation")
    if ann is not None and (not isinstance(ann, str) or not ann):
        raise AcceptanceParamsError("annotation must be a non-empty string (an experiment_id)")
    cols = entry.get("columns")
    if (not isinstance(cols, dict) or not cols
            or not all(isinstance(k, str) and isinstance(v, str) and k and v
                       for k, v in cols.items())):
        raise AcceptanceParamsError(
            "columns must be a non-empty mapping of channel -> CSV column name")
    if len(set(cols.values())) != len(cols):
        raise AcceptanceParamsError("columns maps two channels to the same CSV column")
    reps = entry.get("replicates")
    if reps is not None and (not isinstance(reps, int) or isinstance(reps, bool) or reps < 1):
        raise AcceptanceParamsError("replicates must be a positive integer")
    raw = entry.get("criteria")
    if not isinstance(raw, list) or not raw:
        raise AcceptanceParamsError("criteria must be a non-empty list")
    criteria = tuple(_parse_criterion(i, c, list(cols)) for i, c in enumerate(raw))
    ids = [c.id for c in criteria]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise AcceptanceParamsError(f"duplicate criterion id(s): {dup}")
    return AcceptanceParams(ann, dict(cols), criteria, reps)
