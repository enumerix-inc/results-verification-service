"""
Tests for resolve_notification() in src/main.py (ADR-022 addendum, 2026-10-13).
"""

from src.main import resolve_notification

EXPECTED = {"MP47b_Adverum_02": "run_20261013_001",
            "JS221N_serial_titration_PSF": "run_20261013_001"}


def test_exact_bare_id_keeps_payload_name():
    # Old payload shape: bare experimentId, timestamped name.
    assert resolve_notification(
        "MP47b_Adverum_02", "MP47b_Adverum_02_260925_1416", EXPECTED
    ) == ("MP47b_Adverum_02", "MP47b_Adverum_02_260925_1416")


def test_timestamped_id_is_stripped_and_used_as_pipeline_name():
    # Current payload shape: timestamped experimentId, plain name.
    assert resolve_notification(
        "MP47b_Adverum_02_261013_1053", "MP47b_Adverum_02", EXPECTED
    ) == ("MP47b_Adverum_02", "MP47b_Adverum_02_261013_1053")


def test_unknown_id_matches_nothing():
    exp_id, _ = resolve_notification("Other_Exp_261013_1053", "Other_Exp", EXPECTED)
    assert exp_id is None


def test_only_a_timestamp_shaped_suffix_is_stripped():
    exp_id, _ = resolve_notification("MP47b_Adverum_02_extra", "x", EXPECTED)
    assert exp_id is None
    exp_id, _ = resolve_notification("MP47b_Adverum_02_2610_1053", "x", EXPECTED)
    assert exp_id is None


def test_exact_match_wins_even_if_it_looks_timestamped():
    expected = {"Weird_260101_0000": "run_1"}
    assert resolve_notification("Weird_260101_0000", "n", expected) == (
        "Weird_260101_0000", "n")
