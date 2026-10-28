"""
Tests for the acceptance report (src/reporter/acceptance_report.py), the
test_case label, and the added-column migration (ADR-026, TES-2877).
"""

import csv
import json
import statistics
from pathlib import Path

import pytest

from conftest import mp37_rows
from src.comparator import metrics
from src.comparator.acceptance_params import AcceptanceParamsError, parse_acceptance_params
from src.database import models
from src.database.db import get_connection, initialise_database
from src.orchestrator.orchestrator import verify_run
from src.reporter.acceptance_report import advisory_line, format_limits
from src.reporter.reporter import write_report

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "entry_point" / "manifest_template_acceptance.json"
SCHEMA = REPO / "schema" / "verification_schema.sql"
EXP_ID = "MP37_V_V_Testcase_2_beta05"
RUN_ID = "run_20261028_001"


def experiment():
    return json.loads(TEMPLATE.read_text())["experiments"][0]


def run_verify(tmp_path, csv_path, exp):
    results = tmp_path / "results"
    folder = results / "MP37_V_V_Testcase_2_beta05_261028_1000"
    folder.mkdir(parents=True)
    (folder / csv_path.name).write_bytes(csv_path.read_bytes())
    config = {"paths": {"database": str(tmp_path / "v.db"), "results_dir": str(results),
                        "reports_dir": str(tmp_path / "reports"), "manifests_dir": str(tmp_path)}}
    initialise_database(config["paths"]["database"], str(SCHEMA))
    manifest = {"run": {"run_id": RUN_ID, "triggered_at": "2026-10-28T10:00:00Z",
                        "pipeline_build": "IAP-v3.0.1", "scenario": "acceptance"},
                "build_verdict_policy": {"rule": "all_stability_experiments_must_pass",
                                         "on_exploratory_failure": "warn_only"},
                "experiments": [exp]}
    verify_run(config, RUN_ID, manifest, {EXP_ID: folder.name}, "m.json")
    return tmp_path / "reports" / RUN_ID, config


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def report(tmp_path, summary_csv_factory):
    d, config = run_verify(tmp_path, summary_csv_factory(), experiment())
    return d, config


# ---- acceptance: report and CSV for an MP37 run, values traceable -----------------------

def test_files_are_written_next_to_the_existing_reports(report):
    d, _ = report
    assert {p.name for p in d.iterdir()} == {
        "detail_report.csv", "summary_report.csv", "acceptance_report.md",
        "acceptance_summary.csv", "acceptance_observations.csv"}


def test_summary_csv_has_a_row_per_criterion_and_channel_with_provenance(report):
    rows = read_csv(report[0] / "acceptance_summary.csv")
    assert len(rows) == 32 and {r["verdict"] for r in rows} == {"pass"}
    assert {r["test_case"] for r in rows} == {"TC1.2"}
    r = next(r for r in rows if r["criterion_id"] == "plateauing_ratio_1M" and r["channel"] == "Ch01")
    assert r["limits"] == "-0.2 <= x <= 0.2" and round(float(r["value"]), 3) == 0.165
    assert float(r["margin"]) == pytest.approx(0.0346, abs=1e-4)
    assert r["groups"] == "FourPlex_1M" and r["rows_used"] == "32"        # 8 at 1M + 24 for the slope fit
    assert r["pipeline_build"] == "IAP-v3.0.1" and r["run_id"] == RUN_ID
    assert len(r["annotation_sha256"]) == 64 and len(r["source_file_sha256"]) == 64
    assert len(r["criteria_sha256"]) == 64 and r["annotation_version"] == "1"
    assert next(x for x in rows if x["criterion_id"] == "cv_1k")["rows_used"] == "8"
    assert next(x for x in rows if x["criterion_id"] == "linearity_exponent")["rows_used"] == "24"


def test_observations_csv_has_every_raw_row_and_who_used_it(report):
    rows = read_csv(report[0] / "acceptance_observations.csv")
    assert len(rows) == 384
    by = {(r["sample_name"], r["channel"]): r for r in rows}
    assert set(by[("FourPlex_1k_3", "Ch01")]["used_by"].split()) == {
        "linearity_exponent", "linear_slope", "plateauing_ratio_1M", "cv_1k"}
    assert set(by[("FourPlex_1M_1", "Ch02")]["used_by"].split()) == {"plateauing_ratio_1M", "cv_1M"}
    assert by[("FourPlex_1p5M_1", "Ch01")]["used_by"] == ""            # not used by any criterion
    assert by[("Ch3_100k_2", "Ch03")]["used_by"] == ""
    assert by[("FourPlex_100k_1", "Ch04")]["count"] == "90821"
    assert by[("Ch3_100k_2", "Ch01")]["expected_count"] == ""          # no assay stays blank, not 0
    assert by[("FourPlex_0_1", "Ch01")]["expected_count"] == "0"


