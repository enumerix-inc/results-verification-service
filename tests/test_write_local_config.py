import json

import yaml

from scripts.write_local_config import build_connection_string, main

DEFAULTS = (
    "paths:\n  manifests_dir: F:/default/manifests\n  database: F:/default/v.db\n"
    "  gold_standards_dir: F:/default/gold\n"
    "pipeline:\n  database_connection: postgresql://<user>:<password>@localhost:5432/x\n"
    "  polling_interval_seconds: 30\nlistener:\n  use_mock: false\n"
)


def _run(tmp_path, answers, **extra):
    defaults = tmp_path / "config.yaml"
    defaults.write_text(DEFAULTS)
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps(answers))
    out = tmp_path / "out" / "local_config.yaml"
    argv = ["--defaults", str(defaults), "--answers", str(answers_file), "--out", str(out)]
    argv += extra.get("flags", [])
    return main(argv), out, answers_file


def test_special_characters_in_password_are_percent_encoded():
    dsn = build_connection_string({"db_user": "svc", "db_password": "p@ss:w/rd%1"})
    assert dsn == "postgresql://svc:p%40ss%3Aw%2Frd%251@localhost:5432/countablelabs"


def test_writes_config_with_answers_and_keeps_other_defaults(tmp_path):
    rc, out, _ = _run(tmp_path, {
        "db_user": "svc", "db_password": "pw", "db_host": "dbhost", "db_port": "5433",
        "db_name": "mydb", "manifests_dir": "D:/m"})
    assert rc == 0
    cfg = yaml.safe_load(out.read_text())
    assert cfg["pipeline"]["database_connection"] == "postgresql://svc:pw@dbhost:5433/mydb"
    assert cfg["paths"]["manifests_dir"] == "D:/m"
    assert cfg["paths"]["gold_standards_dir"] == "F:/default/gold"   # untouched default
    assert cfg["pipeline"]["polling_interval_seconds"] == 30
    assert out.read_text().startswith("# Per-machine configuration")


def test_answers_file_with_the_password_is_deleted(tmp_path):
    _, _, answers_file = _run(tmp_path, {"db_user": "u", "db_password": "pw"})
    assert not answers_file.exists()


def test_existing_config_is_not_overwritten_but_answers_still_deleted(tmp_path):
    defaults = tmp_path / "config.yaml"
    defaults.write_text(DEFAULTS)
    out = tmp_path / "local_config.yaml"
    out.write_text("keep: me\n")
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"db_user": "u", "db_password": "pw"}))
    rc = main(["--defaults", str(defaults), "--answers", str(answers), "--out", str(out)])
    assert rc == 0
    assert out.read_text() == "keep: me\n"
    assert not answers.exists()


def test_force_replaces_existing_config(tmp_path):
    defaults = tmp_path / "config.yaml"
    defaults.write_text(DEFAULTS)
    out = tmp_path / "local_config.yaml"
    out.write_text("keep: me\n")
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"db_user": "u", "db_password": "pw"}))
    main(["--defaults", str(defaults), "--answers", str(answers), "--out", str(out), "--force"])
    assert "keep: me" not in out.read_text()
