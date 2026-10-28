"""
workbook_check.py - compare a stored acceptance result with the UMA linearity
workbook values (ADR-026, TES-2879).

Checks, for the MP37 4-plex samples, that (1) every stored count equals the
workbook's count and (2) every stored criterion value (linearity exponent,
linear slope, plateauing ratio, CVs, NTC mean) equals the workbook's value for
all four channels. The workbook values live in tests/data/uma_linearity_golden.json.

The workbook data comes from an earlier software version (2.0.0 summary). A new
reanalysis with a different IAP build may legitimately give slightly different
counts; the report then shows exactly which counts and values moved, and by how
much, so that can be judged. Counts are compared exactly by default.

Usage (from the repo root):
  python -m src.validation.workbook_check --run-id run_20261029_001
  options: --experiment-id (default MP37_V_V_Testcase_2_beta05), --db PATH,
           --config PATH, --golden PATH, --count-tolerance N (default 0),
           --value-tolerance X (default 1e-6)

Exit code: 0 everything within tolerance, 1 differences, 2 cannot be run.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

from src.database.db import get_connection
from src.database.models import (get_aggregate_results, get_result_observations,
                                 list_experiment_results)

DEFAULT_GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "data" / "uma_linearity_golden.json"
LEVELS = ["0", "10", "100", "1k", "10k", "100k", "1M", "1p5M"]
CV_LEVELS = {"1k": 3, "10k": 4, "100k": 5, "1M": 6}


def golden_counts(golden):
    """{(sample_name, channel): count} for the 64 four-plex samples."""
    out = {}
    for ch, g in golden["channels"].items():
        for li, lv in enumerate(LEVELS):
            for rep, count in enumerate(g["replicates"][li]):
                out[(f"FourPlex_{lv}_{rep + 1}", ch)] = count
    return out


def golden_values(golden):
    """{(criterion_id, channel): value} using the manifest template's criterion ids."""
    out = {}
    for ch, g in golden["channels"].items():
        out[("linearity_exponent", ch)] = g["linearity_exponent"]
        out[("linear_slope", ch)] = g["linear_slope"]
        out[("plateauing_ratio_1M", ch)] = g["plateau_ratio_1M"]
        for lv, i in CV_LEVELS.items():
            out[(f"cv_{lv}", ch)] = g["cv"][i]
        out[("ntc_mean_counts", ch)] = statistics.fmean(g["replicates"][0])
    return out


def check(conn, result_id, golden, count_tolerance=0, value_tolerance=1e-6):
    """Return a dict with count_diffs, value_diffs and tallies."""
    obs = {(o["sample_name"], o["channel"]): o["count_value"]
           for o in get_result_observations(conn, result_id)}
    count_diffs, compared = [], 0
    for key, expected in golden_counts(golden).items():
        actual = obs.get(key)
        if actual is None:
            count_diffs.append((key, expected, None, None))
            continue
        compared += 1
        if abs(actual - expected) > count_tolerance:
            count_diffs.append((key, expected, actual, actual - expected))

    agg = {(r["criterion_id"], r["channel"]): r for r in get_aggregate_results(conn, result_id)}
    value_diffs, values_compared = [], 0
    for key, expected in golden_values(golden).items():
        row = agg.get(key)
        if row is None or row["actual_value"] is None:
            value_diffs.append((key, expected, None, row["notes"] if row else "no result row"))
            continue
        values_compared += 1
        if abs(row["actual_value"] - expected) > value_tolerance:
            value_diffs.append((key, expected, row["actual_value"], row["actual_value"] - expected))
    verdicts = {}
    for r in agg.values():
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    return {"counts_compared": compared, "count_diffs": count_diffs,
            "values_compared": values_compared, "value_diffs": value_diffs,
            "verdicts": verdicts}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--experiment-id", default="MP37_V_V_Testcase_2_beta05")
    ap.add_argument("--db")
    ap.add_argument("--config")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    ap.add_argument("--count-tolerance", type=float, default=0)
    ap.add_argument("--value-tolerance", type=float, default=1e-6)
    args = ap.parse_args(argv)

    db = args.db
    if not db:
        from src.config_loader import load_config
        db = load_config(args.config)["paths"]["database"]
    conn = get_connection(db)
    matches = [r for r in list_experiment_results(conn, args.experiment_id)
               if r["run_id"] == args.run_id]
    if not matches:
        print(f"CANNOT RUN: no stored result for {args.experiment_id} in {args.run_id}")
        return 2
    result = matches[-1]
    golden = json.loads(Path(args.golden).read_text())
    rep = check(conn, result["result_id"], golden, args.count_tolerance, args.value_tolerance)
    if rep["counts_compared"] == 0:
        print("CANNOT RUN: the result has no stored observations for the MP37 four-plex samples")
        return 2

    print(f"Result: {args.run_id} / {args.experiment_id} (build {result['pipeline_build']}, "
          f"recorded verdict {result['verdict']})")
    print(f"Service verdicts: {rep['verdicts']}")
    print(f"Counts compared: {rep['counts_compared']} of 256; differing: {len(rep['count_diffs'])} "
          f"(tolerance {args.count_tolerance:g})")
    for (sample, ch), exp, act, d in rep["count_diffs"][:40]:
        shown = act if act is None or not float(act).is_integer() else int(act)
        print(f"  {sample} {ch}: workbook {exp}, service {shown}"
              + ("" if d is None else f" (diff {d:+g})"))
    if len(rep["count_diffs"]) > 40:
        print(f"  ... and {len(rep['count_diffs']) - 40} more")
    print(f"Criterion values compared: {rep['values_compared']} of 32; differing: "
          f"{len(rep['value_diffs'])} (tolerance {args.value_tolerance:g})")
    for (cid, ch), exp, act, d in rep["value_diffs"]:
        print(f"  {cid} {ch}: workbook {exp:.9g}, service "
              f"{'n/a' if act is None else format(act, '.9g')}"
              + (f" (diff {d:+.3g})" if isinstance(d, float) else (f" - {d}" if d else "")))
    ok = not rep["count_diffs"] and not rep["value_diffs"]
    print("PASS: matches the workbook." if ok else
          "DIFFERENCES FOUND: see above. If the IAP build differs from the one that produced "
          "the workbook data, small differences can be legitimate; judge them with the criteria margins.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
