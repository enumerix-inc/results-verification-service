# ADR-019: E: drive deletion watch as pipeline completion signal

## Status
Accepted

## Context
The verification service receives a PostgreSQL NOTIFY when a report lands
in the pipeline database. However, the actual result files (CSVs) are
written to E: drive first and then copied to F: drive for archival. The
NOTIFY fires before the F: copy is complete, creating a race condition
where the verification service may attempt to read files that don't exist
yet on F:.

Two approaches were considered:
1. Poll F: drive on an interval until files appear
2. Watch E: drive for deletion of the temporary folder

## Decision
Watch E:\CountableLabs\ReportData for deletion of the experiment folder.
Use Python `watchdog` library to monitor the folder. When the experiment
folder is deleted from E:, this signals that the F: copy is confirmed
complete and it is safe to read from F:.

## Reasoning

**Deletion is a pipeline postcondition, not a timing assumption.**
The pipeline only deletes from E: after the F: copy is confirmed complete.
This is a guarantee built into the pipeline, not something we have to
guess at. We are leveraging an existing invariant rather than building
new timing logic.

**Failure mode is acceptable.**
If deletion fails (unlikely given pipeline guards), the verification
service never triggers for that run — a missed verification. This is
always recoverable by re-running. It is far preferable to a false
verification against partial data, which would corrupt the longitudinal
record.

**Simpler than polling.**
Polling F: requires choosing a poll interval and hoping files finish
copying within it. Watching E: for deletion is event-driven — no
interval tuning, no partial read risk.

## Implementation

Uses `watchdog` library to monitor E:\CountableLabs\ReportData.
One watcher is started per experiment notification — experiments are
watched independently since the pipeline processes them sequentially.

### Race condition handling — observer-first pattern

A naive implementation checks for folder existence first, then starts
the watcher. This creates a race window where deletion could occur
between the check and the watcher starting — the event is missed and
the run times out.

The correct pattern is **observer-first**:

1. Start the watchdog observer
2. Then check if the folder still exists
3. If already gone → set the completion event manually and proceed
4. If still present → wait for the deletion event from the watcher

This is race-condition-free. The observer is running before the
existence check, so any deletion after that point is caught. If the
folder is already gone, the NOTIFY itself is proof it existed and was
moved to F: — proceed immediately.

**The NOTIFY is proof of existence.** The pipeline only fires the
NOTIFY after the data folder is created and the experiment is complete.
If we receive the NOTIFY and the E: folder is already gone, there is
exactly one explanation: the folder was created, the copy to F:
completed, and deletion occurred before we got around to checking.
Proceeding immediately is safe and correct.

```python
async def watch_and_confirm(exp_id, run_id, expected, config):
    e_drive_dir = Path(config["paths"]["e_drive_report_data"])
    loop = asyncio.get_event_loop()
    deletion_event = asyncio.Event()

    async def on_deletion():
        deletion_event.set()

    # start observer BEFORE checking existence — no missed events
    handler = ReportDataDeletionHandler(exp_id, run_id, loop, on_deletion)
    observer = Observer()
    observer.schedule(handler, str(e_drive_dir), recursive=False)
    observer.start()

    try:
        # check existence AFTER observer is running
        matches = list(e_drive_dir.glob(f"{exp_id}_{run_id}_*"))
        if not matches:
            # folder already gone — NOTIFY proves it existed and was moved
            print(f"{exp_id}/{run_id}: E: folder already gone — proceeding")
            deletion_event.set()

        # wait for event (watcher-triggered or manually set above)
        timeout = config["verification"]["e_drive_deletion_timeout_seconds"]
        try:
            await asyncio.wait_for(deletion_event.wait(), timeout=timeout)
            await _mark_confirmed(exp_id, run_id, expected, config)
        except asyncio.TimeoutError:
            print(f"Warning: E: deletion timeout for {exp_id}/{run_id} "
                  f"after {timeout}s — run will not be verified")
    finally:
        observer.stop()
        observer.join()
```

### Per-experiment watching with asyncio.create_task()

`asyncio.create_task()` starts each watch as a concurrent task so
`handle_notification()` returns immediately and can receive subsequent
notifications while watches run in the background. All experiment
watches run concurrently — each independently confirms its own deletion.

`verify_run()` triggers only when all expected experiments have been
individually confirmed via their own deletion events.

## Config

```yaml
paths:
  e_drive_report_data: "E:/CountableLabs/ReportData"
  e_drive_metafolder:  "E:/CountableLabs/Data/metafolder"  # future
  results_dir:         "F:/CountableLabs/data"

verification:
  e_drive_deletion_timeout_seconds: 600
```

## Full timing sequence (per experiment)

```
NOTIFY fires for {exp_id} / {run_id}
        ↓
handle_notification() receives it
        ↓
asyncio.create_task(watch_and_confirm(exp_id, run_id))
        ↓
Observer starts on E:\CountableLabs\ReportData  ← FIRST
        ↓
Check: is {exp_id}_{run_id}_* still on E:?      ← SECOND
  Already gone → set deletion_event manually → proceed immediately
  Still present → wait for watchdog deletion event
        ↓
deletion_event fires (either path)
        ↓
exp_id added to confirmed_ready[run_id]
        ↓
If confirmed_ready[run_id] ⊇ expected → verify_run()
        ↓
Comparisons, reports, DB writes
```

## Consequences
- `watchdog` added to requirements.txt
- One watcher started per experiment notification via `asyncio.create_task()`
  — cleaned up after each experiment is confirmed
- The asyncio/watchdog thread boundary requires
  `asyncio.run_coroutine_threadsafe()` — documented in asyncio-reference.md
- `run_service()` gains a `confirmed_ready` dict alongside `in_progress`
  — experiments move from notified to confirmed independently
- If E: deletion never fires (pipeline failure), the experiment is never
  confirmed and the run is never verified — visible in DB as a run
  with no associated report
- Metafolder (E:\CountableLabs\Data\metafolder) is explicitly NOT
  watched at this stage — see ADR-020

## Addendum (2026-10-14): poll for the folder leaving E: instead of watching for deletion events
Two runs on the regression machine timed out at 600 s even though the
pipeline had finished. The Report Loader's log showed why: it saves the
report (the NOTIFY), copies outputs to F:, then removes the folder from
`ReportData` - and on that machine `COUNTABLE_PCR_DELETE_REPORT_DATA` is
false, so it MOVES the folder to `Uploaded_To_Database` rather than deleting
it. The move landed about 30 ms after the NOTIFY. The service only handled
`on_deleted` watchdog events, and its "already absent" pre-check could run
just before the move, so the event was never seen.

The signal itself is still right (the folder leaving `ReportData` is the
first point at which the F: copy is known complete, because the loader copies
before it cleans up), so it is kept. The mechanism changed: `watcher.py` now
polls (default every 2 s, `e_drive_poll_seconds`) for no folder matching
`{name}*` under `ReportData`. This covers delete, move, and the 30 ms race,
and removes the watchdog event handler from this path. The timeout
(`e_drive_deletion_timeout_seconds`, 600 s) is unchanged - the wait starts at
the NOTIFY, which fires after analysis completes, so it only needs to cover
the copy and cleanup.

Acting on the NOTIFY alone remains rejected, for the original reason: it
fires before the F: copy.

## Alternatives considered
- Poll F: drive on an interval — rejected because interval tuning is
  arbitrary and partial reads are possible if the interval is too short
- Act immediately on NOTIFY with no wait — rejected because race
  condition is certain to cause failures on slower copy operations
- Watch F: for file creation — rejected because file creation events
  fire before copy is complete; deletion from E: is a stronger signal
