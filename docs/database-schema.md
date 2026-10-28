# Verification database — schema reference

## Technology choice

SQLite. Chosen because the verification service runs on a single dedicated lab machine with no need for concurrent multi-user access, network availability, or a separate database server. The entire database is a single file. See [ADR-006](adr/ADR-006-sqlite.md).

The schema DDL lives at: `schema/verification_schema.sql`

---

## Design principles

**Records are immutable snapshots.** Once a run is written, it is never modified. `expected_value` and `full_expected_metrics` in `sample_results` are denormalised from the gold standard and frozen at the time of the run — they are never backfilled if the gold standard is later updated.

**Gold standard versions are never deleted.** When a gold standard is updated, the old version is retired by setting `superseded_at`. The full history of what was expected at every point in time is always intact.

**The release is a lens, not a load-bearing wall.** `gold_standard_releases` and `release_entries` are convenience tables only. Nothing in the verification logic depends on them. They exist to answer "what were the active standards on date X?" See [ADR-008](adr/ADR-008-gold-standard-versioning.md).

---

## Table reference

### `gold_standard_exp_versions`
One row per registered version of a gold standard experiment. The `superseded_at` field is null for the currently active version of each experiment.

| Column | Type | Description |
|---|---|---|
| `gs_exp_version_id` | TEXT PK | Surrogate UUID |
| `experiment_id` | TEXT | Natural id e.g. `"EXP_LT_control_assay"`. Repeats across versions — this is expected and correct. |
| `version_number` | INTEGER | Per-experiment counter. Increments on each update. |
| `checksum_hash` | TEXT | SHA-256 of the gold standard CSV at registration time. |
| `file_path` | TEXT | Local path to the CSV at registration time. |
| `registered_at` | TEXT | ISO-8601 timestamp. |
| `registered_by` | TEXT | Name of registering person. |
| `reason` | TEXT | Human-readable reason for this version e.g. `"algo improvement v2.4"`. |
| `superseded_at` | TEXT | NULL if currently active. ISO-8601 timestamp when retired. |

---

### `gold_standard_samples`
One row per sample per gold standard experiment version. This is where the actual expected values live.

| Column | Type | Description |
|---|---|---|
| `gs_sample_id` | TEXT PK | Surrogate UUID |
| `gs_exp_version_id` | TEXT FK | References `gold_standard_exp_versions` |
| `sample_id` | TEXT | Natural sample id e.g. `"2DU008_01"`. Repeats across versions. |
| `primary_metric` | TEXT | *(MVP scaffolding)* Name of the metric being compared e.g. `"count"`. Provides a simple single-value path through the pipeline so all machinery can be built before the JSON comparison design is finalised. |
| `primary_metric_value` | REAL | *(MVP scaffolding)* The gold standard value for the primary metric. |
| `full_metrics` | TEXT | JSON blob containing all metric columns from the gold standard CSV. |
| `notes` | TEXT | Optional annotation. |

---

### `gold_standard_releases`
Optional. A named snapshot of which experiment versions were active at a point in time.

| Column | Type | Description |
|---|---|---|
| `release_id` | TEXT PK | Surrogate UUID |
| `release_number` | INTEGER | Auto-incrementing human reference number. |
| `created_at` | TEXT | ISO-8601 timestamp. |
| `created_by` | TEXT | |
| `notes` | TEXT | e.g. `"post algo-v2.4 gold standard update"` |

---

### `release_entries`
Join table linking a release to the experiment versions it captured.

| Column | Type | Description |
|---|---|---|
| `release_id` | TEXT FK | References `gold_standard_releases` |
| `gs_exp_version_id` | TEXT FK | References `gold_standard_exp_versions` |

---

### `runs`
One row per test harness execution.

| Column | Type | Description |
|---|---|---|
| `run_id` | TEXT PK | Matches the `run_id` in the manifest filename. |
| `triggered_at` | TEXT | ISO-8601 timestamp. |
| `pipeline_build` | TEXT | e.g. `"IAP-v2.4.1"` |
| `scenario` | TEXT | e.g. `"full_regression_suite"` |
| `manifest_path` | TEXT | Path to the manifest JSON that governed this run. |
| `verdict` | TEXT | `"pass"` \| `"fail"` \| `"warn"` \| `"aborted"` |

