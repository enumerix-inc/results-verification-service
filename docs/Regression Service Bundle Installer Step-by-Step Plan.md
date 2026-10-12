# Regression Service Bundle Installer: Step-by-Step Plan

Oct 9, 2026 · @Jesse

## Goal and decisions

The goal is a repeatable install of the regression stack on a clean machine, with a manual checkpoint after every phase so nothing builds on an untested piece. Phase 0 needs no installer, so you can start it today on the regression machine.

Decisions made so far:

- The verification service installer uses **Inno Setup**. It is an internal tool, and it needs prompts that WiX handles poorly.
- The instrumentation software (Countable Control Software) is already on the machine and is the version under test. The service installer detects it and never installs it.
- The test harness ships **inside the main software bundle** as an optional feature of `Countable.Pcr.App.Installer`, not in our repo.
- Runs use the **Reanalysis** path only, so the Stubs configuration is not needed. The machine-wide Beta environment variables stay as the App MSI sets them.
- The harness must run with a working directory of `C:\Program Files\Countable Labs\Countable Control Software\`, or be changed so it no longer depends on it.

Each phase below ends with a **Checkpoint**: a manual test you run before starting the next phase.

## What exists today

The verification service is a Python 3.10 service started with `python -m src.main --run` and wrapped by NSSM; the scripts are written but have never run on the real machine. The main bundle already builds in GitHub Actions with WiX 6 and signs with SSL.com eSigner. The harness is built there but never packaged.

| Area | Finding | Source |
| --- | --- | --- |
| Service install | Manual: Python, NSSM, venv, `--init`, runner, repo variables. Scripts untested on real NSSM. | `ADR-024`, `STATUS.md` |
| Service config | Found relative to `src/`; logs go under the repo root. Needs an install dir and a data dir. | `main.py`, install script |
| Secrets | `config/config.yaml` (tracked) contains a Postgres connection string with a password. | `config/config.yaml` |
| Database | `--init` runs the DDL once; no migrations for upgrades. | `main.py`, `schema/` |
| Harness build | In the solution, built Release by CI, not in any MSI. | `Build_Official_Release.yml` |
| Harness runtime | Needs working directory = app install folder (`Config\DataLogger` is read relative to it). Manifest is `requireAdministrator`. Restarts `CountableAnalysisService`. | `DataLoggerService.cs`, `app.manifest`, `Program.cs` |
| Harness hang | Any fatal error calls `Console.Read()` and blocks forever; the workflow caps it at 180 minutes. | `Program.cs` |
| Harness manifests | `LoadManifests()` reprocesses every manifest in the folder on every run. | `ADR-022` |
| Workflow | Offers an Imaging run type, which now needs Stubs and is unsupported. | `trigger_regression_run.yml` |

## Phase 0: Prove it by hand on the regression machine (about 1 to 2 days)

The service and the runner have never run on the real machine, so prove them manually before any installer exists. Everything in this phase uses the current scripts and the existing Debug harness. A failure here is cheap to fix; the same failure inside an installer is not.

1. **Fresh clone and venv.** Clone `results-verification-service` to a throwaway folder on the regression machine. Create the venv and run `pip install -r requirements.txt`. Note any package that fails to install.
2. **Local config.** Create `config/local_config.yaml` with the real F: and E: paths and the Postgres connection. Confirm the drives are real local disks, not mapped network drives.
3. **Initialize the database.** Run `python -m src.main --init`, then confirm `verification.db` appears at the configured path.
4. **Run in a console.** Run `python -m src.main --run` in a terminal. Confirm it logs that it is listening, then press Ctrl+C and confirm a clean shutdown message.
5. **Run under NSSM.** Install NSSM, then run `scripts\install_verification_service.ps1 -Start` as Administrator. Confirm the service shows Running in `services.msc` and the log files fill in `logs\`.
6. **Stop and restart tests.** Run `Stop-Service` and confirm a clean stop. Reboot the machine and confirm the service starts by itself. Kill the python process and confirm NSSM restarts it after about 15 seconds.
7. **Harness by hand.** From `C:\Program Files\Countable Labs\Countable Control Software\` as the working directory, run the harness with `COUNTABLE_PCR_REGRESSION_TEST_MANIFEST_PATH` set to the manifests folder. Run it in an **elevated** terminal.
8. **One end-to-end run.** Stamp a manifest with `entry_point\stamp_manifest_for_run.py`, start the harness, and let one small Reanalysis run finish. Confirm the service archives the manifest to `processed\` and writes a report.

**Checkpoint 0:** one Reanalysis run goes manifest, harness, pipeline notification, verification, report, with the service running under NSSM and surviving a reboot. If this fails, stop and fix it here.

Write down every manual step and every surprise as you go. That list becomes the installer's requirements.

## Phase 1: Make the harness independent of the working directory (about 1 day, Countable.PCR repo)

The harness reads `./Config/DataLogger/...` relative to the current directory, which is why it only works with Start in set to the app install folder. Fix it once in the harness so no installer, shortcut or workflow has to remember it.

1. **Confirm the cause.** Run the harness with a blank Start in and read the newest log in `%LOCALAPPDATA%\Countable Labs\Countable PCR\Logs`. Look for a missing `Config\DataLogger` path or `Required config not available`.
2. **Pick the fix.** Either set the working directory at startup in `Program.cs` to the app install folder (resolved from an environment variable or the registry, not hardcoded), or copy `Config\DataLogger` and `Config\Logging` into the harness's own output so its own folder works. The first is smaller; the second keeps the harness self-contained.
3. **Fail fast instead of hanging.** Replace `Console.Read()` in the catch block with a nonzero exit code, so a failed run ends in seconds rather than at the 180-minute workflow cap. Keep the message printed.
4. **Optional: archive manifests.** Make `LoadManifests()` ignore or move manifests it has already run, closing the gap tracked in `ADR-022`.
5. **Build and test.** Build Release and run the harness with Start in blank, then with Start in set to another folder. Both must work.

**Checkpoint 1:** the Release harness runs a Reanalysis run correctly from any working directory, and exits nonzero with a message when the core fails to initialize.

## Phase 2: Ship the harness in the main bundle (about 1 to 2 days, Countable.PCR repo)

The harness is already built Release by `dotnet build` on the solution, so this phase only packages it. It becomes an optional feature of the App MSI, so customer installs do not get it.

1. **Add a component group.** In `src/Countable.Pcr.App.Installer/Component.wxs`, add a `TestHarnessComponentGroup` that harvests the harness's Release output into `INSTALLFOLDER\TestHarness\`. Use a subfolder so its DLLs and `appsettings.json` never collide with the app's. Leave out the Stubs config.
2. **Add an optional feature.** In `Package.wxs`, add a second `<Feature Id="TestHarness">` with a level above the default install level, so a normal install skips it. Regression machines opt in with `msiexec /i ... ADDLOCAL=CountablePCR,TestHarness`.
3. **Sign it.** Add an eSigner step in `Build_Official_Release.yml` for `Countable.Pcr.TestHarness.exe`, before the installer builds, as is done for the other executables.
4. **Mirror it in the unofficial workflow.** Check `Build_Unofficial_Release.yml` for the same build and sign steps.
5. **Build a test MSI.** Run the workflow and download the MSI artifact.
6. **Install on a clean VM.** Run the MSI once normally and once with `ADDLOCAL=CountablePCR,TestHarness`. Confirm the harness exists only in the second case.

**Checkpoint 2:** the harness installs by itself from the new MSI into `...\Countable Control Software\TestHarness\`, is signed, and a Reanalysis run works from that location. The path `C:\Program Files\Countable Labs\Countable Control Software\TestHarness\Countable.Pcr.TestHarness.exe` is now the stable harness location.

## Phase 3: Make the verification service installable (about 3 to 4 days, this repo)

Today the service assumes it runs from a git checkout. An installed copy needs a code directory that can be replaced on upgrade and a data directory that survives it.

1. **Rotate the leaked credential.** The Postgres password in the tracked `config/config.yaml` is in git history. Rotate it, then replace the value in the tracked file with a placeholder.
2. **Split code from data.** Install code to `C:\Program Files\Countable Labs\Results Verification Service\` and keep config, logs, database and reports under `C:\ProgramData\Countable Labs\Results Verification Service\`. Add a `--config <path>` option and a `RVS_CONFIG` environment variable to `load_config()`, defaulting to the ProgramData file.
3. **Move logs.** Point the NSSM log paths at the ProgramData `logs\` folder instead of the repo root.
4. **Add schema versioning.** Add a `schema_version` table and numbered migration scripts. `--init` becomes create-if-missing, and a new `--migrate` applies pending migrations. An upgrade must never recreate or wipe the database.
5. **Add `--check`.** A preflight command that validates paths, the drives, Postgres connectivity, the harness path, the .NET runtime and the `CountableAnalysisService` service, and prints one pass or fail line each. The installer will call it.
6. **Remove the checkout assumptions.** Make `stamp_manifest_for_run.py` read config the same way, so it works from the installed copy.
7. **Keep the tests green.** Run `tests/` and fix anything the path changes break.

**Checkpoint 3:** copy the service folder to a path outside the repo and run `--check`, `--init`, `--migrate` and `--run` with a config in a separate data folder. It must work with no git checkout present, and re-running `--init` must leave the database untouched.
