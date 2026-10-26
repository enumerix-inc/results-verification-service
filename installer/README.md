# Installer (Inno Setup)

Builds `RegressionVerificationService-Setup-<version>.exe`, which installs the
verification service on a regression machine without Python, pip, or internet
access on the target. Scope for v1: the service only (see ADR-025).

## Build (on a machine with internet)

Prerequisites: Windows, Inno Setup 6.3 or later, the 64-bit `nssm.exe`.

```
powershell -File installer\build_payload.ps1 -NssmExe "C:\Tools\nssm\nssm.exe" -Version 0.1.0
```

This downloads the embeddable Python and pip into `installer\cache\`, installs
`requirements.txt` into `installer\payload\python`, copies `src\`, `schema\`,
`scripts\` and the default `config\config.yaml`, smoke-tests the payload, and
runs the Inno compiler if it finds it. Use `-SkipCompile` to build the payload
only. Output lands in `installer\Output\`.

## What the installer does

1. Installs to `C:\Program Files\Countable Labs\Regression Service`
   (`python\`, `service\`, `tools\`). Upgrades replace these folders.
2. Asks for the Postgres connection and the data folders (skipped on upgrade).
3. Writes `C:\ProgramData\Countable Labs\Regression Service\config\local_config.yaml`
   once, with access limited to SYSTEM and Administrators. It never overwrites an
   existing file, and the password never enters the payload.
4. Runs `python -m src.main --init` to create the SQLite database.
5. Runs `--preflight` (paths, database reachability). If it fails the service is
   registered but not started, and the failure is shown.
6. Registers `ResultsVerificationService` through NSSM
   (`scripts\install_verification_service.ps1` with `-AppDir`, `-PythonExe`,
   `-ConfigDir`, `-LogsDir`), setting `RVS_CONFIG` to the per-machine config folder.

Uninstall removes the service and the program folder. Config, database, and logs
under ProgramData are kept on purpose.

## Silent install

```
Setup.exe /VERYSILENT /DBUSER=svc /DBPASSWORD=... [/DBHOST=localhost] [/DBPORT=5432]
  [/DBNAME=countablelabs] [/MANIFESTSDIR=...] [/REPORTSDIR=...] [/RESULTSDIR=...]
  [/EREPORTDATA=...] [/DBFILE=...]
```

## Not done yet

- Gold standard registration (`--register`) is still a deliberate manual step.
- Creating a dedicated Postgres role, bundling the harness, and the runner
  registration are separate tickets.
- This script has not been compiled or run on a clean machine; test on a clean VM
  before trusting it.
