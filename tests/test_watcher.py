"""
Tests for src/orchestrator/watcher.py - the E: drive clear-out wait.

Covers the cases from the ADR-019 addendum: the Report Loader may delete the
execution's folder or move it to its archive, and the move can land within
milliseconds of the NOTIFY.
"""

import asyncio
import shutil
import threading
import time

from src.orchestrator.watcher import wait_for_e_drive_deletion

NAME = "MP47b_Adverum_02_261014_1116"


def make_config(report_data, timeout=2.0, poll=0.05):
    return {
        "paths": {"e_drive_report_data": str(report_data)},
        "verification": {
            "e_drive_deletion_timeout_seconds": timeout,
            "e_drive_poll_seconds": poll,
        },
    }


def run_wait(config):
    return asyncio.run(wait_for_e_drive_deletion("MP47b_Adverum_02", "run_x", NAME, config))


def after(delay, fn):
    t = threading.Timer(delay, fn)
    t.start()
    return t


def test_already_absent_returns_true(tmp_path):
    report_data = tmp_path / "ReportData"
    report_data.mkdir()
    assert run_wait(make_config(report_data)) is True


def test_folder_deleted_during_wait(tmp_path):
    report_data = tmp_path / "ReportData"
    folder = report_data / NAME
    folder.mkdir(parents=True)
    t = after(0.3, lambda: shutil.rmtree(folder))
    start = time.monotonic()
    assert run_wait(make_config(report_data)) is True
    t.join()
    assert time.monotonic() - start < 1.5


def test_folder_moved_during_wait(tmp_path):
    """The case that timed out in production: the loader MOVES the folder."""
    report_data = tmp_path / "ReportData"
    archive = tmp_path / "Uploaded_To_Database"
    folder = report_data / NAME
    folder.mkdir(parents=True)
    archive.mkdir()
    t = after(0.3, lambda: shutil.move(str(folder), str(archive / NAME)))
    assert run_wait(make_config(report_data)) is True
    t.join()


def test_unrelated_folders_do_not_block(tmp_path):
    report_data = tmp_path / "ReportData"
    (report_data / "OtherExperiment_261014_1116").mkdir(parents=True)
    assert run_wait(make_config(report_data)) is True


def test_times_out_when_folder_never_leaves(tmp_path):
    report_data = tmp_path / "ReportData"
    (report_data / NAME).mkdir(parents=True)
    start = time.monotonic()
    assert run_wait(make_config(report_data, timeout=0.4)) is False
    assert time.monotonic() - start < 1.5
