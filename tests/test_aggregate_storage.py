"""
Tests for the aggregate_results / result_observations tables and their model
functions (ADR-021 / ADR-026, TES-2875).
"""

import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

from src.database import models
from src.database.db import get_connection, initialise_database
from src.observations import Observation

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "verification_schema.sql"
SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "v.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    yield c
    c.close()


def make_result(conn, run_id="run_1", experiment_id="MP37_V_V_Testcase_2_beta05"):
    models.insert_run(conn, {"run_id": run_id, "triggered_at": "2026-10-28T10:00:00Z",
                             "pipeline_build": "IAP-v3.0", "scenario": "acceptance",
                             "manifest_path": "m.json", "verdict": "pass"})
    rid = str(uuid.uuid4())
    models.insert_experiment_result(conn, {
        "result_id": rid, "run_id": run_id, "gs_exp_version_id": None,   # no gold standard
        "experiment_id": experiment_id, "feature_set": "acceptance_criteria_tc1_2",
        "classification": "stability", "pre_verify_status": "pass", "verdict": "pass",
        "verified_at": "2026-10-28T10:05:00Z"})
    conn.commit()
    return rid


def agg(result_id, **over):
    d = dict(aggregate_result_id=str(uuid.uuid4()), result_id=result_id,
             criterion_id="linearity_exponent",
             group_key="FourPlex_1k+FourPlex_10k+FourPlex_100k", channel="Ch01",
             metric="loglog_slope", comparison_type="acceptance_criteria",
             actual_value=0.9841755845, lower_bound=0.9, lower_inclusive=False,
             upper_bound=1.1, upper_inclusive=False, margin=0.0841755845,
             verdict="pass", annotation_version=1, annotation_sha256=SHA_A,
             source_file_sha256=SHA_B,
             filters=[{"name": "exclude_no_assay", "removed": 96}])
    d.update(over)
    return d


def obs(sample="FourPlex_1k_1", ch="Ch01", **over):
    d = dict(sample_name=sample, sample_id="BC_01", strip_barcode="1JH1BB",
             carousel_position="01", tube_position="1", channel=ch,
             column="UM-01_CountsPer50ul", count=987, group="FourPlex_1k",
             role="standard", expected=1000, rule="fourplex", issues="", note="")
    d.update(over)
    return d


# ---- aggregate results ---------------------------------------------------------

def test_aggregate_result_round_trip_with_provenance(conn):
    rid = make_result(conn)
    models.insert_aggregate_result(conn, agg(rid))
    (row,) = models.get_aggregate_results(conn, rid)
    assert row["actual_value"] == pytest.approx(0.9841755845)
    assert (row["lower_bound"], row["lower_inclusive"]) == (0.9, False)
    assert (row["upper_bound"], row["upper_inclusive"]) == (1.1, False)
    assert row["margin"] == pytest.approx(0.0841755845)
    assert row["verdict"] == "pass" and row["channel"] == "Ch01"
    assert row["annotation_version"] == 1
    assert row["annotation_sha256"] == SHA_A and row["source_file_sha256"] == SHA_B
    assert row["filters"] == [{"name": "exclude_no_assay", "removed": 96}]


def test_inclusive_flags_come_back_as_booleans_or_none(conn):
    rid = make_result(conn)
    models.insert_aggregate_result(conn, agg(rid, criterion_id="pr", lower_inclusive=True,
                                             upper_bound=None, upper_inclusive=None))
    (row,) = models.get_aggregate_results(conn, rid)
    assert row["lower_inclusive"] is True
    assert row["upper_bound"] is None and row["upper_inclusive"] is None


def test_optional_fields_may_be_omitted(conn):
    rid = make_result(conn)
    models.insert_aggregate_result(conn, {
        "aggregate_result_id": "x1", "result_id": rid, "group_key": "__ALL__",
        "metric": "cv", "comparison_type": "acceptance_criteria", "verdict": "pass"})
    (row,) = models.get_aggregate_results(conn, rid)
    assert row["channel"] is None and row["filters"] is None and row["margin"] is None


def test_error_verdict_is_allowed_with_a_note(conn):
    rid = make_result(conn)
    models.insert_aggregate_result(conn, agg(rid, verdict="error", actual_value=None,
                                             margin=None, notes="3 samples had no count"))
    (row,) = models.get_aggregate_results(conn, rid)
    assert row["verdict"] == "error" and row["actual_value"] is None
    assert "no count" in row["notes"]


def test_results_come_back_in_insertion_order_and_per_result(conn):
    r1, r2 = make_result(conn, "run_1"), make_result(conn, "run_2")
    for cid in ("a", "b", "c"):
        models.insert_aggregate_result(conn, agg(r1, criterion_id=cid))
    models.insert_aggregate_result(conn, agg(r2, criterion_id="z"))
    assert [r["criterion_id"] for r in models.get_aggregate_results(conn, r1)] == ["a", "b", "c"]
    assert [r["criterion_id"] for r in models.get_aggregate_results(conn, r2)] == ["z"]
    assert models.get_aggregate_results(conn, "nope") == []


