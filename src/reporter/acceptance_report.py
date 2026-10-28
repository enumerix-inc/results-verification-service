"""
acceptance_report.py - reviewer-oriented report for aggregate (acceptance
criteria) results (ADR-026, TES-2877).

A person reads this report and the raw data and makes the release decision, so
it shows, for every criterion and channel: the value, the limits, pass/fail and
the margin to the nearest limit, plus the provenance needed to reproduce the
numbers. The "all criteria met" line is advisory only.

Written to {reports_dir}/{run_id}/ alongside the existing gold standard CSVs,
which are not touched:

  acceptance_report.md          readable summary: provenance, advisory line,
                                criteria x channels table, problems
  acceptance_summary.csv        one row per (experiment, criterion, channel)
  acceptance_observations.csv   the raw rows behind every value, one per
                                sample x channel, with the criteria that used
                                each row (`used_by`)

Tracing a value: a summary row names its groups and channel and how many raw
rows it used; filter acceptance_observations.csv on that experiment, those
groups and that channel (or on `used_by` containing the criterion id).
"""

import csv
import hashlib
import json
from collections import OrderedDict
from pathlib import Path

from src.comparator.acceptance_params import AcceptanceParamsError, parse_acceptance_params

SUMMARY_FIELDS = [
    "experiment_id", "feature_set", "test_case", "criterion_id", "metric", "channel",
    "groups", "value", "limits", "lower_bound", "lower_inclusive", "upper_bound",
    "upper_inclusive", "margin", "verdict", "rows_used", "notes", "annotation_version",
    "annotation_sha256", "source_file_sha256", "criteria_sha256", "pipeline_build", "run_id",
]
OBSERVATION_FIELDS = [
    "experiment_id", "sample_name", "sample_id", "strip_barcode", "carousel_position",
    "tube_position", "channel", "source_column", "count", "group", "role",
    "expected_count", "rule", "issues", "note", "used_by",
]
_ADVISORY = ("ADVISORY ONLY: this line is computed from the table below. The release "
             "decision is made by a reviewer reading this report and the raw data.")


def format_limits(row: dict) -> str:
    """Readable limits, e.g. '0.9 < x < 1.1', 'x < 0.1', '-0.2 <= x <= 0.2'."""
    lo, hi = row.get("lower_bound"), row.get("upper_bound")
    parts = []
    if lo is not None:
        parts.append(f"{lo:g} {'<=' if row.get('lower_inclusive') else '<'}")
    parts.append("x")
    if hi is not None:
        parts.append(f"{'<=' if row.get('upper_inclusive') else '<'} {hi:g}")
    return " ".join(parts) if (lo is not None or hi is not None) else ""


def _criteria_sha(comparison: dict) -> str:
    return hashlib.sha256(json.dumps(comparison, sort_keys=True).encode()).hexdigest()


def _acceptance_entry(exp_result):
    for c in exp_result.get("comparisons", []):
        if c.get("type") == "acceptance_criteria":
            return c
    return None


def _criteria_lookup(entry):
    """criterion_id -> parsed Criterion (empty when the entry is malformed)."""
    try:
        return {c.id: c for c in parse_acceptance_params(entry).criteria}
    except AcceptanceParamsError:
        return {}


