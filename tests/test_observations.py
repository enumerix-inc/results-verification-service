"""
Tests for src/observations.py and src/summary_csv.py (ADR-026, TES-2874).
"""

import json
from pathlib import Path

import pytest

from src.annotation.annotation import AnnotationError, load_annotation
from src.observations import (FILTERS, ObservationError, apply_filters,
                              read_observations)
from src.summary_csv import read_summary_csv

REPO = Path(__file__).resolve().parents[1]
MP37 = REPO / "annotations" / "MP37_V_V_Testcase_2_beta05.yaml"
GOLDEN = json.loads((Path(__file__).parent / "data" / "uma_linearity_golden.json").read_text())
LEVELS = ["0", "10", "100", "1k", "10k", "100k", "1M", "1p5M"]
CHANNELS = ["Ch01", "Ch02", "Ch03", "Ch04"]
COLUMNS = {ch: f"UM-0{i}_CountsPer50ul" for i, ch in enumerate(CHANNELS, 1)}
HEADER = ["CarouselPosition", "TubePosition", "StripBarcode", "SampleName", "SampleID",
          "GroupName"] + list(COLUMNS.values()) + \
         [f"UM-0{i}_IDscore" for i in range(1, 5)] + \
         [f"UM-0{i}_Issues" for i in range(1, 5)] + \
         [f"UM-0{i}_Observation" for i in range(1, 5)]


def summary_rows():
    """96 rows in the MP37 layout, counts from the UMA workbook fixture."""
    rows = []
    for li, lv in enumerate(LEVELS):
        for rep in range(8):
            counts = [GOLDEN["channels"][ch]["replicates"][li][rep] for ch in CHANNELS]
            rows.append((f"FourPlex_{lv}_{rep + 1}", counts))
    for c in range(1, 5):
        for rep in range(8):
            counts = [97000 if i == c else 0 for i in range(1, 5)]
            rows.append((f"Ch{c}_100k_{rep + 1}", counts))
    return rows


def write_csv(path, rows, header=HEADER, drop_col=None, extra=None):
    lines = ["Data,CountableDataSummary", "Version,3.0.0", "InstrumentSerialNumber, ", ",",
             ",".join(header) + ","]
    for i, (name, counts) in enumerate(rows):
        d = {h: "" for h in header}
        d.update(CarouselPosition="01", TubePosition=str(i % 4 + 1), StripBarcode=f"BC{i // 4}",
                 SampleName=name, SampleID=f"BC{i // 4}_0{i % 4 + 1}")
        for col, v in zip(COLUMNS.values(), counts):
            d[col] = str(v)
        for k in range(1, 5):
            d[f"UM-0{k}_IDscore"] = "NA"
        if name == "FourPlex_1p5M_1":
            for k in range(1, 5):
                d[f"UM-0{k}_Observation"] = "High Count Alert (>1M)."
        d.update(extra or {})
        lines.append(",".join(d[h] for h in header if h != drop_col) + ",")
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture(scope="module")
def ann():
    return load_annotation(MP37)


@pytest.fixture
def csv_path(tmp_path):
    return write_csv(tmp_path / "MP37_x_CountableDataSummary.csv", summary_rows())


# ---- acceptance: 96 x 4 = 384, all annotated ------------------------------------

def test_96_samples_times_4_channels_all_annotated(csv_path, ann):
    s = read_observations(csv_path, ann, COLUMNS)
    assert len(s.observations) == 384 and len(s.sample_names) == 96
    assert all(o.group and o.rule and o.role for o in s.observations)
    assert s.experiment_id == "MP37_V_V_Testcase_2_beta05"
    assert s.annotation_sha256 == ann.sha256 and s.annotation_version == 1
    assert len(s.source_sha256) == 64


def test_values_round_trip_from_the_workbook_fixture(csv_path, ann):
    s = read_observations(csv_path, ann, COLUMNS)
    by = {(o.sample_name, o.channel): o for o in s.observations}
    assert by[("FourPlex_100k_1", "Ch01")].count == 97151
    assert by[("FourPlex_100k_1", "Ch04")].count == 90821
    o = by[("FourPlex_1k_3", "Ch02")]
    assert (o.group, o.expected, o.role, o.column) == ("FourPlex_1k", 1000, "standard",
                                                       "UM-02_CountsPer50ul")
    assert by[("FourPlex_0_2", "Ch03")].expected == 0 and by[("FourPlex_0_2", "Ch03")].role == "ntc"
    assert by[("Ch3_100k_5", "Ch03")].expected == 100000
    assert by[("Ch3_100k_5", "Ch01")].expected is None


def test_pipeline_notes_carried_through(csv_path, ann):
    s = read_observations(csv_path, ann, COLUMNS)
    flagged = [o for o in s.observations if o.note]
    assert len(flagged) == 4 and {o.sample_name for o in flagged} == {"FourPlex_1p5M_1"}
    assert flagged[0].note == "High Count Alert (>1M)."
    assert flagged[0].strip_barcode and flagged[0].tube_position


def test_group_selection_gives_eight_replicates_per_channel(csv_path, ann):
    s = read_observations(csv_path, ann, COLUMNS)
    sel = [o for o in s.observations if o.group == "FourPlex_100k" and o.channel == "Ch01"]
    assert len(sel) == 8