def test_aggregate_rejects_bad_input(conn):
    rid = make_result(conn)
    with pytest.raises(RuntimeError, match="verdict"):
        models.insert_aggregate_result(conn, agg(rid, verdict=None))
    with pytest.raises(RuntimeError, match="Failed to insert aggregate"):
        models.insert_aggregate_result(conn, agg(rid, verdict="maybe"))
    with pytest.raises(RuntimeError, match="Failed to insert aggregate"):
        models.insert_aggregate_result(conn, agg("no-such-result"))      # foreign key
    with pytest.raises(RuntimeError, match="Failed to insert aggregate"):
        a = agg(rid)
        models.insert_aggregate_result(conn, a)
        models.insert_aggregate_result(conn, a)                          # duplicate id


# ---- observations ----------------------------------------------------------------

def test_observations_round_trip(conn):
    rid = make_result(conn)
    n = models.insert_result_observations(conn, rid, [
        obs(),
        obs("FourPlex_0_1", count=15, group="FourPlex_0", role="ntc", expected=0),
        obs("Ch3_100k_1", ch="Ch01", count=22, group="Ch3_100k", expected=None,
            rule="singleplex_ch3_100k"),
        obs("FourPlex_1p5M_1", count=None, issues="x", note="High Count Alert (>1M)."),
    ])
    assert n == 4
    rows = models.get_result_observations(conn, rid)
    assert [r["sample_name"] for r in rows] == [
        "FourPlex_1k_1", "FourPlex_0_1", "Ch3_100k_1", "FourPlex_1p5M_1"]
    first, ntc, noassay, missing = rows
    assert (first["count_value"], first["expected_count"], first["group_name"],
            first["role"], first["source_column"]) == (987, 1000, "FourPlex_1k",
                                                       "standard", "UM-01_CountsPer50ul")
    assert ntc["expected_count"] == 0 and ntc["role"] == "ntc"
    assert noassay["expected_count"] is None          # null (no assay) is not 0 (NTC)
    assert missing["count_value"] is None and missing["issues"] == "x"
    assert missing["note"] == "High Count Alert (>1M)." and first["note"] is None


def test_observation_filters(conn):
    rid = make_result(conn)
    models.insert_result_observations(conn, rid, [
        obs("FourPlex_1k_1", "Ch01"), obs("FourPlex_1k_1", "Ch02"),
        obs("FourPlex_10k_1", "Ch01", group="FourPlex_10k")])
    assert len(models.get_result_observations(conn, rid, group_name="FourPlex_1k")) == 2
    assert len(models.get_result_observations(conn, rid, channel="Ch01")) == 2
    assert len(models.get_result_observations(conn, rid, "FourPlex_1k", "Ch02")) == 1


def test_observations_insert_is_all_or_nothing(conn):
    rid = make_result(conn)
    with pytest.raises(RuntimeError, match="Failed to insert observations"):
        models.insert_result_observations(conn, rid, [obs(), obs()])   # duplicate (sample, channel)
    assert models.get_result_observations(conn, rid) == []
    with pytest.raises(RuntimeError):
        models.insert_result_observations(conn, "no-such-result", [obs()])


def test_full_mp37_observation_set_stores_384_rows(conn):
    rid = make_result(conn)
    rows = [obs(f"S{i}", f"Ch0{c}", column=f"UM-0{c}_CountsPer50ul")
            for i in range(96) for c in range(1, 5)]
    assert models.insert_result_observations(conn, rid, rows) == 384
    assert len(models.get_result_observations(conn, rid)) == 384


def test_accepts_observation_dataclass_asdict(conn):
    rid = make_result(conn)
    o = Observation(sample_name="FourPlex_100k_1", sample_id="BC_01", strip_barcode="BC",
                    carousel_position="01", tube_position="1", channel="Ch01",
                    column="UM-01_CountsPer50ul", count=97151, group="FourPlex_100k",
                    role="standard", expected=100000, rule="fourplex", issues="", note="")
    models.insert_result_observations(conn, rid, [asdict(o)])
    (row,) = models.get_result_observations(conn, rid)
    assert row["count_value"] == 97151 and row["expected_count"] == 100000


# ---- existing databases get the tables ---------------------------------------------

def test_existing_database_gains_the_tables_on_reinit(tmp_path):
    db = tmp_path / "old.db"
    initialise_database(str(db), str(SCHEMA))
    c = get_connection(str(db))
    rid = make_result(c)
    c.execute("DROP TABLE aggregate_results")      # simulate a database made before TES-2875
    c.execute("DROP TABLE result_observations")
    c.commit()
    c.close()

    initialise_database(str(db), str(SCHEMA))      # what `python -m src.main --init` does
    c = get_connection(str(db))
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aggregate_results", "result_observations"} <= tables
    assert c.execute("SELECT COUNT(*) FROM experiment_results").fetchone()[0] == 1  # data kept
    models.insert_aggregate_result(c, agg(rid))
    c.commit()
    c.close()
    initialise_database(str(db), str(SCHEMA))      # idempotent: rows survive a second init
    c = get_connection(str(db))
    assert len(models.get_aggregate_results(c, rid)) == 1
    c.close()


def test_foreign_keys_are_enforced_on_the_new_tables(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO result_observations (observation_id, result_id, sample_name,"
                     " sample_id, channel, source_column, group_name, role)"
                     " VALUES ('o', 'missing', 's', 'i', 'Ch01', 'c', 'g', 'standard')")
