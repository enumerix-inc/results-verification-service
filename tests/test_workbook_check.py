"""
Tests for src/workbook_check.py and its use by the stamping script (TES-2991): a manifest
entry must agree with its sample workbook, because the pipeline names experiments from the
workbook, not from the manifest.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.workbook_check import (WorkbookReadError, check_entry, check_workbooks,
                                read_workbook_facts)

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "tests" / "data"
MASTER = DATA / "kmt2a_master_workbook.json"
KMT = "YS_KMT 2tube_E2E on Beta+03"


def write_workbook(path, experiment_id, panels=()):
    tubes = [{"Position": i + 1, "SampleName": f"S{i}", "PanelName": p, "TrainingSample": []}
             for i, p in enumerate(panels or [None])]
    path.write_text(json.dumps({"RunInfo": {"ExperimentId": experiment_id},
                                "Strips": [{"Barcode": "B1", "Tubes": tubes}]}), encoding="utf-8")
    return path


def entry(eid, workbook, **extra):
    return {"experiment_id": eid, "sample_workbook": str(workbook), **extra}


# ---- facts ---------------------------------------------------------------------------

def test_facts_from_the_real_master_workbook():
    assert read_workbook_facts(MASTER) == (KMT, frozenset({"Panel A", "Panel B"}))


def test_split_workbook_has_the_escaped_plus_decoded_and_no_panel_names():
    eid, panels = read_workbook_facts(DATA / "kmt2a_panel_a_workbook.json")
    assert eid == "YS_KMT 2tube_E2E on Beta+03_Panel A" and panels == frozenset()


@pytest.mark.parametrize("body", [
    '"Strips": {"a": 1}',
    '"Strips": ["x"]',
    '"Strips": [{"Tubes": "x"}]',
    '"Strips": [{"Tubes": [null]}]',
])
def test_malformed_strips_or_tubes_raise_a_clear_error(tmp_path, body):
    p = tmp_path / "w.json"
    p.write_text('{"RunInfo": {"ExperimentId": "X"}, ' + body + '}')
    with pytest.raises(WorkbookReadError):
        read_workbook_facts(p)


def test_blank_panel_names_are_not_panels(tmp_path):
    wb = write_workbook(tmp_path / "w.json", "X", panels=[" ", None, ""])
    assert read_workbook_facts(wb) == ("X", frozenset())


@pytest.mark.parametrize("content", ["not json", "[]", '{"RunInfo": {}}', '{"RunInfo": {"ExperimentId": ""}}'])
def test_unreadable_or_incomplete_workbooks_raise(tmp_path, content):
    p = tmp_path / "w.json"
    p.write_text(content)
    with pytest.raises(WorkbookReadError):
        read_workbook_facts(p)


# ---- entry vs workbook ---------------------------------------------------------------

def test_matching_dual_panel_entry_is_ok():
    e = entry(KMT, MASTER, panels={"Panel A": {}, "Panel B": {}})
    assert check_entry(e, *read_workbook_facts(MASTER)) == []


def test_experiment_id_mismatch_is_reported():
    e = entry("YS_KMT 2tube_E2E on Beta03", MASTER, panels={"Panel A": {}, "Panel B": {}})
    problems = check_entry(e, *read_workbook_facts(MASTER))
    assert len(problems) == 1 and "RunInfo.ExperimentId 'YS_KMT 2tube_E2E on Beta+03'" in problems[0]


def test_misspelled_panel_is_reported():
    e = entry(KMT, MASTER, panels={"PanelA": {}, "Panel B": {}})
    (problem,) = check_entry(e, *read_workbook_facts(MASTER))
    assert "declared panels ['Panel B', 'PanelA']" in problem
    assert "workbook's panels ['Panel A', 'Panel B']" in problem


def test_workbook_with_panels_but_entry_without_is_reported():
    (problem,) = check_entry(entry(KMT, MASTER), *read_workbook_facts(MASTER))
    assert "declares no 'panels'" in problem


def test_entry_with_panels_but_single_panel_workbook_is_reported(tmp_path):
    wb = write_workbook(tmp_path / "w.json", "NPM1_x")
    (problem,) = check_entry(entry("NPM1_x", wb, panels={"Panel A": {}}), *read_workbook_facts(wb))
    assert "declared panels ['Panel A']" in problem


def test_plain_entry_and_workbook_agree(tmp_path):
    wb = write_workbook(tmp_path / "w.json", "NPM1_x")
    assert check_workbooks([entry("NPM1_x", wb)]) == ([], [])


def test_escaped_plus_in_the_workbook_matches_a_plain_plus(tmp_path):
    p = tmp_path / "w.json"
    p.write_text('{"RunInfo": {"ExperimentId": "Beta\\u002B03"}, "Strips": []}')
    assert check_workbooks([entry("Beta+03", p)]) == ([], [])


# ---- missing files -------------------------------------------------------------------

def test_missing_workbook_is_a_warning_by_default_and_a_problem_when_required(tmp_path):
    e = entry("X", tmp_path / "nope.json")
    problems, warnings = check_workbooks([e])
    assert problems == [] and len(warnings) == 1
    problems, warnings = check_workbooks([e], require=True)
    assert len(problems) == 1 and warnings == []


def test_entries_without_a_workbook_path_are_skipped():
    assert check_workbooks([{"experiment_id": "X"}], require=True) == ([], [])


def test_unreadable_workbook_is_always_a_problem(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{")
    problems, _ = check_workbooks([entry("X", p)])
    assert len(problems) == 1


# ---- stamping ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stamp():
    spec = importlib.util.spec_from_file_location(
        "stamp_manifest_for_run", REPO / "entry_point" / "stamp_manifest_for_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_stamp(stamp, monkeypatch, tmp_path, experiments, *args):
    tmp_path.mkdir(parents=True, exist_ok=True)
    template = tmp_path / "t.json"
    template.write_text(json.dumps({"build_verdict_policy": {"rule": "r", "on_exploratory_failure": "warn_only"},
                                    "experiments": experiments}))
    manifests = tmp_path / "manifests"
    monkeypatch.setattr(stamp, "load_config", lambda: {"paths": {"manifests_dir": str(manifests)}})
    monkeypatch.setattr(sys, "argv", ["stamp", "--pipeline-build", "3.1.0.1", "--template", str(template), *args])
    rc = stamp.main()
    return rc, sorted(manifests.glob("context_manifest_*.json")) if manifests.exists() else []


def good_kmt(**extra):
    return {"experiment_id": KMT, "feature_set": "f", "classification": "stability",
            "sample_workbook": str(MASTER),
            "panels": {"Panel A": {"comparisons": [{"type": "count_tolerance", "tolerance_percent": 1, "columns": []}]},
                       "Panel B": {}},
            "comparisons": [{"type": "count_tolerance", "tolerance_percent": 1, "columns": []}], **extra}


def test_stamping_accepts_an_entry_that_matches_its_workbook(stamp, monkeypatch, tmp_path):
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, [good_kmt()], "--require-workbooks")
    assert rc == 0 and len(files) == 1


def test_stamping_refuses_a_mismatching_entry_before_writing_anything(stamp, monkeypatch, tmp_path, capsys):
    bad = good_kmt(experiment_id="YS_KMT 2tube_E2E on Beta03")
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, [bad])
    assert rc == 1 and files == []
    assert "RunInfo.ExperimentId" in capsys.readouterr().err


def test_stamping_a_missing_workbook_warns_unless_required(stamp, monkeypatch, tmp_path, capsys):
    exp = good_kmt(sample_workbook=str(tmp_path / "absent.json"))
    rc, files = run_stamp(stamp, monkeypatch, tmp_path, [exp])
    assert rc == 0 and len(files) == 1
    assert "not cross-checked" in capsys.readouterr().err
    rc, files = run_stamp(stamp, monkeypatch, tmp_path / "again", [exp], "--require-workbooks")
    assert rc == 1 and files == []
