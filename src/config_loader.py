"""
config_loader.py - locate and load the service's YAML config.

Kept free of heavy imports (no asyncpg/watchdog) so the installer's
tooling and tests can use it on its own.

Resolution order (first match wins):
  1. an explicit path passed in (the --config command-line option)
  2. the RVS_CONFIG environment variable
  3. the repo's own config/local_config.yaml, then config/config.yaml
     (the original development layout)

An explicit path or RVS_CONFIG value may be a YAML file, or a directory
holding local_config.yaml (preferred) or config.yaml. Unlike the repo
fallback, a path that was asked for explicitly but does not exist is an
error - silently falling back to the tracked defaults would run the
service against the wrong database.
"""

import os
from pathlib import Path

import yaml

ENV_VAR = "RVS_CONFIG"
_REPO_ROOT = Path(__file__).parent.parent


def _from_directory(directory: Path) -> Path:
    for name in ("local_config.yaml", "config.yaml"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No local_config.yaml or config.yaml found in {directory}"
    )


def resolve_config_path(explicit=None, env=None, repo_root=None) -> Path:
    """Return the config file to load. Raises FileNotFoundError if an
    explicitly requested location does not exist."""
    env = os.environ if env is None else env
    requested = explicit or env.get(ENV_VAR)
    if requested:
        path = Path(requested)
        if path.is_dir():
            return _from_directory(path)
        if path.is_file():
            return path
        raise FileNotFoundError(f"Config path does not exist: {path}")

    config_dir = Path(repo_root) / "config" if repo_root else _REPO_ROOT / "config"
    local = config_dir / "local_config.yaml"
    return local if local.exists() else config_dir / "config.yaml"


def load_config(explicit=None) -> dict:
    """Load the service configuration (see module docstring for lookup)."""
    with open(resolve_config_path(explicit)) as f:
        return yaml.safe_load(f)
