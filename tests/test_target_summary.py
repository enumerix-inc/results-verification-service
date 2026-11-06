"""
Ancora target summary CSV (TES-2988): the existing reader, registrar and
sample-scope comparison strategies must handle it unchanged.

Fixture: tests/data/npm1_target_summary_3.0.1.3096.csv, a real
Countable<Assay>TargetSummary.csv from a 3.0.1.3096 NPM1 run (layout
reference only; not a gold standard). 32 samples; count columns are named after
targets (TypeA/TypeB/TypeD/ABL1), not dyes; counts can be in scientific
notation (1.00505e+06); flag columns are non-numeric.
"""

import shutil
from pathlib import Path

import pytest

from src.comparator.comparator import compare_experiment
from src.database.db import get_connection, initialise_database
from src.registration.registrar import register_gold_standard
from src.summary_csv import read_summary_csv

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "data" / "npm1_target_summary_3.0.1.3096.csv"
SCHEMA = ROOT / "schema" / "verification_schema.sql"
RESULT_NAME = "MP52v_E2E_test_Beta01_261016_1531_CountableNPM1TargetSummary.csv"

TARGET_COLUMNS = ["TypeA_CountsPer50ul", "TypeB_CountsPer50ul",
                  "TypeD_CountsPer50ul", "ABL1_CountsPer50ul"]
EXPERIMENT = {
    "experiment_id": "MP52v_test",
    "result_file_suffix": "TargetSummary.csv",
    "comparisons": [{
        "type": "hybrid_tolerance", "tolerance_percent": 10.0,
        "absolute_tolerance": 2, "count_threshold": 100,
        "columns": TARGET_COLUMNS,
    }],
}


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "v.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    register_gold_standard(c, "MP52v_test", str(FIXTURE), "tester", "fixture")
    yield c
    c.close()


def _result_with(tmp_path, edits):
    """Copy the fixture as a realistically named result file, applying
    {(sample_id, column): new_value_text} edits."""
    rows = read_summary_csv(str(FIXTURE))
    header = list(rows[0].keys())
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()[:4]
    lines.append(",".join(header))
    for r in rows:
        for (sid, col), v in edits.items():
            if r["SampleID"] == sid:
                r[col] = v
        lines.append(",".join(r[h] for h in header))
    out = tmp_path / RESULT_NAME
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def test_fixture_parses_with_existing_reader():
    rows = read_summary_csv(str(FIXTURE))
    assert len(rows) == 32
    assert len({r["SampleID"] for r in rows}) == 32
    assert all(c in rows[0] for c in TARGET_COLUMNS)
    assert None not in rows[0] and "" not in rows[0]      # no stray trailing-comma key


def test_scientific_notation_counts_convert():
    by_id = {r["SampleID"]: r for r in read_summary_csv(str(FIXTURE))}
    assert by_id["2I5415_01"]["ABL1_CountsPer50ul"] == "1.00505e+06"
    assert float(by_id["2I5415_01"]["ABL1_CountsPer50ul"]) == pytest.approx(1005050)


def test_result_file_suffix_matches_every_assay_name(tmp_path):
    # orchestrator globs *{result_file_suffix}; one shared suffix covers all assays
    for assay in ("BCRABL", "KMT2A", "NPM1", "KIT"):
        (tmp_path / f"exp_261016_1531_Countable{assay}TargetSummary.csv").write_text("x")
    (tmp_path / "exp_261016_1531_CountableDataSummary.csv").write_text("x")
    assert len(list(tmp_path.glob("*TargetSummary.csv"))) == 4
    assert [p.name for p in tmp_path.glob("*CountableDataSummary.csv")] == \
        ["exp_261016_1531_CountableDataSummary.csv"]


def test_identical_results_pass_every_cell(conn, tmp_path):
    out = compare_experiment(conn, EXPERIMENT, str(_result_with(tmp_path, {})))
    results = out["comparison_results"]
    assert len(results) == 32 * len(TARGET_COLUMNS)
    assert {r["verdict"] for r in results} == {"pass"}


def test_small_change_on_near_zero_count_passes_via_absolute_tolerance(conn, tmp_path):
    # TypeB in sample 2I54DE_02 is 6; +2 is within the absolute tolerance
    out = compare_experiment(conn, EXPERIMENT, str(_result_with(
        tmp_path, {("2I54DE_02", "TypeB_CountsPer50ul"): "8"})))
    assert {r["verdict"] for r in out["comparison_results"]} == {"pass"}


def test_large_change_fails_only_that_cell(conn, tmp_path):
    # TypeA reference count 49794 -> 60000 is ~20% off
    out = compare_experiment(conn, EXPERIMENT, str(_result_with(
        tmp_path, {("2I54B0_01", "TypeA_CountsPer50ul"): "60000"})))
    failures = [r for r in out["comparison_results"] if r["verdict"] == "fail"]
    assert [(f["sample_id"], f["metric"]) for f in failures] == \
        [("2I54B0_01", "TypeA_CountsPer50ul")]


def test_scientific_notation_cell_changes_are_compared(conn, tmp_path):
    out = compare_experiment(conn, EXPERIMENT, str(_result_with(
        tmp_path, {("2I5415_01", "ABL1_CountsPer50ul"): "1.5e+06"})))
    failures = [r for r in out["comparison_results"] if r["verdict"] == "fail"]
    assert [(f["sample_id"], f["metric"]) for f in failures] == \
        [("2I5415_01", "ABL1_CountsPer50ul")]


def test_missing_sample_is_reported_as_failure(conn, tmp_path):
    out_path = _result_with(tmp_path, {})
    lines = out_path.read_text().splitlines()
    out_path.write_text("\n".join(l for l in lines if "2I5507_04" not in l) + "\n")
    out = compare_experiment(conn, EXPERIMENT, str(out_path))
    failures = [r for r in out["comparison_results"] if r["verdict"] == "fail"]
    assert {f["sample_id"] for f in failures} == {"2I5507_04"}
    assert all("not found" in f["note"] for f in failures)
