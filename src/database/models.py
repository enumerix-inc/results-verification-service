"""
models.py — all database insert and query functions.

No other module writes SQL directly. All database interaction
goes through functions defined here.
"""

import json
import uuid


# ---------------------------------------------------------------
# Gold standard registration
# ---------------------------------------------------------------

def insert_gs_exp_version(conn, record: dict) -> str:
    """Insert a new gold standard experiment version. Returns gs_exp_version_id."""
    conn.execute(
        """
        INSERT INTO gold_standard_exp_versions
            (gs_exp_version_id, experiment_id, version_number, checksum_hash,
             file_path, registered_at, registered_by, reason, superseded_at)
        VALUES
            (:gs_exp_version_id, :experiment_id, :version_number, :checksum_hash,
             :file_path, :registered_at, :registered_by, :reason, :superseded_at)
        """,
        record,
    )
    return record["gs_exp_version_id"]


def insert_gs_sample(conn, record: dict) -> None:
    """Insert a single gold standard sample."""
    conn.execute(
        """
        INSERT INTO gold_standard_samples
            (gs_sample_id, gs_exp_version_id, sample_id, full_metrics, notes)
        VALUES
            (:gs_sample_id, :gs_exp_version_id, :sample_id, :full_metrics, :notes)
        """,
        record,
    )


def get_active_gs_version(conn, experiment_id: str) -> dict | None:
    """Return the currently active gold standard version for an experiment."""
    row = conn.execute(
        """
        SELECT * FROM gold_standard_exp_versions
        WHERE experiment_id = ? AND superseded_at IS NULL
        """,
        (experiment_id,),
    ).fetchone()
    return dict(row) if row else None


def retire_gs_version(conn, gs_exp_version_id: str, superseded_at: str) -> None:
    """Set superseded_at on an existing gold standard version."""
    conn.execute(
        """
        UPDATE gold_standard_exp_versions
        SET superseded_at = ?
        WHERE gs_exp_version_id = ?
        """,
        (superseded_at, gs_exp_version_id),
    )


