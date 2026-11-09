"""
Tests for report-only derived values (TES-3010): signed absolute and relative
differences shown in the detail report, never affecting a verdict.
"""

import csv
import io
import json
from pathlib import Path

import pytest

from src.comparator.comparator import compare_experiment
from src.comparator.derived_values import (DERIVED_VALUES, apply_derived_values,
                                           resolve_reported_values, validate_reported_values)
from src.comparator.sample_result import build_sample_result
from src.comparator.strategies.count_tolerance import run_count_tolerance
from src.comparator.strategies.hybrid_tolerance import run_hybrid_tolerance
from src.database import models
from src.database.db import get_connection, initialise_database
from src.reporter.reporter import write_report

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "verification_schema.sql"
BOTH = ["absolute_difference", "relative_difference_percent"]


def result(actual, expected):
    return build_sample_result(sample_id="S", comparison_type="t", metric="m", actual_value=actual,
                               expected_value=expected, deviation_percent=None, verdict="pass")


# ---- the calculations ---------------------------------------------------------------------

def test_both_values_are_signed_against_the_gold_standard():
    r = apply_derived_values(result(1180, 1000), BOTH)
    assert r["derived"] == {"absolute_difference": 180.0, "relative_difference_percent": 18.0}
    r = apply_derived_values(result(820, 1000), BOTH)
    assert r["derived"] == {"absolute_difference": -180.0, "relative_difference_percent": -18.0}


def test_a_zero_gold_value_blanks_the_relative_value_and_says_why_but_keeps_the_absolute():
    r = apply_derived_values(result(3, 0), BOTH)
    assert r["derived"] == {"absolute_difference": 3.0, "relative_difference_percent": None}
    assert "relative_difference_percent not computed" in r["note"] and r["verdict"] == "pass"


def test_zero_actual_and_zero_gold_gives_zero_absolute_and_blank_relative():
    r = apply_derived_values(result(0, 0), BOTH)
    assert r["derived"] == {"absolute_difference": 0.0, "relative_difference_percent": None}


def test_a_missing_value_gives_blanks_without_inventing_a_note():
    r = apply_derived_values(result(None, 50), BOTH)
    assert r["derived"] == {"absolute_difference": None, "relative_difference_percent": None}
    assert r["note"] is None


def test_unrequested_values_are_not_computed_and_order_follows_the_registry():
    assert apply_derived_values(result(10, 5), [])["derived"] == {}
    r = apply_derived_values(result(10, 5), ["relative_difference_percent", "absolute_difference"])
    assert list(r["derived"]) == list(DERIVED_VALUES)


def test_garbage_input_never_raises():
    r = apply_derived_values(result("x", 5), BOTH)
    assert r["derived"] == {"absolute_difference": None, "relative_difference_percent": None}
    assert "not computed" in r["note"]


def test_existing_notes_are_kept():
    r = result(3, 0)
    r["note"] = "existing"
    assert apply_derived_values(r, BOTH)["note"].startswith("existing; ")


# ---- manifest field and validation --------------------------------------------------------

def test_comparison_list_wins_over_the_experiment_default():
    exp = {"reported_values": ["absolute_difference"]}
    assert resolve_reported_values(exp, {}) == ["absolute_difference"]
    assert resolve_reported_values(exp, {"reported_values": ["relative_difference_percent"]}) == ["relative_difference_percent"]
    assert resolve_reported_values(exp, {"reported_values": []}) == []
    assert resolve_reported_values({}, {}) == []


def exp(**kw):
    return {"experiment_id": "E", "comparisons": [{"type": "count_tolerance", "tolerance_percent": 1,
                                                    "columns": ["c"]}], **kw}


def test_valid_declarations_pass():
    assert validate_reported_values([exp(reported_values=BOTH)]) == []
    e = exp()
    e["comparisons"][0]["reported_values"] = ["absolute_difference"]
    assert validate_reported_values([e]) == []


@pytest.mark.parametrize("value, text", [
    (["absolut_difference"], "unknown reported value"),
    ("absolute_difference", "must be a list"),
    (["absolute_difference", "absolute_difference"], "more than once"),
    ([3], "unknown reported value"),
])
def test_bad_declarations_are_reported(value, text):
    problems = validate_reported_values([exp(reported_values=value)])
    assert problems and text in problems[0] and problems[0].startswith("E")


def test_reported_values_on_an_aggregate_comparison_is_refused():
    e = {"experiment_id": "E", "comparisons": [{"type": "acceptance_criteria", "reported_values": BOTH}]}
    assert "sample-scope" in validate_reported_values([e])[0]


def test_an_experiment_default_is_fine_alongside_an_acceptance_entry():
    e = {"experiment_id": "E", "reported_values": BOTH,
         "comparisons": [{"type": "acceptance_criteria"}]}
    assert validate_reported_values([e]) == []


# ---- strategies never let derived values touch a verdict ------------------------------------

def _inputs():
    gs = [{"sample_id": "S1", "full_metrics": json.dumps({"c": 1000, "lo": 5})},
          {"sample_id": "S2", "full_metrics": json.dumps({"c": 1000, "lo": 0})}]
    actual = [{"SampleID": "S1", "c": "1500", "lo": "6"}, {"SampleID": "S2", "c": "1005", "lo": "1"}]
    return actual, gs


