-- =============================================================
-- Regression Test Verification Service — SQLite Schema
-- =============================================================
-- Conventions:
--   - All PKs are UUIDs stored as TEXT (SQLite has no UUID type)
--   - Timestamps stored as TEXT in ISO-8601 format
--   - Foreign keys enforced via PRAGMA foreign_keys = ON at runtime
--   - superseded_at NULL means the record is currently active
-- =============================================================

PRAGMA foreign_keys = ON;


-- -------------------------------------------------------------
-- GOLD STANDARD TABLES
-- These are the real sources of truth. Everything else references
-- back to these.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gold_standard_exp_versions (
    gs_exp_version_id   TEXT        PRIMARY KEY,
    experiment_id       TEXT        NOT NULL,       -- natural id e.g. "EXP_LT_control_assay"
    version_number      INTEGER     NOT NULL,       -- per-experiment counter
    checksum_hash       TEXT        NOT NULL,       -- SHA-256 of the gold standard CSV
    file_path           TEXT        NOT NULL,       -- local path to the CSV at registration time
    registered_at       TEXT        NOT NULL,       -- ISO-8601
    registered_by       TEXT        NOT NULL,
    reason              TEXT,                       -- e.g. "algo improvement v2.4"
    superseded_at       TEXT        DEFAULT NULL,   -- NULL = currently active for this experiment
    UNIQUE (experiment_id, version_number)
);

CREATE TABLE IF NOT EXISTS gold_standard_samples (
    gs_sample_id            TEXT    PRIMARY KEY,
    gs_exp_version_id       TEXT    NOT NULL
        REFERENCES gold_standard_exp_versions (gs_exp_version_id),
    sample_id               TEXT    NOT NULL,
    full_metrics            TEXT    NOT NULL,   -- JSON blob, all columns
    notes                   TEXT    DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_gs_samples_version
    ON gold_standard_samples (gs_exp_version_id);

CREATE INDEX IF NOT EXISTS idx_gs_exp_versions_experiment
    ON gold_standard_exp_versions (experiment_id, superseded_at);


-- -------------------------------------------------------------
-- RELEASE TABLES (optional convenience — not load-bearing)
-- A release is a named snapshot of whichever experiment versions
-- were active at a given point in time. Nothing in the
-- verification logic depends on these tables.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gold_standard_releases (
    release_id          TEXT        PRIMARY KEY,
    release_number      INTEGER     NOT NULL UNIQUE,
    created_at          TEXT        NOT NULL,       -- ISO-8601
    created_by          TEXT        NOT NULL,
    notes               TEXT        DEFAULT NULL    -- e.g. "post algo-v2.4 update"
);

CREATE TABLE IF NOT EXISTS release_entries (
    release_id          TEXT        NOT NULL REFERENCES gold_standard_releases (release_id),
    gs_exp_version_id   TEXT        NOT NULL REFERENCES gold_standard_exp_versions (gs_exp_version_id),
    PRIMARY KEY (release_id, gs_exp_version_id)
);


-- -------------------------------------------------------------
-- RUN TABLES
-- One run = one execution of the test harness.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT        PRIMARY KEY,
    triggered_at        TEXT        NOT NULL,       -- ISO-8601
    pipeline_build      TEXT        NOT NULL,       -- e.g. "IAP-v2.4.1"
    scenario            TEXT        NOT NULL,       -- e.g. "full_regression_suite"
    manifest_path       TEXT        NOT NULL,       -- path to the JSON manifest that governed this run
    verdict             TEXT        NOT NULL        -- "pass" | "fail" | "warn" | "aborted"
        CHECK (verdict IN ('pass', 'fail', 'warn', 'aborted'))
);


-- -------------------------------------------------------------
-- EXPERIMENT RESULTS
-- One row per experiment per run.
-- pre_verify_status captures the integrity gate outcome
-- separately from the comparison verdict.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS experiment_results (
    result_id           TEXT        PRIMARY KEY,
    run_id              TEXT        NOT NULL REFERENCES runs (run_id),
    gs_exp_version_id   TEXT        REFERENCES gold_standard_exp_versions (gs_exp_version_id),
                                                    -- NULL when gate fails with no_gold_standard
    experiment_id       TEXT        NOT NULL,       -- denormalised for convenient querying
    feature_set         TEXT        NOT NULL,
    classification      TEXT        NOT NULL        -- "stability" | "exploratory"
        CHECK (classification IN ('stability', 'exploratory')),
    pre_verify_status   TEXT        NOT NULL
        CHECK (pre_verify_status IN ('pass', 'checksum_fail', 'subset_fail', 'no_gold_standard', 'result_not_found')),
    verdict             TEXT        NOT NULL        -- "pass" | "fail" | "warn" | "aborted"
        CHECK (verdict IN ('pass', 'fail', 'warn', 'aborted')),
    verified_at         TEXT        NOT NULL        -- ISO-8601
);

