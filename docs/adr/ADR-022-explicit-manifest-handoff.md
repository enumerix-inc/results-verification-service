# ADR-022: Explicit manifest hand-off replaces run_id stamping for run correlation

## Status
Accepted

## Context
ADR-012 documented the `reports_table_changes` NOTIFY payload as carrying
an `experimentId` field in the format `{exp_id}_{run_id}_{timestamp}`.
ADR-014 documented the mechanism behind that: the test harness would stamp
`run_id` into the experiment folder name at runtime, as the first step of
simulating image acquisition, and this stamped name would propagate through
the pipeline (IAP) into the `Reports` table and its NOTIFY payload.
`parse_experiment_notification()` extracted `run_id` from that string via
regex, and the orchestrator used `{exp_id}_{run_id}_*` to locate result
folders on disk.

In session 14, a full weekend Reanalysis-mode run — three experiments, all
pipeline writes and the `Reports` trigger confirmed healthy — produced zero
verification output, with no error anywhere. Manually replaying a
realistic NOTIFY payload against the running service reproduced this
silently: `parse_experiment_notification()` received a bare `experimentId`
(e.g. `"MP47b_Adverum_02"`, no `_run_` suffix of any kind), its regex never
matched, it returned `None`, and `handle_notification()` returned without
logging anything.

Investigating the harness codebase (`Countable.PCR`) directly found no code
path — Imaging or Reanalysis — that performs the stamping ADR-014
describes. Both `AcquisitionWorkflow.ExperimentName` and
`ReanalysisWorkflow.ExperimentName` derive the experiment name directly
from the workbook's own `RunInfo.ExperimentId`, unmodified, and that bare
name is what reaches `IapAnalyzer.AddWorkbook()` and, from there, the
`Reports` table. It is unclear whether this stamping step was removed at
some point or never actually implemented — either way, the payload
contract ADR-012/ADR-014 documented has never matched what the pipeline
actually sends, in either run mode.

A second, related gap found during the same investigation: the harness's
`LoadManifests()` globs every `context_manifest_*.json` file in
`manifests_dir` on every invocation and never archives one once its
experiments finish, so manifests accumulate indefinitely and are
reprocessed on every future run.

## Decision
Stop relying on `run_id` being encoded in any pipeline-supplied string.
The verification service now explicitly watches `manifests_dir` for new
manifest files (`src/orchestrator/manifest_watcher.py`, using the same
observer-first pattern as ADR-019's E: drive watch) and registers every
`experiment_id` a manifest declares as "expected" — building
`expected_experiment_to_run: {experiment_id: run_id}` — the moment the
file appears, before any pipeline notification for it has necessarily
arrived. A notification's bare `experiment_id` is now resolved with a
plain dictionary lookup against this map. `parse_experiment_notification()`
and its regex are removed entirely; `listener.py`'s only remaining job is
delivering a raw NOTIFY payload.

Once a run either completes verification or times out waiting on the E:
drive watch, its manifest is moved out of `manifests_dir` into a
`processed/` or `timed_out/` subfolder, so the folder stops accumulating
and a human can see at a glance which outcome a given run had.

This makes the manifest — already the system's documented contract
(ADR-001) — the sole mechanism for run correlation, rather than splitting
that responsibility between the manifest and an implicit pipeline-side
naming convention that had no enforcement and, in practice, was never
honored.

## Reasoning
The previous design required a naming convention to be independently
maintained correctly across two systems this codebase does not control —
the harness and, transitively, IAP — with no way to verify the assumption
held and no visibility when it silently didn't. The new design requires
nothing of the pipeline beyond what it already sends (a bare
`experiment_id`) and nothing of the harness beyond what ADR-015 already
assumed (a manifest appearing in `manifests_dir` before or as a run
starts).

It also resolves the multi-manifest correlation question raised while
designing this fix: since each manifest explicitly declares its own
experiment set the moment it's registered, two pending manifests can never
be silently confused with each other. A collision — the same
`experiment_id` pending under two different runs — is now something the
service detects and warns about loudly, instead of a passive listener
having no way to know it was even possible.

## What this supersedes / amends
- **Supersedes ADR-014** (Experiment folder naming convention and run_id
  stamping) — the run_id-in-folder-name convention and its parsing are
  removed. No harness change is required for this: the harness's current
  bare-experiment-id behavior, which turned out to be what it already
  does in both Imaging and Reanalysis modes, is correct as-is under the
  new contract.
- **Amends ADR-012's documented payload structure** — the `experimentId`
  field is now understood to be a bare experiment_id, not
  `{exp_id}_{run_id}_{timestamp}`. ADR-012's core decision (NOTIFY/LISTEN
  over polling) is unaffected.
