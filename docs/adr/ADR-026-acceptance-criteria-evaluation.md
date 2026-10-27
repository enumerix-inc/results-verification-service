# ADR-026: Acceptance criteria evaluation as aggregate-scope comparisons with sidecar sample annotations

**Status:** Accepted (2026-10-27, TES-2869)

## Context

TES-2869 asks for a regression workflow that judges release readiness against
absolute acceptance criteria (DH062, Table 2, Test Case 1.2) instead of against a
registered gold standard. The first delivery covers the 4-plex UM series of
`MP37_V_V_Testcase_2_beta05`, evaluated per channel (Ch1-Ch4):

| Metric | Criterion |
|---|---|
| Linearity exponent n, expected 1k-100k | 0.9 < n < 1.1 |
| Linear slope s, expected 1k-100k | 0.7 < s < 1.2 |
| Plateauing ratio PR at expected 1M | -20% <= PR <= 20% |
| CV at 1k, 10k, 100k, 1M | each < 10% |
| NTC counts (mean of the replicates) | < 50 |

Facts that shape the design:

- These are experiment-level properties of groups of samples (replicates at an
  expected count), not per-sample checks. This is the aggregate scope described in
  ADR-021.
- A gold standard comparison asks "did behaviour change?"; an acceptance check asks
  "does this build meet spec?" The acceptance check needs no baseline.
- A person reviews the report and the data and makes the release decision. There is
  no automated release gate for now.
- Counts depend on the experiment's training sample, which is prepared with the other
  samples and applied to every sample in that experiment. Experiments must therefore
  be replayed whole; samples cannot be re-packed across experiments.
- From pipeline v3.0 analysis is deterministic: the same images through the same IAP
  version produce the same counts.

## Decision

1. **Aggregate-scope comparisons in the existing registry.** Acceptance evaluation is
   implemented as new aggregate-scope strategies in `COMPARISON_REGISTRY`
   (ADR-016), pulling forward the `scope` metadata and the `aggregate_results` table
   proposed in ADR-021. It is not a parallel evaluator system. The gold standard
   comparator is unchanged and its further development is on the back burner.
2. **Experiments are replayed whole and untouched.** Regrouping happens on pipeline
   outputs, never on pipeline inputs. The service never edits a workbook, including
   its `GroupName` column.
3. **Sidecar sample annotations.** What each sample is lives in a per-experiment
   annotation file (YAML), keyed by `experiment_id` and referenced from the
   experiment's entry in the run context manifest. It holds ordered pattern rules
   over `SampleName` that yield a group, a role, and expected counts per channel.
   - Validation is strict: a sample matched by no rule, or by more than one, fails the
     run with a clear reason.
   - An assay-less channel is `null`; a no-template control is an expected count of 0.
     The two are never conflated.
   - The file carries an `annotation_version`. Each result records the file's SHA-256
     and version, the same audit idea the registrar uses for gold standards. Git
     history is the change log. A change in sample naming is a new experiment id, not
     an edit.
   - The annotation is loaded by the verification service each time the experiment is
     verified. The harness never touches it.
   - It is not called a "manifest", to avoid confusion with the run context manifest.
4. **Criteria are comparison parameters.** Limits, bound inclusivity, and the
   per-channel scope are `params` on the experiment's `comparisons` entry in the run
   context manifest, which is stamped per run. The stamped manifest is the record of
   what each run was judged against.
5. **Calculation primitives are small, pure, tested functions.** New math means a new
   tested function; criteria never contain scripts. Definitions (from DH062 and the
   v2.1 performance metrics workbook):
   - counts for replicate samples are averaged per level and channel
   - n = slope of the regression of log10(mean counts) on log10(expected counts), 1k-100k
   - s = 10^mean(log10(counts) - log10(expected counts)), 1k-100k
   - PR = (s * expected - counts) / (s * expected) at 1M (positive = below the linear prediction)
   - CV = sample standard deviation / mean across the replicates at a level
   - NTC = mean of the NTC replicates
   The UMA linearity workbook is the golden test fixture. Its data matches the MP37
   CountableDataSummary.
