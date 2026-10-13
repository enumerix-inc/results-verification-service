"""
main.py — entry point for the results verification service.

Usage:
    python -m src.main --init      Initialise the database
    python -m src.main --register  Run the gold standard registration tool
    python -m src.main --run       Start the verification service
"""

import argparse
import asyncio
import json
import re
import shutil
import yaml
from pathlib import Path

from src.database.db import get_connection, initialise_database
from src.database.models import get_all_processed_run_ids, get_all_reported_run_ids
from src.listener.listener import listen_async, listen_async_mock
from src.orchestrator.orchestrator import verify_run
from src.orchestrator.watcher import watch_and_confirm
from src.orchestrator.manifest_watcher import watch_manifests_dir
from src.registration.registrar import register_gold_standard

# Manifest read retry: watchdog's on_created/on_moved can fire before a
# writer has finished flushing the file (or mid-rename on some
# filesystems). Retry with backoff rather than giving up on the first
# read — see ADR-022 / Copilot review, ticket for manifest_watcher.py.
MANIFEST_READ_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6)

# The pipeline's Reports.ExperimentId carries a trailing timestamp
# ("{experiment_id}_{yymmdd}_{hhmm}", e.g. "MP47b_Adverum_02_261013_1053"),
# while a manifest declares the bare experiment_id. Observed live on
# 2026-10-13; earlier notifications carried the bare id in experimentId and
# the timestamped identifier in "name" (see ADR-022 addendum).
PIPELINE_TIMESTAMP_SUFFIX = re.compile(r"_\d{6}_\d{4}$")


def resolve_notification(payload_experiment_id: str, payload_name: str,
                         expected_ids) -> tuple:
    """
    Map a NOTIFY payload to (manifest_experiment_id, pipeline_name).

    manifest_experiment_id is the key into expected_ids (the manifests'
    bare experiment_ids), or None if the notification matches no pending
    manifest. pipeline_name is the timestamped identifier the pipeline used
    for this execution's folders - the value find_result_folder() and the
    E: drive watch need.

    An exact match always wins (old payload shape: bare experimentId plus a
    timestamped "name"). Otherwise a trailing _yymmdd_hhmm is stripped from
    experimentId and the match retried; in that case the timestamped
    experimentId itself is the pipeline's folder identifier, since "name"
    may now be the plain experiment name.
    """
    if payload_experiment_id in expected_ids:
        return payload_experiment_id, payload_name
    stripped = PIPELINE_TIMESTAMP_SUFFIX.sub("", payload_experiment_id)
    if stripped != payload_experiment_id and stripped in expected_ids:
        return stripped, payload_experiment_id
    return None, payload_name


def load_config() -> dict:
    """Load configuration from local_config.yaml if it exists, else config.yaml."""
    config_dir = Path(__file__).parent.parent / "config"
    local = config_dir / "local_config.yaml"
    default = config_dir / "config.yaml"
    config_path = local if local.exists() else default
    with open(config_path) as f:
        return yaml.safe_load(f)


def init(config: dict) -> None:
    """Initialise the SQLite database from the schema DDL."""
    db_path = config["paths"]["database"]
    schema_path = Path(__file__).parent.parent / "schema" / "verification_schema.sql"
    print("Initialising database...")
    initialise_database(db_path, str(schema_path))
    print("Done.")


def register(config: dict) -> None:
    """Run the interactive gold standard registration tool."""
    print("Starting registration tool...")
    experiment_id = input("Experiment ID: ")

    while True:
        gold_standard_csv_path = input("Gold standard CSV path: ")
        path = Path(gold_standard_csv_path)
        if not path.exists():
            print(f"Error: file not found at {gold_standard_csv_path}")
        elif path.suffix.lower() != ".csv":
            print(f"Error: expected a .csv file, got '{path.suffix}'")
        else:
            break

    registered_by = input("Registered by: ")
    reason = input("Reason: ")

    try:
        with get_connection(config["paths"]["database"]) as conn:
            new_gs_exp_version_id = register_gold_standard(conn, experiment_id,
                                                        gold_standard_csv_path,
                                                        registered_by, reason)
            print("Registered new gold standard version:", new_gs_exp_version_id)
    except Exception as e:
        print(f"Registration failed: {e}")
        return