def build_acceptance_rows(experiment_results, run_info=None):
    """Return (summary_rows, observation_rows, per_experiment) for the CSVs and
    the markdown. per_experiment is a list of dicts with the pieces the
    markdown needs."""
    run_info = run_info or {}
    summary, observations, per_exp = [], [], []
    for exp in experiment_results:
        aggregates = exp.get("aggregate_results") or []
        if not aggregates:
            continue
        entry = _acceptance_entry(exp) or {}
        criteria = _criteria_lookup(entry) if entry else {}
        obs_set = (exp.get("comparison") or {}).get("observation_set")
        obs = list(obs_set.observations) if obs_set else []
        csha = _criteria_sha(entry) if entry else ""

        used_by = {}      # (sample_name, channel) -> [criterion ids]
        rows_used = {}    # (criterion_id, channel) -> int
        for cid, c in criteria.items():
            groups = set(c.groups) | set(c.fit_groups)
            for o in obs:
                if o.group in groups and o.channel in c.channels:
                    used_by.setdefault((o.sample_name, o.channel), []).append(cid)
                    rows_used[(cid, o.channel)] = rows_used.get((cid, o.channel), 0) + 1

        exp_rows = []
        for a in aggregates:
            row = {
                "experiment_id": exp["experiment_id"], "feature_set": exp["feature_set"],
                "test_case": a.get("test_case") or "", "criterion_id": a.get("criterion_id") or "",
                "metric": a["metric"], "channel": a.get("channel") or "",
                "groups": a["group_key"],
                "value": "" if a["actual_value"] is None else repr(a["actual_value"]),
                "limits": format_limits(a),
                "lower_bound": "" if a.get("lower_bound") is None else a["lower_bound"],
                "lower_inclusive": "" if a.get("lower_inclusive") is None else a["lower_inclusive"],
                "upper_bound": "" if a.get("upper_bound") is None else a["upper_bound"],
                "upper_inclusive": "" if a.get("upper_inclusive") is None else a["upper_inclusive"],
                "margin": "" if a.get("margin") is None else repr(a["margin"]),
                "verdict": a["verdict"],
                "rows_used": rows_used.get((a.get("criterion_id"), a.get("channel")), ""),
                "notes": a.get("note") or "",
                "annotation_version": a.get("annotation_version") or "",
                "annotation_sha256": a.get("annotation_sha256") or "",
                "source_file_sha256": a.get("source_file_sha256") or "",
                "criteria_sha256": csha,
                "pipeline_build": run_info.get("pipeline_build", ""),
                "run_id": run_info.get("run_id", ""),
            }
            exp_rows.append(row)
        summary.extend(exp_rows)

        for o in obs:
            observations.append({
                "experiment_id": exp["experiment_id"], "sample_name": o.sample_name,
                "sample_id": o.sample_id, "strip_barcode": o.strip_barcode,
                "carousel_position": o.carousel_position, "tube_position": o.tube_position,
                "channel": o.channel, "source_column": o.column,
                "count": "" if o.count is None else o.count, "group": o.group, "role": o.role,
                "expected_count": "" if o.expected is None else o.expected,
                "rule": o.rule, "issues": o.issues, "note": o.note,
                "used_by": " ".join(used_by.get((o.sample_name, o.channel), [])),
            })
        per_exp.append({"experiment": exp, "rows": exp_rows, "criteria": criteria,
                        "obs_set": obs_set, "criteria_sha256": csha})
    return summary, observations, per_exp


def _num(x) -> str:
    return f"{float(x):.4g}"


def _cell(row) -> str:
    if row["verdict"] == "error":
        return "ERROR"
    m = float(row["margin"])
    return f"{_num(row['value'])} {row['verdict'].upper()} ({m:+.3g})"


def advisory_line(rows) -> str:
    total = len(rows)
    bad = [r for r in rows if r["verdict"] != "pass"]
    if not bad:
        return f"All {total} criterion checks met their limits (advisory)."
    f = sum(r["verdict"] == "fail" for r in bad)
    e = sum(r["verdict"] == "error" for r in bad)
    return (f"{len(bad)} of {total} criterion checks did NOT meet their limits "
            f"({f} outside limits, {e} could not be computed) (advisory).")