---

### `experiment_results`
One row per experiment per run.

| Column | Type | Description |
|---|---|---|
| `result_id` | TEXT PK | Surrogate UUID |
| `run_id` | TEXT FK | References `runs` |
| `gs_exp_version_id` | TEXT FK | The exact gold standard version this experiment was verified against. |
| `experiment_id` | TEXT | Denormalised for convenient querying. |
| `feature_set` | TEXT | Denormalised for convenient querying. |
| `classification` | TEXT | `"stability"` \| `"exploratory"` |
| `pre_verify_status` | TEXT | `"pass"` \| `"checksum_fail"` \| `"subset_fail"` |
| `verdict` | TEXT | `"pass"` \| `"fail"` \| `"warn"` \| `"aborted"` |
| `verified_at` | TEXT | ISO-8601 timestamp. |

---

### `sample_results`
One row per sample per experiment result.

| Column | Type | Description |
|---|---|---|
| `sample_result_id` | TEXT PK | Surrogate UUID |
| `result_id` | TEXT FK | References `experiment_results` |
| `gs_sample_id` | TEXT FK | The exact gold standard sample this was compared against. |
| `sample_id` | TEXT | Denormalised for convenient querying. |
| `primary_metric` | TEXT | *(MVP scaffolding)* Name of the metric compared. Mirrors `gold_standard_samples.primary_metric`. |
| `actual_value` | REAL | *(MVP scaffolding)* The pipeline output value for the primary metric. |
| `expected_value` | REAL | *(MVP scaffolding)* Snapshot of the primary metric gold standard value at time of run. Never backfilled. |
| `deviation_percent` | REAL | *(MVP scaffolding)* `abs(actual - expected) / expected * 100` |
| `full_actual_metrics` | TEXT | JSON blob of all actual metric values produced by the pipeline for this sample. |
| `full_expected_metrics` | TEXT | JSON blob snapshot of all gold standard metric values at time of run. Never backfilled. |
| `verdict` | TEXT | `"pass"` \| `"fail"` |

---

### `aggregate_results`
One row per aggregate-scope check (ADR-021, ADR-026): a metric computed over one or more annotation groups, per channel, judged against absolute limits. Parallel to `sample_results`, which stays one row per sample. Added for TES-2869; existing databases gain the table when `python -m src.main --init` is re-run (the installer does this on upgrade). The schema uses `CREATE TABLE IF NOT EXISTS`, so re-running is safe.

| Column | Type | Description |
|---|---|---|
| `aggregate_result_id` | TEXT PK | Surrogate UUID |
| `result_id` | TEXT FK | References `experiment_results` |
| `criterion_id` | TEXT (nullable) | The criterion's `id` in the manifest, e.g. `cv_1k`. |
| `group_key` | TEXT | The group(s) the metric was computed over, joined with `+`; `__ALL__` when ungrouped. |
| `channel` | TEXT (nullable) | Logical channel, e.g. `Ch01`. |
| `metric` | TEXT | e.g. `loglog_slope`, `geomean_recovery`, `plateau_ratio`, `cv`, `mean_counts`. |
| `comparison_type` | TEXT | e.g. `acceptance_criteria`. |
| `actual_value` | REAL | The computed value. NULL when the check could not be computed. |
| `expected_value`, `deviation_percent` | REAL (nullable) | For strategies that compare to a target; unused by acceptance criteria. |
| `lower_bound`, `upper_bound` | REAL (nullable) | The limits the value was judged against (snapshot of the manifest). |
| `lower_inclusive`, `upper_inclusive` | INTEGER (nullable) | 1 = the bound itself passes, 0 = it fails, NULL = no bound on that side. The model functions read and write these as booleans. |
| `margin` | REAL (nullable) | Distance to the nearest bound; negative = outside the limits. |
| `verdict` | TEXT | `"pass"` \| `"fail"` \| `"error"`. `error` = could not be computed (see `notes`). It is allowed from the start because SQLite cannot alter a CHECK later. |
| `notes` | TEXT (nullable) | Reason for an `error`, or reviewer-facing remarks. |
| `annotation_version`, `annotation_sha256` | INTEGER, TEXT | Version and SHA-256 of the sample annotation file used. |
| `source_file_sha256` | TEXT | SHA-256 of the pipeline output CSV evaluated. |
| `filters` | TEXT | JSON list of the preprocessing filters applied and how many observations each removed. |