def run(config: dict) -> None:
    """
    Start the verification service listener loop.

    Catches Ctrl+C / a console-close-style interrupt so a normal shutdown
    request prints a clean message instead of dumping a KeyboardInterrupt
    traceback. This matters once the service runs unattended under a
    service wrapper (ADR-024): that's how NSSM's default stop method
    signals a running console app to shut down.

    Note on what actually happens underneath: asyncio.run()'s own cleanup
    (not this except block) is what does the real work — on interrupt it
    cancels the still-running run_service() coroutine at whatever await
    point it's suspended, which lets run_service()'s own
    `finally: manifest_watch_task.cancel()` execute, which in turn lets
    watch_manifests_dir()'s finally (observer.stop()/join()) execute. It
    also cancels manifest_watch_task directly and, since Python 3.9,
    calls loop.shutdown_default_executor() before returning — which waits
    for any verify_run() currently in flight on its worker thread
    (asyncio.to_thread) to actually finish, rather than killing it
    mid-write. None of that is untested-in-theory: it's Python's
    documented asyncio.run() shutdown sequence, not something this
    project added. What this except block adds on top is purely cosmetic
    — a clean log line instead of a traceback — since the token: Event
    passed to listen_async() is never actually set anywhere; shutdown
    goes through task cancellation, not the token.
    """
    token = asyncio.Event()
    try:
        asyncio.run(run_service(config, token))
    except KeyboardInterrupt:
        print("Shutdown requested — verification service stopping.")


