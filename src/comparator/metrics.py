"""
metrics.py - calculation primitives for acceptance criteria (ADR-026, TES-2873).

Small, pure functions with no I/O and no dependencies beyond the standard
library. A criterion in the manifest names one of these (see
acceptance_params.METRICS); new math means a new tested function here, never
a script in configuration.

Definitions follow DH062 and the "v2.1 Performance testing metrics
calculation - 2: UMA Linearity" workbook, which is the golden test fixture
(tests/data/uma_linearity_golden.json):

  mean_counts       arithmetic mean of replicate counts (also the NTC metric)
  cv                sample standard deviation / mean across replicates
  loglog_slope      linearity exponent n: slope of the least-squares line of
                    log10(mean counts) on log10(expected counts)
  geomean_recovery  linear slope s: 10 ** mean(log10(counts) - log10(expected))
  plateau_ratio     (s * expected - counts) / (s * expected); positive means
                    the measured counts fall BELOW the linear prediction

Callers average replicates first (mean_counts) and pass one mean per level to
loglog_slope, geomean_recovery and plateau_ratio. Ratios are fractions
(0.20 = 20%). The calculation details are expected to be reviewed and
refined later (including QA sign-off).
"""

import math
import statistics


class MetricError(ValueError):
    """The inputs cannot produce a meaningful value."""


def _need_positive(name, values):
    bad = [v for v in values if not (v > 0) or math.isnan(v)]
    if bad:
        raise MetricError(f"{name} must all be > 0 for a log10 calculation, got {bad}")


def _paired(expected, counts, minimum):
    expected, counts = list(expected), list(counts)
    if len(expected) != len(counts):
        raise MetricError(f"expected ({len(expected)}) and counts ({len(counts)}) "
                          f"must have the same length")
    if len(expected) < minimum:
        raise MetricError(f"need at least {minimum} levels, got {len(expected)}")
    _need_positive("expected counts", expected)
    _need_positive("counts", counts)
    return expected, counts


def mean_counts(values) -> float:
    values = list(values)
    if not values:
        raise MetricError("no values to average")
    return statistics.fmean(values)


def cv(values) -> float:
    """Sample standard deviation divided by the mean (n - 1 denominator)."""
    values = list(values)
    if len(values) < 2:
        raise MetricError("CV needs at least 2 replicates")
    m = statistics.fmean(values)
    if m == 0:
        raise MetricError("CV is undefined when the mean is 0")
    return statistics.stdev(values) / m


def loglog_slope(expected, counts) -> float:
    """Linearity exponent n, from one mean count per expected level."""
    expected, counts = _paired(expected, counts, 2)
    x = [math.log10(e) for e in expected]
    y = [math.log10(c) for c in counts]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx == 0:
        raise MetricError("all expected levels are identical; slope is undefined")
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx


def geomean_recovery(expected, counts) -> float:
    """Linear slope s, from one mean count per expected level."""
    expected, counts = _paired(expected, counts, 1)
    return 10 ** statistics.fmean(
        math.log10(c) - math.log10(e) for e, c in zip(expected, counts))


def plateau_ratio(slope, expected, counts) -> float:
    """Shortfall of the measured counts against the linear prediction."""
    if not (slope > 0):
        raise MetricError(f"slope must be > 0, got {slope}")
    if not (expected > 0):
        raise MetricError(f"expected must be > 0, got {expected}")
    predicted = slope * expected
    return (predicted - counts) / predicted
