"""
Tests for the acceptance aggregate strategy, registry scope, gate change,
comparator dispatch and orchestrator persistence (ADR-026, TES-2876).
"""

import copy
import json
import statistics
import uuid
from pathlib import Path

import pytest

from conftest import CHANNELS, COLUMNS, GOLDEN, LEVELS, mp37_rows
from src.annotation.annotation import load_annotation
from src.comparator import metrics
from src.comparator.comparator import compare_experiment
from src.comparator.registry import AGGREGATE, COMPARISON_REGISTRY, SAMPLE, comparison_scope
from src.comparator.strategies.acceptance_criteria import run_acceptance_criteria
from src.database import models
from src.database.db import get_connection, initialise_database
from src.gate.gate import run_gate
from src.observations import read_observations
from src.orchestrator.orchestrator import verify_run

REPO = Path(__file__).resolve().parents[1]
ANN = REPO / "annotations" / "MP37_V_V_Testcase_2_beta05.yaml"
TEMPLATE = REPO / "entry_point" / "manifest_template_acceptance.json"
SCHEMA = REPO / "schema" / "verification_schema.sql"
EXP_ID = "MP37_V_V_Testcase_2_beta05"


def experiment():
    return copy.deepcopy(json.loads(TEMPLATE.read_text())["experiments"][0])


def comparison():
    return experiment()["comparisons"][0]


def evaluate(csv_path, comp=None):
    obs = read_observations(csv_path, load_annotation(ANN), COLUMNS)
    return run_acceptance_criteria(obs, comp or comparison())


def by_key(results):
    return {(r["criterion_id"], r["channel"]): r for r in results}


# ---- acceptance: the MP37 data reproduces the workbook ---------------------------

def test_mp37_32_results_all_pass_and_match_the_workbook(summary_csv_factory):
    results = evaluate(summary_csv_factory())
    assert len(results) == 8 * 4
    assert {r["verdict"] for r in results} == {"pass"}
    r = by_key(results)
    for ch in CHANNELS:
        g = GOLDEN["channels"][ch]
        assert r[("linearity_exponent", ch)]["actual_value"] == pytest.approx(g["linearity_exponent"], abs=1e-9)
        assert r[("linear_slope", ch)]["actual_value"] == pytest.approx(g["linear_slope"], abs=1e-9)
        assert r[("plateauing_ratio_1M", ch)]["actual_value"] == pytest.approx(g["plateau_ratio_1M"], abs=1e-9)
        for lv, i in (("1k", 3), ("10k", 4), ("100k", 5), ("1M", 6)):
            assert r[(f"cv_{lv}", ch)]["actual_value"] == pytest.approx(g["cv"][i], abs=1e-9)
        assert r[("ntc_mean_counts", ch)]["actual_value"] == pytest.approx(
            statistics.fmean(g["replicates"][0]))


def test_ch1_values_the_reviewer_will_look_at(summary_csv_factory):
    r = by_key(evaluate(summary_csv_factory()))
    n = r[("linearity_exponent", "Ch01")]
    assert round(n["actual_value"], 3) == 0.984 and n["margin"] == pytest.approx(0.0842, abs=1e-4)
    pr = r[("plateauing_ratio_1M", "Ch01")]
    assert round(pr["actual_value"], 3) == 0.165 and pr["margin"] == pytest.approx(0.0346, abs=1e-4)


def test_result_carries_limits_and_provenance(summary_csv_factory):
    obs_path = summary_csv_factory()
    results = evaluate(obs_path)
    r = by_key(results)[("plateauing_ratio_1M", "Ch02")]
    assert (r["lower_bound"], r["lower_inclusive"], r["upper_bound"], r["upper_inclusive"]) == (-0.2, True, 0.2, True)
    assert r["group_key"] == "FourPlex_1M" and r["metric"] == "plateau_ratio"
    assert r["comparison_type"] == "acceptance_criteria"
    ann = load_annotation(ANN)
    assert r["annotation_sha256"] == ann.sha256 and r["annotation_version"] == 1
    assert len(r["source_file_sha256"]) == 64 and r["filters"] == []
    assert by_key(results)[("linearity_exponent", "Ch01")]["group_key"] == "FourPlex_1k+FourPlex_10k+FourPlex_100k"


