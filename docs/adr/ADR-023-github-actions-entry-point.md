# ADR-023: GitHub Actions as the manual regression-run entry point

## Status
Accepted

## Context
ADR-015 deliberately kept the verification service decoupled from
whatever initiates a run: it is always-on and manifest-driven, and
doesn't care who wrote the manifest or started the harness. Until now,
"whatever initiates a run" has meant a developer manually creating a
context manifest and running the test harness by hand.

With ADR-022 landed (explicit manifest hand-off, robust run correlation)
and the service now running reliably end-to-end against real harness
runs, the next gap is that manual step itself: someone has to remember
the manifest schema, hand-write JSON, and separately kick off the
harness process. This is error-prone (a typo'd `experiment_id` has
already caused a silent blank-report bug — STATUS.md session 8) and
has no audit trail of who ran what, when, against which build.

The regression machine itself is a fixed, dedicated piece of hardware
(it needs to be — it drives real or simulated instrument hardware and
talks to a specific pipeline install) that isn't part of any build
process. The pipeline build, its installation onto that machine, and
the regression run against it are three genuinely separate steps done
by a person, in that order — automating "build" and "install" together
with "run" is out of scope here; see the "What this doesn't do" section.

## Decision
Add a `workflow_dispatch`-triggered GitHub Actions workflow
(`.github/workflows/trigger_regression_run.yml`) that runs on a
self-hosted runner registered on the regression machine itself
(labels `[self-hosted, pcr-regression]`). Dispatching it:

1. Runs `entry_point/stamp_manifest_for_run.py`, which takes the
   workflow's inputs (`pipeline_build`, `scenario`, `run_type`) and a
   committed `entry_point/manifest_template.json` (the stable
   `build_verdict_policy` + `experiments` blocks) and writes a new,
   fully-formed `context_manifest_{run_id}.json` into the live
   `manifests_dir` — the same file a developer would previously have
   hand-written.
2. Runs the already-built, already-installed `Countable.Pcr.TestHarness.exe`
   with `COUNTABLE_PCR_REGRESSION_TEST_MANIFEST_PATH` pointing at that
   same `manifests_dir`, and waits for it to exit.

The already-running verification service picks up the new manifest and
the harness's resulting NOTIFY events exactly as it would if a human had
done both steps by hand — nothing about ADR-022's design changes. This
workflow is purely a typed, audited, one-click replacement for the manual
manifest-writing step, plus scripting the "now start the harness" step
that was previously a manual command too.

### Trigger type: `workflow_dispatch`
Chosen over `repository_dispatch` or `schedule` because a regression run
can only meaningfully happen after a human has manually built and
installed a specific pipeline version on the regression machine — there
is no event to trigger off of, and no fixed cadence a schedule could
follow, since "the build changed" isn't itself a signal this workflow
(or anything) currently observes. `workflow_dispatch` gives a typed input
form in the GitHub UI (a text field for `pipeline_build`, a dropdown for
`run_type`) for exactly this "a person decides now is the time" case.