def render_markdown(run_id, run_info, per_exp) -> str:
    out = [f"# Acceptance criteria report - run {run_id}", ""]
    if run_info.get("pipeline_build"):
        out.append(f"Pipeline build: {run_info['pipeline_build']}  ")
    if run_info.get("scenario"):
        out.append(f"Scenario: {run_info['scenario']}  ")
    out += ["", f"_{_ADVISORY}_", "",
            "Ratios (plateauing ratio, CV) are fractions: 0.20 = 20%. Each cell is "
            "value, verdict, and margin to the nearest limit (negative = outside).", ""]
    for p in per_exp:
        exp, rows, obs_set = p["experiment"], p["rows"], p["obs_set"]
        out += [f"## {exp['feature_set']} ({exp['experiment_id']})", "",
                f"**{advisory_line(rows)}**", ""]
        first = rows[0]
        prov = [f"- Recorded experiment verdict: {exp.get('verdict', '')}",
                f"- Annotation: version {first['annotation_version'] or 'n/a'}, "
                f"SHA-256 {first['annotation_sha256'] or 'n/a'}",
                f"- Source file: {obs_set.source_file if obs_set else 'n/a'}",
                f"- Source file SHA-256: {first['source_file_sha256'] or 'n/a'}",
                f"- Criteria (manifest entry) SHA-256: {p['criteria_sha256'] or 'n/a'}"]
        if obs_set:
            prov.append("- Channel columns: " + ", ".join(f"{k} = {v}" for k, v in obs_set.columns.items()))
            prov.append("- Preprocessing filters: " + (", ".join(
                f"{f['name']} (removed {f['removed']})" for f in obs_set.filters) or "none"))
            prov.append(f"- Observations: {len(obs_set.observations)} "
                        f"({len(obs_set.sample_names)} samples)")
        out += prov + [""]

        channels = list(OrderedDict.fromkeys(r["channel"] for r in rows if r["channel"]))
        cases = list(OrderedDict.fromkeys(r["test_case"] for r in rows))
        for case in cases:
            case_rows = [r for r in rows if r["test_case"] == case]
            if len(cases) > 1 or case:
                out += [f"### Test case {case or '(unlabelled)'}", ""]
            out += ["| Criterion | Metric | Groups | Limits | " + " | ".join(channels) + " |",
                    "|---|---|---|---|" + "---|" * len(channels)]
            crits = list(OrderedDict.fromkeys(r["criterion_id"] for r in case_rows))
            for cid in crits:
                crow = [r for r in case_rows if r["criterion_id"] == cid]
                by_ch = {r["channel"]: r for r in crow}
                cells = [_cell(by_ch[ch]) if ch in by_ch else "-" for ch in channels]
                out.append(f"| {cid or '(setup)'} | {crow[0]['metric']} | {crow[0]['groups']} | "
                           f"`{crow[0]['limits']}` | " + " | ".join(cells) + " |")
            out.append("")
        problems = [r for r in rows if r["verdict"] != "pass"]
        if problems:
            out += ["### Problems", ""]
            for r in problems:
                what = "outside limits" if r["verdict"] == "fail" else "could not be computed"
                detail = r["notes"] or f"value {_num(r['value'])}, limits {r['limits']}"
                out.append(f"- {r['criterion_id'] or '(setup)'} {r['channel']}: {what} - {detail}")
            out.append("")
    out += ["## Raw data", "",
            "`acceptance_summary.csv` has one row per criterion and channel, with the "
            "number of raw rows used. `acceptance_observations.csv` has every raw row "
            "(sample x channel) with the annotation applied; its `used_by` column lists "
            "the criteria that used each row.", ""]
    return "\n".join(out)


def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_acceptance_report(reports_dir, run_id, experiment_results, run_info=None):
    """Write the markdown and CSVs. Returns the summary rows (empty when the
    run had no aggregate results, in which case nothing is written)."""
    run_info = dict(run_info or {})
    run_info.setdefault("run_id", run_id)
    summary, observations, per_exp = build_acceptance_rows(experiment_results, run_info)
    if not summary:
        return []
    reports_dir = Path(reports_dir)
    _write_csv(reports_dir / "acceptance_summary.csv", SUMMARY_FIELDS, summary)
    _write_csv(reports_dir / "acceptance_observations.csv", OBSERVATION_FIELDS, observations)
    (reports_dir / "acceptance_report.md").write_text(
        render_markdown(run_id, run_info, per_exp), encoding="utf-8")
    return summary
