"""
summary_csv.py - read a pipeline wide-format summary CSV.

Shared by the gold standard registrar, the comparator, the annotation
validator and the acceptance observation reader. Format: METADATA_ROWS
preamble lines, then a header row and one row per sample. Rows may end in a
trailing comma, so DictReader yields an extra '' key (header) / '' or None key
(rows); callers ignore those.

Kept free of database imports so tools and tests can use it on its own.
"""

import csv

METADATA_ROWS = 4


def read_summary_csv(file_path: str) -> list[dict]:
    """Read a wide-format summary CSV and return a list of sample dicts."""
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        for _ in range(METADATA_ROWS):
            f.readline()
        return list(csv.DictReader(f))