# ---- fail, and never silently skipped ----------------------------------------------

def noisy(rows, sample_prefix, ch_index, factor):
    out = []
    for name, counts in rows:
        counts = list(counts)
        if name.startswith(sample_prefix):
            counts[ch_index] = int(counts[ch_index] * factor if name.endswith(("1", "2")) else counts[ch_index])
        out.append((name, counts))
    return out


def test_a_failing_criterion_is_fail_with_negative_margin(summary_csv_factory):
    rows = noisy(mp37_rows(), "FourPlex_10k_", 0, 3.0)         # inflate two of eight replicates
    r = by_key(evaluate(summary_csv_factory(rows)))
    cv = r[("cv_10k", "Ch01")]
    assert cv["verdict"] == "fail" and cv["actual_value"] > 0.10 and cv["margin"] < 0
    assert r[("cv_10k", "Ch02")]["verdict"] == "pass"           # other channels unaffected


def test_wrong_replicate_count_is_an_explicit_error(summary_csv_factory):
    rows = [(n, c) for n, c in mp37_rows() if n != "FourPlex_1k_8"]
    r = by_key(evaluate(summary_csv_factory(rows)))
    for key in ("cv_1k", "linearity_exponent", "linear_slope", "plateauing_ratio_1M"):
        e = r[(key, "Ch01")]
        assert e["verdict"] == "error" and e["actual_value"] is None
        assert "FourPlex_1k" in e["note"] and "7 replicates" in e["note"] and "expected 8" in e["note"]
    assert r[("cv_10k", "Ch01")]["verdict"] == "pass"           # groups that are fine still evaluate
    assert r[("cv_1k", "Ch01")]["lower_bound"] is None and r[("cv_1k", "Ch01")]["upper_bound"] == 0.1


def test_replicates_param_is_optional_but_groups_must_agree(summary_csv_factory):
    comp = comparison()
    del comp["replicates"]
    rows = [(n, c) for n, c in mp37_rows() if n != "FourPlex_1k_8"]
    r = by_key(evaluate(summary_csv_factory(rows), comp))
    assert r[("cv_1k", "Ch01")]["verdict"] == "pass"            # nothing to compare a single group to
    e = r[("linearity_exponent", "Ch01")]
    assert e["verdict"] == "error" and "different replicate counts" in e["note"]


def test_missing_count_is_an_error_not_a_dropped_sample(summary_csv_factory):
    p = summary_csv_factory()
    lines = p.read_text().splitlines()
    i = next(i for i, l in enumerate(lines) if ",FourPlex_100k_3," in l)
    cells = lines[i].split(",")
    cells[6] = "NA"                                              # Ch01 count
    lines[i] = ",".join(cells)
    p.write_text("\n".join(lines) + "\n")
    r = by_key(evaluate(p))
    e = r[("cv_100k", "Ch01")]
    assert e["verdict"] == "error" and "1 of 8 samples" in e["note"] and "FourPlex_100k_3" in e["note"]
    assert r[("cv_100k", "Ch02")]["verdict"] == "pass"


def test_unknown_or_empty_group_is_an_error(summary_csv_factory):
    comp = comparison()
    next(c for c in comp["criteria"] if c["id"] == "cv_1k")["groups"] = ["FourPlex_2k"]
    r = by_key(evaluate(summary_csv_factory(), comp))
    assert r[("cv_1k", "Ch01")]["verdict"] == "error"
    assert "no observations" in r[("cv_1k", "Ch01")]["note"]


def test_group_without_an_assay_on_the_channel_is_an_error(summary_csv_factory):
    comp = comparison()
    crit = next(c for c in comp["criteria"] if c["id"] == "linearity_exponent")
    crit["groups"] = ["Ch1_100k", "FourPlex_10k"]                # Ch1_100k has no assay on Ch02
    r = by_key(evaluate(summary_csv_factory(), comp))
    assert r[("linearity_exponent", "Ch02")]["verdict"] == "error"
    assert "no assay" in r[("linearity_exponent", "Ch02")]["note"]