# ---- tolerant parsing ---------------------------------------------------------------

def test_na_and_blank_counts_become_none(tmp_path, ann):
    rows = summary_rows()
    p = write_csv(tmp_path / "s.csv", rows)
    text = p.read_text().replace("FourPlex_10_1", "FourPlex_10_1")
    lines = text.splitlines()
    i = next(i for i, l in enumerate(lines) if ",FourPlex_10_1," in l)
    cells = lines[i].split(",")
    cells[HEADER.index("UM-02_CountsPer50ul")] = "NA"
    cells[HEADER.index("UM-03_CountsPer50ul")] = ""
    lines[i] = ",".join(cells)
    p.write_text("\n".join(lines) + "\n")
    s = read_observations(p, ann, COLUMNS)
    by = {(o.sample_name, o.channel): o.count for o in s.observations}
    assert by[("FourPlex_10_1", "Ch02")] is None and by[("FourPlex_10_1", "Ch03")] is None
    assert by[("FourPlex_10_1", "Ch01")] is not None


def test_trailing_commas_and_empty_group_name_are_fine(csv_path):
    rows = read_summary_csv(csv_path)
    assert len(rows) == 96 and rows[0]["GroupName"] == ""


# ---- fail loudly -----------------------------------------------------------------------

def test_missing_channel_column_fails(tmp_path, ann):
    header = [h for h in HEADER if h != "UM-03_CountsPer50ul"]
    p = write_csv(tmp_path / "s.csv", summary_rows(), header=header)
    with pytest.raises(ObservationError, match="Ch03.*UM-03_CountsPer50ul"):
        read_observations(p, ann, COLUMNS)


def test_old_column_names_need_an_override(tmp_path, ann):
    """A 2.0.0-style file (Ch01_...) fails with the UM columns, works when mapped."""
    old = {ch: f"{ch}_CountsPer50ul" for ch in CHANNELS}
    header = [h.replace("UM-0", "Ch0") for h in HEADER]
    rows = summary_rows()
    p = tmp_path / "old.csv"
    text = write_csv(p, rows).read_text().replace("UM-0", "Ch0")
    p.write_text(text)
    with pytest.raises(ObservationError, match="missing"):
        read_observations(p, ann, COLUMNS)
    assert len(read_observations(p, ann, old).observations) == 384
    assert header  # silence unused


def test_channel_mismatch_with_annotation_fails(csv_path, ann):
    with pytest.raises(ObservationError, match="do not match the annotation"):
        read_observations(csv_path, ann, {"Ch01": COLUMNS["Ch01"]})


def test_garbage_count_fails(tmp_path, ann):
    rows = summary_rows()
    rows[0] = (rows[0][0], ["abc", 0, 0, 0])
    with pytest.raises(ObservationError, match="not a number"):
        read_observations(write_csv(tmp_path / "s.csv", rows), ann, COLUMNS)
    rows[0] = (rows[0][0], [-5, 0, 0, 0])
    with pytest.raises(ObservationError, match="not a valid count"):
        read_observations(write_csv(tmp_path / "s2.csv", rows), ann, COLUMNS)


def test_unannotated_sample_fails(tmp_path, ann):
    rows = summary_rows() + [("Mystery_1", [1, 2, 3, 4])]
    with pytest.raises(AnnotationError, match="Mystery_1: matched no rule"):
        read_observations(write_csv(tmp_path / "s.csv", rows), ann, COLUMNS)


def test_no_sample_name_column_fails(tmp_path, ann):
    p = tmp_path / "bad.csv"
    p.write_text("a,b\n1,2\nx,y\nz,w\n\nfoo,bar\n1,2\n")
    with pytest.raises(ObservationError, match="SampleName"):
        read_observations(p, ann, COLUMNS)


def test_unknown_reader(csv_path, ann):
    with pytest.raises(ObservationError, match="unknown reader"):
        read_observations(csv_path, ann, COLUMNS, reader="rnd")


# ---- preprocessing filters ------------------------------------------------------------

def test_no_filters_by_default(csv_path, ann):
    assert read_observations(csv_path, ann, COLUMNS).filters == []


def test_filters_applied_in_order_and_recorded(csv_path, ann):
    s = read_observations(csv_path, ann, COLUMNS,
                          filters=["exclude_no_assay", "exclude_missing_count"])
    assert s.filters == [{"name": "exclude_no_assay", "removed": 96},   # 32 singleplex x 3 off-channels
                         {"name": "exclude_missing_count", "removed": 0}]
    assert len(s.observations) == 384 - 96


def test_unknown_filter_fails(csv_path, ann):
    with pytest.raises(ObservationError, match="unknown filter"):
        read_observations(csv_path, ann, COLUMNS, filters=["nope"])
    assert set(FILTERS) >= {"exclude_no_assay"}
    assert apply_filters([], []) == ([], [])


# ---- registrar still reads through the shared module ---------------------------------

def test_registrar_reader_is_the_shared_reader(csv_path):
    pytest.importorskip("src.registration.registrar")
    from src.registration.registrar import read_gold_standard_csv
    assert read_gold_standard_csv(str(csv_path)) == read_summary_csv(str(csv_path))
