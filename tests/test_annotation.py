"""
Tests for src/annotation (ADR-026, TES-2871).
"""

from pathlib import Path

import pytest

from src.annotation.annotation import (ROLE_NTC, ROLE_STANDARD, AnnotationError,
                                       find_annotation, load_annotation,
                                       parse_annotation)
from src.annotation import validate

REPO = Path(__file__).resolve().parents[1]
MP37 = REPO / "annotations" / "MP37_V_V_Testcase_2_beta05.yaml"
LEVELS = ["0", "10", "100", "1k", "10k", "100k", "1M", "1p5M"]


def mp37_sample_names():
    names = [f"FourPlex_{lv}_{r}" for lv in LEVELS for r in range(1, 9)]
    names += [f"Ch{c}_100k_{r}" for c in range(1, 5) for r in range(1, 9)]
    return names


MINI = """
experiment_id: EXP
annotation_version: 3
channels: [A, B]
levels: {"0": 0, "1k": 1000}
rules:
  - name: both
    pattern: 'Both_(?P<level>\\w+)_(?P<rep>\\d+)'
    group: "Both_{level}"
    expected: {A: "{level}", B: "{level}"}
  - name: only_a
    pattern: 'OnlyA_(?P<rep>\\d+)'
    group: "OnlyA"
    expected: {A: 500, B: null}
"""


@pytest.fixture
def mini():
    return parse_annotation(MINI)


# ---- MP37 file -------------------------------------------------------------

def test_mp37_covers_all_96_samples():
    ann = load_annotation(MP37)
    out = ann.annotate(mp37_sample_names())
    assert len(out) == 96
    assert ann.experiment_id == "MP37_V_V_Testcase_2_beta05"


def test_mp37_fourplex_expected_and_roles():
    by = {a.sample_name: a for a in load_annotation(MP37).annotate(mp37_sample_names())}
    s = by["FourPlex_1k_3"]
    assert s.expected == {"Ch01": 1000, "Ch02": 1000, "Ch03": 1000, "Ch04": 1000}
    assert s.group == "FourPlex_1k" and s.role == ROLE_STANDARD
    assert by["FourPlex_1p5M_1"].expected["Ch02"] == 1500000
    assert by["FourPlex_0_8"].role == ROLE_NTC
    assert by["FourPlex_0_8"].expected["Ch01"] == 0


def test_mp37_singleplex_has_single_assay():
    by = {a.sample_name: a for a in load_annotation(MP37).annotate(mp37_sample_names())}
    assert by["Ch3_100k_2"].expected == {"Ch01": None, "Ch02": None,
                                         "Ch03": 100000, "Ch04": None}
    assert by["Ch3_100k_2"].role == ROLE_STANDARD


def test_mp37_found_by_experiment_id():
    path = find_annotation(REPO / "annotations", "MP37_V_V_Testcase_2_beta05")
    assert path == MP37


# ---- strict validation -----------------------------------------------------

def test_unmatched_sample_fails(mini):
    with pytest.raises(AnnotationError, match=r"Mystery_1: matched no rule"):
        mini.annotate(["Both_1k_1", "Mystery_1"])


def test_double_matched_sample_fails():
    text = MINI + """
  - name: greedy
    pattern: 'Both_.*'
    group: "G"
    expected: {A: 1, B: 1}
"""
    ann = parse_annotation(text)
    with pytest.raises(AnnotationError, match=r"matched 2 rules \(both, greedy\)"):
        ann.annotate(["Both_1k_1"])


def test_all_problems_reported_together(mini):
    with pytest.raises(AnnotationError) as e:
        mini.annotate(["X_1", "Y_1", "Both_1k_1"])
    assert "2 problem(s)" in str(e.value)


def test_duplicate_sample_name_fails(mini):
    with pytest.raises(AnnotationError, match="duplicate"):
        mini.annotate(["Both_1k_1", "Both_1k_1"])


def test_partial_match_does_not_count(mini):
    with pytest.raises(AnnotationError, match="matched no rule"):
        mini.annotate(["xBoth_1k_1"])


# ---- null vs 0, levels ------------------------------------------------------