6. **The gate requires a gold standard only for sample-scope comparisons.** An
   experiment whose comparisons are all aggregate-scope needs no registered gold
   standard. `experiment_results.gs_exp_version_id` is already nullable.
7. **Input is the CountableDataSummary CSV only.** It is the existing wide format
   (four preamble rows, one row per sample, `UM-01_CountsPer50ul` ... columns, named after the dye from software 3.0 onwards; earlier outputs used `Ch01_...`). Channels (`Ch01`...) are logical names in the annotation and criteria; the manifest `columns` map translates them to whatever the CSV calls them (dye names differ by software version and assay, e.g. TaqMan), already
   read by the registrar. RnDdata (long form, inside the archived metadata) and the
   linkage summary are out of scope for now. The reader stays behind an interface so
   other pipeline outputs can be added later.
8. **Storage.** Aggregate results go in `aggregate_results` (ADR-021's schema,
   extended with the criterion bounds, bound inclusivity, margin to limit, channel,
   and annotation hash and version). The pipeline rows the evaluation used
   (run, experiment, sample, channel, count) are stored too, so pipeline outputs and
   regression-service outputs sit side by side.
9. **Reporting is reviewer-oriented.** The summary lists each criterion per channel
   with value, limits, pass/fail, margin to limit, and provenance (experiment, IAP
   build, annotation version, criteria). It includes a clearly labelled advisory
   "all criteria met" line. Raw tabular data is written alongside as CSV so every
   number can be traced. The verdict is computed and recorded, but the release
   decision is made by a person.

## Consequences

- A manifest can mix gold standard and acceptance experiments. Each experiment carries
  its own `comparisons`, and each produces its own result and report. No separate
  `evaluators` input is needed.
- `registry.py` entries change from bare functions to `{fn, scope}` (ADR-021). The
  comparator dispatch, orchestrator persistence, and reporter gain a scope branch.
- A schema migration adds `aggregate_results` and the observation storage. The schema
  file has no migration mechanism yet.
- The annotation validator is the main authoring tool. A command that loads a past
  run's CSV and prints which rule matched each sample makes writing a new annotation
  file quick.
- Determinism lets a failing criterion be treated as a real failure and makes
  reanalysis results cacheable by (experiment, IAP build). A cheap determinism check
  (run the same build twice and compare) guards the assumption.

## Alternatives considered

- **Sample catalog with an assembler that re-packs samples into new experiments.**
  Rejected. The training sample belongs to its experiment, so counts from a re-packed
  experiment are not comparable to the V&V results.
- **`group_by: GroupName` (ADR-021's original idea).** Rejected for this work. It
  requires modifying a pipeline input, and a single group name per sample cannot carry
  per-channel expected counts or a sample's membership in several analyses.
- **Pattern rules inline in the run context manifest.** Rejected. They describe the
  experiment, not the run, and would be repeated in every stamped manifest.
- **A separate acceptance evaluator service or an `evaluators` dispatch input.**
  Rejected. The comparison registry already provides per-experiment selection.
- **LLM inference of annotations at run time.** Deferred. Drafting annotation rules
  with an LLM for human approval is acceptable later; run-time inference is not.

## Open items

- Where annotation files are stored on an installed machine (a `paths` entry alongside
  `gold_standards_dir`, with tracked copies in the repo).
- Whether QA wants sign-off on the metric calculation code (deferred; skeleton first).
- An imaging-system attribute is not derivable from sample names. Add it only if a
  source is identified.
- Release-gate policy beyond human review (deferred).

## Related

ADR-001 (manifest as contract), ADR-002 (one experiment per feature), ADR-003
(classification), ADR-005 (pre-verification gate), ADR-008 (gold standard versioning),
ADR-016 (comparison registry), ADR-017 (report structure), ADR-021 (scope and
aggregate strategies; this ADR implements its aggregate scope and supersedes its
`group_by: GroupName` idea), ADR-022 (explicit manifest handoff).
