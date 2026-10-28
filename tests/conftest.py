"""
Shared fixtures: a factory for MP37-shaped CountableDataSummary CSVs built from
the UMA linearity workbook fixture (96 samples, UM-0N column names).
"""

import json
from pathlib import Path

import pytest

GOLDEN = json.loads((Path(__file__).parent / "data" / "uma_linearity_golden.json").read_text())
LEVELS = ["0", "10", "100", "1k", "10k", "100k", "1M", "1p5M"]
CHANNELS = ["Ch01", "Ch02", "Ch03", "Ch04"]
COLUMNS = {ch: f"UM-0{i}_CountsPer50ul" for i, ch in enumerate(CHANNELS, 1)}
HEADER = (["CarouselPosition", "TubePosition", "StripBarcode", "SampleName", "SampleID", "GroupName"]
          + list(COLUMNS.values())
          + [f"UM-0{i}_IDscore" for i in range(1, 5)]
          + [f"UM-0{i}_Issues" for i in range(1, 5)]
          + [f"UM-0{i}_Observation" for i in range(1, 5)])


def mp37_rows():
    """[(sample_name, [count per channel])] for all 96 MP37 samples."""
    rows = []
    for li, lv in enumerate(LEVELS):
        for rep in range(8):
            rows.append((f"FourPlex_{lv}_{rep + 1}",
                         [GOLDEN["channels"][ch]["replicates"][li][rep] for ch in CHANNELS]))
    for c in range(1, 5):
        for rep in range(8):
            rows.append((f"Ch{c}_100k_{rep + 1}", [97000 if i == c else 0 for i in range(1, 5)]))
    return rows


def write_summary_csv(path, rows=None, header=HEADER):
    rows = mp37_rows() if rows is None else rows
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
        lines.append(",".join(d[h] for h in header) + ",")
    Path(path).write_text("\n".join(lines) + "\n")
    return Path(path)


@pytest.fixture
def summary_csv_factory(tmp_path):
    counter = [0]

    def make(rows=None, header=HEADER):
        counter[0] += 1
        return write_summary_csv(tmp_path / f"s{counter[0]}_CountableDataSummary.csv", rows, header)
    return make