def test_null_and_zero_are_distinct(mini):
    ntc, single = mini.annotate(["Both_0_1", "OnlyA_1"])
    assert ntc.expected == {"A": 0, "B": 0} and ntc.role == ROLE_NTC
    assert single.expected == {"A": 500, "B": None}
    assert single.expected["B"] is None and ntc.expected["B"] == 0
    assert single.role == ROLE_STANDARD   # a null channel is not an NTC


def test_level_parsing(mini):
    (s,) = mini.annotate(["Both_1k_7"])
    assert s.expected == {"A": 1000, "B": 1000}
    assert s.captures == {"level": "1k", "rep": "7"}


def test_unknown_level_fails(mini):
    with pytest.raises(AnnotationError, match="not in the level map"):
        mini.annotate(["Both_2k_1"])


# ---- structural validation --------------------------------------------------

@pytest.mark.parametrize("old,new,msg", [
    ("annotation_version: 3\n", "", "missing required key 'annotation_version'"),
    ("annotation_version: 3", "annotation_version: x", "must be an integer"),
    ("expected: {A: 500, B: null}", "expected: {A: 500}", "exactly the channels"),
    ("expected: {A: 500, B: null}", "expected: {A: 500, B: -1}", "non-negative"),
    ("expected: {A: 500, B: null}", "expected: {A: 500, B: '{nope}'}", "not a capture group"),
    ("name: only_a", "name: both", "duplicate rule name"),
    ('{"0": 0, "1k": 1000}', '{"0": 0, "1k": -5}', "non-negative integer"),
    ("pattern: 'OnlyA_(?P<rep>\\d+)'", "pattern: '(['", "invalid pattern"),
])
def test_structural_errors(old, new, msg):
    assert old in MINI
    with pytest.raises(AnnotationError, match=msg):
        parse_annotation(MINI.replace(old, new, 1))


# ---- hash / lookup ----------------------------------------------------------

def test_sha256_is_over_file_bytes(tmp_path):
    import hashlib
    p = tmp_path / "EXP.yaml"
    p.write_bytes(MINI.encode())
    ann = load_annotation(p)
    assert ann.sha256 == hashlib.sha256(MINI.encode()).hexdigest()
    p.write_bytes((MINI + "# edit\n").encode())
    assert load_annotation(p).sha256 != ann.sha256


def test_find_annotation_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_annotation(tmp_path, "NOPE")


def test_find_annotation_id_mismatch(tmp_path):
    (tmp_path / "OTHER.yaml").write_text(MINI)
    with pytest.raises(AnnotationError, match="declares experiment_id 'EXP'"):
        find_annotation(tmp_path, "OTHER")


# ---- validate command -------------------------------------------------------

def _write_summary(path, names):
    lines = ["Data,CountableDataSummary", "Version,2.0.0",
             "InstrumentSerialNumber, ", ",",
             "CarouselPosition,TubePosition,StripBarcode,SampleName,SampleID,GroupName"]
    lines += [f"01,{i},BC,{n},BC_{i},," for i, n in enumerate(names, 1)]
    path.write_text("\n".join(lines) + "\n")


def test_validate_ok(tmp_path, capsys):
    csv_path = tmp_path / "s.csv"
    _write_summary(csv_path, mp37_sample_names())
    rc = validate.main([str(csv_path), "--annotation", str(MP37)])
    out = capsys.readouterr().out
    assert rc == 0 and "OK: every sample matches exactly one rule" in out
    assert "FourPlex_1k_3  ->  fourplex" in out.replace("   ", "  ") or "fourplex" in out
    assert "fourplex=64" in out and "singleplex_ch2_100k=8" in out


def test_validate_reports_unmatched(tmp_path, capsys):
    csv_path = tmp_path / "s.csv"
    _write_summary(csv_path, mp37_sample_names() + ["Bogus_1"])
    rc = validate.main([str(csv_path), "--annotation", str(MP37)])
    out = capsys.readouterr().out
    assert rc == 1 and "Bogus_1: matched no rule" in out and "NO MATCH" in out


def test_validate_by_experiment_id(tmp_path, capsys):
    csv_path = tmp_path / "s.csv"
    _write_summary(csv_path, mp37_sample_names())
    rc = validate.main([str(csv_path), "--experiment-id", "MP37_V_V_Testcase_2_beta05",
                        "--annotations-dir", str(REPO / "annotations")])
    assert rc == 0
