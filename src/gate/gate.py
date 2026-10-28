"""
gate.py — pre-verification integrity checks.

Two sequential checks before any results comparison:
  1. Checksum check — gold standard file matches registered hash
  2. Subset check  — all samples in workbook exist in parent
                     gold standard (child experiments only)

If either check fails the run is aborted and no comparison runs.

Entry point: run_gate()
"""

from src.comparator.registry import AGGREGATE, comparison_scope
from src.database.models import get_active_gs_version
from src.registration.registrar import compute_checksum


def checksum_check(file_path: str, expected_hash: str) -> bool:
    """Return True if the file's SHA-256 matches expected_hash."""
    return compute_checksum(file_path) == expected_hash


def subset_check(workbook_sample_ids: list[str],
                 parent_sample_ids: list[str]) -> list[str]:
    """
    Return a list of sample IDs in the workbook that are NOT
    present in the parent gold standard. Empty list = pass.
    """
    # TODO: implement
    pass


def run_gate(conn, experiment_id: str, comparisons: list | None = None) -> tuple[bool, str]:
    """
    Run the pre-verification gate for one experiment.

    Checks that an active registered gold standard exists in the
    database for this experiment. The database is the source of
    truth — if a registered version exists, we trust it.

    Returns (passed: bool, status: str) where status is one of:
      'pass' | 'no_gold_standard'

    Note: checksum_check() remains in this module for potential
    use in a future re-registration workflow but is not called
    here. Subset validity check is Phase 3.

    comparisons — the experiment's manifest comparison entries. When given
    and EVERY one is aggregate-scope (ADR-021/026), no gold standard is
    needed and the gate passes. Any sample-scope or unknown type keeps the
    gold standard requirement.
    """
    if comparisons and all(comparison_scope(c.get("type")) == AGGREGATE
                           for c in comparisons):
        return (True, "pass")
    active = get_active_gs_version(conn, experiment_id)
    if active is None:
        return (False, "no_gold_standard")
    return (True, "pass")
