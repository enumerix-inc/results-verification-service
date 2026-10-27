# ADR-021: Comparison registry extension — sample vs aggregate scope

## Status
Proposed — rough shaping discussion, not required for current OKR scope. Captures direction for the comparator to grow into; no implementation planned until after the data team's philosophy discussion on linkage criteria.

## Context

ADR-016 established the comparator as a strategy-pattern registry: the manifest declares a `comparisons` list per experiment, each entry names a `type`, and the registry maps that type string to a handler function. Only `count_tolerance` is implemented — a per-sample, per-column percent-deviation check.

While shaping gold standards for the current build (linkage and dynamic range experiments specifically), a pattern emerged in how future comparison types differ from `count_tolerance`, beyond just "different math":

- **Linkage** (`CountableLinkageSummary_beta.csv`) is still a sample-vs-its-own-gold-standard-row comparison — same shape as `count_tolerance` — but needs a hybrid tolerance rule: percent deviation above a count floor, absolute deviation below it (low-count dye combinations make percent deviation noisy/meaningless near zero).
- **Dynamic range** (from `CountableDataSummary.csv`) is not a per-sample comparison at all. It's a property of the whole sample set for an experiment — linearity (R²), %CV across samples — and can't be evaluated one sample at a time.
- **Grouped samples** — a third case identified in this discussion: workbooks already carry a `GroupName` column (currently unused by the comparator). Some future features may need a comparison computed per group of samples within an experiment (e.g. replicate groups, dilution series) rather than per individual sample or across the whole experiment.

The dynamic-range case and the grouped-samples case turn out to be the same shape at different granularity: "aggregate some subset of samples and compute a property of that subset." Whole-experiment trend metrics are the degenerate case where the subset is "all samples in the experiment."

This raised the underlying design question: how should the comparator be structured so that reusable comparison *shapes* stay data-driven (manifest parameters), while feature-specific comparison *algorithms* stay code, without every new feature requiring a redesign?

## Decision

Extend the comparison registry (ADR-016) with two scopes rather than one implicit scope:

```python
COMPARISON_REGISTRY = {
    "count_tolerance":  {"fn": run_count_tolerance,  "scope": "sample"},
    "hybrid_tolerance": {"fn": run_hybrid_tolerance, "scope": "sample"},
    "curve_fit":        {"fn": run_curve_fit,         "scope": "aggregate"},
    "cv_check":         {"fn": run_cv_check,          "scope": "aggregate"},
}
```

**`sample` scope** (existing behaviour, unchanged): one result row per (sample, metric). The strategy receives `actual_samples`, `gs_samples`, and `params`, and returns one dict per sample per declared column — exactly what `run_count_tolerance` does today.

