"""
registrar.py — gold standard registration tool.

Reads a gold standard CSV, computes its SHA-256 checksum,
inserts a new version into gold_standard_exp_versions, and
inserts one row per sample into gold_standard_samples.

Entry point: register_gold_standard()

# NOTE: Currently handles wide-format CSVs only (4 metadata rows,
# one row per sample). Long/melted format CSVs (e.g. RnDdata)
# require a pivot preprocessing step — see STATUS.md.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from src.summary_csv import METADATA_ROWS, read_summary_csv  # noqa: F401 (METADATA_ROWS re-exported)
from src.database.models import (
    get_active_gs_version,
    insert_gs_exp_version,
    insert_gs_sample,
    retire_gs_version,
)

SAMPLE_ID_COLUMN = "SampleID"


def compute_checksum(file_path: str) -> str:
    """Compute and return the SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_gold_standard_csv(file_path: str) -> list[dict]:
    """Read a gold standard CSV and return a list of sample dicts."""
    return read_summary_csv(file_path)


def register_gold_standard(conn, experiment_id: str, file_path: str,
                            registered_by: str, reason: str) -> str:
    """
    Full registration flow for a gold standard experiment version.

    1. Compute checksum
    2. Read samples from CSV
    3. Retire current active version if one exists
    4. Insert new gs_exp_version record
    5. Insert gs_sample records

    Returns the new gs_exp_version_id.
    
    NOTE:   Handles wide-format CSVs only (4 metadata rows, one row
            per sample). Long/melted format CSVs (e.g. RnDdata) 
            require pivot preprocessing — see STATUS.md.

    Checksum_hash is stored at registration time as an audit trail
    and to support a future re-registration guard workflow.
    It is not read during verification — the database is the source
    of truth for gold standard values.
    """
    checksum = compute_checksum(file_path)
    rows = read_gold_standard_csv(file_path)
    now = datetime.now(timezone.utc).isoformat()

    active = get_active_gs_version(conn, experiment_id)
    next_version = (active["version_number"] + 1) if active else 1
    gs_exp_version_id = str(uuid.uuid4())

    version_record = {
        "gs_exp_version_id": gs_exp_version_id,
        "experiment_id":     experiment_id,
        "version_number":    next_version,
        "checksum_hash":     checksum,
        "file_path":         str(file_path),
        "registered_at":     now,
        "registered_by":     registered_by,
        "reason":            reason,
        "superseded_at":     None,
    }

    sample_records = [
        {
            "gs_sample_id":      str(uuid.uuid4()),
            "gs_exp_version_id": gs_exp_version_id,
            "sample_id":         row[SAMPLE_ID_COLUMN],
            "full_metrics":      json.dumps(dict(row)),
            "notes":             None,
        }
        for row in rows
    ]

    try:
        with conn:
            if active:
                retire_gs_version(conn, active["gs_exp_version_id"], now)
            insert_gs_exp_version(conn, version_record)
            for record in sample_records:
                insert_gs_sample(conn, record)
    except Exception as e:
        raise RuntimeError(
            f"Registration failed for experiment '{experiment_id}': {e}"
        ) from e

    return gs_exp_version_id
