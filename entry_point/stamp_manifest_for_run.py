"""
stamp_manifest_for_run.py — the entry point's "stamp" step (ADR-023).

Takes the stable, committed manifest_template.json (build_verdict_policy +
experiments — the parts that don't change run to run) and produces a new,
fully-formed context_manifest_{run_id}.json in the live manifests_dir,
filling in the "run" block with per-run metadata.

This is deliberately dumb: it does not build anything, does not install
anything, and does not talk to the pipeline or the verification service.
It only computes a run_id and writes one file — see ADR-015 (decoupled
entry point) and ADR-023 (GitHub Actions trigger).

Usage:
    python stamp_manifest_for_run.py --pipeline-build "3.0.0.2977" \\
        [--scenario new_build_regression_baseline] [--run-type Reanalysis] \\
        [--template PATH] [--also-template PATH ...]

--also-template adds the experiments of further templates to the stamped
manifest (e.g. the acceptance criteria experiment, manifest_template_acceptance.json,
alongside the gold standard experiments). Experiment ids must be unique across
the templates. The build_verdict_policy comes from --template.

Before anything is written, acceptance_criteria entries are validated (shape,
annotation file found and loadable, channels match the annotation), so a typo
fails here in seconds instead of after hours of reanalysis.

Run standalone (no GitHub Actions) for a manual/local stamp — it only
needs pyyaml, not the full service's runtime dependencies.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Standalone imports from the repo (stdlib + pyyaml only; no service runtime deps).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.annotation.annotation import (AnnotationError, find_annotation,  # noqa: E402
                                       load_annotation, resolve_annotations_dir)
from src.comparator.acceptance_params import (AcceptanceParamsError,  # noqa: E402
                                              parse_acceptance_params)

RUN_ID_RE = re.compile(r"^context_manifest_run_(\d{8})_(\d{3})\.json$")


def load_config() -> dict:
    """
    Minimal, standalone copy of src.main.load_config().

    Deliberately duplicated rather than imported: importing src.main pulls
    in asyncpg and watchdog (the full service's runtime deps) just to read
    a YAML file. This script only needs the paths block.
    """
    config_dir = Path(__file__).parent.parent / "config"
    local = config_dir / "local_config.yaml"
    default = config_dir / "config.yaml"
    config_path = local if local.exists() else default
    with open(config_path) as f:
        return yaml.safe_load(f)


def merge_experiments(templates: list[tuple[str, dict]]) -> list[dict]:
    """Concatenate the experiments of several templates; ids must be unique."""
    experiments, seen = [], {}
    for name, template in templates:
        for exp in template["experiments"]:
            eid = exp["experiment_id"]
            if eid in seen:
                raise ValueError(f"experiment '{eid}' appears in both {seen[eid]} and {name}")
            seen[eid] = name
            experiments.append(exp)
    return experiments


def validate_acceptance_experiments(experiments: list[dict], annotations_dir=None) -> list[str]:
    """Return a list of problems with any acceptance_criteria entries (empty = ok)."""
    problems = []
    for exp in experiments:
        entries = [c for c in exp.get("comparisons", []) if c.get("type") == "acceptance_criteria"]
        if len(entries) > 1:
            problems.append(f"{exp['experiment_id']}: more than one acceptance_criteria entry; "
                            f"put all criteria in one entry")
        for entry in entries:
            where = f"{exp['experiment_id']}"
            try:
                params = parse_acceptance_params(entry)
                annotation = load_annotation(find_annotation(
                    resolve_annotations_dir(annotations_dir),
                    params.annotation or exp["experiment_id"]))
            except (AcceptanceParamsError, AnnotationError, FileNotFoundError) as e:
                problems.append(f"{where}: {e}")
                continue
            if set(params.columns) != set(annotation.channels):
                problems.append(f"{where}: columns {sorted(params.columns)} do not match the "
                                f"annotation's channels {sorted(annotation.channels)}")
    return problems


def compute_triggered_by() -> str:
    """Identify who/what triggered this run for the manifest's audit trail."""
    actor = os.environ.get("GITHUB_ACTOR")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if actor and run_id:
        return f"github_actions:{actor}:run_{run_id}"
    return "manual"


def next_run_id(manifests_dir: Path, today: str) -> str:
    """
    Compute the next run_YYYYMMDD_NNN for today, scanning every place a
    manifest for today could be sitting: the live top level, and each
    archive subfolder (a manifest keeps its original filename when
    archived — see main.py's _archive()). NNN is per-day and only ever
    increases; it does not reuse numbers from archived runs.
    """
    search_dirs = [
        manifests_dir,
        manifests_dir / "processed",
        manifests_dir / "timed_out",
        manifests_dir / "failed",
    ]

    highest_seq = 0
    for d in search_dirs:
        if not d.is_dir():
            continue
        for path in d.glob(f"context_manifest_run_{today}_*.json"):
            m = RUN_ID_RE.match(path.name)
            if m and m.group(1) == today:
                highest_seq = max(highest_seq, int(m.group(2)))

    return f"run_{today}_{highest_seq + 1:03d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-build", required=True,
                         help="Build identifier of the IAP/pipeline build "
                              "currently installed on this machine, e.g. "
                              "'3.0.0.2977'. Recorded as-is — this script "
                              "does not verify it against what's actually "
                              "installed.")
    parser.add_argument("--scenario", default="new_build_regression_baseline")
    parser.add_argument("--run-type", choices=["Imaging", "Reanalysis"],
                         default="Reanalysis")
    parser.add_argument("--template",
                         default=str(Path(__file__).parent / "manifest_template.json"),
                         help="Path to the stable manifest template.")
    parser.add_argument("--also-template", action="append", default=[],
                         help="Path to another template whose experiments are added "
                              "to the manifest (repeatable), e.g. "
                              "manifest_template_acceptance.json.")
    parser.add_argument("--triggered-by", default=None,
                         help="Override the computed triggered_by value.")
    args = parser.parse_args()

    config = load_config()
    manifests_dir = Path(config["paths"]["manifests_dir"])
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # Safety check (ADR-023): refuse to stamp a new manifest while an
    # unarchived one is still sitting at the top level. Non-recursive glob,
    # so processed/timed_out/failed are correctly excluded — only a manifest
    # the service hasn't finished with yet (or a crash left behind) will
    # match here.
    # Same prefix/suffix glob as manifest_watcher.py's MANIFEST_PREFIX/
    # MANIFEST_SUFFIX (non-recursive, so processed/timed_out/failed are
    # still correctly excluded) — not narrowed to "run_" names. The live
    # service registers *any* context_manifest_*.json it finds at the top
    # level, so a stale or malformed file that doesn't match the run_id
    # naming convention (e.g. a hand-dropped context_manifest_old.json)
    # would otherwise slip past this check and defeat the collision
    # safeguard entirely (PR review, session 15).
    pending = sorted(manifests_dir.glob("context_manifest_*.json"))
    if pending:
        print("Error: refusing to stamp a new manifest — the following "
              "manifest(s) are still unarchived in "
              f"{manifests_dir}:", file=sys.stderr)
        for p in pending:
            print(f"  {p.name}", file=sys.stderr)
        print("\nThis usually means a prior run is still in flight, or "
              "didn't finish cleanly (crashed / service was down / E: "
              "drive watch timed out without the timeout firing yet). "
              "Confirm the prior run is really done — check the "
              "verification service's own state — before re-running this "
              "workflow. If it's genuinely stuck, move the file into "
              "processed/ or timed_out/ by hand once you've confirmed why.",
              file=sys.stderr)
        return 1

    template_path = Path(args.template)
    template = json.loads(template_path.read_text())
    try:
        experiments = merge_experiments(
            [(template_path.name, template)]
            + [(Path(p).name, json.loads(Path(p).read_text())) for p in args.also_template])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    problems = validate_acceptance_experiments(experiments, config["paths"].get("annotations_dir"))
    if problems:
        print("Error: refusing to stamp - the acceptance criteria entries are invalid:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    today = datetime.now().strftime("%Y%m%d")
    run_id = next_run_id(manifests_dir, today)

    manifest = {
        "run": {
            "run_id": run_id,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "triggered_by": args.triggered_by or compute_triggered_by(),
            "pipeline_build": args.pipeline_build,
            "scenario": args.scenario,
            "run_type": args.run_type,
        },
        "build_verdict_policy": template["build_verdict_policy"],
        "experiments": experiments,
    }

    manifest_path = manifests_dir / f"context_manifest_{run_id}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print(f"Stamped manifest: {manifest_path}")
    print(f"  run_id:         {run_id}")
    print(f"  pipeline_build: {args.pipeline_build}")
    print(f"  scenario:       {args.scenario}")
    print(f"  run_type:       {args.run_type}")
    print(f"  triggered_by:   {manifest['run']['triggered_by']}")
    print(f"  experiments:    {[e['experiment_id'] for e in manifest['experiments']]}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"run_id={run_id}\n")
            f.write(f"manifest_path={manifest_path}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
