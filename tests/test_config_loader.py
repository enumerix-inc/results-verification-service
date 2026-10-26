import pytest

from src.config_loader import ENV_VAR, load_config, resolve_config_path


def _write(path, text="paths:\n  database: x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_repo_fallback_prefers_local_config(tmp_path):
    _write(tmp_path / "config" / "config.yaml")
    local = _write(tmp_path / "config" / "local_config.yaml")
    assert resolve_config_path(env={}, repo_root=tmp_path) == local


def test_repo_fallback_uses_default_when_no_local(tmp_path):
    default = _write(tmp_path / "config" / "config.yaml")
    assert resolve_config_path(env={}, repo_root=tmp_path) == default


def test_explicit_file_wins_over_env_and_repo(tmp_path):
    explicit = _write(tmp_path / "mine.yaml")
    other = _write(tmp_path / "other.yaml")
    assert resolve_config_path(str(explicit), env={ENV_VAR: str(other)},
                               repo_root=tmp_path) == explicit


def test_env_var_used_when_no_explicit(tmp_path):
    other = _write(tmp_path / "other.yaml")
    assert resolve_config_path(env={ENV_VAR: str(other)}, repo_root=tmp_path) == other


def test_directory_prefers_local_config(tmp_path):
    _write(tmp_path / "cfg" / "config.yaml")
    local = _write(tmp_path / "cfg" / "local_config.yaml")
    assert resolve_config_path(str(tmp_path / "cfg"), env={}) == local


def test_directory_without_config_files_is_an_error(tmp_path):
    (tmp_path / "cfg").mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_config_path(str(tmp_path / "cfg"), env={})


def test_missing_explicit_path_is_an_error_not_a_fallback(tmp_path):
    _write(tmp_path / "config" / "config.yaml")
    with pytest.raises(FileNotFoundError):
        resolve_config_path(str(tmp_path / "nope.yaml"), env={}, repo_root=tmp_path)


def test_load_config_parses_yaml(tmp_path):
    f = _write(tmp_path / "c.yaml", "paths:\n  database: D\n")
    assert load_config(str(f)) == {"paths": {"database": "D"}}