### Scope: fire-and-forget, ending at the harness process exiting
The job waits for `Countable.Pcr.TestHarness.exe` to exit (it has to —
the job runs on the one machine that can run it), but does **not** poll
the verification service or its SQLite database for a verdict. Once the
harness exits, the job succeeds and finishes; verification happens
asynchronously on whatever timeline the E: drive watch and NOTIFY stream
actually deliver on (minutes, driven by real or simulated pipeline
processing time). Making the GitHub Actions job itself wait for that
would mean either polling a database from inside the job or building a
notification channel back into GitHub Actions — real future work
(ADR-018's pipeline health direction gestures at this), but unnecessary
complexity for what this ADR is trying to fix today: eliminating manual,
error-prone manifest authoring.

### The stamping script, not inline YAML
`stamp_manifest_for_run.py` is a plain Python script, not inline
PowerShell in the workflow, because manifest construction is exactly the
kind of logic (JSON structure, a safety check, a stateful sequence
number) that's painful and easy to get subtly wrong in YAML/PowerShell
string-templating, and because it needs to run identically whether it's
dispatched by GitHub Actions or invoked by hand locally for a quick
manual test — the script takes no GitHub-specific input beyond an
optional `--triggered-by` override; the workflow's `GITHUB_ACTOR`/
`GITHUB_RUN_ID` env vars are picked up automatically if present, and
`GITHUB_OUTPUT` is written to only when running under Actions.

### `manifest_template.json`, not a fully-generated manifest
The three registered experiments (`T078_run3`, `MP47b_Adverum_02`,
`JS221N_serial_titration_PSF`), their `comparisons`, and the
`build_verdict_policy` almost never change run to run — only the `run`
block does. Committing the stable parts as a template and stamping only
the `run` block:
- keeps the experiment set under version control and code review, same
  as any other part of the contract (ADR-001)
- means a manifest is never hand-typed from scratch, eliminating the
  session-8-class typo bug at the source
- keeps the stamping script itself trivial — it does not need any
  knowledge of what a valid `comparisons` entry looks like

Adding, removing, or reconfiguring an experiment is still a deliberate,
reviewed edit to `manifest_template.json` — this is intentional; it is
the "who owns adding a new experiment_id" open question from STATUS.md,
and a template file people actually read is a better home for that than
generating it from some other source of truth that doesn't exist yet.

### run_id sequencing
`stamp_manifest_for_run.py` computes the next `run_YYYYMMDD_NNN` by
scanning `manifests_dir` **and** its `processed/`, `timed_out/`, and
`failed/` subfolders for today's date and taking the highest existing
sequence number plus one. Archived manifests keep their original
filename (`main.py`'s `_archive()` only moves the file, never renames
it), so this is a complete history of every run_id ever issued today,
not just the ones still pending. NNN only ever increases for a given
day; a discarded/failed run's number is never reused.

### Safety check: refuse to stamp while an unarchived manifest exists
Before writing anything, the script glob-checks `manifests_dir`'s top
level (non-recursively, so the archive subfolders are correctly
excluded) for any `context_manifest_run_*.json` still sitting there
unarchived, and refuses to proceed if one is found — printing which
file(s) and why this usually happens. This is the direct analogue of
`main.py`'s own collision handling (ADR-022 addendum): two runs with
overlapping pending experiments would otherwise silently corrupt
`expected_experiment_to_run`. Catching it here, before the harness even
starts, is cheaper than catching it in the running service after a
human has already walked away.

### Self-hosted runner, `clean: false`
The runner has to be the regression machine itself — it is the only
thing with the installed pipeline, the real or simulated instrument
hardware, and the E:/F: drives. Unlike a GitHub-hosted runner, this
workspace is **not** a fresh VM per job: it persists between dispatches.
`actions/checkout`'s default `clean: true` runs `git clean -ffdx`, which
removes ignored files too — that would delete `config/local_config.yaml`
(this machine's real paths, and previously its pipeline DB credentials)
and `.venv` on every single run. The checkout step explicitly sets
`clean: false` to prevent that.

## What this doesn't do
- **Does not build or install anything.** `pipeline_build` is a free-text
  label the person dispatching the workflow types in, describing
  whatever they already installed by hand. The workflow does not check
  it against what's actually running on the machine — a wrong value
  here produces a manifest with a misleading `pipeline_build` field but
  otherwise runs normally. Automating build → install is a separate,
  larger problem (it touches code-signing, MSI installation, and
  possibly the pipeline's own service lifecycle) explicitly deferred.
- **Does not modify the test harness.** `Countable.Pcr.TestHarness.exe`
  is invoked exactly as it already runs today — this includes its
  existing limitations (whole-folder manifest glob without archiving —
  tracked in STATUS.md — and an unhandled-exception path that blocks on
  `Console.Read()`, which is why the harness step has a 3-hour
  `timeout-minutes` cap rather than running unbounded). Harness changes
  are explicitly out of scope for this pass, per the same sequencing
  decision that put this ADR before them.
- **Does not wait for or report a verdict.** See "Scope" above.

## Consequences
- A new `entry_point/` directory (`manifest_template.json`,
  `stamp_manifest_for_run.py`) and `.github/workflows/trigger_regression_run.yml`
  are added to this repo.
- `requirements.txt` gained an explicit `pyyaml` dependency — both
  `src/main.py` and the new stamping script import `yaml`, but it was
  previously undeclared (present in the working venv incidentally, not
  by requirements.txt). The stamping script deliberately does not import
  `src.main.load_config()` to avoid pulling in `asyncpg`/`watchdog` just
  to read a path out of YAML; it carries its own minimal copy of that
  function instead, at the cost of the two needing to be kept in sync if
  the config file format ever changes.
- Two GitHub Actions repository variables need to be set once, outside
  this repo's version control, for the workflow to run:
  `PCR_MANIFESTS_DIR` (must match `config.paths.manifests_dir`) and
  `PCR_TEST_HARNESS_EXE_PATH` (wherever the built harness executable
  actually lives on the regression machine).
- Triggering a run now requires a self-hosted runner to be registered
  and online on the regression machine — a one-time setup step, not yet
  performed as of this ADR.

## Alternatives considered
- **`repository_dispatch`**, triggered by the end of the pipeline's own
  build workflow — rejected because a successful build does not mean the
  build has been installed on the regression machine yet; that step is
  still manual and there's no event to hook when it's done.
- **A scheduled (`schedule`) nightly run** against whatever happens to be
  installed — rejected for the same reason: there's no guarantee a new
  build is installed on any particular night, and running against an
  unchanged install repeatedly adds no information.
- **Generating the manifest programmatically from the experiment
  registry in the verification database**, rather than a committed
  template file — appealing in principle (single source of truth), but
  the database's `gold_standard_samples`/`gold_standard_versions` tables
  don't currently carry `comparisons` configuration at all (that lives
  only in the manifest today); building that out is real scope beyond
  "add a trigger" and deferred.
- **Having the job poll the SQLite verification DB or watch for a report
  file before finishing** — rejected per "Scope" above; deferred until
  there's an actual need to gate something (e.g. a downstream deployment
  step) on the verdict.

## Addendum (session 15, GitHub Copilot PR review)

A round of PR review on this ADR's implementation surfaced three issues,
all fixed the same session:

- **PowerShell command injection via dispatch inputs.** The "Stamp a new
  run manifest" and "Summary" steps interpolated `${{ inputs.pipeline_build }}`
  etc. directly into their `run:` scripts. A `${{ }}` expression is
  substituted into a step's script as raw text *before* PowerShell parses
  it — so a dispatch input containing a quote and a statement terminator
  (e.g. `foo"; Remove-Item -Recurse C:\; #`) could break out of the
  quoted argument and run arbitrary commands on the self-hosted runner.
  Fixed by exposing the three inputs as job-level `env:` variables
  (`PIPELINE_BUILD`, `SCENARIO`, `RUN_TYPE`) and reading them via
  `$env:...` inside both scripts instead — an actual environment variable
  read at runtime, not a text splice into the script source. Since
  `workflow_dispatch` can be triggered by anyone with write access to the
  repo (not just the person who wrote this workflow), this was a real,
  not theoretical, risk on a runner that also has the credentials in
  `config/local_config.yaml`.
- **The stamping script's collision check was narrower than what the
  live service actually watches.** `stamp_manifest_for_run.py`'s safety
  check globbed `context_manifest_run_*.json`, but `manifest_watcher.py`
  registers *any* `context_manifest_*.json` it finds — the prefix/suffix
  match, not a "contains `run_`" match. A stale or hand-dropped file like
  `context_manifest_old.json` would sail past the stamping script's check
  (allowing a new manifest to be stamped) while still being exactly the
  kind of unarchived file the safety check exists to catch, defeating it.
  Fixed by using the same non-recursive `context_manifest_*.json` glob as
  `manifest_watcher.py`'s `MANIFEST_PREFIX`/`MANIFEST_SUFFIX`. Verified
  against a scratch manifests_dir with exactly this file name.
- **The job summary always printed success text.** The "Summary" step
  runs `if: always()` so it still executes after an earlier step fails,
  but it unconditionally wrote "Regression run triggered" / "The harness
  process has exited" regardless of what actually happened — including
  when stamping failed (no manifest, harness never started) or the
  harness step failed or hit its timeout. That is exactly backwards for
  what a job summary is for: it needs to be most informative during the
  failure modes an operator is checking it for. Fixed by giving the
  harness step an `id` and branching the summary on `steps.stamp.outcome`
  / `steps.harness.outcome`, with distinct failure-specific text for
  "stamping failed" vs. "harness didn't complete successfully" vs. the
  original success case.

## Relationship to other ADRs
- Fulfils the future automation ADR-015 explicitly designed for: "the
  test generator is purely additive... no verification service code
  changes are required." This workflow *is* (a minimal version of) that
  test generator.
- Depends on ADR-022's explicit manifest hand-off — a workflow that
  writes a manifest and then fires the harness would not have been safe
  to build against the old passive-listener design, since there would
  have been no clean way to detect the "already unarchived manifest
  pending" collision case this ADR's safety check relies on.
- The stable `experiments`/`build_verdict_policy` template is the same
  manifest schema documented in ADR-001 and `docs/manifest-schema.md` —
  no schema changes were needed for this ADR.

## Amendment — acceptance criteria experiment (TES-2869)

**Date:** 2026-10-28

The `template` dispatch input gains a third choice, `acceptance`, which stamps only
`manifest_template_acceptance.json` (the MP37 acceptance criteria experiment, ADR-026),
and a boolean `include_acceptance` that adds that experiment to whichever template was
chosen, so one run (and one manifest) can cover both gold standard and acceptance
experiments. The stamping script gains a repeatable `--also-template PATH` to merge
experiments from several templates (ids must be unique; the verdict policy comes from
`--template`). The default `manifest_template.json` is unchanged, and a dispatch that
doesn't ask for acceptance stamps exactly what it did before.

Stamping now validates every `acceptance_criteria` entry before writing anything (shape
via `acceptance_params`, the annotation file is found and loads, the channels match
the annotation) and refuses to stamp otherwise, so a typo fails in seconds instead of
after hours of reanalysis. No `evaluators` input is needed: the experiments' own
`comparisons` select what runs.
