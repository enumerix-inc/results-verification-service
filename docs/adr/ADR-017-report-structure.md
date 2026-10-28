# ADR-017: Report structure, output format, and multi-column comparisons

## Status
Accepted

## Context
The reporter module needed a design that could demonstrate value to
leadership in the MVP while remaining easy to extend as the system matures.
Several decisions needed to be made simultaneously:

1. What structure should reports have?
2. What format should the human-readable output be?
3. How should multi-dye / multi-column experiments be handled in comparisons
   and reports?
4. Where should reports be stored?

## Decisions

### 1. Two report types per run

**Detail report** — one row per sample per metric per experiment.
The diagnostic view used to investigate failures.

Columns: `feature_set, comparison_type, metric, criteria, sample_id,
expected, actual, deviation_percent, verdict, notes`

**Summary report** — one row per metric per experiment. The executive
view used to assess overall build health. Melted on comparison type —
each (feature_set, comparison_type, metric) combination is one row.

Columns: `feature_set, comparison_type, metric, criteria, samples_passed,
samples_failed, verdict, notes`

Both reports are written to `{reports_dir}/{run_id}/` as CSV files for
the MVP. CSV was chosen because it is immediately human-readable, openable
in Excel, and requires no additional tooling. It is explicitly considered
scaffolding — to be replaced with HTML templating in a future iteration.

### 2. JSON stored in database alongside CSV output

The full structured result data is stored as a JSON blob in the `reports`
table. The CSV files are the human-readable rendering of that JSON. When
HTML templating is introduced, it will read from the JSON blob rather than
parsing CSVs.

### 3. Feature set over experiment ID in reports

Reports surface `feature_set` rather than `experiment_id`. Feature sets
are the human-meaningful labels that map to testable pipeline capabilities.
Experiment IDs are internal keys. Future iterations may replace feature
set names with formal requirement identifiers (e.g. RT-001).

### 4. Three-level report hierarchy

Reports have three distinct semantic levels that each get their own column:

- **Feature set** — what pipeline capability is being tested
  e.g. `baseline_algo_performance`
- **Comparison type** — what kind of test is being performed
  e.g. `count_tolerance`
- **Metric** — which specific column is being measured
  e.g. `UM-01_CountsPer50ul`, `FAM_CountsPer50ul`

Reports are melted on comparison type — each `(feature_set,
comparison_type, metric)` combination gets its own row. For a 3-dye
experiment with a single `count_tolerance` comparison, the summary
report has 3 rows — one per dye channel.

The result dict from each strategy function carries all three levels:

```python
{
    "sample_id": sample_id,
    "comparison_type": "count_tolerance",   # from params["type"]
    "metric": column_name,                  # which column was measured
    "actual_value": actual_value,
    "expected_value": expected_value,
    "deviation_percent": ...,
    "verdict": "pass" | "fail"
}
```

### 5. "Criteria" terminology

The comparison parameter field in reports is labelled `criteria` rather
than `threshold`. This is intentional — not all comparison types use
simple threshold values (e.g. a future `correlation` type uses a minimum
R² value, not a tolerance percentage). `criteria` is neutral enough to
describe any comparison parameter.

### 6. Multi-column comparison support via manifest `columns` field

The `comparisons` entry in the manifest now includes a `columns` list
declaring which CSV columns to compare for a given comparison type:

```json
{
  "type": "count_tolerance",
  "tolerance_percent": 10.0,
  "columns": ["UM-01_CountsPer50ul", "FAM_CountsPer50ul"]
}
```

The comparison strategy iterates over the `columns` list and produces
per-column results for each sample. This handles variable dye counts
(4-10 channels) without any code changes — the manifest declares what
to compare.

This change also supersedes the hardcoded `PRIMARY_METRIC` constant in
`registrar.py` and the `primary_metric` / `primary_metric_value`
scaffolding columns. With columns declared in the manifest, hardcoding
is no longer needed.

### 7. Report storage location

Reports are written to `{reports_dir}/{run_id}/` where `reports_dir` is
configured in `config.yaml`. On the regression machine this is the F:
drive (`F:/RegressionTesting/reports/`) which is a dedicated 20TB
archival storage drive. Path is config-driven — no code changes needed
to change the location.

### 8. Notes field as LLM placeholder

The `notes` column in both report types is empty string for the MVP.
It is explicitly designed as the future insertion point for LLM-generated
narrative analysis (Phase 3). The field is present in all data structures
from the start so the LLM upgrade requires only populating it rather than
adding a new field.

## Consequences

- CSV output is scaffolding — a future task to replace with HTML
  templating is tracked in STATUS.md
- The `columns` field in the manifest is now required for all comparison
  types — existing manifests must be updated
- `PRIMARY_METRIC` constant and `primary_metric` / `primary_metric_value`
  scaffold columns are retired — tracked in STATUS.md schema changes
- Each comparison type's strategy function must handle the `columns` list
  from params — `count_tolerance` updated accordingly
- Reports directory must exist or be created before first run —
  `write_report()` creates `{reports_dir}/{run_id}/` automatically

## Future work captured
- Replace CSV output with HTML templating (similar to attached reference)
- Populate `notes` field with LLM narrative (Phase 3)
- Replace `feature_set` labels with formal requirement IDs (e.g. RT-001)
- Drop `primary_metric` / `primary_metric_value` scaffold columns from
  schema once all manifests use `columns` field

## Alternatives considered
- Single report type only — rejected because summary and detail serve
  different audiences and use cases
- HTML output from the start — rejected as premature; CSV is sufficient
  to demonstrate value and HTML templating is a separable concern
- Hardcode column names per experiment type — rejected; manifest-declared
  columns are more flexible and eliminate the PRIMARY_METRIC coupling
- Store reports as files only, not in DB — rejected because JSON in DB
  enables future programmatic querying and longitudinal report analysis

## Amendment — acceptance report added for TES-2869

**Date:** 2026-10-28

Aggregate-scope results (ADR-021, ADR-026) add three files to `{reports_dir}/{run_id}/`
when a run has any. The detail and summary CSVs above are unchanged, and a run with
only gold standard comparisons writes exactly the same files as before.

- `acceptance_report.md` — reviewer-oriented: provenance (pipeline build, annotation
  version and SHA-256, source file and its SHA-256, a SHA-256 of the manifest entry
  holding the criteria, channel columns, preprocessing filters), an advisory
  "all criteria met" line, a criteria-by-channel table (value, verdict, margin to the
  nearest limit) grouped by `test_case`, and a Problems list for failures and errors.
- `acceptance_summary.csv` — one row per criterion per channel, with the limits, margin,
  verdict, number of raw rows used, and provenance columns.
- `acceptance_observations.csv` — every raw row (sample x channel) with the annotation
  applied; `used_by` lists the criteria that used the row, so any value can be traced
  to, and recomputed from, its rows.

The JSON blob in the `reports` table gains an `acceptance` list (the summary rows)
only when these files are written. Reports stay CSV/Markdown for now; HTML remains
future work.