def test_every_reported_value_can_be_recomputed_from_the_raw_rows(report):
    summary = read_csv(report[0] / "acceptance_summary.csv")
    raw = read_csv(report[0] / "acceptance_observations.csv")

    def counts(group, ch):
        return [float(r["count"]) for r in raw if r["group"] == group and r["channel"] == ch]
    for ch in ("Ch01", "Ch02", "Ch03", "Ch04"):
        cv = float(next(r for r in summary if r["criterion_id"] == "cv_100k" and r["channel"] == ch)["value"])
        assert cv == pytest.approx(metrics.cv(counts("FourPlex_100k", ch)))
        ntc = float(next(r for r in summary if r["criterion_id"] == "ntc_mean_counts" and r["channel"] == ch)["value"])
        assert ntc == pytest.approx(statistics.fmean(counts("FourPlex_0", ch)))
        n = float(next(r for r in summary if r["criterion_id"] == "linearity_exponent" and r["channel"] == ch)["value"])
        lv = [(1000, "FourPlex_1k"), (10000, "FourPlex_10k"), (100000, "FourPlex_100k")]
        assert n == pytest.approx(metrics.loglog_slope(
            [e for e, _ in lv], [statistics.fmean(counts(g, ch)) for _, g in lv]))


def test_markdown_report_reads_for_a_reviewer(report):
    md = (report[0] / "acceptance_report.md").read_text(encoding="utf-8")
    assert "# Acceptance criteria report - run run_20261028_001" in md
    assert "ADVISORY ONLY" in md and "release decision is made by a reviewer" in md
    assert "All 32 criterion checks met their limits (advisory)." in md
    assert "Pipeline build: IAP-v3.0.1" in md and "Annotation: version 1" in md
    assert "Criteria (manifest entry) SHA-256" in md and "Preprocessing filters: none" in md
    assert "Observations: 384 (96 samples)" in md and "Ch01 = UM-01_CountsPer50ul" in md
    assert "### Test case TC1.2" in md and "| Criterion | Metric | Groups | Limits | Ch01 | Ch02 | Ch03 | Ch04 |" in md
    assert "| linearity_exponent | loglog_slope | FourPlex_1k+FourPlex_10k+FourPlex_100k | `0.9 < x < 1.1` | 0.9842 PASS (+0.0842)" in md
    assert "| plateauing_ratio_1M | plateau_ratio | FourPlex_1M | `-0.2 <= x <= 0.2` | 0.1654 PASS (+0.0346)" in md
    assert "### Problems" not in md and "acceptance_observations.csv" in md


def test_failures_and_errors_are_called_out(tmp_path, summary_csv_factory):
    rows = [(n, c) for n, c in mp37_rows() if n != "FourPlex_1k_8"]
    d, _ = run_verify(tmp_path, summary_csv_factory(rows), experiment())
    md = (d / "acceptance_report.md").read_text(encoding="utf-8")
    assert "did NOT meet their limits (0 outside limits, 16 could not be computed) (advisory)" in md
    assert "### Problems" in md and "7 replicates" in md and "| ERROR |" in md
    srows = read_csv(d / "acceptance_summary.csv")
    err = next(r for r in srows if r["verdict"] == "error")
    assert err["value"] == "" and err["margin"] == "" and "expected 8" in err["notes"]
    assert err["limits"] != ""                                          # limits still shown


def test_setup_failure_is_reported_not_hidden(tmp_path, summary_csv_factory):
    exp = experiment()
    exp["comparisons"][0]["annotation"] = "NO_SUCH"
    d, _ = run_verify(tmp_path, summary_csv_factory(), exp)
    md = (d / "acceptance_report.md").read_text(encoding="utf-8")
    assert "NO_SUCH" in md and "(setup)" in md and "could not be computed" in md
    assert read_csv(d / "acceptance_observations.csv") == []


def test_test_case_labels_group_the_markdown(tmp_path, summary_csv_factory):
    exp = experiment()
    for c in exp["comparisons"][0]["criteria"]:
        if c["id"].startswith("cv_"):
            c["test_case"] = "TC1.9"
    d, _ = run_verify(tmp_path, summary_csv_factory(), exp)
    md = (d / "acceptance_report.md").read_text(encoding="utf-8")
    assert "### Test case TC1.2" in md and "### Test case TC1.9" in md
    cases = {r["criterion_id"]: r["test_case"] for r in read_csv(d / "acceptance_summary.csv")}
    assert cases["cv_1k"] == "TC1.9" and cases["linear_slope"] == "TC1.2"


# ---- gold standard output is unchanged --------------------------------------------------

