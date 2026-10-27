"""
Tests for src/comparator/metrics.py, validated against the UMA linearity
workbook (tests/data/uma_linearity_golden.json). ADR-026, TES-2873.
"""

import json
import math
from pathlib import Path

import pytest

from src.comparator import acceptance_params, metrics
from src.comparator.metrics import MetricError

GOLDEN = json.loads((Path(__file__).parent / "data" / "uma_linearity_golden.json").read_text())
LEVELS = GOLDEN["levels"]
CHANNELS = sorted(GOLDEN["channels"])
IDX = {lv: i for i, lv in enumerate(LEVELS)}
LIN = [1000, 10000, 100000]


def means(ch):
    return [metrics.mean_counts(r) for r in GOLDEN["channels"][ch]["replicates"]]


def fit_inputs(ch):
    m = means(ch)
    return LIN, [m[IDX[lv]] for lv in LIN]


# ---- golden values from the workbook ----------------------------------------

@pytest.mark.parametrize("ch", CHANNELS)
def test_linearity_exponent_matches_workbook(ch):
    exp, cnt = fit_inputs(ch)
    assert metrics.loglog_slope(exp, cnt) == pytest.approx(
        GOLDEN["channels"][ch]["linearity_exponent"], abs=1e-9)


def test_ch1_linearity_exponent_is_0_984():
    exp, cnt = fit_inputs("Ch01")
    assert round(metrics.loglog_slope(exp, cnt), 3) == 0.984


@pytest.mark.parametrize("ch", CHANNELS)
def test_linear_slope_matches_workbook(ch):
    exp, cnt = fit_inputs(ch)
    assert metrics.geomean_recovery(exp, cnt) == pytest.approx(
        GOLDEN["channels"][ch]["linear_slope"], abs=1e-9)


@pytest.mark.parametrize("ch", CHANNELS)
def test_plateau_ratio_matches_workbook(ch):
    s = metrics.geomean_recovery(*fit_inputs(ch))
    pr = metrics.plateau_ratio(s, 1_000_000, means(ch)[IDX[1_000_000]])
    assert pr == pytest.approx(GOLDEN["channels"][ch]["plateau_ratio_1M"], abs=1e-9)


@pytest.mark.parametrize("ch", CHANNELS)
def test_cv_matches_workbook_at_every_level(ch):
    g = GOLDEN["channels"][ch]
    for lv, reps, expected in zip(LEVELS, g["replicates"], g["cv"]):
        if sum(reps) == 0:
            continue
        assert metrics.cv(reps) == pytest.approx(expected, abs=1e-9), (ch, lv)


def test_workbook_cv_is_sample_not_population_stdev():
    reps = GOLDEN["channels"]["Ch01"]["replicates"][0]
    pop = math.sqrt(sum((x - sum(reps) / 8) ** 2 for x in reps) / 8) / (sum(reps) / 8)
    assert metrics.cv(reps) != pytest.approx(pop)


def test_ntc_mean_and_the_all_criteria_pass_on_the_workbook():
    """The workbook data meets every first-delivery criterion (PR is close)."""
    for ch in CHANNELS:
        m, g = means(ch), GOLDEN["channels"][ch]
        assert m[IDX[0]] < 50
        n = metrics.loglog_slope(*fit_inputs(ch))
        s = metrics.geomean_recovery(*fit_inputs(ch))
        pr = metrics.plateau_ratio(s, 1_000_000, m[IDX[1_000_000]])
        assert 0.9 < n < 1.1 and 0.7 < s < 1.2 and -0.2 <= pr <= 0.2
        for lv in (1000, 10000, 100000, 1000000):
            assert metrics.cv(g["replicates"][IDX[lv]]) < 0.10


# ---- hand-checkable cases ----------------------------------------------------

def test_perfectly_linear_data():
    exp = [1000, 10000, 100000]
    assert metrics.loglog_slope(exp, exp) == pytest.approx(1.0)
    assert metrics.geomean_recovery(exp, exp) == pytest.approx(1.0)
    half = [e / 2 for e in exp]
    assert metrics.loglog_slope(exp, half) == pytest.approx(1.0)
    assert metrics.geomean_recovery(exp, half) == pytest.approx(0.5)


def test_loglog_slope_of_a_square_law_is_two():
    exp = [10, 100, 1000]
    assert metrics.loglog_slope(exp, [e ** 2 for e in exp]) == pytest.approx(2.0)


def test_plateau_ratio_sign_convention():
    assert metrics.plateau_ratio(1.0, 1000, 800) == pytest.approx(0.2)    # below prediction
    assert metrics.plateau_ratio(1.0, 1000, 1200) == pytest.approx(-0.2)  # above prediction
    assert metrics.plateau_ratio(0.5, 1000, 500) == 0


def test_cv_and_mean_simple():
    assert metrics.mean_counts([1, 2, 3]) == 2
    assert metrics.cv([2, 4]) == pytest.approx(math.sqrt(2) / 3)


def test_inputs_may_be_any_iterable():
    assert metrics.mean_counts(iter([2, 4])) == 3
    assert metrics.cv(x for x in [2, 4]) == pytest.approx(math.sqrt(2) / 3)


# ---- bad inputs --------------------------------------------------------------

def test_cv_needs_two_replicates_and_nonzero_mean():
    with pytest.raises(MetricError, match="at least 2"):
        metrics.cv([5])
    with pytest.raises(MetricError, match="mean is 0"):
        metrics.cv([0, 0, 0])


def test_mean_of_nothing():
    with pytest.raises(MetricError):
        metrics.mean_counts([])


def test_log_metrics_reject_zero_and_negative():
    with pytest.raises(MetricError, match="> 0"):
        metrics.loglog_slope([10, 100], [0, 100])
    with pytest.raises(MetricError, match="> 0"):
        metrics.geomean_recovery([0, 100], [5, 100])
    with pytest.raises(MetricError, match="> 0"):
        metrics.loglog_slope([10, 100], [float("nan"), 100])


def test_length_mismatch_and_too_few_levels():
    with pytest.raises(MetricError, match="same length"):
        metrics.loglog_slope([10, 100], [10])
    with pytest.raises(MetricError, match="at least 2"):
        metrics.loglog_slope([10], [10])
    with pytest.raises(MetricError, match="identical"):
        metrics.loglog_slope([10, 10], [5, 6])


def test_plateau_ratio_rejects_bad_slope_or_expected():
    with pytest.raises(MetricError):
        metrics.plateau_ratio(0, 1000, 5)
    with pytest.raises(MetricError):
        metrics.plateau_ratio(1, 0, 5)


# ---- the manifest and the code agree on the metric names ---------------------

def test_every_manifest_metric_has_a_primitive():
    for name in acceptance_params.METRICS:
        assert callable(getattr(metrics, name)), name