def test_derived_values_never_change_a_verdict_in_either_strategy():
    actual, gs = _inputs()
    ct = {"type": "count_tolerance", "tolerance_percent": 10, "columns": ["c"]}
    hy = {"type": "hybrid_tolerance", "tolerance_percent": 10, "absolute_tolerance": 2,
          "count_threshold": 100, "columns": ["c", "lo"]}
    for run, params in ((run_count_tolerance, ct), (run_hybrid_tolerance, hy)):
        plain = run(actual, gs, params)
        rich = run(actual, gs, {**params, "reported_values": BOTH})
        assert [r["verdict"] for r in plain] == [r["verdict"] for r in rich]
        assert [r["deviation_percent"] for r in plain] == [r["deviation_percent"] for r in rich]
        assert "fail" in {r["verdict"] for r in rich}            # S1 really fails


def test_hybrid_below_threshold_rows_get_derived_values_too():
    actual, gs = _inputs()
    rows = run_hybrid_tolerance(actual, gs, {"type": "hybrid_tolerance", "tolerance_percent": 10,
                                             "absolute_tolerance": 2, "count_threshold": 100,
                                             "columns": ["lo"], "reported_values": BOTH})
    by = {r["sample_id"]: r for r in rows}
    assert by["S1"]["derived"] == {"absolute_difference": 1.0, "relative_difference_percent": 20.0}
    assert by["S2"]["derived"]["relative_difference_percent"] is None        # gold value 0


# ---- through compare_experiment and into the report -------------------------------------------

@pytest.fixture
def conn_with_gold(tmp_path):
    initialise_database(str(tmp_path / "v.db"), str(SCHEMA))
    conn = get_connection(str(tmp_path / "v.db"))
    gs_csv = tmp_path / "gs.csv"
    gs_csv.write_text("x\nx\nx\nx\nSampleID,c\nS1,1000\nS2,1000\n")
    from src.registration.registrar import register_gold_standard
    register_gold_standard(conn, "E", str(gs_csv), registered_by="t", reason="r")
    actual = tmp_path / "actual.csv"
    actual.write_text("x\nx\nx\nx\nSampleID,c\nS1,1500\nS2,1005\n")
    return conn, str(actual)


def _experiment(**kw):
    return {"experiment_id": "E", "feature_set": "fs", "classification": "exploratory", "comparisons":
            [{"type": "count_tolerance", "tolerance_percent": 10, "columns": ["c"]}], **kw}


def test_an_experiment_default_reaches_the_results_with_gold_standard_provenance(conn_with_gold):
    conn, actual = conn_with_gold
    out = compare_experiment(conn, _experiment(reported_values=BOTH), actual)
    by = {r["sample_id"]: r for r in out["comparison_results"]}
    assert by["S1"]["derived"] == {"absolute_difference": 500.0, "relative_difference_percent": 50.0}
    assert by["S2"]["derived"] == {"absolute_difference": 5.0, "relative_difference_percent": 0.5}
    gs = by["S1"]["gold_standard"]
    assert gs["version_number"] == 1 and len(gs["checksum_sha256"]) == 64


def test_no_declaration_means_empty_derived(conn_with_gold):
    conn, actual = conn_with_gold
    out = compare_experiment(conn, _experiment(), actual)
    assert all(r["derived"] == {} for r in out["comparison_results"])


def test_compare_experiment_refuses_an_unknown_value(conn_with_gold):
    conn, actual = conn_with_gold
    with pytest.raises(ValueError, match="unknown reported value"):
        compare_experiment(conn, _experiment(reported_values=["nope"]), actual)


def _write(tmp_path, results, exp_id="E"):
    initialise_database(str(tmp_path / "w.db"), str(SCHEMA))
    conn = get_connection(str(tmp_path / "w.db"))
    models.insert_run(conn, {"run_id": "r", "triggered_at": "t", "pipeline_build": "b",
                             "scenario": "s", "manifest_path": "m", "verdict": "pass"})
    conn.commit()
    exp_result = {"experiment_id": exp_id, "feature_set": "fs", "classification": "exploratory",
                  "verdict": "pass", "comparisons": [], "sample_results": results}
    write_report(conn, {"paths": {"reports_dir": str(tmp_path / "reports")}}, "r", [exp_result], "pass")
    text = (tmp_path / "reports" / "r" / "detail_report.csv").read_text(encoding="utf-8")
    blob = json.loads(conn.execute("SELECT report_json FROM reports").fetchone()[0])
    return list(csv.DictReader(io.StringIO(text))), text.splitlines()[0], blob


def test_the_detail_report_shows_derived_columns_and_provenance(conn_with_gold, tmp_path):
    conn, actual = conn_with_gold
    out = compare_experiment(conn, _experiment(reported_values=BOTH), actual)
    rows, header, blob = _write(tmp_path / "rep", out["comparison_results"])
    assert header == ("feature_set,comparison_type,metric,criteria,sample_id,expected,actual,deviation_percent,"
                      "absolute_difference,relative_difference_percent,gold_standard_version,"
                      "gold_standard_sha256,verdict,notes")
    s1 = next(r for r in rows if r["sample_id"] == "S1")
    assert (s1["absolute_difference"], s1["relative_difference_percent"], s1["verdict"]) == ("500.0", "50.0", "fail")
    assert s1["gold_standard_version"] == "1" and len(s1["gold_standard_sha256"]) == 12
    assert blob["detail"][0]["absolute_difference"] is not None


def test_requesting_only_one_value_adds_only_that_column(conn_with_gold, tmp_path):
    conn, actual = conn_with_gold
    out = compare_experiment(conn, _experiment(reported_values=["relative_difference_percent"]), actual)
    _, header, _ = _write(tmp_path / "rep", out["comparison_results"])
    assert "relative_difference_percent" in header and "absolute_difference" not in header


def test_a_blank_value_is_an_empty_cell(tmp_path):
    r = apply_derived_values(result(3, 0), BOTH)
    rows, _, _ = _write(tmp_path, [r])
    assert rows[0]["relative_difference_percent"] == "" and rows[0]["absolute_difference"] == "3.0"