def test_metric_failure_surfaces_as_error(summary_csv_factory):
    rows = [(n, [0, c[1], c[2], c[3]] if n.startswith("FourPlex_1k_") else c) for n, c in mp37_rows()]
    r = by_key(evaluate(summary_csv_factory(rows)))
    e = r[("linearity_exponent", "Ch01")]                       # mean of 1k group is 0 -> log10 fails
    assert e["verdict"] == "error" and "> 0" in e["note"]


def test_channel_scope_limits_which_channels_run(summary_csv_factory):
    comp = comparison()
    next(c for c in comp["criteria"] if c["id"] == "cv_1k")["channels"] = ["Ch03"]
    results = evaluate(summary_csv_factory(), comp)
    assert len(results) == 29 and [r["channel"] for r in results if r["criterion_id"] == "cv_1k"] == ["Ch03"]


# ---- registry and gate -------------------------------------------------------------

def test_registry_entries_have_fn_and_scope():
    assert all(callable(e["fn"]) and e["scope"] in (SAMPLE, AGGREGATE) for e in COMPARISON_REGISTRY.values())
    assert comparison_scope("count_tolerance") == SAMPLE == comparison_scope("hybrid_tolerance")
    assert comparison_scope("acceptance_criteria") == AGGREGATE
    assert comparison_scope("nope") is None


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "v.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    yield c
    c.close()


def test_gate_passes_aggregate_only_experiments_without_a_gold_standard(conn):
    assert run_gate(conn, EXP_ID, comparison_list()) == (True, "pass")


def comparison_list():
    return [comparison()]


def test_gate_still_requires_a_gold_standard_otherwise(conn):
    tol = {"type": "count_tolerance", "tolerance_percent": 10, "columns": ["x"]}
    assert run_gate(conn, EXP_ID) == (False, "no_gold_standard")                  # legacy call
    assert run_gate(conn, EXP_ID, [tol]) == (False, "no_gold_standard")
    assert run_gate(conn, EXP_ID, [comparison(), tol]) == (False, "no_gold_standard")   # mixed
    assert run_gate(conn, EXP_ID, [{"type": "mystery"}]) == (False, "no_gold_standard")
    assert run_gate(conn, EXP_ID, []) == (False, "no_gold_standard")


# ---- comparator dispatch -----------------------------------------------------------

def test_compare_experiment_aggregate_only_needs_no_gold_standard(conn, summary_csv_factory):
    out = compare_experiment(conn, experiment(), str(summary_csv_factory()))
    assert out["gs_exp_version_id"] is None and out["comparison_results"] == []
    assert len(out["aggregate_results"]) == 32
    assert len(out["observation_set"].observations) == 384


def test_setup_failures_become_a_recorded_error_not_a_crash(conn, summary_csv_factory, tmp_path):
    exp = experiment()
    exp["comparisons"][0]["annotation"] = "NO_SUCH_EXPERIMENT"
    out = compare_experiment(conn, exp, str(summary_csv_factory()))
    (row,) = out["aggregate_results"]
    assert row["verdict"] == "error" and "NO_SUCH_EXPERIMENT" in row["note"] and out["observation_set"] is None

    exp = experiment()
    exp["comparisons"][0]["columns"]["Ch03"] = "NOT_A_COLUMN"
    (row,) = compare_experiment(conn, exp, str(summary_csv_factory()))["aggregate_results"]
    assert row["verdict"] == "error" and "NOT_A_COLUMN" in row["note"]

    exp = experiment()
    exp["comparisons"][0]["criteria"][0]["metric"] = "bogus"
    (row,) = compare_experiment(conn, exp, str(summary_csv_factory()))["aggregate_results"]
    assert row["verdict"] == "error" and "unknown metric" in row["note"]


