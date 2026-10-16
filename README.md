# Results Verification Service

Automated regression testing system for the Countable PCR image analysis pipeline.

## Overview

The verification service runs on a dedicated lab machine. It polls the pipeline database for completed test runs, verifies results against registered gold standards, and generates reports.

See `docs/architecture.md` for the full system design.

## Project structure

```
results-verification-service/
├── docs/               Architecture, schema reference, ADRs, glossary
├── schema/             SQLite DDL
├── config/             Configuration (paths, polling interval, tolerances)
├── entry_point/        Manifest template + stamping script for the
│                       GitHub Actions trigger (ADR-023) — see below
├── .github/workflows/  trigger_regression_run.yml — manual workflow_dispatch
│                       entry point, runs on the regression machine itself
├── src/
│   ├── main.py         Service entry point (the always-on listener/orchestrator)
│   ├── registration/   Gold standard registration tool
│   ├── listener/       Listens for pipeline DB NOTIFY events
│   ├── gate/           Pre-verification integrity checks
│   ├── orchestrator/   Coordinates the verification flow
│   ├── comparator/     Per-sample comparison logic
│   ├── llm/            LLM API calls and prompt management
│   ├── reporter/       Assembles and writes reports
│   └── database/       All database interaction
└── tests/              Unit tests
```

## Triggering a regression run

Runs are started manually with the "Trigger Regression Run" GitHub Actions
workflow (`workflow_dispatch`), after a build has been produced and
installed on the regression machine by hand — this workflow does not
build or install anything itself. Dispatching it stamps a new manifest
from `entry_point/manifest_template.json` and starts the test harness;
it does not wait for verification to complete. See ADR-023 for the full
design and its `entry_point/stamp_manifest_for_run.py` --help for the
same script run locally/manually.

Requires a self-hosted GitHub Actions runner registered on the regression
machine (labels `self-hosted, pcr-regression`) and two repository
variables set (Settings → Secrets and variables → Actions → Variables):
`PCR_MANIFESTS_DIR` (matching `config.paths.manifests_dir` on that
machine) and `PCR_TEST_HARNESS_EXE_PATH` (where the built test harness
executable lives there).

## Data directory

Runtime data lives outside the project on the regression machine:

```
C:/RegressionTesting/
├── manifests/          Run context manifests (written by test harness)
├── gold_standards/     Gold standard CSVs
├── workbooks/          Experiment workbooks
└── verification.db     SQLite database
```

Paths are configured in `config/config.yaml`.

## Credentials and local configuration

`config/config.yaml` is tracked and holds defaults only. Its `database_connection` is a placeholder.
Put real credentials and machine-specific paths in `config/local_config.yaml`, which is gitignored and
loaded instead of `config.yaml` when it exists. Never commit real passwords. The planned installer will
generate this per-machine file outside the program folder.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create the data directory and initialise the database:
   ```
   python src/main.py --init
   ```

3. Register gold standards:
   ```
   python src/main.py --register
   ```

4. Start the service (for interactive/manual use — for always-on
   background operation, see "Running as a Windows Service" below):
   ```
   python src/main.py --run
   ```

## Running as a Windows Service

For the service to actually pick up runs triggered by the GitHub Actions
workflow (see "Triggering a regression run" above), it needs to be
running continuously in the background — not dependent on someone having
a terminal open. This is wrapped as a Windows Service using
[NSSM](https://nssm.cc) rather than a native Python service; see ADR-024
for the full reasoning.

One-time setup on the regression machine, as Administrator:

1. Install NSSM (download from https://nssm.cc and either put `nssm.exe`
   on PATH or note its path — this is a new tool for this machine, not
   already present, since every other service here is a native .NET
   service).
2. Complete the "Setup" steps above (venv, `--init`, `--register`) if you
   haven't already.
3. Run:
   ```
   powershell -File scripts\install_verification_service.ps1
   ```
   (add `-NssmPath "C:\path\to\nssm.exe"` if it isn't on PATH, and
   `-Start` to start it immediately).

The script is idempotent — re-run it any time to reconfigure the service
(e.g. after changing the stop timeout). To remove it entirely:
```
powershell -File scripts\uninstall_verification_service.ps1
```

Logs land in `logs\service_stdout.log` / `logs\service_stderr.log`
under the repo root (rotated at 10 MB). Manage the service with
`Get-Service`, `Start-Service`, `Stop-Service`, or `services.msc`.

**Note:** these scripts were written and reviewed carefully but have not
been run against the real regression machine yet — verify them there
before relying on this for an actual triggered run.

## Dependencies

See `requirements.txt`.
