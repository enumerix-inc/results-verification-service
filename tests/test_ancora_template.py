"""
entry_point/manifest_template_ancora.json against the real v3.0.1.3141 gold standard target
summaries (TES-2985): every compared column must exist in the gold standard it is compared with,
and the whole template must stamp, expand and compare cleanly.
"""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from src.comparator.comparator import compare_experiment
from src.database.db import get_connection, initialise_database
from src.panels import expand_panels
from src.registration.registrar import register_gold_standard
from src.workbook_check import check_entry, read_workbook_facts

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "tests" / "data"
TEMPLATE = REPO / "entry_point" / "manifest_template_ancora.json"
ACCEPTANCE = REPO / "entry_point" / "manifest_template_acceptance.json"
SCHEMA = REPO / "schema" / "verification_schema.sql"
KMT = "YS_KMT 2tube_E2E on Beta+03"

# expanded experiment id -> the v3.0.1.3141 gold standard target summary
GOLD = {
    "JW326A_beta3_BCRABL_recovery": "bcrabl_target_summary_3.0.1.3141.csv",
    "NP_cKIT_E2E_Beta01": "kit_target_summary_3.0.1.3141.csv",
    "MP52v_E2E_test_Beta+03": "npm1_target_summary_3.0.1.3141.csv",
    f"{KMT}_Panel A": "kmt2a_panel_a_target_summary_3.0.1.3141.csv",
    f"{KMT}_Panel B": "kmt2a_panel_b_target_summary_3.0.1.3141.csv",
}


def template():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def header(fixture):
    with open(DATA / fixture, newline="", encoding="utf-8-sig") as f:
        for _ in range(4):
            f.readline()
        return next(csv.reader(f))


def expanded():
    return {e["experiment_id"]: e for e in expand_panels(template())["experiments"]}


def test_template_declares_the_four_assays_with_kmt2a_split_into_two_panels():
    assert [e["experiment_id"] for e in template()["experiments"]] == \
        ["JW326A_beta3_BCRABL_recovery", "NP_cKIT_E2E_Beta01", "MP52v_E2E_test_Beta+03", KMT]
    assert sorted(expanded()) == sorted(GOLD)


def test_every_compared_column_exists_in_its_gold_standard_and_none_is_missed():
    for eid, exp in expanded().items():
        (comparison,) = exp["comparisons"]
        count_columns = [c for c in header(GOLD[eid]) if c.endswith("_CountsPer50ul")]
        assert comparison["columns"] == count_columns, eid


def test_entries_use_the_workbook_paths_on_the_regression_machine():
    for e in template()["experiments"]:
        folder = e["image_source"].rsplit("\\", 1)[1]
        assert e["image_source"].startswith("E:\\CountableLabs\\Data\\")
        assert e["sample_workbook"] == f"{e['image_source']}\\Workbook_{e['experiment_id']}.json"
        assert folder.startswith(e["experiment_id"] + "_")      # folder = experiment id + imaging timestamp


def test_kmt2a_entry_agrees_with_the_real_master_workbook():
    kmt = next(e for e in template()["experiments"] if e["experiment_id"] == KMT)
    assert check_entry(kmt, *read_workbook_facts(DATA / "kmt2a_master_workbook.json")) == []


def test_template_stamps_alone_and_with_the_acceptance_experiment(tmp_path, monkeypatch):
    import sys
    spec = importlib.util.spec_from_file_location("stamp", REPO / "entry_point" / "stamp_manifest_for_run.py")
    stamp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stamp)
    monkeypatch.setattr(stamp, "load_config", lambda: {"paths": {"manifests_dir": str(tmp_path / "m")}})
    monkeypatch.setattr(sys, "argv", ["stamp", "--pipeline-build", "3.1.0.1", "--template", str(TEMPLATE),
                                      "--also-template", str(ACCEPTANCE)])
    assert stamp.main() == 0                                    # E: absent here: warnings only
    (path,) = (tmp_path / "m").glob("context_manifest_*.json")
    ids = [e["experiment_id"] for e in json.loads(path.read_text())["experiments"]]
    assert ids == [e["experiment_id"] for e in template()["experiments"]] + ["MP37_V_V_Testcase_2_beta05"]


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "v.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    for eid, fixture in GOLD.items():
        register_gold_standard(c, eid, str(DATA / fixture), "tester", "fixture")
    yield c
    c.close()


@pytest.mark.parametrize("eid", sorted(GOLD))
def test_each_experiment_passes_against_its_own_gold_standard(conn, eid):
    out = compare_experiment(conn, expanded()[eid], str(DATA / GOLD[eid]))
    results = out["comparison_results"]
    assert results and {r["verdict"] for r in results} == {"pass"}