def _manifest_identity(manifest) -> tuple:
    """
    Validate a parsed manifest's structure and return (run_id, experiment_ids).

    Raises ValueError for any shape that isn't usable — a non-object
    document, a non-object "run", a missing/non-string run_id, or a
    missing/malformed "experiments" list. Callers treat ValueError as
    "malformed manifest: skip it", so one bad file can never escape as a
    stray TypeError/KeyError/AttributeError and abort service startup.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not a JSON object")
    run = manifest.get("run")
    if not isinstance(run, dict):
        raise ValueError('"run" is missing or not an object')
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('"run.run_id" is missing or not a non-empty string')
    experiments = manifest.get("experiments")
    if not isinstance(experiments, list):
        raise ValueError('"experiments" is missing or not a list')
    experiment_ids = set()
    for exp in experiments:
        if not isinstance(exp, dict) or not isinstance(exp.get("experiment_id"), str):
            raise ValueError('an "experiments" entry has no string experiment_id')
        experiment_ids.add(exp["experiment_id"])
    return run_id, experiment_ids


async def run_service(config: dict, token: asyncio.Event) -> None:
    """
    Async service loop.

    Two watchers run side by side:
      - watch_manifests_dir: the explicit hand-off. A new manifest file
        is the signal that a run's experiments are about to start
        reporting; its experiments are registered as "expected"
        immediately, before any notification for them ever arrives.
      - listen_async / listen_async_mock: the pipeline's Postgres NOTIFY
        stream. Each notification's experiment_id is looked up against
        what's expected — a plain dictionary lookup, replacing the old
        approach of trying to parse a run_id out of the payload itself.

    The manifest watcher's initial catch-up scan must finish before the
    listener starts consuming notifications: otherwise a notification for
    a manifest that's already sitting in manifests_dir at startup can
    arrive (or, for the mock listener, fire immediately) before that
    manifest has been registered, and gets dropped as "no pending
    manifest". manifest_watcher_ready is the barrier for that.
    """
    with get_connection(config["paths"]["database"]) as conn:
        processed_run_ids = get_all_processed_run_ids(conn)
        reported_run_ids = get_all_reported_run_ids(conn)

    in_progress = {}                 # {run_id: set of exp_ids notified}
    confirmed_ready = {}             # {run_id: set of exp_ids confirmed on F:}
    manifests = {}                   # {run_id: manifest dict}
    manifest_paths = {}              # {run_id: Path — for archiving once done}
    experiment_names = {}            # {run_id: {experiment_id: pipeline's timestamped name}}
    expected_experiment_to_run = {}  # {experiment_id: run_id} — the explicit contract

    manifests_dir = Path(config["paths"]["manifests_dir"])
    processed_dir = manifests_dir / "processed"
    timed_out_dir = manifests_dir / "timed_out"
    failed_dir = manifests_dir / "failed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    timed_out_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    def _reconcile_processed_manifests() -> None:
        """
        Startup self-healing (ADR-024 addendum, sessions 16-17).

        A manifest can end up sitting unarchived at the top of
        manifests_dir even though its run_id is already in
        processed_run_ids — i.e. verify_run() already committed its
        runs/experiment_results/sample_results transaction. The main way
        this happens: a stop signal (Ctrl+C, or NSSM's equivalent on a
        service stop) arrives while on_confirmed() is awaiting
        asyncio.to_thread(verify_run, ...). asyncio.run()'s shutdown
        still lets that worker thread finish — so the DB write
        completes — but the cancellation unwinds past on_confirmed()'s
        try/except/else entirely (CancelledError is a BaseException, not
        an Exception, so it isn't caught there), and the follow-up
        _archive(run_id, processed_dir) call never runs.

        Left alone this is permanent: nothing ever revisits an already-
        registered manifest, and stamp_manifest_for_run.py's safety check
        refuses every future dispatch because of it, even though the run
        it's complaining about is actually done. Reconciling here, before
        anything is registered as "expected," fixes this on the very next
        restart — including recovering from causes other than a clean
        Ctrl+C, e.g. the process being killed outright.

        A run row alone isn't proof the run is fully done, though:
        verify_run() commits runs/experiment_results/sample_results in
        one transaction and only afterward calls write_report(), which
        writes the CSVs and inserts into reports as a separate,
        later transaction. A hard kill in that gap leaves a runs row
        with no report. Archiving a manifest on the strength of the
        runs row alone would permanently hide that half-finished state,
        so this function requires both processed_run_ids (from runs)
        and reported_run_ids (from reports) before it will archive —
        a run_id in the former but not the latter is left unarchived
        and flagged for a human to investigate instead.

        Uses the same context_manifest_*.json glob as manifest_watcher.py
        (not narrowed to *_run_* names) so this catches exactly the set
        of files the live watcher would otherwise pick up. Any manifest
        that fails to parse or is missing/malformed structure (bad JSON,
        an unreadable file, or a shape that doesn't match what's
        expected, e.g. {"run": null}) is skipped rather than raised —
        one broken file must never prevent the service from starting.
        """
        for path in sorted(manifests_dir.glob("context_manifest_*.json")):
            try:
                manifest = json.loads(path.read_text())
                run_id, _ = _manifest_identity(manifest)
            except (ValueError, OSError) as e:
                # ValueError covers JSONDecodeError, UnicodeDecodeError
                # (invalid UTF-8) and _manifest_identity's structural
                # checks (e.g. {"run": null}).
                # Not this function's job to fix a malformed/partially
                # written file — handle_new_manifest's own retry-with-
                # backoff handles that once the watcher picks it up
                # normally.
                print(f"Startup reconciliation: skipping unreadable "
                      f"{path.name}: {e}")
                continue

            if run_id in processed_run_ids and run_id in reported_run_ids:
                try:
                    shutil.move(str(path), str(processed_dir / path.name))
                    print(f"Startup reconciliation: {path.name} (run "
                          f"{run_id}) was already verified and reported "
                          f"but left unarchived — moved to "
                          f"{processed_dir / path.name}")
                except OSError as e:
                    print(f"Warning: could not reconcile/archive "
                          f"{path}: {e}")
            elif run_id in processed_run_ids:
                # insert_run() committed but write_report() never
                # completed -- e.g. the process was hard-killed in the
                # gap between the two. The run is NOT actually done,
                # so archiving it here would hide the missing report
                # forever (nothing else ever revisits an archived
                # manifest). Leave it in place instead: it stays
                # unarchived, so stamp_manifest_for_run.py's safety
                # check keeps refusing new runs, same as before this
                # function ran -- which is the point, since this state
                # needs a human to look at the runs/reports tables for
                # this run_id and decide whether to re-verify or clean
                # it up by hand.
                print(f"Warning: {path.name} (run {run_id}) has a "
                      f"runs row but no report — verification likely "
                      f"completed but reporting did not. Leaving "
                      f"manifest in place for investigation; NOT "
                      f"archiving.")

    _reconcile_processed_manifests()

    def _archive(run_id: str, destination: Path) -> None:
        """Drop a finished run's in-memory state and move its manifest aside."""
        in_progress.pop(run_id, None)
        confirmed_ready.pop(run_id, None)
        manifest = manifests.pop(run_id, None)
        path = manifest_paths.pop(run_id, None)
        experiment_names.pop(run_id, None)

        if manifest is not None:
            for exp_id in {e["experiment_id"] for e in manifest["experiments"]}:
                if expected_experiment_to_run.get(exp_id) == run_id:
                    expected_experiment_to_run.pop(exp_id, None)

        if path is not None and path.exists():
            try:
                shutil.move(str(path), str(destination / path.name))
                print(f"Run {run_id}: manifest archived to {destination / path.name}")
            except OSError as e:
                print(f"Warning: could not archive manifest {path}: {e}")

    async def handle_new_manifest(path: Path) -> None:
        """Register a manifest's experiments as soon as the file appears.

        Retries the read with backoff: a filesystem event can fire before
        the writer has finished flushing (or mid atomic-rename), so a
        JSONDecodeError or a transient OSError on the first attempt
        doesn't necessarily mean the manifest is bad.
        """
        manifest = None
        last_error = None
        for delay in MANIFEST_READ_RETRY_DELAYS:
            try:
                manifest = json.loads(path.read_text())
                break
            except (json.JSONDecodeError, UnicodeError, OSError) as e:
                last_error = e
                await asyncio.sleep(delay)
        else:
            print(f"Warning: could not read manifest {path} after "
                  f"{len(MANIFEST_READ_RETRY_DELAYS)} attempts: {last_error}")
            return

        try:
            run_id, expected = _manifest_identity(manifest)
        except ValueError as e:
            # Must not raise: during the startup catch-up scan an escaping
            # exception would kill the watcher before it signals ready,
            # and run_service() would never start the listener.
            print(f"Warning: manifest {path.name} is malformed ({e}) — "
                  f"skipping registration. Fix or remove it and re-drop "
                  f"it to retry.")
            return

        if run_id in processed_run_ids or run_id in manifests:
            return  # already verified, or already registered

        conflicts = expected & expected_experiment_to_run.keys()
        if conflicts:
            conflicting_runs = sorted({expected_experiment_to_run[c] for c in conflicts})
            print(f"Warning: manifest {path.name} (run {run_id}) declares "
                  f"experiment(s) {sorted(conflicts)} already pending under "
                  f"run(s) {conflicting_runs} — skipping registration. This "
                  f"usually means a prior run never finished and its manifest "
                  f"was never archived. Resolve the stuck run (or move its "
                  f"manifest out of {path.parent}) and re-drop this manifest "
                  f"to retry.")
            return

        manifests[run_id] = manifest
        manifest_paths[run_id] = path
        in_progress[run_id] = set()
        confirmed_ready[run_id] = set()
        experiment_names[run_id] = {}
        for exp_id in expected:
            expected_experiment_to_run[exp_id] = run_id

        print(f"Manifest registered: run {run_id} — expecting {len(expected)} experiment(s)")

    async def handle_notification(payload: str) -> None:
        data = json.loads(payload)
        raw_id = data.get("experimentId", "")

        exp_id, name = resolve_notification(
            raw_id, data.get("name", ""), expected_experiment_to_run)
        if exp_id is None:
            print(f"Warning: notification for experiment '{raw_id}' with no "
                  f"pending manifest - ignoring. Full payload: {payload}")
            return

        run_id = expected_experiment_to_run[exp_id]

        if run_id in processed_run_ids:
            print(f"Run {run_id} already processed — ignoring")
            return

        expected = {e["experiment_id"] for e in manifests[run_id]["experiments"]}

        in_progress[run_id].add(exp_id)
        experiment_names[run_id][exp_id] = name
        print(f"Run {run_id}: notification received for {exp_id} "
              f"(payload experimentId={raw_id!r}, pipeline name={name!r})")

        asyncio.create_task(_watch_experiment(exp_id, run_id, name, expected, config))

    async def _watch_experiment(exp_id, run_id, name, expected, config):
        """Watch E: for this experiment and trigger verify_run when all ready."""

        async def on_confirmed():
            if run_id not in confirmed_ready:
                return
            confirmed_ready[run_id].add(exp_id)
            print(f"Run {run_id}: {len(confirmed_ready[run_id])}/"
                  f"{len(expected)} experiments confirmed on F:")

            if expected.issubset(confirmed_ready[run_id]):
                print(f"Run {run_id}: all experiments confirmed — "
                      f"starting verification")
                # The manifest is only archived to processed/ once
                # verify_run() succeeds (see except branch below), so this
                # is a true prediction of where it will live — that's what
                # gets recorded in runs.manifest_path.
                manifest_record_path = str(processed_dir / manifest_paths[run_id].name)
                try:
                    await asyncio.to_thread(
                        verify_run, config, run_id, manifests[run_id],
                        experiment_names[run_id], manifest_record_path,
                    )
                except Exception as e:
                    print(f"Error: verification failed for run {run_id}: "
                          f"{e!r} — archiving manifest to {failed_dir} and "
                          f"freeing state so later runs aren't blocked")
                    _archive(run_id, failed_dir)
                else:
                    processed_run_ids.add(run_id)
                    _archive(run_id, processed_dir)

        async def on_timeout():
            if run_id not in confirmed_ready:
                return  # another experiment already triggered cleanup for this run
            print(f"Run {run_id}: experiment {exp_id} timed out — "
                  f"run will not be verified")
            _archive(run_id, timed_out_dir)

        await watch_and_confirm(exp_id, run_id, name, config, on_confirmed, on_timeout)

    manifest_watcher_ready = asyncio.Event()
    manifest_watch_task = asyncio.create_task(
        watch_manifests_dir(config, handle_new_manifest, manifest_watcher_ready)
    )
    ready_wait = asyncio.create_task(manifest_watcher_ready.wait())
    await asyncio.wait({ready_wait, manifest_watch_task},
                       return_when=asyncio.FIRST_COMPLETED)
    if not manifest_watcher_ready.is_set():
        # The watcher exited before signalling ready — surface its error
        # rather than waiting forever with no listener running.
        ready_wait.cancel()
        manifest_watch_task.result()
        raise RuntimeError("manifest watcher stopped before becoming ready")

    try:
        if config["listener"]["use_mock"]:
            await listen_async_mock(config, handle_notification)
        else:
            await listen_async(config, token, handle_notification)
    finally:
        manifest_watch_task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Results verification service")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="Initialise the database")
    group.add_argument("--register", action="store_true", help="Register gold standards")
    group.add_argument("--run", action="store_true", help="Start the service")
    args = parser.parse_args()

    config = load_config()

    if args.init:
        init(config)
    elif args.register:
        register(config)
    elif args.run:
        run(config)