def test_unknown_type_and_duplicate_aggregate_entries_raise(conn, summary_csv_factory):
    exp = experiment()
    exp["comparisons"].append({"type": "mystery"})
    with pytest.raises(ValueError, match="Unknown comparison type"):
        compare_experiment(conn, exp, str(summary_csv_factory()))
    exp = experiment()
    exp["comparisons"].append(copy.deepcopy(exp["comparisons"][0]))
    with pytest.raises(ValueError, match="one entry holding all criteria"):
        compare_experiment(conn, exp, str(summary_csv_factory()))


def test_sample_scope_comparison_still_runs_through_the_registry(conn, summary_csv_factory):
    """A mixed experiment runs both scopes; sample scope uses the registered gold standard."""
    from src.registration.registrar import register_gold_standard
    csv_path = str(summary_csv_factory())
    register_gold_standard(conn, EXP_ID, csv_path, "tester", "test")
    conn.commit()
    exp = experiment()
    exp["comparisons"].append({"type": "count_tolerance", "tolerance_percent": 10.0,
                               "columns": [COLUMNS["Ch01"]]})
    out = compare_experiment(conn, exp, csv_path)
    assert out["gs_exp_version_id"] and len(out["comparison_results"]) == 96
    assert all(r["verdict"] == "pass" for r in out["comparison_results"])
    assert len(out["aggregate_results"]) == 32


# ---- orchestrator persistence -------------------------------------------------------

def run_verify(tmp_path, csv_path, exp, run_id="run_20261028_001"):
    results = tmp_path / "results"
    folder = results / "MP37_V_V_Testcase_2_beta05_261028_1000"
    folder.mkdir(parents=True)
    (folder / csv_path.name).write_bytes(csv_path.read_bytes())
    config = {"paths": {"database": str(tmp_path / "v.db"), "results_dir": str(results),
                        "reports_dir": str(tmp_path / "reports"),
                        "manifests_dir": str(tmp_path / "manifests")}}
    initialise_database(config["paths"]["database"], str(SCHEMA))
    manifest = {"run": {"run_id": run_id, "triggered_at": "2026-10-28T10:00:00Z",
                        "pipeline_build": "IAP-v3.0", "scenario": "acceptance"},
                "build_verdict_policy": {"rule": "all_stability_experiments_must_pass",
                                         "on_exploratory_failure": "warn_only"},
                "experiments": [exp]}
    verify_run(config, run_id, manifest, {EXP_ID: folder.name}, "m.json")
    return get_connection(config["paths"]["database"]), run_id


def test_verify_run_persists_aggregate_results_and_observations(tmp_path, summary_csv_factory):
    conn, run_id = run_verify(tmp_path, summary_csv_factory(), experiment())
    assert conn.execute("SELECT verdict FROM runs").fetchone()[0] == "pass"
    er = conn.execute("SELECT * FROM experiment_results").fetchone()
    assert er["verdict"] == "pass" and er["pre_verify_status"] == "pass" and er["gs_exp_version_id"] is None
    agg = models.get_aggregate_results(conn, er["result_id"])
    assert len(agg) == 32 and {a["verdict"] for a in agg} == {"pass"}
    assert all(a["annotation_sha256"] and a["source_file_sha256"] for a in agg)
    assert len(models.get_result_observations(conn, er["result_id"])) == 384
    assert conn.execute("SELECT COUNT(*) FROM sample_results").fetchone()[0] == 0
    assert (tmp_path / "reports" / run_id / "summary_report.csv").exists()


def test_verify_run_marks_the_experiment_failed_on_a_failing_or_error_criterion(tmp_path, summary_csv_factory):
    rows = [(n, c) for n, c in mp37_rows() if n != "FourPlex_1k_8"]
    conn, _ = run_verify(tmp_path, summary_csv_factory(rows), experiment())
    er = conn.execute("SELECT * FROM experiment_results").fetchone()
    assert er["verdict"] == "fail"
    agg = models.get_aggregate_results(conn, er["result_id"])
    assert {a["verdict"] for a in agg} == {"pass", "error"}
    assert conn.execute("SELECT verdict FROM runs").fetchone()[0] == "fail"
