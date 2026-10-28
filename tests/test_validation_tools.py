"""
Tests for the determinism canary and the workbook check (ADR-026, TES-2879),
and the annotation preflight check.
"""

import json
from pathlib import Path

import pytest

from conftest import mp37_rows
from src.database.db import get_connection, initialise_database
from src.orchestrator.orchestrator import verify_run
from src.preflight import check_annotations, run_preflight
from src.validation import canary, workbook_check

REPO = Path(__file__).resolve().parents[1]
SCHEMA = REPO / "schema" / "verification_schema.sql"
TEMPLATE = REPO / "entry_point" / "manifest_template_acceptance.json"
GOLDEN = json.loads((REPO / "tests" / "data" / "uma_linearity_golden.json").read_text())
EXP_ID = "MP37_V_V_Testcase_2_beta05"


def run_into(tmp_path, csv_path, run_id, build="IAP-v3.0.1"):
    """Run the acceptance experiment through the service into tmp_path/v.db."""
    results = tmp_path / "results"
    folder = results / f"{EXP_ID}_{run_id}"
    folder.mkdir(parents=True)
    (folder / csv_path.name).write_bytes(csv_path.read_bytes())
    config = {"paths": {"database": str(tmp_path / "v.db"), "results_dir": str(results),
                        "reports_dir": str(tmp_path / "reports"), "manifests_dir": str(tmp_path)}}
    initialise_database(config["paths"]["database"], str(SCHEMA))
    exp = json.loads(TEMPLATE.read_text())["experiments"][0]
    manifest = {"run": {"run_id": run_id, "triggered_at": "2026-10-29T10:00:00Z",
                        "pipeline_build": build, "scenario": "canary"},
                "build_verdict_policy": {"rule": "all_stability_experiments_must_pass",
                                         "on_exploratory_failure": "warn_only"},
                "experiments": [exp]}
    verify_run(config, run_id, manifest, {EXP_ID: folder.name}, "m.json")
    return str(tmp_path / "v.db")


# ---- canary ------------------------------------------------------------------------------

def test_canary_passes_for_two_identical_runs(tmp_path, summary_csv_factory, capsys):
    csv_path = summary_csv_factory()
    run_into(tmp_path, csv_path, "run_20261029_001")
    db = run_into(tmp_path, csv_path, "run_20261029_002")
    assert canary.main(["--db", db, "--experiment-id", EXP_ID]) == 0
    out = capsys.readouterr().out
    assert "Compared 384 observations and 32 aggregate results" in out and "PASS: identical" in out


def test_canary_names_the_difference(tmp_path, summary_csv_factory, capsys):
    rows = mp37_rows()
    run_into(tmp_path, summary_csv_factory(rows), "run_20261029_001")
    changed = [(n, [c[0] + 1] + c[1:]) if n == "FourPlex_10k_4" else (n, c) for n, c in rows]
    db = run_into(tmp_path, summary_csv_factory(changed), "run_20261029_002")
    assert canary.main(["--db", db, "--experiment-id", EXP_ID]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "observation FourPlex_10k_4 Ch01: count_value" in out
    assert "aggregate cv_10k Ch01: actual_value" in out


def test_canary_refuses_different_builds_unless_allowed(tmp_path, summary_csv_factory, capsys):
    csv_path = summary_csv_factory()
    run_into(tmp_path, csv_path, "run_20261029_001", build="IAP-v3.0.1")
    db = run_into(tmp_path, csv_path, "run_20261029_002", build="IAP-v3.1.0")
    assert canary.main(["--db", db, "--experiment-id", EXP_ID]) == 2
    assert "builds differ" in capsys.readouterr().out
    assert canary.main(["--db", db, "--experiment-id", EXP_ID, "--allow-different-builds"]) == 0


def test_canary_needs_two_results_and_can_pick_runs(tmp_path, summary_csv_factory, capsys):
    csv_path = summary_csv_factory()
    db = run_into(tmp_path, csv_path, "run_20261029_001")
    assert canary.main(["--db", db, "--experiment-id", EXP_ID]) == 2
    assert "at least two" in capsys.readouterr().out
    run_into(tmp_path, csv_path, "run_20261029_002")
    assert canary.main(["--db", db, "--experiment-id", EXP_ID,
                        "--run-a", "run_20261029_001", "--run-b", "run_20261029_002"]) == 0
    assert canary.main(["--db", db, "--experiment-id", EXP_ID,
                        "--run-a", "run_20261029_001", "--run-b", "run_nope"]) == 2


# ---- workbook check ----------------------------------------------------------------------

def test_workbook_check_passes_on_workbook_data(tmp_path, summary_csv_factory, capsys):
    db = run_into(tmp_path, summary_csv_factory(), "run_20261029_001")
    assert workbook_check.main(["--db", db, "--run-id", "run_20261029_001"]) == 0
    out = capsys.readouterr().out
    assert "Counts compared: 256 of 256; differing: 0" in out
    assert "Criterion values compared: 32 of 32; differing: 0" in out
    assert "PASS: matches the workbook." in out and "'pass': 32" in out


def test_workbook_check_reports_what_moved(tmp_path, summary_csv_factory, capsys):
    changed = [(n, [c[0], c[1] + 50] + c[2:]) if n == "FourPlex_100k_2" else (n, c) for n, c in mp37_rows()]
    db = run_into(tmp_path, summary_csv_factory(changed), "run_20261029_001")
    assert workbook_check.main(["--db", db, "--run-id", "run_20261029_001"]) == 1
    out = capsys.readouterr().out
    assert "FourPlex_100k_2 Ch02: workbook 95753, service 95803 (diff +50)" in out
    assert "linearity_exponent Ch02" in out and "DIFFERENCES FOUND" in out
    # a count tolerance and a value tolerance let a reviewer judge a small shift
    assert workbook_check.main(["--db", db, "--run-id", "run_20261029_001",
                                "--count-tolerance", "100", "--value-tolerance", "0.01"]) == 0


def test_workbook_check_unknown_run(tmp_path, summary_csv_factory, capsys):
    db = run_into(tmp_path, summary_csv_factory(), "run_20261029_001")
    assert workbook_check.main(["--db", db, "--run-id", "run_nope"]) == 2


def test_golden_helpers_cover_every_criterion():
    assert len(workbook_check.golden_counts(GOLDEN)) == 256
    ids = {k[0] for k in workbook_check.golden_values(GOLDEN)}
    exp = json.loads(TEMPLATE.read_text())["experiments"][0]["comparisons"][0]["criteria"]
    assert ids == {c["id"] for c in exp}                  # same criterion ids as the template


# ---- preflight: annotation files ----------------------------------------------------------

def test_preflight_ok_with_the_shipped_annotation():
    results = check_annotations({"paths": {}})
    assert [l for l, _ in results] == ["ok"] and EXP_ID in results[0][1]


def test_preflight_warns_when_no_annotations(tmp_path, monkeypatch):
    import src.annotation.annotation as a
    monkeypatch.setattr(a, "REPO_ANNOTATIONS_DIR", tmp_path / "none")
    assert check_annotations({"paths": {"annotations_dir": str(tmp_path / "missing")}})[0][0] == "warn"


def test_preflight_fails_on_a_broken_annotation(tmp_path, capsys):
    (tmp_path / "BAD.yaml").write_text("experiment_id: BAD\n")
    results = check_annotations({"paths": {"annotations_dir": str(tmp_path)}})
    assert results[0][0] == "fail" and "BAD.yaml does not load" in results[0][1]
    assert run_preflight({"paths": {"annotations_dir": str(tmp_path)}, "listener": {"use_mock": True}}) == 1
