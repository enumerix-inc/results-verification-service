"""
workbook_check.py - cross-check manifest entries against their sample workbooks (TES-2991).

The pipeline names a reanalysis experiment from the workbook's
RunInfo.ExperimentId (and each panel experiment "{ExperimentId}_{PanelName}"),
NOT from the manifest. The service matches notifications and gold standards on
the manifest's experiment_id, so an entry whose experiment_id or panels differ
from its workbook produces a run that waits for notifications that never match,
and a stuck run only found hours later. Stamping calls this to fail in seconds
instead.

Checked per entry that has a readable sample_workbook:
  - experiment_id == the workbook's RunInfo.ExperimentId
  - the declared `panels` keys == the set of PanelName values in the workbook
    (an entry without `panels` must be matched by a workbook without any)

The workbook is read as JSON, so an escaped "+" is a plain "+". Kept free
of service imports (stdlib only) so the stamping script can use it standalone.
"""

import json
from pathlib import Path


class WorkbookReadError(ValueError):
    """A sample workbook that cannot be read or does not look like one."""


def read_workbook_facts(path) -> tuple[str, frozenset]:
    """Return (RunInfo.ExperimentId, set of non-blank PanelName values)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise WorkbookReadError(f"cannot read workbook {path}: {e}") from e
    run_info = data.get("RunInfo") if isinstance(data, dict) else None
    experiment_id = run_info.get("ExperimentId") if isinstance(run_info, dict) else None
    if not isinstance(experiment_id, str) or not experiment_id:
        raise WorkbookReadError(f"workbook {path} has no RunInfo.ExperimentId")
    strips = data.get("Strips")
    if strips is None:
        strips = []
    if not isinstance(strips, list):
        raise WorkbookReadError(f"workbook {path}: 'Strips' must be a list")
    panels = set()
    for strip in strips:
        if not isinstance(strip, dict):
            raise WorkbookReadError(f"workbook {path}: every entry of 'Strips' must be an object")
        tubes = strip.get("Tubes")
        if tubes is None:
            tubes = []
        if not isinstance(tubes, list):
            raise WorkbookReadError(f"workbook {path}: a strip's 'Tubes' must be a list")
        for tube in tubes:
            if not isinstance(tube, dict):
                raise WorkbookReadError(f"workbook {path}: every entry of 'Tubes' must be an object")
            name = tube.get("PanelName")
            if isinstance(name, str) and name.strip():
                panels.add(name)
    return experiment_id, frozenset(panels)


def check_entry(experiment: dict, workbook_experiment_id: str, workbook_panels) -> list[str]:
    """Problems (empty = ok) comparing one manifest entry with its workbook's facts."""
    eid = experiment.get("experiment_id")
    where = f"{eid} ({experiment.get('sample_workbook')})"
    problems = []
    if eid != workbook_experiment_id:
        problems.append(
            f"{where}: experiment_id does not match the workbook's RunInfo.ExperimentId "
            f"'{workbook_experiment_id}'. The pipeline names the experiment from the "
            f"workbook, so the service would never match its notifications")
    declared = set(experiment["panels"]) if isinstance(experiment.get("panels"), dict) else set()
    if declared != set(workbook_panels):
        if workbook_panels and not declared:
            problems.append(
                f"{where}: the workbook defines panels {sorted(workbook_panels)} (the app splits "
                f"it into one experiment per panel) but the entry declares no 'panels'")
        else:
            problems.append(
                f"{where}: declared panels {sorted(declared)} do not match the "
                f"workbook's panels {sorted(workbook_panels)}")
    return problems


def check_workbooks(experiments: list[dict], require: bool = False) -> tuple[list[str], list[str]]:
    """
    Cross-check every entry that names a sample_workbook. Returns (problems, warnings).
    A workbook that does not exist is a warning (this machine may not have the data
    drive, e.g. a dev checkout) unless require=True, which makes it a problem: on the
    regression machine a missing workbook means the harness fails later and leaves a
    run to clean up by hand.
    """
    problems, warnings = [], []
    for exp in experiments:
        path = exp.get("sample_workbook")
        if not path:
            continue
        if not Path(path).is_file():
            msg = f"{exp.get('experiment_id')}: sample workbook not found: {path}"
            (problems if require else warnings).append(msg)
            continue
        try:
            workbook_id, panels = read_workbook_facts(path)
        except WorkbookReadError as e:
            problems.append(f"{exp.get('experiment_id')}: {e}")
            continue
        problems += check_entry(exp, workbook_id, panels)
    return problems, warnings
