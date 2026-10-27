"""
Tests for src/comparator/acceptance_params.py (ADR-026, TES-2872).
"""

import copy
import json
from pathlib import Path

import pytest

from src.annotation.annotation import load_annotation
from src.comparator.acceptance_params import (METRICS, AcceptanceParamsError,
                                              parse_acceptance_params)

REPO = Path(__file__).resolve().parents[1]
MP37 = REPO / "annotations" / "MP37_V_V_Testcase_2_beta05.yaml"
LEVELS = ["0", "10", "100", "1k", "10k", "100k", "1M", "1p5M"]
TEMPLATE = REPO / "entry_point" / "manifest_template_acceptance.json"


def mp37_sample_names():
    names = [f"FourPlex_{lv}_{r}" for lv in LEVELS for r in range(1, 9)]
    return names + [f"Ch{c}_100k_{r}" for c in range(1, 5) for r in range(1, 9)]


def template_entry():
    t = json.loads(TEMPLATE.read_text())
    return t["experiments"][0]["comparisons"][0]


def bad(mutate, msg):
    e = copy.deepcopy(template_entry())
    mutate(e)
    with pytest.raises(AcceptanceParamsError, match=msg):
        parse_acceptance_params(e)


def crit(e, cid):
    return next(c for c in e["criteria"] if c["id"] == cid)


# ---- the TES-2869 entry -----------------------------------------------------

def test_template_entry_loads_with_the_eight_criteria():
    p = parse_acceptance_params(template_entry())
    assert [c.id for c in p.criteria] == [
        "linearity_exponent", "linear_slope", "plateauing_ratio_1M",
        "cv_1k", "cv_10k", "cv_100k", "cv_1M", "ntc_mean_counts"]
    assert list(p.columns) == ["Ch01", "Ch02", "Ch03", "Ch04"]
    assert p.annotation == "MP37_V_V_Testcase_2_beta05"
    assert all(c.channels == ("Ch01", "Ch02", "Ch03", "Ch04") for c in p.criteria)


def test_template_groups_exist_in_mp37_annotation():
    groups = {a.group for a in load_annotation(MP37).annotate(mp37_sample_names())}
    p = parse_acceptance_params(template_entry())
    for c in p.criteria:
        assert set(c.groups + c.fit_groups) <= groups, c.id


def test_template_bounds_match_the_spec():
    by = {c.id: c for c in parse_acceptance_params(template_entry()).criteria}
    n, s, pr = by["linearity_exponent"], by["linear_slope"], by["plateauing_ratio_1M"]
    assert (n.lower, n.lower_inclusive, n.upper, n.upper_inclusive) == (0.9, False, 1.1, False)
    assert (s.lower, s.upper) == (0.7, 1.2)
    assert (pr.lower, pr.lower_inclusive, pr.upper, pr.upper_inclusive) == (-0.2, True, 0.2, True)
    assert by["cv_1k"].upper == 0.1 and by["cv_1k"].lower is None
    assert by["ntc_mean_counts"].upper == 50


# ---- bound semantics ---------------------------------------------------------

def test_exclusive_vs_inclusive_bounds():
    by = {c.id: c for c in parse_acceptance_params(template_entry()).criteria}
    n, pr = by["linearity_exponent"], by["plateauing_ratio_1M"]
    assert not n.passes(0.9) and not n.passes(1.1) and n.passes(1.0)
    assert pr.passes(0.2) and pr.passes(-0.2) and not pr.passes(0.2001)
    assert not by["ntc_mean_counts"].passes(50) and by["ntc_mean_counts"].passes(49.9)


def test_margin_is_distance_to_nearest_bound():
    n = {c.id: c for c in parse_acceptance_params(template_entry()).criteria}["linearity_exponent"]
    assert n.margin(0.984) == pytest.approx(0.084)   # Ch1 in the UMA workbook
    assert n.margin(0.85) < 0


# ---- rejection ---------------------------------------------------------------

def test_unknown_metric_rejected():
    bad(lambda e: crit(e, "cv_1k").update(metric="cvv"), "unknown metric 'cvv'")


def test_every_known_metric_is_listed_in_error():
    bad(lambda e: crit(e, "cv_1k").update(metric="x"), "|".join(METRICS))


def test_missing_inclusive_flag_rejected():
    bad(lambda e: crit(e, "cv_1k").pop("upper_inclusive"), "upper_inclusive.*required")


def test_non_boolean_inclusive_rejected():
    bad(lambda e: crit(e, "cv_1k").update(upper_inclusive="no"), "upper_inclusive.*required")


def test_non_numeric_bound_rejected():
    bad(lambda e: crit(e, "cv_1k").update(upper="10%"), "'upper' must be a number")
    bad(lambda e: crit(e, "cv_1k").update(upper=True), "'upper' must be a number")


def test_no_bounds_rejected():
    def m(e):
        for k in ("upper", "upper_inclusive"):
            crit(e, "cv_1k").pop(k)
    bad(m, "at least one of")


def test_inclusive_without_bound_rejected():
    bad(lambda e: crit(e, "cv_1k").update(lower_inclusive=True), "without 'lower'")


def test_inverted_or_empty_bounds_rejected():
    bad(lambda e: crit(e, "linearity_exponent").update(lower=1.2), "no passing values")
    bad(lambda e: crit(e, "linearity_exponent").update(lower=1.0, upper=1.0),
        "no passing values")   # both exclusive


def test_equal_inclusive_bounds_allowed():
    e = template_entry()
    c = crit(e, "linearity_exponent")
    c.update(lower=1.0, upper=1.0, lower_inclusive=True, upper_inclusive=True)
    parse_acceptance_params(e)


def test_groups_rules():
    bad(lambda e: crit(e, "cv_1k").update(groups=[]), "groups")
    bad(lambda e: crit(e, "cv_1k").update(groups=["a", "b"]), "at most 1")
    bad(lambda e: crit(e, "linearity_exponent").update(groups=["FourPlex_1k"]), "at least 2")
    bad(lambda e: crit(e, "cv_1k").update(groups=["a", "a"]), "unique")


def test_plateau_ratio_needs_fit_groups_others_reject_them():
    bad(lambda e: crit(e, "plateauing_ratio_1M").pop("fit_groups"), "fit_groups")
    bad(lambda e: crit(e, "cv_1k").update(fit_groups=["FourPlex_1k"]), "not used")


def test_unknown_keys_rejected():
    bad(lambda e: crit(e, "cv_1k").update(limit=3), "unknown key")
    bad(lambda e: e.update(evaluator="x"), "unknown key")


def test_duplicate_ids_rejected():
    bad(lambda e: crit(e, "cv_10k").update(id="cv_1k"), "duplicate criterion id")


def test_channel_scope():
    e = template_entry()
    crit(e, "cv_1k")["channels"] = ["Ch02"]
    assert next(c for c in parse_acceptance_params(e).criteria if c.id == "cv_1k").channels == ("Ch02",)
    bad(lambda e: crit(e, "cv_1k").update(channels=["Ch09"]), "not in 'columns'")


def test_columns_rules():
    bad(lambda e: e.update(columns={}), "columns")
    bad(lambda e: e.update(columns={"Ch01": "x", "Ch02": "x"}), "same CSV column")


def test_wrong_type_and_empty_criteria():
    bad(lambda e: e.update(type="count_tolerance"), "type must be")
    bad(lambda e: e.update(criteria=[]), "criteria")
