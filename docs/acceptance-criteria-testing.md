# Acceptance criteria testing: how it works, how to run it, how to review it

This is the practical guide for the acceptance criteria regression test (TES-2869,
design in ADR-026). The service judges a reanalysed experiment against absolute
acceptance criteria (V&V Test Case 1.2, DH062 Table 2) instead of a gold standard.
It records everything and produces a report; **a person reviews the report and the raw
data and makes the release decision.** The "all criteria met" line is advisory.

## How a run flows

1. A dispatch of the *Trigger Regression Run* workflow stamps a run context manifest
   (template `acceptance`, or `include_acceptance` on another template) and starts the
   test harness, which reanalyses the experiment's images.
2. The service detects the finished reanalysis, reads the experiment's
   `CountableDataSummary.csv`, and loads the sample annotation
   `annotations/<experiment_id>.yaml`. Every sample must match exactly one annotation rule.
3. Each criterion in the manifest entry is computed per channel from the annotated
   counts and judged against its limits.
4. Results go to the database (`aggregate_results`, `result_observations`) and to
   the report folder `{reports_dir}\{run_id}\`.

No gold standard is needed. A manifest may mix acceptance and gold standard experiments.

## Reviewing a run

Open `acceptance_report.md` in the run's report folder.

- **Advisory line.** "All 32 criterion checks met their limits", or how many did not.
  It is computed from the table, not a decision.
- **Provenance.** Pipeline build, annotation version and SHA-256, source file and its
  SHA-256, a SHA-256 of the criteria entry, the channel columns, and any preprocessing
  filters. Two reports with the same provenance were judged identically.
- **Table.** One row per criterion, one column per channel. Each cell is
  `value VERDICT (margin)`; the margin is the distance to the nearest limit and is
  negative when outside. Watch small margins: in the MP37 workbook data the plateauing
  ratio sits at 0.14 to 0.17 against a limit of 0.20. Ratios (PR, CV) are fractions
  (0.20 = 20%).
- **Problems.** Anything outside limits (`fail`) or not computable (`error`, with the
  reason: a group missing or with the wrong replicate count, an NA count, a channel
  without an assay, an annotation that could not be loaded). An `error` is never
  skipped and counts as a failure for the experiment.

Tracing any number to its raw rows:

- `acceptance_summary.csv` has one row per criterion and channel: the groups used, how
  many raw rows (`rows_used`), the limits, the margin and the provenance.
- `acceptance_observations.csv` has every raw row (sample x channel) with the group,
  role and expected count applied, the pipeline's Issues and Observation notes, and
  `used_by`: the criteria that used that row. Filter it on the criterion id (or on the
  group and channel) and recompute the value in a spreadsheet.
- Observations flagged by the pipeline (for example "High Count Alert (>1M)" on the
  1.5M samples) are shown but only affect a criterion if a criterion uses those samples.

The report only records. Write the release decision where your process keeps it.

## Validating a new setup: the MP37 end-to-end run

### Before you start

- Merge the branches for TES-2871 to TES-2879 to `main` (the workflow checks out the
  ref you dispatch it from, and the installer is built from your working tree).
- The software build and the test harness must come from the same version, and the
  version must write the dye column names `UM-01_CountsPer50ul` ... (software 3.0 and
  later). For an older build whose summary has `Ch01_CountsPer50ul` columns, change the
  `columns` map in `entry_point/manifest_template_acceptance.json` to match.
- No unarchived `context_manifest_*.json` may be sitting in the manifests folder (the
  stamp step refuses otherwise).

### 1. Rebuild the installer and upgrade the service

Build with `installer\build_payload.ps1` (see `installer\README.md`) and run the new
setup over the existing install. Config is kept. The installer re-runs `--init`, which
adds the new tables and the `test_case` column, then `--preflight`. Expect a line like

    [OK  ] annotation MP37_V_V_Testcase_2_beta05.yaml: experiment MP37_V_V_Testcase_2_beta05, version 1

If it shows `[FAIL]` or `[WARN] no annotation files`, the annotations folder did not
make it into the payload. The service should then be Running.

### 2. Put the images where the harness expects them

    E:\CountableLabs\Data\MP37_V_V_Testcase_2_beta05\
        (the raw images)
        Workbook_MP37_V_V_Testcase_2_beta05.json

### 3. Install the matching software and harness

Install the software version you are validating together with its matching test harness
(see the setup guide). Remember the service, config, harness and installed app must all
be from the same version.

### 4. Dispatch

*Trigger Regression Run* with `template: acceptance`, the `pipeline_build` you installed,
and the runner label of the machine (`pcr-regression-test` for a test runner). To run it
together with the gold standard experiments instead, use `template: full` and tick
`include_acceptance`.

### 5. Check the result

When the service reports the run complete, open the report folder and review it as above,
then compare with the UMA workbook:

    python -m src.validation.workbook_check --run-id run_YYYYMMDD_NNN

Run it from the repository folder with the repo's `.venv` (it reads `paths.database` from
your config; pass `--db` to point at another file). It compares all 256 four-plex counts
and all 32 criterion values with the workbook and prints exactly what differs. The
workbook data came from an earlier software version, so on a different build small
differences can be legitimate: use `--count-tolerance` and `--value-tolerance` and judge
them against the margins in the report.

### 6. Determinism canary

Dispatch the same thing again with the same installed build (after the first run's
manifest has been archived), then

    python -m src.validation.canary --experiment-id MP37_V_V_Testcase_2_beta05

It compares the two most recent results (or name them with `--run-a` / `--run-b`) and
passes only if every observation and every aggregate value is identical. It refuses to
compare different builds: determinism holds within one IAP version only. A failure means
the assumption that a failing criterion is a real failure, not noise, needs looking at.

## When something goes wrong

| Symptom | Likely cause |
|---|---|
| Stamp step refuses with an acceptance error | The entry in the template is malformed, the annotation file was not found, or its channels differ from `columns`. The message says which. |
| Report shows one `(setup)` error row | The annotation could not be loaded, the CSV lacks a column named in `columns` (wrong software version?), or a sample matched no annotation rule. The note has the reason. |
| `error` rows for several criteria | A group has the wrong number of replicates (`replicates` in the entry), an NA count, or a missing sample. |
| No report, run marked aborted, `result_not_found` | The service never got the notification for the experiment or could not find the result folder or file. Same causes as for gold standard runs. |
| `no annotation files` in preflight | `annotations\` was not in the installer payload, or `paths.annotations_dir` names an existing but empty folder. |
| Canary says the builds differ | The two runs used different `pipeline_build` values. Re-run with the same build, or pass `--allow-different-builds` knowing it proves nothing about determinism. |

Authoring or changing an annotation: `python -m src.annotation.validate <summary.csv>
--experiment-id <id>` prints which rule matched every sample and lists any that matched
none or several. Annotation changes go through a pull request and a new
`annotation_version`; a change in sample naming means a new experiment id.