---

### `result_observations`
The pipeline rows an evaluation used: one per sample per channel, with the annotation applied (ADR-026). Stored so every aggregate number can be traced to the counts behind it, and so pipeline outputs sit beside the service's own outputs. Unique on (`result_id`, `sample_name`, `channel`).

| Column | Type | Description |
|---|---|---|
| `observation_id` | TEXT PK | Surrogate UUID |
| `result_id` | TEXT FK | References `experiment_results` |
| `sample_name`, `sample_id` | TEXT | The workbook sample name and the pipeline's SampleID. |
| `strip_barcode`, `carousel_position`, `tube_position` | TEXT (nullable) | Where the sample ran. |
| `channel` | TEXT | Logical channel, e.g. `Ch01`. |
| `source_column` | TEXT | CSV column the count came from, e.g. `UM-01_CountsPer50ul`. |
| `count_value` | REAL (nullable) | The count; NULL when the pipeline reported NA or blank. |
| `group_name` | TEXT | Annotation group, e.g. `FourPlex_1k`. |
| `role` | TEXT | `standard` or `ntc`. |
| `expected_count` | REAL (nullable) | Integer = assay with that many copies, `0` = no-template control, NULL = no assay on that channel. Never conflate NULL and 0. |
| `rule` | TEXT | The annotation rule that matched the sample. |
| `issues`, `note` | TEXT (nullable) | The pipeline's Issues and Observation columns, for the reviewer. |

---

### `reports`
LLM-generated narrative reports. `result_id` is nullable — a null value indicates a run-level summary report rather than a per-experiment report.

| Column | Type | Description |
|---|---|---|
| `report_id` | TEXT PK | Surrogate UUID |
| `run_id` | TEXT FK | References `runs` |
| `result_id` | TEXT FK (nullable) | References `experiment_results`. NULL = run-level report. |
| `llm_narrative` | TEXT | Full narrative text from the LLM analysis module. |
| `overall_verdict` | TEXT | `"pass"` \| `"fail"` \| `"warn"` |
| `generated_at` | TEXT | ISO-8601 timestamp. |

---

## Key queries

**Current active gold standard for an experiment:**
```sql
SELECT * FROM gold_standard_exp_versions
WHERE experiment_id = 'EXP_LT_control_assay'
  AND superseded_at IS NULL;
```

**Longitudinal trend for a sample against a fixed gold standard version:**
```sql
SELECT r.pipeline_build, r.triggered_at,
       sr.actual_value, sr.expected_value,
       sr.deviation_percent, sr.verdict
FROM sample_results sr
JOIN experiment_results er ON sr.result_id = er.result_id
JOIN runs r ON er.run_id = r.run_id
JOIN gold_standard_exp_versions gsv ON er.gs_exp_version_id = gsv.gs_exp_version_id
WHERE sr.sample_id = '2DU008_01'
  AND gsv.version_number = 1
ORDER BY r.triggered_at ASC;
```

**All experiments updated in a given release:**
```sql
SELECT gsv.experiment_id, gsv.version_number, gsv.reason
FROM release_entries re
JOIN gold_standard_exp_versions gsv ON re.gs_exp_version_id = gsv.gs_exp_version_id
WHERE re.release_id = '<release_id>';
```

**All failed stability experiments across the last 10 runs:**
```sql
SELECT r.run_id, r.pipeline_build, er.experiment_id, er.verdict
FROM experiment_results er
JOIN runs r ON er.run_id = r.run_id
WHERE er.classification = 'stability'
  AND er.verdict = 'fail'
ORDER BY r.triggered_at DESC
LIMIT 10;
```
