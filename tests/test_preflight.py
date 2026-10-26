from src.preflight import check_database, check_paths, run_preflight


def test_placeholder_connection_string_fails_without_connecting():
    level, message = check_database(
        {"pipeline": {"database_connection": "postgresql://<user>:<password>@h/db"}})
    assert level == "fail"
    assert "placeholder" in message


def test_unset_connection_string_fails():
    assert check_database({})[0] == "fail"


def test_missing_path_key_is_a_warning():
    results = check_paths({"paths": {}})
    assert results and all(level == "warn" for level, _ in results)


def test_existing_path_is_ok(tmp_path):
    results = dict((m.split(":")[0], l) for l, m in check_paths(
        {"paths": {"manifests_dir": str(tmp_path / "m")}}))
    assert results["paths.manifests_dir"] == "ok"


def test_mock_listener_skips_database_check(tmp_path, capsys):
    cfg = {"paths": {}, "listener": {"use_mock": True}}
    assert run_preflight(cfg) == 0
    assert "database check skipped" in capsys.readouterr().out


def test_failed_database_check_makes_preflight_fail(capsys):
    cfg = {"paths": {}, "pipeline": {"database_connection": "postgresql://<user>:<password>@h/db"}}
    assert run_preflight(cfg) == 1
