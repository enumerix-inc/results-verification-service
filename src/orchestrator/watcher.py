"""
watcher.py - E: drive clear-out wait for pipeline completion signal.

Waits for the Report Loader to finish with an execution's folder under
E:/CountableLabs/ReportData. The loader saves the report to the database
(this is what fires the NOTIFY), copies the outputs to F:, and only then
removes the folder from ReportData - either deleting it
(COUNTABLE_PCR_DELETE_REPORT_DATA=true) or moving it to its
Uploaded_To_Database archive (false). Either way the folder leaving
ReportData is the first moment the F: copy is known to be complete.

This used to be a watchdog event handler that only reacted to deletions
(ADR-019). Two problems showed up on the regression machine (see the
ADR-019 addendum): the loader moves rather than deletes when
deletion is disabled, and the loader's move lands ~30 ms after the NOTIFY,
so the event could be missed entirely. It now simply polls for the
folder's absence, which covers delete, move, and the race alike.

Matches on the pipeline's own timestamped `name` for the execution (the
same string used by orchestrator.find_result_folder()) rather than
experiment_id/run_id. See ADR-022.
"""

import asyncio
from pathlib import Path

DEFAULT_POLL_SECONDS = 2.0


async def wait_for_e_drive_deletion(exp_id: str, run_id: str, name: str,
                                     config: dict) -> bool:
    """
    Wait for the execution's folder to leave E:/.../ReportData, signalling
    that the F: drive copy is complete.

    `name` is the pipeline's own timestamped identifier for this execution
    (e.g. "JS221N_serial_titration_PSF_260925_1416"). exp_id/run_id are
    accepted only for logging context.

    Returns True as soon as no folder matching `{name}*` exists under
    ReportData (including when it was already gone on the first check -
    the NOTIFY is the anchor). Returns False if the configured timeout
    elapses first.
    """
    watch_dir = Path(config["paths"]["e_drive_report_data"])
    timeout_seconds = config["verification"]["e_drive_deletion_timeout_seconds"]
    poll_seconds = config["verification"].get("e_drive_poll_seconds", DEFAULT_POLL_SECONDS)
    label = f"{exp_id}/{run_id} ({name})"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    first_check = True

    while True:
        if not any(watch_dir.glob(f"{name}*")):
            if first_check:
                print(f"E: folder already absent for {label} - F: copy complete")
            else:
                print(f"E: folder cleared for {label} - F: copy complete")
            return True
        first_check = False

        remaining = deadline - loop.time()
        if remaining <= 0:
            print(f"Warning: E: folder still present for {label} "
                  f"after {timeout_seconds}s - run will not be verified")
            return False
        await asyncio.sleep(min(poll_seconds, remaining))


async def watch_and_confirm(exp_id: str, run_id: str, name: str, config: dict,
                             on_confirmed, on_timeout) -> None:
    """
    Wait for the execution's folder to leave E: and dispatch to the
    appropriate callback.

    Calls on_confirmed() if the folder clears within the configured
    timeout, on_timeout() otherwise.
    """
    if await wait_for_e_drive_deletion(exp_id, run_id, name, config):
        await on_confirmed()
    else:
        await on_timeout()