- **Consistent with ADR-015** (decoupled entry point) — the manifest
  remains the sole interface regardless of what produces it or when; this
  decision only changes *when* the service reads that interface
  (proactively, on file arrival, rather than reactively inferring it from
  a notification).

## Consequences
- `listener.py` no longer does any experiment_id/run_id parsing.
- `main.py`'s `run_service()` runs a second concurrent watcher
  (`watch_manifests_dir`) alongside the Postgres listener for the
  lifetime of the service.
- A notification for an experiment_id with no pending manifest now prints
  a visible warning instead of returning silently — this alone would have
  surfaced session 14's incident immediately instead of after a full lost
  weekend.
- Manifests are archived (moved to `processed/`, `timed_out/`, or
  `failed/`) once their run concludes on the verification-service side.
  The harness's own `LoadManifests()` still never archives — a related
  but separate gap, tracked as a follow-up, not fixed by this change.
- ~~An unhandled exception inside a run's verification flow can still
  crash the whole service (observed session 14) — graceful per-run error
  isolation is tracked as an open design question, not addressed here.~~
  **Resolved (session 14, PR review):** see Addendum below.
- `result_file_suffix` (a related but separate manifest field, addressing
  which output CSV to read for a given experiment type) was added the
  same session — see `manifest-schema.md`. It is out of this ADR's scope,
  since it addresses a different problem than run correlation.

## Addendum (session 14, GitHub Copilot PR review)

A round of PR review on this ADR's implementation surfaced six further
gaps, all fixed the same session:

- **`watcher.py` still keyed off the superseded `{exp_id}_{run_id}_*`
  convention.** The E: drive deletion watch (`ReportDataDeletionHandler`,
  `wait_for_e_drive_deletion`, `watch_and_confirm`) matched folders by
  `{exp_id}_{run_id}_*` — exactly the naming convention this ADR
  established never actually existed on the pipeline side. In practice
  this meant the pre-check glob almost always matched zero folders,
  treated the folder as "already absent," and returned `True` immediately
  — skipping the real wait for the F: copy to complete on nearly every
  run. Fixed by matching on the pipeline's own timestamped `name` (the
  same value `find_result_folder()` already uses on F:) instead.
- **No exception boundary around `verify_run()`.** This is the crash
  observed live during session 14. `on_confirmed()` in `main.py` now
  wraps the `asyncio.to_thread(verify_run, ...)` call in try/except: on
  failure the run's manifest is archived to a new `manifests_dir/failed/`
  subfolder and its in-memory state is freed via the existing `_archive()`
  helper, so one run's failure no longer takes down the service or blocks
  later runs.
- **`runs.manifest_path` went stale after archiving.** `verify_run()`
  computed the manifest's path from `manifests_dir` + `run_id`, but the
  file is moved into `processed/` immediately after `verify_run()`
  returns. `verify_run()` now takes an explicit `manifest_record_path`
  argument — the caller passes the path the manifest is *about* to be
  archived to, so the DB always records where the file actually ends up.
- **No readiness barrier before the listener starts.** `watch_manifests_dir()`
  now accepts an optional `ready_event`, set once its startup catch-up
  scan completes; `run_service()` awaits it before starting the NOTIFY
  listener, closing the startup race where a manifest already on disk
  could be missed by a notification (or, for the mock listener, its
  immediate callback) arriving before that manifest was registered.