**`aggregate` scope** (new): one result row per (group, metric), where "group" is defined by an optional `group_by` param naming a column to group samples on (e.g. `"GroupName"`). When `group_by` is omitted, all samples in the experiment form a single implicit group — this is how whole-experiment trend metrics (dynamic range's linearity, %CV) are expressed, as a special case of the same mechanism rather than a third scope. An aggregate strategy receives the full sample set (already grouped) and `params`, and returns one dict per group per metric — group key, computed value(s), criteria, verdict.

This keeps three things separate cleanly:
1. What shape of comparison this is (sample vs aggregate) — a registry-level property
2. What algorithm computes it (percent tolerance, hybrid tolerance, curve fit, %CV) — a strategy function
3. What the specific numbers/columns/grouping are for a given experiment — manifest params

A **new generic sample-scope strategy, `hybrid_tolerance`**, generalizes `count_tolerance` to cover the linkage low-count problem (and any future feature with the same shape): percent deviation above a count threshold, absolute deviation below it, both configured via manifest params — no feature-specific code needed for this variant.

## Reasoning

- **Params vs code is decided by whether the "recipe" is a threshold or an algorithm.** Percent tolerance, absolute tolerance, and the hybrid of the two are all just numbers — a generic strategy parameterized by the manifest handles all of them without new code. A curve fit or a %CV calculation is a real algorithm — it has to be code, but it still fits the same registry dispatch mechanism.
- **Grouping and whole-experiment trends are the same mechanism at different granularity**, not two separate concepts. Treating "no grouping" as the degenerate single-group case avoids a three-way branch (sample / group / experiment) in the orchestrator and reporter — there are only ever two result shapes to route: per-sample and per-aggregate.
- **`sample_results` should not absorb aggregate-scope results.** ADR-011's amendment normalized `sample_results` specifically to fix the problem of one row meaning more than one thing. Storing a %CV value under a synthetic sample_id would reintroduce exactly that problem. A sibling table keeps both meanings intact.
- **The registry already does the hard part.** No change is needed to how the comparator dispatches — `experiment["comparisons"]` → registry lookup → call handler — only to what metadata the registry carries about each handler and how the orchestrator routes the result.

## Anticipated schema change (not applied yet)

A new table parallel to `sample_results`, scoped to the group rather than the sample:

```sql
CREATE TABLE IF NOT EXISTS aggregate_results (
    aggregate_result_id   TEXT    PRIMARY KEY,
    result_id             TEXT    NOT NULL REFERENCES experiment_results (result_id),
    group_key              TEXT    NOT NULL,   -- value of group_by column, or '__ALL__' when ungrouped
    metric                  TEXT    NOT NULL,
    comparison_type          TEXT    NOT NULL,
    actual_value              REAL,
    expected_value             REAL,
    deviation_percent           REAL,
    verdict                      TEXT    NOT NULL CHECK (verdict IN ('pass', 'fail')),
    notes                         TEXT    DEFAULT NULL
);
```

Reports need no structural change — aggregate-scope results are summary-report rows by definition (ADR-017's summary report is already melted on `comparison_type`/`metric`, not tied to per-sample detail). They simply have no corresponding detail-report rows underneath them, which the existing report structure already tolerates for aborted/no-gold-standard cases.

## What this does NOT change

`count_tolerance` is untouched. The current build's cell-by-cell percent-deviation comparison ships exactly as it is today — this ADR doesn't block or delay it. The one near-term gap worth noting separately: the current strategy only supports percent deviation, not a standalone absolute-difference mode, which came up as a real near-term need (see STATUS.md).

## Consequences

- Registry entries become `{fn, scope}` dicts instead of bare functions — a small, additive change to `registry.py` and the comparator's dispatch loop
- The orchestrator/reporter need a branch on scope when persisting results (`sample_results` vs `aggregate_results`) and when assembling reports
- A new `aggregate_results` table and its insert function are required before any aggregate-scope strategy can be implemented — schema migration, not yet applied
- `group_by` becomes a new optional manifest field on aggregate-scope `comparisons` entries — additive, no existing manifests affected
- Nothing here is required for the current quarter's ship — it's a shape to grow into once linkage and dynamic range criteria are actually defined with the data team

## Alternatives considered

- **Three explicit scopes (sample / group / experiment)** — rejected; whole-experiment is just the single-group case of group-scoped aggregation, and collapsing them avoids a redundant branch throughout the orchestrator, storage, and reporter.
- **Store aggregate results in `sample_results` with a sentinel `sample_id`** — rejected; reintroduces the mixed-meaning-per-row problem ADR-011's amendment was written to eliminate.
- **Fully config-driven comparison specs (no new code ever)** — rejected as a general solution; works for the sample-scope, threshold-shaped strategies (which is why `hybrid_tolerance` is proposed as config-parameterized), but aggregate-scope strategies like curve fitting require real algorithms that can't be expressed as thresholds in a config file.

## Related
- ADR-016 (comparison registry) — this extends rather than replaces the registry pattern
- ADR-011 (JSON blob metrics, amendment) — precedent for why per-row meaning shouldn't be mixed
- ADR-017 (report structure) — summary report already accommodates comparison-type-level rows without per-sample detail

## Amendment — hybrid_tolerance implemented ahead of the rest of this ADR

**Date:** 2026-09-24

The sample-scope `hybrid_tolerance` strategy proposed above was implemented
immediately (session 12) — it needed no schema change, no registry
restructuring, and directly served a near-term need (cell-by-cell percent
*or* absolute difference thresholding, per current sprint requirements).
See `src/comparator/strategies/hybrid_tolerance.py` and STATUS.md.

Everything else in this ADR — the `scope` metadata on registry entries,
the `aggregate_results` table, and any aggregate-scope strategy
(`curve_fit`, group-by comparisons) — remains unimplemented and still
deferred until after the data team's philosophy discussion. This ADR's
overall status is unchanged; only the one piece that turned out to be
separable and immediately useful was pulled forward.

## Amendment — aggregate scope pulled forward for TES-2869

**Date:** 2026-10-27

The aggregate scope in this ADR is being implemented for the acceptance criteria
regression test (TES-2869); see ADR-026. One idea here is superseded: grouping by the
workbook's `GroupName` column. ADR-026 uses a sidecar annotation file with pattern
rules over `SampleName` instead, so no pipeline input is modified.
