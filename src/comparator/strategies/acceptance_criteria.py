"""
acceptance_criteria.py - aggregate-scope strategy: judge metrics computed over
groups of annotated observations against absolute limits (ADR-026, TES-2876).

Unlike the sample-scope strategies this one needs no gold standard. It takes
the annotated observations (src/observations.py) and the manifest entry
(validated by src/comparator/acceptance_params.py) and returns one result per
criterion per channel.

Nothing is skipped silently. A criterion that cannot be computed - a group
that does not exist or is empty, a replicate count that is wrong or
inconsistent, a missing (NA) count, a channel with no assay, a value the metric
cannot handle - produces a result with verdict 'error' and the reason in
`note`; the experiment verdict counts it as a failure.
"""

from src.comparator import metrics
from src.comparator.acceptance_params import parse_acceptance_params
from src.comparator.metrics import MetricError

COMPARISON_TYPE = "acceptance_criteria"
_NEEDS_EXPECTED = {"loglog_slope", "geomean_recovery", "plateau_ratio"}


class CriterionError(ValueError):
    """This criterion/channel cannot be computed; the message says why."""


def _group(by_group, params, group, channel, need_expected):
    """Validated observations of one group on one channel."""
    obs = by_group.get((group, channel), [])
    if not obs:
        raise CriterionError(f"group '{group}' has no observations on {channel}")
    missing = [o.sample_name for o in obs if o.count is None]
    if missing:
        raise CriterionError(
            f"{len(missing)} of {len(obs)} samples in group '{group}' have no count "
            f"on {channel} ({', '.join(missing[:3])}{'...' if len(missing) > 3 else ''})")
    if params.replicates is not None and len(obs) != params.replicates:
        raise CriterionError(
            f"group '{group}' has {len(obs)} replicates on {channel}, "
            f"expected {params.replicates}")
    expected = None
    if need_expected:
        values = {o.expected for o in obs}
        if values == {None}:
            raise CriterionError(f"group '{group}' has no assay on {channel} "
                                 f"(expected count is null)")
        if len(values) != 1 or None in values:
            raise CriterionError(f"group '{group}' has mixed expected counts on "
                                 f"{channel}: {sorted(map(str, values))}")
        expected = values.pop()
    return [o.count for o in obs], expected


def _mean_and_expected(by_group, params, groups, channel, need_expected):
    sizes, means, expecteds = set(), [], []
    for g in groups:
        counts, expected = _group(by_group, params, g, channel, need_expected)
        sizes.add(len(counts))
        means.append(metrics.mean_counts(counts))
        expecteds.append(expected)
    if len(sizes) > 1:
        raise CriterionError(f"groups {list(groups)} have different replicate counts "
                             f"on {channel}: {sorted(sizes)}")
    return means, expecteds


def _compute(criterion, params, by_group, channel) -> float:
    m = criterion.metric
    need = m in _NEEDS_EXPECTED
    if m in ("cv", "mean_counts"):
        counts, _ = _group(by_group, params, criterion.groups[0], channel, False)
        return metrics.cv(counts) if m == "cv" else metrics.mean_counts(counts)
    means, expecteds = _mean_and_expected(by_group, params, criterion.groups, channel, need)
    if m == "loglog_slope":
        return metrics.loglog_slope(expecteds, means)
    if m == "geomean_recovery":
        return metrics.geomean_recovery(expecteds, means)
    if m == "plateau_ratio":
        fit_means, fit_expected = _mean_and_expected(
            by_group, params, criterion.fit_groups, channel, True)
        s = metrics.geomean_recovery(fit_expected, fit_means)
        return metrics.plateau_ratio(s, expecteds[0], means[0])
    raise CriterionError(f"no calculation for metric '{m}'")   # METRICS and this module out of step


def run_acceptance_criteria(observation_set, comparison: dict) -> list[dict]:
    """Evaluate every criterion on every channel it covers.

    observation_set - src.observations.ObservationSet for the experiment
    comparison      - the manifest entry (type 'acceptance_criteria')

    Returns one aggregate result dict per (criterion, channel), carrying the
    provenance of the annotation and source file.
    """
    params = parse_acceptance_params(comparison)
    by_group = {}
    for o in observation_set.observations:
        by_group.setdefault((o.group, o.channel), []).append(o)

    results = []
    for c in params.criteria:
        for channel in c.channels:
            value, note, verdict, margin = None, None, "error", None
            try:
                value = _compute(c, params, by_group, channel)
                verdict = "pass" if c.passes(value) else "fail"
                margin = c.margin(value)
            except (CriterionError, MetricError) as e:
                note = str(e)
            results.append({
                "criterion_id":       c.id,
                "group_key":          "+".join(c.groups),
                "channel":            channel,
                "metric":             c.metric,
                "comparison_type":    COMPARISON_TYPE,
                "actual_value":       value,
                "expected_value":     None,
                "deviation_percent":  None,
                "lower_bound":        c.lower,
                "lower_inclusive":    c.lower_inclusive,
                "upper_bound":        c.upper,
                "upper_inclusive":    c.upper_inclusive,
                "margin":             margin,
                "verdict":            verdict,
                "note":               note,
                "annotation_version": observation_set.annotation_version,
                "annotation_sha256":  observation_set.annotation_sha256,
                "source_file_sha256": observation_set.source_sha256,
                "filters":            observation_set.filters,
            })
    return results