def test_a_gold_standard_only_run_writes_no_acceptance_files(tmp_path):
    initialise_database(str(tmp_path / "v.db"), str(SCHEMA))
    conn = get_connection(str(tmp_path / "v.db"))
    models.insert_run(conn, {"run_id": "r", "triggered_at": "t", "pipeline_build": "b",
                             "scenario": "s", "manifest_path": "m", "verdict": "pass"})
    conn.commit()
    config = {"paths": {"reports_dir": str(tmp_path / "reports")}}
    exp = {"experiment_id": "E", "feature_set": "fs", "classification": "stability", "verdict": "pass",
           "comparisons": [{"type": "count_tolerance", "tolerance_percent": 10.0, "columns": ["c"]}],
           "sample_results": [{"sample_id": "S1", "comparison_type": "count_tolerance", "metric": "c",
                               "actual_value": 100, "expected_value": 100, "deviation_percent": 0.0,
                               "verdict": "pass"}]}
    write_report(conn, config, "r", [exp], "pass")
    assert {p.name for p in (tmp_path / "reports" / "r").iterdir()} == {"detail_report.csv", "summary_report.csv"}
    blob = json.loads(conn.execute("SELECT report_json FROM reports").fetchone()[0])
    assert set(blob) == {"run_id", "detail", "summary"}


def test_report_blob_in_the_database_carries_the_acceptance_rows(report):
    conn = get_connection(report[1]["paths"]["database"])
    blob = json.loads(conn.execute("SELECT report_json FROM reports").fetchone()[0])
    assert len(blob["acceptance"]) == 32 and {"detail", "summary"} <= set(blob)


# ---- formatting helpers ----------------------------------------------------------------

def test_format_limits():
    f = format_limits
    assert f(dict(lower_bound=0.9, lower_inclusive=False, upper_bound=1.1, upper_inclusive=False)) == "0.9 < x < 1.1"
    assert f(dict(lower_bound=None, upper_bound=0.1, upper_inclusive=False)) == "x < 0.1"
    assert f(dict(lower_bound=-0.2, lower_inclusive=True, upper_bound=0.2, upper_inclusive=True)) == "-0.2 <= x <= 0.2"
    assert f(dict(lower_bound=None, upper_bound=None)) == ""


def test_advisory_line_counts():
    ok, bad, err = {"verdict": "pass"}, {"verdict": "fail"}, {"verdict": "error"}
    assert advisory_line([ok, ok]) == "All 2 criterion checks met their limits (advisory)."
    assert "1 of 3" in advisory_line([ok, bad, ok]) and "1 outside limits, 0 could" in advisory_line([ok, bad, ok])
    assert "2 of 3" in advisory_line([ok, bad, err]) and "1 could not be computed" in advisory_line([ok, bad, err])


# ---- test_case label: params, storage, and in-place column migration ---------------------

def test_test_case_param_is_validated():
    e = experiment()["comparisons"][0]
    assert parse_acceptance_params(e).criteria[0].test_case == "TC1.2"
    e["criteria"][0]["test_case"] = "  "
    with pytest.raises(AcceptanceParamsError, match="test_case"):
        parse_acceptance_params(e)
    del e["criteria"][0]["test_case"]
    assert parse_acceptance_params(e).criteria[0].test_case is None


def test_test_case_is_stored_and_read_back(tmp_path, summary_csv_factory):
    _, config = run_verify(tmp_path, summary_csv_factory(), experiment())
    conn = get_connection(config["paths"]["database"])
    rid = conn.execute("SELECT result_id FROM experiment_results").fetchone()[0]
    assert {a["test_case"] for a in models.get_aggregate_results(conn, rid)} == {"TC1.2"}


def test_existing_aggregate_table_without_test_case_gains_the_column(tmp_path):
    db = tmp_path / "old.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    c.execute("DROP TABLE aggregate_results")
    c.execute("""CREATE TABLE aggregate_results (
        aggregate_result_id TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES experiment_results (result_id),
        criterion_id TEXT, group_key TEXT NOT NULL, channel TEXT, metric TEXT NOT NULL,
        comparison_type TEXT NOT NULL, actual_value REAL, expected_value REAL, deviation_percent REAL,
        lower_bound REAL, lower_inclusive INTEGER, upper_bound REAL, upper_inclusive INTEGER, margin REAL,
        verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail','error')), notes TEXT,
        annotation_version INTEGER, annotation_sha256 TEXT, source_file_sha256 TEXT, filters TEXT)""")
    c.commit()
    c.close()
    initialise_database(str(db), str(SCHEMA))
    initialise_database(str(db), str(SCHEMA))                           # idempotent
    c = get_connection(str(db))
    assert "test_case" in {r[1] for r in c.execute("PRAGMA table_info(aggregate_results)")}
