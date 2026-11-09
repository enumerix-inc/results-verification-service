"""
Tests for the sample result builder (TES-3009): one place owns the shape of a
sample-scope result, and `derived` is carried through without any layer naming
its keys.
"""

import csv
import json
from pathlib import Path

from src.comparator.sample_result import build_sample_result
from src.comparator.strategies.count_tolerance import run_count_tolerance
from src.comparator.strategies.hybrid_tolerance import run_hybrid_tolerance
from src.database import models
from src.database.db import get_connection, initialise_database
from src.reporter.reporter import write_report

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "verification_schema.sql"

FIELDS = {"sample_id", "comparison_type", "metric", "actual_value", "expected_value",
          "deviation_percent", "verdict", "note", "derived"}


def _inputs():
    gs = [{"sample_id": "S1", "full_metrics": json.dumps({"c": 100, "z": 0})},
          {"sample_id": "S2", "full_metrics": json.dumps({"c": 5, "z": 0})},
          {"sample_id": "MISSING", "full_metrics": json.dumps({"c": 50, "z": 0})}]
    actual = [{"SampleID": "S1", "c": "130", "z": "0"}, {"SampleID": "S2", "c": "6", "z": "3"}]
    return actual, gs


def test_builder_returns_every_field_with_defaults():
    r = build_sample_result(sample_id="S", comparison_type="t", metric="m", actual_value=1,
                            expected_value=2, deviation_percent=50.0, verdict="fail")
    assert set(r) == FIELDS and r["note"] is None and r["derived"] == {}


def test_derived_is_copied_not_shared():
    d = {"absolute_difference": 1.0}
    r = build_sample_result(sample_id="S", comparison_type="t", metric="m", actual_value=1,
                            expected_value=0, deviation_percent=None, verdict="pass", derived=d)
    d["x"] = 1
    assert r["derived"] == {"absolute_difference": 1.0}


def test_every_strategy_result_has_the_full_shape():
    actual, gs = _inputs()
    count = run_count_tolerance(actual, gs, {"type": "count_tolerance", "tolerance_percent": 10,
                                             "columns": ["c", "z"]})
    hybrid = run_hybrid_tolerance(actual, gs, {"type": "hybrid_tolerance", "tolerance_percent": 10,
                                               "absolute_tolerance": 2, "count_threshold": 100,
                                               "columns": ["c", "z"]})
    # S1/S2 x c/z, plus the missing sample x c/z: normal, zero-expected, below-threshold, missing rows
    assert len(count) == len(hybrid) == 6
    for r in count + hybrid:
        assert set(r) == FIELDS and r["derived"] == {}


def test_missing_and_note_rows_keep_their_notes():
    actual, gs = _inputs()
    rows = run_count_tolerance(actual, gs, {"type": "count_tolerance", "tolerance_percent": 10,
                                            "columns": ["c", "z"]})
    by = {(r["sample_id"], r["metric"]): r for r in rows}
    assert by[("MISSING", "c")]["note"] == "sample not found in results"
    assert "division avoided" in by[("S2", "z")]["note"]
    assert by[("S1", "c")]["note"] is None


def _report(tmp_path, sample_results):
    initialise_database(str(tmp_path / "v.db"), str(SCHEMA))
    conn = get_connection(str(tmp_path / "v.db"))
    models.insert_run(conn, {"run_id": "r", "triggered_at": "t", "pipeline_build": "b",
                             "scenario": "s", "manifest_path": "m", "verdict": "pass"})
    conn.commit()
    exp = {"experiment_id": "E", "feature_set": "fs", "classification": "stability", "verdict": "pass",
           "comparisons": [{"type": "count_tolerance", "tolerance_percent": 10.0, "columns": ["c"]}],
           "sample_results": sample_results}
    write_report(conn, {"paths": {"reports_dir": str(tmp_path / "reports")}}, "r", [exp], "pass")
    return (tmp_path / "reports" / "r" / "detail_report.csv").read_bytes()


def test_a_derived_entry_does_not_change_the_detail_report(tmp_path):
    """derived rides along on a result; until a later change asks for it, the output is unchanged."""
    plain = build_sample_result(sample_id="S1", comparison_type="count_tolerance", metric="c",
                                actual_value=100, expected_value=100, deviation_percent=0.0,
                                verdict="pass")
    rich = dict(plain, derived={"absolute_difference": 0.0})
    assert _report(tmp_path / "a", [plain]) == _report(tmp_path / "b", [rich])


def test_a_none_note_is_written_as_an_empty_cell(tmp_path):
    r = build_sample_result(sample_id="S1", comparison_type="count_tolerance", metric="c",
                            actual_value=100, expected_value=100, deviation_percent=0.0, verdict="pass")
    data = _report(tmp_path, [r]).decode("utf-8")
    row = next(csv.DictReader(data.splitlines()))
    assert row["notes"] == ""
