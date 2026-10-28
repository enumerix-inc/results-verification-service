# Regression Testing Service - Manual Setup Guide

Oct 20, 2026 · @Jesse

## Overview

This guide walks through every manual step needed to stand up the regression testing stack on one Windows machine, as done during Phase 0. The Inno Setup installer will automate most of it; the last section maps each manual step to what the installer takes over.

The stack has three parts:

- **Results verification service** (Python, this repo): a Windows service run through NSSM. It watches a manifests folder, listens for Postgres NOTIFY events, and verifies results against registered gold standards stored in SQLite.
- **Test harness** (`Countable.Pcr.TestHarness`, .NET, in the Countable.PCR repo): runs the experiments in a manifest. Runs are Reanalysis-only, started with `--reanalysis-only`.
- **GitHub Actions workflow plus a self-hosted runner**: the runner polls GitHub, then stamps a manifest and launches the harness on the machine.

A run flows like this:

1. A person dispatches the workflow from GitHub (inputs: pipeline build, scenario, template, timeout).
2. The runner stamps `context_manifest_<run_id>.json` into the manifests folder.
3. The service sees the manifest and registers the run, expecting one report per experiment.
4. The harness runs reanalysis for each experiment. A single experiment takes about 47 minutes; the full manifest takes hours.
5. The Report Loader inserts a row into the Reports table when an experiment finishes. A Postgres trigger fires NOTIFY, and the service receives it.
6. The service waits for the experiment folder to leave `E:\CountableLabs\ReportData`, which means the copy on F: is complete.
7. When every experiment is confirmed, the service compares results to the gold standards and writes a report.

## Before you start

The most common failure is a version mismatch, so settle that first.

**Match versions.** The harness, the installed Countable PCR app, and its config must all come from the same version. Mixing an EA2 install with a harness built from main fails with a data logger type error, because main added the `Countable.AssayConfig` submodule and changed namespaces. Gold standards must also come from the same version as the runs they are compared against.

**Machine requirements:**

