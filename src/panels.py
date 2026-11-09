"""
panels.py - expand dual-panel assays into one experiment per panel (TES-2990).

Some assays (KMT2A today) are one workbook with a PanelName per sample. The
app splits that workbook automatically into one experiment per panel, named
"{experiment_id}_{panel_name}" (WorkbookPanelSplitter), so a single replay
produces one report and one notification per panel.

The manifest keeps ONE entry for such an assay, because the test harness must
replay the workbook once, and declares its panels:

    {
      "experiment_id": "KMT2A_E2E",
      "image_source": "...", "sample_workbook": "...",
      "comparisons": [...],                  # default for every panel
      "panels": {
        "PanelA": {"comparisons": [...]},    # per-panel override
        "PanelB": {}                         # inherits the entry's comparisons
      }
    }

expand_panels() turns that entry into one ordinary experiment per panel
("KMT2A_E2E_PanelA", "KMT2A_E2E_PanelB"). It runs once, where the service
loads a manifest, so the gate, comparator, reporter and completion logic only
ever see ordinary experiments. The manifest file on disk is never modified,
and the harness (which ignores unknown fields) still sees the single entry.

Kept free of database imports so the stamping script can use it too.
"""

import copy

# Fields a panel may override; everything else comes from the entry itself.
# (counts_for_build_verdict is deliberately not here: nothing in the service reads
# it, the build verdict is decided by `classification`; allowing a per-panel value
# would suggest an effect it does not have.)
PANEL_OVERRIDABLE = ("comparisons", "result_file_suffix", "classification")


class PanelError(ValueError):
    """A manifest 'panels' declaration that cannot be expanded."""


def panel_experiment_id(experiment_id: str, panel: str) -> str:
    """The id the app gives a panel's experiment (before the pipeline's timestamp)."""
    return f"{experiment_id}_{panel}"


def _expand_entry(entry: dict) -> list[dict]:
    where = entry.get("experiment_id", "<no experiment_id>")
    panels = entry["panels"]
    if not isinstance(panels, dict) or not panels:
        raise PanelError(f"{where}: 'panels' must be a non-empty object of "
                         f"panel name -> overrides")

    expanded = []
    for name, overrides in panels.items():
        if not isinstance(name, str) or not name.strip():
            raise PanelError(f"{where}: panel names must be non-empty strings")
        if not isinstance(overrides, dict):
            raise PanelError(f"{where}: panel '{name}' must be an object "
                             f"(use {{}} to inherit everything)")
        unknown = sorted(set(overrides) - set(PANEL_OVERRIDABLE))
        if unknown:
            raise PanelError(f"{where}: panel '{name}' overrides unsupported "
                             f"field(s) {unknown}; allowed: {list(PANEL_OVERRIDABLE)}")

        exp = copy.deepcopy({k: v for k, v in entry.items() if k != "panels"})
        exp.update(copy.deepcopy(overrides))
        comparisons = exp.get("comparisons")
        if comparisons is None or comparisons == []:
            raise PanelError(f"{where}: panel '{name}' has no comparisons (declare "
                             f"them on the entry or on the panel)")
        if (not isinstance(comparisons, list)
                or not all(isinstance(c, dict) and isinstance(c.get("type"), str) and c["type"]
                           for c in comparisons)):
            raise PanelError(f"{where}: panel '{name}' comparisons must be a non-empty list "
                             f"of objects, each with a 'type'")
        exp["experiment_id"] = panel_experiment_id(entry["experiment_id"], name)
        exp["parent_experiment_id"] = entry["experiment_id"]
        exp["panel"] = name
        expanded.append(exp)
    return expanded


def expand_panels(manifest):
    """
    Return a copy of the manifest with every entry that declares 'panels'
    replaced, in place and in order, by one experiment per panel. A manifest
    without panels comes back unchanged. Raises PanelError (a ValueError) for
    a bad declaration or when expansion produces duplicate experiment ids.
    Shapes this module does not own (non-dict manifest, no experiments list)
    are returned as-is for the caller's own validation to reject.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("experiments"), list):
        return manifest
    if not any(isinstance(e, dict) and "panels" in e for e in manifest["experiments"]):
        return manifest

    experiments, seen = [], set()
    for entry in manifest["experiments"]:
        if isinstance(entry, dict) and "panels" in entry:
            if not isinstance(entry.get("experiment_id"), str):
                raise PanelError("an entry with 'panels' has no string experiment_id")
            produced = _expand_entry(entry)
        else:
            produced = [entry]
        for exp in produced:
            eid = exp.get("experiment_id") if isinstance(exp, dict) else None
            if isinstance(eid, str):
                if eid in seen:
                    raise PanelError(f"experiment id '{eid}' appears more than once "
                                     f"after expanding panels")
                seen.add(eid)
        experiments.extend(produced)

    expanded = dict(manifest)
    expanded["experiments"] = experiments
    return expanded


def validate_panels(experiments: list[dict]) -> list[str]:
    """Problems with any 'panels' declarations in a list of experiments
    (empty = ok); used by the stamping script to fail before a long run."""
    try:
        expand_panels({"experiments": experiments})
    except PanelError as e:
        return [str(e)]
    return []
