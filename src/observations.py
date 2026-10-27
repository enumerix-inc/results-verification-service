"""
observations.py - turn a pipeline output into annotated per-channel observations
(ADR-026, TES-2874).

A reader takes a pipeline output file, a sample annotation and the manifest's
channel -> column map, and returns an ObservationSet: one Observation per
sample x channel, each carrying the annotation (group, role, expected count)
and the pipeline's Issues/Observation notes for the reviewer.

Only the CountableDataSummary CSV is supported for now. Other outputs (RnDdata
long form, linkage summary) plug in by adding a reader to READERS.

Failures are loud: a missing column, an unparseable count, an unannotated
sample, or channels that do not match the annotation raise ObservationError
or AnnotationError. Nothing is silently skipped.

Preprocessing is a named, ordered list of filters (FILTERS); the names that
were applied and how many observations each removed are recorded in the
result so the report can show exactly what was excluded.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from src.annotation.annotation import Annotation
from src.summary_csv import read_summary_csv

SAMPLE_NAME_COLUMN = "SampleName"
COUNT_SUFFIX = "_CountsPer50ul"
_MISSING = {"", "NA", "N/A", "NaN"}


class ObservationError(ValueError):
    """The pipeline output cannot be turned into observations."""


@dataclass(frozen=True)
class Observation:
    sample_name: str
    sample_id: str
    strip_barcode: str
    carousel_position: str
    tube_position: str
    channel: str
    column: str
    count: float | None          # None when the pipeline reported NA / blank
    group: str
    role: str
    expected: int | None         # None = no assay; 0 = no-template control
    rule: str
    issues: str
    note: str                    # the pipeline's Observation column


@dataclass
class ObservationSet:
    experiment_id: str
    annotation_version: int
    annotation_sha256: str
    source_file: str
    source_sha256: str
    columns: dict
    observations: list = field(default_factory=list)
    filters: list = field(default_factory=list)   # [{"name":..., "removed":...}]

    @property
    def sample_names(self) -> list:
        return list(dict.fromkeys(o.sample_name for o in self.observations))


# ---- preprocessing filters ----------------------------------------------------

FILTERS = {
    # channels with no assay on that sample (e.g. the other three channels of a singleplex)
    "exclude_no_assay": lambda o: o.expected is not None,
    # observations the pipeline reported no count for
    "exclude_missing_count": lambda o: o.count is not None,
}


def apply_filters(observations, names):
    """Apply the named filters in order; return (kept, [{"name", "removed"}])."""
    unknown = [n for n in names if n not in FILTERS]
    if unknown:
        raise ObservationError(f"unknown filter(s) {unknown} (known: {', '.join(FILTERS)})")
    kept, record = list(observations), []
    for name in names:
        before = len(kept)
        kept = [o for o in kept if FILTERS[name](o)]
        record.append({"name": name, "removed": before - len(kept)})
    return kept, record


# ---- readers -------------------------------------------------------------------

class PipelineOutputReader(Protocol):
    def read(self, path, annotation: Annotation, columns: dict,
             filters=()) -> ObservationSet: ...


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_count(raw, where):
    raw = (raw or "").strip()
    if raw in _MISSING:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ObservationError(f"{where}: count {raw!r} is not a number") from None
    if value != value or value < 0:
        raise ObservationError(f"{where}: count {raw!r} is not a valid count")
    return int(value) if value.is_integer() else value


class DataSummaryReader:
    """Wide-format <experiment>_<timestamp>_CountableDataSummary.csv."""

    def read(self, path, annotation: Annotation, columns: dict, filters=()) -> ObservationSet:
        if set(columns) != set(annotation.channels):
            raise ObservationError(
                f"columns {sorted(columns)} do not match the annotation's channels "
                f"{sorted(annotation.channels)}")
        rows = read_summary_csv(path)
        if not rows:
            raise ObservationError(f"{path}: no sample rows")
        header = set(rows[0])
        if SAMPLE_NAME_COLUMN not in header:
            raise ObservationError(
                f"{path}: no '{SAMPLE_NAME_COLUMN}' column (is the file a "
                f"CountableDataSummary with the 4-line preamble?)")
        missing = {ch: col for ch, col in columns.items() if col not in header}
        if missing:
            raise ObservationError(
                f"{path}: count column(s) missing for {missing}; the file has "
                f"{sorted(h for h in header if h and h.endswith(COUNT_SUFFIX))}")

        names = [r[SAMPLE_NAME_COLUMN] for r in rows]
        annotated = {a.sample_name: a for a in annotation.annotate(names)}  # strict

        out = []
        for r in rows:
            a = annotated[r[SAMPLE_NAME_COLUMN]]
            for ch in annotation.channels:
                col = columns[ch]
                prefix = col[:-len(COUNT_SUFFIX)] if col.endswith(COUNT_SUFFIX) else None
                out.append(Observation(
                    sample_name=a.sample_name,
                    sample_id=r.get("SampleID", ""),
                    strip_barcode=r.get("StripBarcode", ""),
                    carousel_position=r.get("CarouselPosition", ""),
                    tube_position=r.get("TubePosition", ""),
                    channel=ch, column=col,
                    count=_parse_count(r[col], f"{a.sample_name} {col}"),
                    group=a.group, role=a.role, expected=a.expected[ch], rule=a.rule,
                    issues=(r.get(f"{prefix}_Issues") or "").strip() if prefix else "",
                    note=(r.get(f"{prefix}_Observation") or "").strip() if prefix else "",
                ))
        kept, record = apply_filters(out, filters)
        return ObservationSet(annotation.experiment_id, annotation.annotation_version,
                              annotation.sha256, str(path), _sha256(path),
                              dict(columns), kept, record)


READERS = {"data_summary": DataSummaryReader()}


def read_observations(path, annotation: Annotation, columns: dict,
                      filters=(), reader: str = "data_summary") -> ObservationSet:
    if reader not in READERS:
        raise ObservationError(f"unknown reader {reader!r} (known: {', '.join(READERS)})")
    return READERS[reader].read(path, annotation, columns, filters)
