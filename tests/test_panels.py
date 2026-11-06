"""
Tests for src/panels.py (TES-2990): dual-panel assays expand into one experiment
per panel. Panel names below are placeholders, not the real KMT2A panel names.
"""

import copy
import json

import pytest

from src.panels import PanelError, expand_panels, validate_panels

BASE = "YS_KMT 2tube_E2E on Beta+03_261008_1707"
CMP_A = [{"type": "hybrid_tolerance", "tolerance_percent": 10.0, "absolute_tolerance": 2,
          "count_threshold": 100, "columns": ["A1_CountsPer50ul", "A2_CountsPer50ul"]}]
CMP_B = [{"type": "hybrid_tolerance", "tolerance_percent": 10.0, "absolute_tolerance": 2,
          "count_threshold": 100, "columns": ["B1_CountsPer50ul"]}]


def kmt2a(**extra):
    entry = {"experiment_id": BASE, "feature_set": "ancora", "classification": "stability",
             "counts_for_build_verdict": True, "result_file_suffix": "TargetSummary.csv",
             "image_source": f"P:\\CountableLabs\\Data\\{BASE}",
             "sample_workbook": f"P:\\CountableLabs\\Data\\{BASE}\\Workbook.json",
             "panels": {"PanelA": {"comparisons": CMP_A}, "PanelB": {"comparisons": CMP_B}}}
    entry.update(extra)
    return entry


def manifest(*entries):
    return {"run": {"run_id": "run_20261106_001"}, "experiments": list(entries)}


def plain(eid="NPM1_x"):
    return {"experiment_id": eid, "feature_set": "ancora", "classification": "stability",
            "comparisons": CMP_A}


def test_manifest_without_panels_is_returned_unchanged():
    m = manifest(plain())
    assert expand_panels(m) is m


def test_entry_expands_to_one_experiment_per_panel_in_order():
    out = expand_panels(manifest(plain("first"), kmt2a(), plain("last")))
    assert [e["experiment_id"] for e in out["experiments"]] == \
        ["first", f"{BASE}_PanelA", f"{BASE}_PanelB", "last"]


def test_panel_experiment_has_own_comparisons_and_inherits_the_rest():
    a, b = expand_panels(manifest(kmt2a()))["experiments"]
    assert a["comparisons"] == CMP_A and b["comparisons"] == CMP_B
    assert a["image_source"] == b["image_source"] == f"P:\\CountableLabs\\Data\\{BASE}"
    assert a["result_file_suffix"] == "TargetSummary.csv"
    assert (a["panel"], a["parent_experiment_id"]) == ("PanelA", BASE)
    assert "panels" not in a and "panels" not in b


def test_panel_inherits_entry_comparisons_and_can_override_classification():
    entry = kmt2a(comparisons=CMP_A, panels={"PanelA": {}, "PanelB": {
        "comparisons": CMP_B, "classification": "exploratory", "counts_for_build_verdict": False}})
    a, b = expand_panels(manifest(entry))["experiments"]
    assert a["comparisons"] == CMP_A and a["classification"] == "stability"
    assert b["classification"] == "exploratory" and b["counts_for_build_verdict"] is False


def test_input_manifest_is_not_modified():
    m = manifest(kmt2a())
    before = copy.deepcopy(m)
    expand_panels(m)
    assert m == before


@pytest.mark.parametrize("panels", [{}, [], ["PanelA"], {"": {}}, {"PanelA": []}])
def test_bad_panels_shapes_are_rejected(panels):
    with pytest.raises(PanelError):
        expand_panels(manifest(kmt2a(panels=panels)))


def test_unsupported_override_is_rejected():
    with pytest.raises(PanelError, match="image_source"):
        expand_panels(manifest(kmt2a(panels={"PanelA": {"image_source": "x"}})))


def test_panel_with_no_comparisons_anywhere_is_rejected():
    entry = kmt2a(panels={"PanelA": {}})
    with pytest.raises(PanelError, match="no comparisons"):
        expand_panels(manifest(entry))


def test_duplicate_ids_after_expansion_are_rejected():
    with pytest.raises(PanelError, match="more than once"):
        expand_panels(manifest(kmt2a(), plain(f"{BASE}_PanelA")))


def test_panel_error_is_a_value_error_so_callers_treat_it_as_malformed():
    assert issubclass(PanelError, ValueError)


def test_non_manifest_shapes_pass_through_for_the_callers_own_validation():
    assert expand_panels(None) is None
    assert expand_panels({"run": {}}) == {"run": {}}


def test_validate_panels_reports_problems_for_stamping():
    assert validate_panels([plain(), kmt2a()]) == []
    assert len(validate_panels([kmt2a(panels={})])) == 1


def test_service_tracks_every_panel_and_matches_pipeline_notifications():
    pytest.importorskip("asyncpg")      # src.main pulls in the service's runtime deps
    from src.main import _manifest_identity, resolve_notification
    run_id, expected = _manifest_identity(expand_panels(manifest(plain("NPM1_x"), kmt2a())))
    assert run_id == "run_20261106_001"
    assert expected == {"NPM1_x", f"{BASE}_PanelA", f"{BASE}_PanelB"}
    # the pipeline appends its own _yymmdd_hhmm to the split experiment's name
    for panel in ("PanelA", "PanelB"):
        raw = f"{BASE}_{panel}_261106_0912"
        assert resolve_notification(raw, "", expected) == (f"{BASE}_{panel}", raw)


def test_harness_view_of_the_manifest_is_the_single_unexpanded_entry():
    # the stamped file keeps one entry; only the service expands it
    stamped = json.loads(json.dumps(manifest(kmt2a())))
    assert [e["experiment_id"] for e in stamped["experiments"]] == [BASE]
