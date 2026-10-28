"""
canary.py - determinism canary (ADR-026, TES-2879).

As of IAP v3.0 reanalysis is deterministic: the same images analysed by the
same IAP build give the same counts. This compares two stored results of the
same experiment and asserts every observation and every aggregate value is
identical. If it ever fails, the determinism assumption behind "a failing
criterion is a real failure, not noise" (and behind caching results) no longer
holds and needs looking at.

It only means something within ONE build, so two results from different
pipeline builds are refused unless --allow-different-builds is given.

Usage (from the repo root):
  python -m src.validation.canary --experiment-id MP37_V_V_Testcase_2_beta05
  python -m src.validation.canary --experiment-id ... --run-a run_20261029_001 --run-b run_20261029_002
  options: --db PATH (default: paths.database from the config), --config PATH,
           --allow-different-builds

Exit code: 0 identical, 1 differences found, 2 cannot be run (fewer than two
results, builds differ, no stored observations).
"""

import argparse
import sys

from src.database.db import get_connection
from src.database.models import (get_aggregate_results, get_result_observations,
                                 list_experiment_results)


class CanaryReport:
    def __init__(self):
        self.problems = []            # human-readable differences
        self.observations_compared = 0
        self.aggregates_compared = 0

    @property
    def identical(self) -> bool:
        return not self.problems


def compare_results(conn, result_a: str, result_b: str) -> CanaryReport:
    """Compare the stored observations and aggregate results of two results."""
    rep = CanaryReport()
    obs = {}
    for tag, rid in (("A", result_a), ("B", result_b)):
        obs[tag] = {(o["sample_name"], o["channel"]): o
                    for o in get_result_observations(conn, rid)}
    keys = sorted(set(obs["A"]) | set(obs["B"]))
    for key in keys:
        a, b = obs["A"].get(key), obs["B"].get(key)
        if a is None or b is None:
            rep.problems.append(f"observation {key[0]} {key[1]}: only in "
                                f"{'B' if a is None else 'A'}")
            continue
        rep.observations_compared += 1
        for field in ("count_value", "group_name", "expected_count", "role", "source_column"):
            if a[field] != b[field]:
                rep.problems.append(f"observation {key[0]} {key[1]}: {field} "
                                    f"{a[field]!r} (A) != {b[field]!r} (B)")

    agg = {}
    for tag, rid in (("A", result_a), ("B", result_b)):
        agg[tag] = {(r["criterion_id"], r["channel"]): r for r in get_aggregate_results(conn, rid)}
    for key in sorted(set(agg["A"]) | set(agg["B"]), key=str):
        a, b = agg["A"].get(key), agg["B"].get(key)
        if a is None or b is None:
            rep.problems.append(f"aggregate {key[0]} {key[1]}: only in {'B' if a is None else 'A'}")
            continue
        rep.aggregates_compared += 1
        for field in ("actual_value", "verdict", "lower_bound", "upper_bound", "margin"):
            if a[field] != b[field]:
                rep.problems.append(f"aggregate {key[0]} {key[1]}: {field} "
                                    f"{a[field]!r} (A) != {b[field]!r} (B)")
    return rep


def pick_results(conn, experiment_id, run_a=None, run_b=None):
    """Return (result_a, result_b) dicts: the named runs, or the two most recent."""
    results = list_experiment_results(conn, experiment_id)
    if run_a or run_b:
        by_run = {r["run_id"]: r for r in results}
        missing = [r for r in (run_a, run_b) if r not in by_run]
        if not run_a or not run_b or missing:
            raise ValueError(f"need both --run-a and --run-b with stored results for "
                             f"'{experiment_id}'; not found: {missing or 'one of them was not given'}")
        return by_run[run_a], by_run[run_b]
    if len(results) < 2:
        raise ValueError(f"need at least two stored results for '{experiment_id}', "
                         f"found {len(results)}")
    return results[-2], results[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--experiment-id", required=True)
    ap.add_argument("--run-a")
    ap.add_argument("--run-b")
    ap.add_argument("--db")
    ap.add_argument("--config")
    ap.add_argument("--allow-different-builds", action="store_true")
    args = ap.parse_args(argv)

    db = args.db
    if not db:
        from src.config_loader import load_config
        db = load_config(args.config)["paths"]["database"]
    conn = get_connection(db)
    try:
        a, b = pick_results(conn, args.experiment_id, args.run_a, args.run_b)
    except ValueError as e:
        print(f"CANNOT RUN: {e}")
        return 2
    print(f"A: {a['run_id']} (build {a['pipeline_build']}, verified {a['verified_at']})")
    print(f"B: {b['run_id']} (build {b['pipeline_build']}, verified {b['verified_at']})")
    if a["pipeline_build"] != b["pipeline_build"] and not args.allow_different_builds:
        print("CANNOT RUN: the builds differ. Determinism holds within one IAP build only; "
              "pass --allow-different-builds to compare anyway.")
        return 2
    rep = compare_results(conn, a["result_id"], b["result_id"])
    if rep.observations_compared == 0:
        print("CANNOT RUN: no stored observations to compare (was an acceptance "
              "experiment run for both?)")
        return 2
    print(f"Compared {rep.observations_compared} observations and "
          f"{rep.aggregates_compared} aggregate results.")
    if rep.identical:
        print("PASS: identical. Determinism holds for this build.")
        return 0
    print(f"FAIL: {len(rep.problems)} difference(s):")
    for p in rep.problems[:50]:
        print(f"  {p}")
    if len(rep.problems) > 50:
        print(f"  ... and {len(rep.problems) - 50} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
