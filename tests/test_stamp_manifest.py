"""
Tests for entry_point/stamp_manifest_for_run.py acceptance support and for
mixed gold standard + acceptance manifests end to end (ADR-026, TES-2878).
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.database import models
from src.database.db import get_connection, initialise_database
from src.orchestrator.orchestrator import verify_run
from src.registration.registrar import register_gold_standard

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "entry_point"
SCHEMA = REPO / "schema" / "verification_schema.sql"
ACCEPTANCE = ENTRY / "manifest_template_acceptance.json"
FULL = ENTRY / "manifest_template.json"
SINGLE = ENTRY / "manifest_template_single.json"
EXP_ID = "MP37_V_V_Testcase_2_beta05"


@pytest.fixture(scope="module")
def stamp():
    spec = importlib.util.spec_from_file_location("stamp_manifest_for_run", ENTRY / "stamp_manifest_for_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_stamp(stamp, monkeypatch, tmp_path, *args):
    manifests = tmp_path / "manifests"
    monkeypatch.setattr(stamp, "load_config", lambda: {"paths": {"manifests_dir": str(manifests)}})
    monkeypatch.setattr(sys, "argv", ["stamp", "--pipeline-build", "3.0.0.2977", *args])
    rc = stamp.main()
    files = sorted(manifests.glob("context_manifest_*.json")) if manifests.exists() else []
    return rc, files


def experiments_of(path):
    return json.loads(Path(path).read_text())["experiments"]


# ---- stamping -----------------------------------------------------------------------

def test_stamp_the_acceptance_template_alone(stamp, monkeypatch, tmp_path):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, "--template", str(ACCEPTANCE))
    assert rc == 0 and len(files) == 1
    m = json.loads(files[0].read_text())
    assert [e["experiment_id"] for e in m["experiments"]] == [EXP_ID]
    assert m["experiments"][0]["comparisons"][0]["type"] == "acceptance_criteria"
    assert m["run"]["pipeline_build"] == "3.0.0.2977" and m["run"]["run_type"] == "Reanalysis"


def test_stamp_mixed_gold_standard_and_acceptance(stamp, monkeypatch, tmp_path):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, "--template", str(SINGLE),
                          "--also-template", str(ACCEPTANCE))
    assert rc == 0
    ids = [e["experiment_id"] for e in experiments_of(files[0])]
    assert ids == ["MP47b_Adverum_02", EXP_ID]


def test_stamp_full_plus_acceptance_keeps_every_gold_standard_experiment(stamp, monkeypatch, tmp_path):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, "--also-template", str(ACCEPTANCE))
    full_ids = [e["experiment_id"] for e in json.loads(FULL.read_text())["experiments"]]
    assert rc == 0 and [e["experiment_id"] for e in experiments_of(files[0])] == full_ids + [EXP_ID]


def test_gold_standard_only_stamping_is_unchanged(stamp, monkeypatch, tmp_path):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path)           # default template, no extras
    assert rc == 0
    assert experiments_of(files[0]) == json.loads(FULL.read_text())["experiments"]
    assert not any(c["type"] == "acceptance_criteria" for e in experiments_of(files[0]) for c in e["comparisons"])


def test_duplicate_experiment_ids_across_templates_are_refused(stamp, monkeypatch, tmp_path, capsys):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, "--template", str(ACCEPTANCE),
                          "--also-template", str(ACCEPTANCE))
    assert rc == 1 and files == [] and "appears in both" in capsys.readouterr().err


# ---- fail fast on a bad acceptance entry ---------------------------------------------

def bad_template(tmp_path, mutate):
    t = json.loads(ACCEPTANCE.read_text())
    mutate(t["experiments"][0])
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(t))
    return p


@pytest.mark.parametrize("mutate,msg", [
    (lambda e: e["comparisons"][0]["criteria"][0].update(metric="cvv"), "unknown metric"),
    (lambda e: e["comparisons"][0]["criteria"][0].pop("lower_inclusive"), "lower_inclusive"),
    (lambda e: e["comparisons"][0].update(annotation="NO_SUCH_EXPERIMENT"), "NO_SUCH_EXPERIMENT"),
    (lambda e: e["comparisons"][0]["columns"].pop("Ch04"), "do not match the annotation's channels"),
    (lambda e: e["comparisons"].append(copy.deepcopy(e["comparisons"][0])), "more than one"),
])
def test_invalid_acceptance_entry_refuses_to_stamp(stamp, monkeypatch, tmp_path, capsys, mutate, msg):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, "--template", str(bad_template(tmp_path, mutate)))
    err = capsys.readouterr().err
    assert rc == 1 and files == []                                # nothing written, harness never starts
    assert "refusing to stamp" in err and msg in err


# ---- mixed manifest through the service ----------------------------------------------

def test_mixed_manifest_runs_both_kinds_and_persists_each_way(stamp, monkeypatch, tmp_path, summary_csv_factory):
    csv_path = summary_csv_factory()
    gs_id = "GS_EXP"
    gs_template = tmp_path / "gs.json"
    gs_template.write_text(json.dumps({
        "build_verdict_policy": {"rule": "all_stability_experiments_must_pass",
                                 "on_exploratory_failure": "warn_only"},
        "experiments": [{"experiment_id": gs_id, "feature_set": "baseline", "classification": "stability",
                         "counts_for_build_verdict": True, "image_source": "x", "sample_workbook": "y",
                         "comparisons": [{"type": "count_tolerance", "tolerance_percent": 10.0,
                                          "columns": ["UM-01_CountsPer50ul"]}]}]}))
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, "--template", str(gs_template),
                          "--also-template", str(ACCEPTANCE))
    assert rc == 0
    manifest = json.loads(files[0].read_text())

    db = tmp_path / "v.db"
    results = tmp_path / "results"
    names = {}
    for eid in (gs_id, EXP_ID):
        folder = results / f"{eid}_261028_1000"
        folder.mkdir(parents=True)
        (folder / csv_path.name).write_bytes(csv_path.read_bytes())
        names[eid] = folder.name
    initialise_database(str(db), str(SCHEMA))
    conn = get_connection(str(db))
    register_gold_standard(conn, gs_id, str(csv_path), "tester", "test")
    conn.commit()
    conn.close()
    config = {"paths": {"database": str(db), "results_dir": str(results),
                        "reports_dir": str(tmp_path / "reports"), "manifests_dir": str(tmp_path)}}
    run_id = manifest["run"]["run_id"]
    verify_run(config, run_id, manifest, names, str(files[0]))

    conn = get_connection(str(db))
    assert conn.execute("SELECT verdict FROM runs").fetchone()[0] == "pass"
    by_exp = {r["experiment_id"]: r for r in conn.execute("SELECT * FROM experiment_results")}
    assert by_exp[gs_id]["gs_exp_version_id"] and by_exp[EXP_ID]["gs_exp_version_id"] is None
    assert {r["verdict"] for r in by_exp.values()} == {"pass"}
    # gold standard experiment -> sample_results only; acceptance -> aggregate_results + observations
    assert conn.execute("SELECT COUNT(*) FROM sample_results WHERE result_id = ?",
                        (by_exp[gs_id]["result_id"],)).fetchone()[0] == 96
    assert models.get_aggregate_results(conn, by_exp[gs_id]["result_id"]) == []
    assert len(models.get_aggregate_results(conn, by_exp[EXP_ID]["result_id"])) == 32
    assert len(models.get_result_observations(conn, by_exp[EXP_ID]["result_id"])) == 384
    assert conn.execute("SELECT COUNT(*) FROM sample_results WHERE result_id = ?",
                        (by_exp[EXP_ID]["result_id"],)).fetchone()[0] == 0
    out = tmp_path / "reports" / run_id
    assert {p.name for p in out.iterdir()} == {"detail_report.csv", "summary_report.csv",
                                               "acceptance_report.md", "acceptance_summary.csv",
                                               "acceptance_observations.csv"}
    assert len(list(__import__("csv").DictReader(open(out / "detail_report.csv")))) == 96


def test_gold_standard_only_manifest_through_the_service_is_unchanged(stamp, monkeypatch, tmp_path, summary_csv_factory):
    csv_path = summary_csv_factory()
    db = tmp_path / "v.db"
    initialise_database(str(db), str(SCHEMA))
    conn = get_connection(str(db))
    register_gold_standard(conn, "GS_EXP", str(csv_path), "tester", "test")
    conn.commit()
    conn.close()
    folder = tmp_path / "results" / "GS_EXP_261028_1000"
    folder.mkdir(parents=True)
    (folder / csv_path.name).write_bytes(csv_path.read_bytes())
    manifest = {"run": {"run_id": "run_20261028_002", "triggered_at": "t", "pipeline_build": "b", "scenario": "s"},
                "build_verdict_policy": {"rule": "all_stability_experiments_must_pass",
                                         "on_exploratory_failure": "warn_only"},
                "experiments": [{"experiment_id": "GS_EXP", "feature_set": "f", "classification": "stability",
                                 "comparisons": [{"type": "count_tolerance", "tolerance_percent": 10.0,
                                                  "columns": ["UM-01_CountsPer50ul"]}]}]}
    config = {"paths": {"database": str(db), "results_dir": str(tmp_path / "results"),
                        "reports_dir": str(tmp_path / "reports"), "manifests_dir": str(tmp_path)}}
    verify_run(config, "run_20261028_002", manifest, {"GS_EXP": folder.name}, "m.json")
    conn = get_connection(str(db))
    assert conn.execute("SELECT verdict FROM runs").fetchone()[0] == "pass"
    assert conn.execute("SELECT COUNT(*) FROM sample_results").fetchone()[0] == 96
    assert conn.execute("SELECT COUNT(*) FROM aggregate_results").fetchone()[0] == 0
    assert {p.name for p in (tmp_path / "reports" / "run_20261028_002").iterdir()} == {
        "detail_report.csv", "summary_report.csv"}
