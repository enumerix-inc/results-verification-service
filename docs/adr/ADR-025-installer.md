# ADR-025: Inno Setup installer with bundled Python and a per-machine config

**Status:** Proposed

## Context

Setting up the service by hand (see the Manual Setup Guide) needs a repo
checkout, Python, pip, internet access, a hand-written `local_config.yaml`, and
a manual NSSM registration. Regression machines are often offline, and the real
database credential must not travel in an installer payload or in git.

## Decision

Ship the service with an Inno Setup installer (TES-2805):

- **Bundled interpreter.** The payload carries the official embeddable Python
  with dependencies pre-installed, built on an internet-connected build machine.
  The service still runs as `python -m src.main --run` under NSSM, so ADR-024 is
  unchanged. The embeddable distribution takes `sys.path` only from its `._pth`
  file, which the build script edits to include the service folder.
- **Program and data are separate.** Program files go under Program Files and are
  replaced on upgrade. Config, the SQLite database, and logs go under
  `C:\ProgramData\Countable Labs\Regression Service` and survive upgrades and
  uninstall.
- **Per-machine config is generated, never bundled.** The wizard collects the
  Postgres connection and folder paths; `scripts/write_local_config.py` writes
  `local_config.yaml` once (percent-encoding the password) and the installer
  restricts it to SYSTEM and Administrators. The service finds it through the
  `RVS_CONFIG` environment variable that NSSM sets, or `--config`
  (`src/config_loader.py`). An explicitly requested config that is missing is an
  error, not a silent fallback to the tracked defaults.
- **Preflight before start.** `--preflight` checks drives and database
  reachability. A failing preflight registers the service but does not start it.

## Scope and consequences

v1 covers the service only. Deferred: a dedicated least-privilege Postgres role
(created once per machine, random password), bundling the test harness in the
Countable.PCR MSI, schema migrations, gold standard registration, and runner
setup, which stays manual because it needs a per-machine GitHub token.
The installer has been written but not yet compiled or tested on a clean machine.