- **Collision handling overwrote instead of rejecting.** `handle_new_manifest()`
  previously warned on a detected `experiment_id` collision between two
  pending manifests but still registered the new one anyway, silently
  reassigning that experiment_id to the new run. It now skips registering
  the conflicting manifest entirely, leaving the earlier run's mapping
  intact until it's resolved.
- **`manifest_watcher.py` missed atomic rename hand-offs and partial
  writes.** `ManifestCreatedHandler` now also handles `on_moved` (an
  atomic temp-file rename into `manifests_dir` fires this event, not
  `on_created`), and `handle_new_manifest()` retries the JSON read with
  backoff (5 attempts, up to ~3s total) to tolerate a file being read
  before its writer has finished flushing.

Also fixed as a consequence of the above: `scratch/smoke_test.py`'s call
to `verify_run()` updated to pass `experiment_names` and
`manifest_record_path`, matching the signature change from earlier this
session.

The `watcher.py` name-based matching fix rests on an assumption not yet
directly verified against a live E: drive folder: that the pipeline names
the E: drive folder identically to the F: result folder (both by its own
`name` field). This held for `find_result_folder()` on F: but should be
confirmed against a real E: drive folder on the next live run.


## Addendum (2026-10-13, first live run on the regression machine)

The first live harness-triggered run after installing the service under
NSSM completed on the harness side, but verification never started. The
service log showed the manifest registered correctly, then:
`Warning: notification for experiment 'MP47b_Adverum_02_261013_1053' with
no pending manifest - ignoring`.

The notification's `experimentId` now carries a trailing timestamp
(`{experiment_id}_{yymmdd}_{hhmm}`, the same shape the harness's Imaging
workflow uses for experiment folders), while the manifest declares the
bare `experiment_id` (`MP47b_Adverum_02`). `handle_notification()` did an
exact dictionary lookup, so nothing matched. This amends the payload
description above and in ADR-012, which describe a bare `experimentId`
with the timestamped identifier in `name`. That description most likely
came from payloads fired by hand from pgAdmin (the session-14 recovery
replayed NOTIFYs manually; ADR-013's mock payload is also hand-written),
not from live pipeline traffic, so the live pipeline may never have sent
a bare `experimentId`. This is unconfirmed: the older rows in the
`Reports` table (`ExperimentId`, `Date`, `Version`) would show whether the
suffix was always there. The database trigger that builds the payload
(`Triggers.sql`, unchanged since 2025-11) maps `experimentId` from
`Reports.ExperimentId` ("assigned by IAP") and `name` from `Reports.Name`.

Fix: `resolve_notification()` in `main.py`. An exact match still wins, so
the old bare-`experimentId` shape keeps working. Otherwise a trailing
`_dddddd_dddd` is stripped from `experimentId` and the lookup retried.
This is deliberately narrow (a known timestamp shape, only after the exact
match fails) and is not the removed `{exp_id}_{run_id}_*` regex.

Because `name` may now be the plain experiment name, folder lookups can no
longer assume `name` is timestamped: when a suffix had to be stripped, the
timestamped `experimentId` is passed on as the pipeline name used by
`find_result_folder()` and the E: drive watch; when the match was exact,
`name` is used as before. The unmatched-notification warning now prints the
full payload so a future format change is diagnosable from the log alone.

## Alternatives considered
- Fix `parse_experiment_notification()` to match the harness's actual
  (bare) format, and add real run_id stamping at the harness/IAP boundary
  instead — rejected as more invasive (touches IAP, a system this
  codebase doesn't own) and doesn't resolve the multi-manifest ambiguity
  question that prompted this redesign.
- Keep passive inference but disambiguate using timestamp proximity
  between a notification and pending manifests — rejected for the same
  reason ADR-014 originally rejected it: fragile, and two runs starting
  close together could produce ambiguous matches.
- Have the harness or a future entry point call the service directly (an
  API or socket) to register a run, rather than watching the filesystem —
  a heavier alternative, deferred since the filesystem-watch pattern is
  already established (ADR-019) and requires no new interface between the
  harness and the service.
