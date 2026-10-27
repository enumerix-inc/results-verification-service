"""
validate.py - check an annotation file against a pipeline CountableDataSummary CSV.

Prints which rule matched each sample and lists unmatched / double-matched
samples, so annotation files can be authored interactively. Exit code 0 when
every sample matches exactly one rule, 1 otherwise.

Usage (from the repo root):
  python -m src.annotation.validate SUMMARY.csv --experiment-id MP37_V_V_Testcase_2_beta05
  python -m src.annotation.validate SUMMARY.csv --annotation path/to/file.yaml
Options:  --annotations-dir DIR   (default: paths.annotations_dir from config,
                                   else the repo's annotations/ folder)
          --config PATH
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from src.annotation.annotation import (AnnotationError, find_annotation,
                                       load_annotation)
from src.summary_csv import read_summary_csv

SAMPLE_NAME_COLUMN = "SampleName"
_REPO_ANNOTATIONS = Path(__file__).resolve().parents[2] / "annotations"


def default_annotations_dir(config_path=None) -> Path:
    from src.config_loader import load_config
    try:
        configured = load_config(config_path).get("paths", {}).get("annotations_dir")
    except FileNotFoundError:
        configured = None
    return Path(configured) if configured and Path(configured).is_dir() else _REPO_ANNOTATIONS


def check(annotation, sample_names):
    """Return (rows, problems): rows = (sample, [rule names]) in file order."""
    rows = [(n, [r.name for r in annotation.match(n)]) for n in sample_names]
    problems = [f"{n}: matched no rule" if not hits else
                f"{n}: matched {len(hits)} rules ({', '.join(hits)})"
                for n, hits in rows if len(hits) != 1]
    names = Counter(sample_names)
    problems += [f"{n}: appears {c} times" for n, c in names.items() if c > 1]
    return rows, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("summary_csv")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--experiment-id")
    grp.add_argument("--annotation")
    ap.add_argument("--annotations-dir")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    try:
        if args.annotation:
            path = Path(args.annotation)
        else:
            adir = args.annotations_dir or default_annotations_dir(args.config)
            path = find_annotation(adir, args.experiment_id)
        ann = load_annotation(path)
    except (AnnotationError, FileNotFoundError) as e:
        print(f"ERROR: {e}")
        return 1

    records = read_summary_csv(args.summary_csv)
    if not records or SAMPLE_NAME_COLUMN not in records[0]:
        print(f"ERROR: no '{SAMPLE_NAME_COLUMN}' column in {args.summary_csv}")
        return 1
    names = [r[SAMPLE_NAME_COLUMN] for r in records]

    rows, problems = check(ann, names)
    print(f"Annotation: {path}\n  experiment_id={ann.experiment_id} "
          f"version={ann.annotation_version} sha256={ann.sha256}")
    print(f"Samples: {len(names)}\n")
    width = max(len(n) for n, _ in rows)
    for n, hits in rows:
        print(f"  {n:<{width}}  ->  {', '.join(hits) if hits else '*** NO MATCH ***'}")
    print("\nRule usage: " + ", ".join(
        f"{r.name}={sum(1 for _, h in rows if h == [r.name])}" for r in ann.rules))
    if not problems:
        try:  # also exercise level resolution
            ann.annotate(names)
        except AnnotationError as e:
            problems = str(e).splitlines()[1:]
    if problems:
        print(f"\nFAILED: {len(problems)} problem(s)")
        for p in problems:
            print(f"  {p.strip()}")
        return 1
    print("\nOK: every sample matches exactly one rule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
