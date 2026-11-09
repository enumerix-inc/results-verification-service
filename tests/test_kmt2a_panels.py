"""
TES-2990 against real dual-panel output: the KMT2A target summaries the v3.0.1.3141
pipeline wrote for panels A and B of one workbook (tests/data/kmt2a_panel_*).

The pipeline names the split experiments "{experiment}_{panel name}_{yymmdd}_{hhmm}",
e.g. "YS_KMT 2tube_E2E on Beta+03_Panel A_261105_1108": the panel name is
"Panel A" (with a space) and each panel reports its own target columns.
"""

import json
from pathlib import Path

import pytest

from src.comparator.comparator import compare_experiment
from src.database.db import get_connection, initialise_database
from src.panels import expand_panels
from src.registration.registrar import register_gold_standard
from src.summary_csv import read_summary_csv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"
SCHEMA = ROOT / "schema" / "verification_schema.sql"
FIXTURES = {"Panel A": DATA / "kmt2a_panel_a_target_summary_3.0.1.3141.csv",
            "Panel B": DATA / "kmt2a_panel_b_target_summary_3.0.1.3141.csv"}

BASE = "YS_KMT 2tube_E2E on Beta+03"   # the master workbook's RunInfo.ExperimentId
COLUMNS = {
    "Panel A": ["KMT2A::AFF1_CountsPer50ul", "KMT2A::MLLT3_CountsPer50ul",
                "KMT2A::MLLT1_CountsPer50ul", "ABL1_CountsPer50ul"],
    "Panel B": ["KMT2A::MLLT4_CountsPer50ul", "KMT2A::ELL_CountsPer50ul",
                "KMT2A::MLLT10_CountsPer50ul", "ABL1_CountsPer50ul"],
}


def _cmp(columns):
    return [{"type": "hybrid_tolerance", "tolerance_percent": 10.0, "absolute_tolerance": 2,
             "count_threshold": 100, "columns": columns}]


def _manifest():
    return {"run": {"run_id": "run_20261106_001"}, "experiments": [{
        "experiment_id": BASE, "feature_set": "ancora", "classification": "stability",
        "result_file_suffix": "TargetSummary.csv",
        "panels": {name: {"comparisons": _cmp(cols)} for name, cols in COLUMNS.items()}}]}


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "v.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    for panel, path in FIXTURES.items():           # gold standards under the expanded ids
        register_gold_standard(c, f"{BASE}_{panel}", str(path), "tester", "fixture")
    yield c
    c.close()


def _experiments():
    return {e["panel"]: e for e in expand_panels(_manifest())["experiments"]}


def test_fixtures_have_distinct_per_panel_columns_and_shared_abl1():
    for panel, path in FIXTURES.items():
        rows = read_summary_csv(str(path))
        assert len(rows) == 24
        assert all(c in rows[0] for c in COLUMNS[panel])
    a = set(read_summary_csv(str(FIXTURES["Panel A"]))[0])
    b = set(read_summary_csv(str(FIXTURES["Panel B"]))[0])
    assert {c for c in a - b if c.endswith("_CountsPer50ul")} == \
        {c for c in COLUMNS["Panel A"] if c != "ABL1_CountsPer50ul"}


def test_expansion_yields_the_ids_the_pipeline_uses():
    assert sorted(_experiments()) == ["Panel A", "Panel B"]
    assert {e["experiment_id"] for e in _experiments().values()} == \
        {f"{BASE}_Panel A", f"{BASE}_Panel B"}


@pytest.mark.parametrize("panel", ["Panel A", "Panel B"])
def test_each_panel_compares_cleanly_against_its_own_gold_standard(conn, panel):
    out = compare_experiment(conn, _experiments()[panel], str(FIXTURES[panel]))
    results = out["comparison_results"]
    assert len(results) == 24 * 4
    assert {r["verdict"] for r in results} == {"pass"}


def test_a_changed_target_count_fails_only_that_cell(conn, tmp_path):
    changed = tmp_path / "x_CountableKMT2ApanelATargetSummary.csv"
    text = FIXTURES["Panel A"].read_text(encoding="utf-8")
    assert "2I5A27_01,,0,17314,0," in text
    changed.write_text(text.replace("2I5A27_01,,0,17314,0,", "2I5A27_01,,0,25000,0,"),
                       encoding="utf-8")
    out = compare_experiment(conn, _experiments()["Panel A"], str(changed))
    failures = [r for r in out["comparison_results"] if r["verdict"] == "fail"]
    assert [(f["sample_id"], f["metric"]) for f in failures] == \
        [("2I5A27_01", "KMT2A::MLLT3_CountsPer50ul")]


def test_wiring_a_panels_result_to_the_other_panel_fails_loudly_not_silently(conn):
    out = compare_experiment(conn, _experiments()["Panel A"], str(FIXTURES["Panel B"]))
    results = out["comparison_results"]
    assert results and {r["verdict"] for r in results} == {"fail"}
    assert all("not found" in r["note"] for r in results)


def test_pipeline_notifications_resolve_to_the_expanded_ids():
    pytest.importorskip("asyncpg")
    from src.main import _manifest_identity, resolve_notification
    _, expected = _manifest_identity(expand_panels(_manifest()))
    for panel in ("Panel A", "Panel B"):
        raw = f"{BASE}_{panel}_261105_1108"
        assert resolve_notification(raw, "", expected) == (f"{BASE}_{panel}", raw)


# --- the master workbook the app is given vs the split workbooks it generates ---

def _tubes(workbook):
    return [t for strip in workbook["Strips"] for t in strip["Tubes"]]


def test_expanded_ids_equal_the_split_workbooks_experiment_ids():
    master = json.loads((DATA / "kmt2a_master_workbook.json").read_text(encoding="utf-8"))
    splits = [json.loads((DATA / f"kmt2a_panel_{p}_workbook.json").read_text(encoding="utf-8"))
              for p in ("a", "b")]
    assert master["RunInfo"]["ExperimentId"] == BASE
    assert {t["PanelName"] for t in _tubes(master)} == set(COLUMNS)
    assert {e["experiment_id"] for e in _experiments().values()} == \
        {w["RunInfo"]["ExperimentId"] for w in splits}


def test_each_gold_standard_holds_exactly_that_panels_samples():
    master = json.loads((DATA / "kmt2a_master_workbook.json").read_text(encoding="utf-8"))
    for panel, path in FIXTURES.items():
        wanted = {t["SampleName"] for t in _tubes(master) if t["PanelName"] == panel}
        assert {r["SampleName"] for r in read_summary_csv(str(path))} == wanted