CREATE INDEX IF NOT EXISTS idx_exp_results_run
    ON experiment_results (run_id);

CREATE INDEX IF NOT EXISTS idx_exp_results_experiment
    ON experiment_results (experiment_id, verified_at);  -- longitudinal queries


-- -------------------------------------------------------------
-- SAMPLE RESULTS
-- One row per sample per experiment result.
-- expected_value is denormalised from gold_standard_samples
-- as a snapshot — it never changes even if the gold standard
-- is later updated.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sample_results (
    sample_result_id    TEXT    PRIMARY KEY,
    result_id           TEXT    NOT NULL
        REFERENCES experiment_results (result_id),
    gs_sample_id        TEXT    NOT NULL
        REFERENCES gold_standard_samples (gs_sample_id),
    sample_id           TEXT    NOT NULL,
    metric              TEXT    NOT NULL,       -- column name e.g. UM-01_CountsPer50ul
    comparison_type     TEXT    NOT NULL,       -- e.g. count_tolerance
    actual_value        REAL,                  -- NULL when sample is missing from result CSV
    expected_value      REAL    NOT NULL,
    deviation_percent   REAL,                  -- NULL when expected_value is zero
    verdict             TEXT    NOT NULL
        CHECK (verdict IN ('pass', 'fail')),
    notes               TEXT    DEFAULT NULL   -- future LLM annotation per sample result
);

CREATE INDEX IF NOT EXISTS idx_sample_results_result
    ON sample_results (result_id);

CREATE INDEX IF NOT EXISTS idx_sample_results_sample
    ON sample_results (sample_id, metric);     -- longitudinal per-(sample, metric) queries


-- -------------------------------------------------------------
-- REPORTS
-- LLM-generated narrative linked to a run and optionally to
-- a specific experiment result for per-experiment reports.
-- result_id is nullable to allow run-level summary reports.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reports (
    report_id           TEXT        PRIMARY KEY,
    run_id              TEXT        NOT NULL REFERENCES runs (run_id),
    result_id           TEXT        DEFAULT NULL    -- NULL = run-level report
                                    REFERENCES experiment_results (result_id),
    llm_narrative       TEXT        NOT NULL,       -- Phase 3 placeholder
    overall_verdict     TEXT        NOT NULL        -- "pass" | "fail" | "warn"
        CHECK (overall_verdict IN ('pass', 'fail', 'warn')),
    generated_at        TEXT        NOT NULL,       -- ISO-8601
    report_json         TEXT        NOT NULL DEFAULT ''  -- structured JSON blob for future HTML rendering
);

CREATE INDEX IF NOT EXISTS idx_reports_run
    ON reports (run_id);


-- -------------------------------------------------------------
-- AGGREGATE RESULTS (ADR-021 / ADR-026)
-- One row per aggregate-scope check: a metric computed over a group
-- (or groups) of samples, per channel, judged against absolute limits
-- instead of a gold standard. Parallel to sample_results, which stays
-- one-row-per-sample. Applied to existing databases by re-running
-- `python -m src.main --init` (CREATE TABLE IF NOT EXISTS; idempotent).
--
-- verdict 'error' means the check could not be computed (e.g. a needed
-- count was missing); actual_value is NULL and notes says why. It is
-- here from the start because SQLite cannot alter a CHECK later.
-- lower/upper_inclusive: 1 = the bound itself passes, 0 = it fails,
-- NULL = no bound on that side.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aggregate_results (
    aggregate_result_id TEXT    PRIMARY KEY,
    result_id           TEXT    NOT NULL
        REFERENCES experiment_results (result_id),
    criterion_id        TEXT    DEFAULT NULL,   -- id of the criterion in the manifest, e.g. cv_1k
    group_key           TEXT    NOT NULL,       -- group(s) the metric was computed over; '__ALL__' when ungrouped
    channel             TEXT    DEFAULT NULL,   -- e.g. Ch01; NULL when the check is not per channel
    metric              TEXT    NOT NULL,       -- e.g. loglog_slope, cv
    comparison_type     TEXT    NOT NULL,       -- e.g. acceptance_criteria
    actual_value        REAL,
    expected_value      REAL    DEFAULT NULL,   -- only for strategies that compare to a target
    deviation_percent   REAL    DEFAULT NULL,
    lower_bound         REAL    DEFAULT NULL,
    lower_inclusive     INTEGER DEFAULT NULL CHECK (lower_inclusive IN (0, 1)),
    upper_bound         REAL    DEFAULT NULL,
    upper_inclusive     INTEGER DEFAULT NULL CHECK (upper_inclusive IN (0, 1)),
    margin              REAL    DEFAULT NULL,   -- distance to the nearest bound; negative = outside
    verdict             TEXT    NOT NULL
        CHECK (verdict IN ('pass', 'fail', 'error')),
    notes               TEXT    DEFAULT NULL,
    -- provenance: which annotation and which pipeline output this came from
    annotation_version  INTEGER DEFAULT NULL,
    annotation_sha256   TEXT    DEFAULT NULL,
    source_file_sha256  TEXT    DEFAULT NULL,   -- SHA-256 of the pipeline output CSV
    filters             TEXT    DEFAULT NULL    -- JSON: preprocessing filters applied, with counts removed
);