| Requirement | Detail |
| --- | --- |
| Account | Administrator. The harness has a `requireAdministrator` manifest and restarts `CountableAnalysisService`. |
| Drives | E: and F: visible to the account running the service and the runner. E: holds `CountableLabs\ReportData`; F: is the archive and holds `RegressionTesting\`. |
| Python | 3.10 or later, with `pip`. |
| PowerShell 7 | `pwsh -v` must work, because the workflow steps use `shell: pwsh`. Install with `winget install Microsoft.PowerShell` if missing. |
| NSSM | `nssm.exe` (the win64 build) in a permanent folder such as `C:\Tools\nssm\`. The service calls it from there, so do not leave it in Downloads. It does not need to be on PATH if you pass `-NssmPath`. |
| Countable PCR | The installed app, the Postgres database, `CountableAnalysisService`, and the Report Loader, all from one version. |
| GitHub access | Permission to add a self-hosted runner to the results-verification-service repo and to set repository variables. |

**PowerShell scripts:** save `.ps1` files as BOM-less UTF-8 with ASCII hyphens. Windows PowerShell 5.1 misparses em dashes and breaks the script.

## Part 1: Verification service

Run every command from an elevated prompt in the service repo folder.

1. **Get the code.** Clone or copy the repo to a permanent location, separate from the runner's `_work` checkout. The service is long-lived; the runner's copy is rewritten by each dispatch.
2. **Create the virtual environment and install dependencies:** `python -m venv .venv`, then `.venv\Scripts\pip install -r requirements.txt`.
3. **Create `config/local_config.yaml`.** The tracked `config/config.yaml` holds defaults and a placeholder connection string. `local_config.yaml` is gitignored and is loaded instead when it exists. Copy `config.yaml` to `local_config.yaml`, then set:
   - `paths.manifests_dir`, `database`, `reports_dir`, `results_dir`, and `e_drive_report_data` to this machine's folders.
   - `pipeline.database_connection` to the real Postgres connection string. Never put real credentials in the tracked file.
   - If the service logs `password authentication failed for user "<user>"`, it loaded the placeholder, so `local_config.yaml` is missing or not in `config/`.
4. **Initialise the SQLite database:** `python src/main.py --init`. The file is created at `paths.database`, which defaults to `F:/RegressionTesting/verification.db`. The parent folder is created for you, but the drive must exist.
5. **Register gold standards:** `python src/main.py --register`. It asks for the experiment ID, the gold standard CSV path, who registered it, and why. Run it once per experiment on the manifest. Files sitting in `gold_standards\` do nothing until registered, because the database is the source of truth.
6. **Clear stale manifests.** Any `context_manifest_*.json` left in the manifests folder makes the stamp step refuse to run, and the service registers it as a pending run at startup. Move old ones into `processed\` (do not delete) before starting.
7. **Install the service:** `powershell -File scripts\install_verification_service.ps1 -NssmPath "C:\Tools\nssm\nssm.exe" -Start`. The script is idempotent. Add `-Start` only after steps 4 and 5.
8. **Verify.** `Get-Service ResultsVerificationService` should show Running, and `logs\service_stdout.log` should show the listener connecting with no errors. Restart the service after any config change, since config is read at startup.

What happens without a gold standard: the harness still runs the experiment, but the service marks it `no_gold_standard` and records it as aborted. An experiment with nothing to compare gets verdict `fail`, so a stability experiment turns the whole run to `fail`, and an exploratory one gives `warn`. Register a gold standard for every experiment on the manifest before a real run.

## Part 2: Test harness and installed app

Until the harness ships inside the Countable.PCR MSI (Phase 2), it is built separately and pointed at by a path variable.

1. **Install the Countable PCR bundle** for the target version, with its Postgres database, `CountableAnalysisService`, and Report Loader. The default install folder is `C:\Program Files\Countable Labs\Countable Control Software`.
2. **Build the harness from the matching branch** of the Countable.PCR repo (`Countable.Pcr.TestHarness`). If the restore fails with NU1902 or NU1903 audit errors, a new package advisory is being treated as an error. The one-time workaround is `-p:NuGetAudit=false` on the build command. The real fix is updating the vulnerable package, such as `System.Security.Cryptography.Pkcs`.
3. **Confirm the harness has the reanalysis changes:** the `--reanalysis-only` flag, and `ImageFormat` detection. Reanalysis fails with `ImageFormat is null` without it. The harness picks `jxl` or `tiff` by scanning the sample folders (named `barcode_tubeposition_samplename`) for exactly channels times scan count images, the same rule the app uses.
4. **Set the manifest folder** in the machine environment variable `COUNTABLE_PCR_REGRESSION_TEST_MANIFEST_PATH`. It must point at the same folder as `paths.manifests_dir` in the service config. The workflow also sets it per run.
5. **Test by hand before using the workflow.** From an elevated prompt, set the working directory to the app install folder and run `<harness.exe> --reanalysis-only`. The working directory matters because the harness resolves `./Config/...` and `appsettings.json` against it.

The harness needs the manifest to exist first. A single experiment takes about 47 minutes, so use the single-experiment template (`entry_point/manifest_template_single.json`) for first runs.

## Part 3: Self-hosted GitHub runner

The runner is an outbound-polling service. It asks GitHub for jobs, so no inbound firewall rule is needed. Where the folder lives does not matter, but it must be a fresh folder.

1. **Check what is already there.** If the machine already has a runner, look at its `.runner` file and the Services list for `actions.runner.*`. Leave any live runner alone. Use a new folder with a different name, such as `C:\actions-runner-rvs`, and download a fresh runner zip into it. Never copy an old runner folder, because that copies its registration and credentials.
2. **Get a registration token** from the repo: Settings, Actions, Runners, New self-hosted runner.
3. **Run `config.cmd` from an elevated prompt** in the new folder:
   - Runner group: accept the default.
   - Runner **name**: an identity only, such as `pcr-regression-rvs`. Jobs are not routed by name.
   - **Additional labels**: enter `pcr-regression` here. Labels are what route jobs, and `runs-on` labels are ANDed. Entering the label as the name leaves the runner with only `self-hosted`, `Windows`, and `X64`, and jobs stay Queued.
   - Run as a service: yes, using an administrator account with E: and F: visible. The service account needs its Windows password.
4. **Confirm it is Idle** on the repo's Runners page, with the `pcr-regression` label shown. The service starts automatically after configuration.
5. **Copy `config/local_config.yaml` into the runner's checkout.** The stamp step runs from `<runner>\_work\results-verification-service\results-verification-service`, which is a separate copy of the repo. Create its `config\` folder and drop the file in. It needs the same `manifests_dir` as the service. The workflow checks out with `clean: false`, so the file survives between runs.

**Shared machine caveat.** If another runner on the same machine builds other things, jobs are routed by label so they will not cross over. A regression run does restart the analysis service and uses a lot of CPU and disk, so agree on timing with the owner of the other runner.

## Part 4: Workflow, variables, and a first run

The workflow is `.github/workflows/trigger_regression_run.yml`. It must be on the repo's default branch to appear in the Actions tab.

**Repository variables** (GitHub: Settings, Secrets and variables, Actions, Variables). These live on GitHub, not on the machine, and the workflow reads them as `vars.*`:

| Variable | Value |
| --- | --- |
| `PCR_MANIFESTS_DIR` | The manifests folder, same as `paths.manifests_dir`. |
| `PCR_TEST_HARNESS_EXE_PATH` | Full path to the built `Countable.Pcr.TestHarness.exe`. |
| `PCR_APP_INSTALL_DIR` | The installed app folder. Defaults to `C:\Program Files\Countable Labs\Countable Control Software` if unset. |

**Dispatch inputs:**

| Input | Meaning |
| --- | --- |
| `pipeline_build` | Build identifier recorded in the manifest. |
| `scenario` | Scenario name recorded in the manifest. |
| `runner_label` | Routes the job. Defaults to `pcr-regression`. |
| `template` | `full` (all experiments), `single` (one experiment, for smoke tests), or `acceptance` (only the MP37 acceptance criteria experiment; needs no gold standard). |
| `include_acceptance` | `true` adds the acceptance criteria experiment to the chosen template, so one run covers both. Default `false`. |
| `harness_timeout_minutes` | Cap for the harness step. Defaults to 720. The workflow converts it with `fromJSON(...)` because GitHub passes number inputs as strings. |

**First-run checklist:**

1. The service is Running, and a gold standard is registered for each experiment in the template.
2. The manifests folder holds no stale `context_manifest_*.json` files.
3. The runner shows Idle, with the right label.
4. `pwsh -v` works for the runner's service account.
5. Dispatch with `template: single`. Expect the run to stay Queued until the runner picks it up, then about 47 minutes for the harness.
6. Check the service log for "Manifest registered", then "experiments confirmed on F:", then "starting verification". The reports land under `paths.reports_dir`.

If you cancel a run after the manifest has been stamped, the service keeps waiting for it. Move that manifest out of the manifests folder and restart the service before dispatching again.

## Troubleshooting

Every problem hit during Phase 0, with its cause and fix.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Error resolving type ... SimpleLaserChannelDataLogger` | Installed app and config are an older version (EA2) than the harness built from main. | Install the bundle that matches the harness, or build the harness from the installed version's branch. |
| `[FATAL] ImageFormat is null for workflow` | The harness never set the reanalysis image format. | Use a harness with `DetectImageFormat`, which applies the app's exact-count rule over the sample folders. |
| NU1902 or NU1903 errors on restore | A new package advisory plus warnings-as-errors. | Build once with `-p:NuGetAudit=false`, and update the vulnerable package. |
| Run times out waiting for the E: folder | The Report Loader moves the folder to `Uploaded_To_Database` about 30 ms after the NOTIFY, and the old event watcher missed it. | Fixed: the service polls for the folder leaving E: instead of watching events (ADR-019 addendum). |
| Manifest never matches a notification | The notification's `experimentId` carries a `_yymmdd_hhmm` suffix. | Fixed by `resolve_notification()`, which strips it (ADR-022 addendum). |
| Stamp step refuses with "still unarchived" | A stale or in-flight manifest is in the manifests folder. | Confirm the earlier run is finished, then move the file to `processed\` or `timed_out\`. |
| Service skips a run as "already pending" | A prior manifest with the same experiment IDs was never archived. | Move that manifest out and restart the service. |
| Listener error: `password authentication failed for user "<user>"` | The service loaded the placeholder `config.yaml`. | Add `config/local_config.yaml` with the real connection string and restart. |
| Job stays Queued | The runner has no matching label, often because `pcr-regression` was entered as the name. | Add the label on the runner's page, then re-dispatch. |
| Annotation: `Unexpected value '720'` or step timeout error | `workflow_dispatch` number inputs arrive as strings. | `timeout-minutes: ${{ fromJSON(inputs.harness_timeout_minutes) }}`. |
| PowerShell script fails to parse | Em dashes or a BOM in a `.ps1` file under Windows PowerShell 5.1. | ASCII hyphens, BOM-less UTF-8. |
| `nssm version` reports a failure | It exits nonzero by design. | Check for output, not the exit code. |
| Harness cannot find its config | It resolves `./Config/...` against the working directory. | Start in the app install folder (the workflow does this). |
| `git diff` shows every line changed | Files use CRLF line endings. | Use `git diff --ignore-space-at-eol`, and consider adding a `.gitattributes`. |

Timing notes: one experiment takes about 47 minutes of analysis. Multi-experiment runs take hours, so size `harness_timeout_minutes` to the template, and keep the 720 default for the full manifest.

## What the installer will abstract away

The Inno Setup installer is planned to take over most of Part 1 and part of Part 2. Items marked planned are design intent, not built yet.

| Manual step today | Installer (planned) |
| --- | --- |
| Install NSSM to a permanent folder | Bundles `nssm.exe` and installs it under the program folder. |
| Clone repo, create venv, `pip install` | Ships a self-contained payload with its Python environment, so no repo checkout or pip on the target. |
| Hand-write `local_config.yaml` | Generates a per-machine config outside the program folder (for example under ProgramData), so upgrades do not overwrite it and secrets stay out of the payload. Install-time options are written there. |
| Real Postgres password in config | Never in the payload. Optionally creates a dedicated least-privilege Postgres role once per machine, with a random password written to the per-machine config. It is idempotent, so upgrades do not create another account. |
| `--init` | Runs schema creation and migrations on install and upgrade. |
| `--register` | Stays a deliberate step, since gold standards are data that people choose. The installer may offer a registration prompt or import. |
| Run the NSSM install script, start the service | Registers and starts `ResultsVerificationService`, with the stop timeout and log rotation already set. |
| Create folders on F: and E: | Creates what is safe to create and runs a preflight check for drives, `pwsh`, and Postgres reachability. |
| Build and place the harness | Ships inside the Countable.PCR MSI (Phase 2), at a fixed path that the workflow defaults to. |
| Set `PCR_*` repository variables | Remains a GitHub-side step. With the harness at a fixed path, only the manifests folder should need setting. |
| Register the self-hosted runner | Stays manual. It needs a per-machine token from GitHub and a decision about labels and the service account. |

Still manual after the installer: matching app and harness versions, registering gold standards from that version's results, registering the runner with the `pcr-regression` label, and clearing stale manifests.

One product-level note for the wider team: the main app connects to Postgres as `postgres` with a fixed password, which looks like a deliberate choice for offline, location-locked instruments. Do not rotate that password without updating the product first.