def get_gs_samples(conn, gs_exp_version_id: str) -> list[dict]:
    """Return all samples for a gold standard experiment version."""
    rows = conn.execute(
        """
        SELECT gs_sample_id, sample_id, full_metrics
        FROM gold_standard_samples
        WHERE gs_exp_version_id = ?
        """,
        (gs_exp_version_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------

def get_all_processed_run_ids(conn) -> set:
    """Return the set of all run_ids already recorded in the verification DB."""
    rows = conn.execute("SELECT run_id FROM runs").fetchall()
    return {row["run_id"] for row in rows}


def get_all_reported_run_ids(conn) -> set:
    """
    Return the set of run_ids that have a completed report.

    A row in runs only proves verify_run() committed its run/results
    transaction; write_report() (CSVs + the reports table row) happens
    afterward, in a separate transaction. Reconciliation logic that needs
    to know a run is fully done -- not just recorded -- should check this
    set as well as get_all_processed_run_ids(), not the runs table alone.
    """
    rows = conn.execute("SELECT DISTINCT run_id FROM reports").fetchall()
    return {row["run_id"] for row in rows}


def insert_run(conn, record: dict) -> None:
    """Insert a new run record."""
    try:
        conn.execute(
            """
            INSERT INTO runs
                (run_id, triggered_at, pipeline_build, scenario, manifest_path, verdict)
            VALUES
                (:run_id, :triggered_at, :pipeline_build, :scenario, :manifest_path, :verdict)
            """,
            record,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to insert run '{record['run_id']}': {e}") from e


# ---------------------------------------------------------------
# Results
# ---------------------------------------------------------------

def insert_experiment_result(conn, record: dict) -> str:
    """Insert an experiment result. Returns result_id."""
    try:
        conn.execute(
            """
            INSERT INTO experiment_results
                (result_id, run_id, gs_exp_version_id, experiment_id, feature_set,
                 classification, pre_verify_status, verdict, verified_at)
            VALUES
                (:result_id, :run_id, :gs_exp_version_id, :experiment_id, :feature_set,
                 :classification, :pre_verify_status, :verdict, :verified_at)
            """,
            record,
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to insert experiment result for '{record['experiment_id']}': {e}"
        ) from e
    return record["result_id"]


def insert_sample_result(conn, record: dict) -> None:
    """Insert a single sample result (one row per sample per metric)."""
    try:
        conn.execute(
            """
            INSERT INTO sample_results
                (sample_result_id, result_id, gs_sample_id, sample_id,
                 metric, comparison_type, actual_value, expected_value,
                 deviation_percent, verdict, notes)
            VALUES
                (:sample_result_id, :result_id, :gs_sample_id, :sample_id,
                 :metric, :comparison_type, :actual_value, :expected_value,
                 :deviation_percent, :verdict, :notes)
            """,
            record,
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to insert sample result for '{record['sample_id']}': {e}"
        ) from e


# ---------------------------------------------------------------
# Reports
# ---------------------------------------------------------------

def insert_report(conn, record: dict) -> None:
    """Insert a report record."""
    try:
        conn.execute(
            """
            INSERT INTO reports
                (report_id, run_id, result_id, llm_narrative,
                 overall_verdict, generated_at, report_json)
            VALUES
                (:report_id, :run_id, :result_id, :llm_narrative,
                 :overall_verdict, :generated_at, :report_json)
            """,
            record,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to insert report for run '{record['run_id']}': {e}") from e


# ---------------------------------------------------------------
# Aggregate results and observations (ADR-021 / ADR-026)
# ---------------------------------------------------------------

_AGGREGATE_COLUMNS = (
    "aggregate_result_id", "result_id", "criterion_id", "test_case", "group_key", "channel",
    "metric", "comparison_type", "actual_value", "expected_value",
    "deviation_percent", "lower_bound", "lower_inclusive", "upper_bound",
    "upper_inclusive", "margin", "verdict", "notes", "annotation_version",
    "annotation_sha256", "source_file_sha256", "filters",
)
_AGGREGATE_REQUIRED = ("aggregate_result_id", "result_id", "group_key", "metric",
                       "comparison_type", "verdict")


def _tri_bool(value):
    """bool/None -> 1/0/None for the inclusive flags."""
    return None if value is None else int(bool(value))


def insert_aggregate_result(conn, record: dict) -> str:
    """Insert one aggregate-scope result. Returns aggregate_result_id.

    Optional fields may be omitted (stored as NULL). `lower_inclusive` and
    `upper_inclusive` take True/False/None; `filters` takes a list or dict
    and is stored as JSON.
    """
    missing = [k for k in _AGGREGATE_REQUIRED if record.get(k) is None]
    if missing:
        raise RuntimeError(f"Aggregate result is missing required field(s): {missing}")
    row = {k: record.get(k) for k in _AGGREGATE_COLUMNS}
    row["lower_inclusive"] = _tri_bool(row["lower_inclusive"])
    row["upper_inclusive"] = _tri_bool(row["upper_inclusive"])
    if row["filters"] is not None and not isinstance(row["filters"], str):
        row["filters"] = json.dumps(row["filters"])
    try:
        conn.execute(
            f"""
            INSERT INTO aggregate_results ({", ".join(_AGGREGATE_COLUMNS)})
            VALUES ({", ".join(":" + c for c in _AGGREGATE_COLUMNS)})
            """,
            row,
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to insert aggregate result '{record['metric']}' "
            f"({record.get('criterion_id')}, {record.get('channel')}): {e}"
        ) from e
    return record["aggregate_result_id"]


def get_aggregate_results(conn, result_id: str) -> list[dict]:
    """Return a result's aggregate rows in insertion order.

    Inclusive flags come back as True/False/None and `filters` as parsed JSON.
    """
    rows = conn.execute(
        "SELECT * FROM aggregate_results WHERE result_id = ? ORDER BY rowid",
        (result_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("lower_inclusive", "upper_inclusive"):
            d[k] = None if d[k] is None else bool(d[k])
        if d["filters"] is not None:
            d["filters"] = json.loads(d["filters"])
        out.append(d)
    return out


def insert_result_observations(conn, result_id: str, observations: list[dict]) -> int:
    """Insert the pipeline observations an evaluation used. Returns the row count.

    Each observation dict uses the field names of src.observations.Observation
    (dataclasses.asdict output works): sample_name, sample_id, strip_barcode,
    carousel_position, tube_position, channel, column, count, group, role,
    expected, rule, issues, note. All rows go in or none do.
    """
    rows = []
    for o in observations:
        rows.append({
            "observation_id": str(uuid.uuid4()),
            "result_id": result_id,
            "sample_name": o["sample_name"],
            "sample_id": o["sample_id"],
            "strip_barcode": o.get("strip_barcode"),
            "carousel_position": o.get("carousel_position"),
            "tube_position": o.get("tube_position"),
            "channel": o["channel"],
            "source_column": o["column"],
            "count_value": o.get("count"),
            "group_name": o["group"],
            "role": o["role"],
            "expected_count": o.get("expected"),
            "rule": o.get("rule"),
            "issues": o.get("issues") or None,
            "note": o.get("note") or None,
        })
    try:
        with conn:  # one transaction: all rows or none
            conn.executemany(
                """
                INSERT INTO result_observations
                    (observation_id, result_id, sample_name, sample_id, strip_barcode,
                     carousel_position, tube_position, channel, source_column,
                     count_value, group_name, role, expected_count, rule, issues, note)
                VALUES
                    (:observation_id, :result_id, :sample_name, :sample_id, :strip_barcode,
                     :carousel_position, :tube_position, :channel, :source_column,
                     :count_value, :group_name, :role, :expected_count, :rule, :issues, :note)
                """,
                rows,
            )
    except Exception as e:
        raise RuntimeError(
            f"Failed to insert observations for result '{result_id}': {e}") from e
    return len(rows)


def get_result_observations(conn, result_id: str, group_name: str | None = None,
                            channel: str | None = None) -> list[dict]:
    """Return a result's observations in insertion order, optionally filtered."""
    sql, args = "SELECT * FROM result_observations WHERE result_id = ?", [result_id]
    if group_name is not None:
        sql += " AND group_name = ?"
        args.append(group_name)
    if channel is not None:
        sql += " AND channel = ?"
        args.append(channel)
    return [dict(r) for r in conn.execute(sql + " ORDER BY rowid", args).fetchall()]


def list_experiment_results(conn, experiment_id: str) -> list[dict]:
    """All results for an experiment, oldest first, with the run's pipeline build
    and manifest path (for comparing runs, e.g. the determinism canary)."""
    rows = conn.execute(
        """
        SELECT er.result_id, er.run_id, er.experiment_id, er.verdict, er.pre_verify_status,
               er.verified_at, r.pipeline_build, r.triggered_at
        FROM experiment_results er JOIN runs r ON r.run_id = er.run_id
        WHERE er.experiment_id = ?
        ORDER BY er.verified_at, er.rowid
        """,
        (experiment_id,),
    ).fetchall()
    return [dict(r) for r in rows]