CREATE INDEX IF NOT EXISTS idx_aggregate_results_result
    ON aggregate_results (result_id);

CREATE INDEX IF NOT EXISTS idx_aggregate_results_criterion
    ON aggregate_results (criterion_id, channel);   -- longitudinal per-criterion queries


-- -------------------------------------------------------------
-- RESULT OBSERVATIONS (ADR-026)
-- The pipeline rows an evaluation used, one per sample per channel,
-- with the annotation applied. Stored so every aggregate number can
-- be traced to the counts behind it, and so pipeline outputs sit
-- beside the regression service's own outputs.
-- expected_count: integer = assay with that many copies, 0 = no-template
-- control, NULL = no assay on that channel. Never conflate NULL and 0.
-- count_value is NULL when the pipeline reported NA / blank.
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS result_observations (
    observation_id      TEXT    PRIMARY KEY,
    result_id           TEXT    NOT NULL
        REFERENCES experiment_results (result_id),
    sample_name         TEXT    NOT NULL,
    sample_id           TEXT    NOT NULL,       -- the pipeline's SampleID, e.g. 1JH1BB_01
    strip_barcode       TEXT    DEFAULT NULL,
    carousel_position   TEXT    DEFAULT NULL,
    tube_position       TEXT    DEFAULT NULL,
    channel             TEXT    NOT NULL,
    source_column       TEXT    NOT NULL,       -- CSV column the count came from
    count_value         REAL,
    group_name          TEXT    NOT NULL,       -- annotation group
    role                TEXT    NOT NULL,       -- 'standard' | 'ntc'
    expected_count      REAL    DEFAULT NULL,
    rule                TEXT    DEFAULT NULL,   -- annotation rule that matched the sample
    issues              TEXT    DEFAULT NULL,   -- pipeline Issues column
    note                TEXT    DEFAULT NULL,   -- pipeline Observation column
    UNIQUE (result_id, sample_name, channel)
);

CREATE INDEX IF NOT EXISTS idx_result_observations_result
    ON result_observations (result_id);

CREATE INDEX IF NOT EXISTS idx_result_observations_group
    ON result_observations (result_id, group_name, channel);


-- =============================================================
-- USEFUL QUERIES (reference)
-- =============================================================

-- Current active gold standard for a given experiment:
--
--   SELECT * FROM gold_standard_exp_versions
--   WHERE experiment_id = 'EXP_LT_control_assay'
--     AND superseded_at IS NULL;

-- Longitudinal trend for a sample against a fixed gs version:
--
--   SELECT r.pipeline_build, r.triggered_at,
--          sr.actual_value, sr.expected_value,
--          sr.deviation_percent, sr.verdict
--   FROM sample_results sr
--   JOIN experiment_results er ON sr.result_id = er.result_id
--   JOIN runs r ON er.run_id = r.run_id
--   JOIN gold_standard_exp_versions gsv ON er.gs_exp_version_id = gsv.gs_exp_version_id
--   WHERE sr.sample_id = '2DU008_01'
--     AND gsv.version_number = 1
--   ORDER BY r.triggered_at ASC;

-- All experiments that changed in a given release:
--
--   SELECT gsv.experiment_id, gsv.version_number, gsv.reason
--   FROM release_entries re
--   JOIN gold_standard_exp_versions gsv ON re.gs_exp_version_id = gsv.gs_exp_version_id
--   WHERE re.release_id = '<release_id>';
