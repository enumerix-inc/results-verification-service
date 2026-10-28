"""
preflight.py - quick environment checks, run by `--preflight` and by the
installer after it writes the per-machine config.

Each check returns (level, message) where level is "ok", "warn" or
"fail". Only "fail" makes the preflight exit nonzero; missing drives are
warnings because E:/F: may legitimately be attached after install.
"""

import asyncio
from pathlib import Path

from src.annotation.annotation import AnnotationError, load_annotation, resolve_annotations_dir

PATH_KEYS = ("manifests_dir", "reports_dir", "results_dir",
             "e_drive_report_data", "database")


def check_paths(config: dict) -> list[tuple[str, str]]:
    results = []
    paths = config.get("paths", {})
    for key in PATH_KEYS:
        value = paths.get(key)
        if not value:
            results.append(("warn", f"paths.{key} is not set"))
            continue
        anchor = Path(value).anchor
        if anchor and not Path(anchor).exists():
            results.append(("warn", f"paths.{key}: drive {anchor} not found ({value})"))
        else:
            results.append(("ok", f"paths.{key}: {value}"))
    return results


def check_annotations(config: dict) -> list[tuple[str, str]]:
    """Sample annotation files (ADR-026): the folder the service will use must
    exist with at least one file, and every file must load."""
    folder = resolve_annotations_dir(config.get("paths", {}).get("annotations_dir"))
    files = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    if not files:
        return [("warn", f"no annotation files in {folder}; acceptance criteria "
                         f"experiments will fail until one is added")]
    results = []
    for path in files:
        try:
            ann = load_annotation(path)
        except (AnnotationError, OSError) as e:
            results.append(("fail", f"annotation {path.name} does not load: {e}"))
        else:
            results.append(("ok", f"annotation {path.name}: experiment {ann.experiment_id}, "
                                  f"version {ann.annotation_version}"))
    return results


async def _try_connect(dsn: str, timeout: float) -> None:
    import asyncpg  # imported here so the other checks work without it
    conn = await asyncpg.connect(dsn, timeout=timeout)
    await conn.close()


def check_database(config: dict, timeout: float = 5.0) -> tuple[str, str]:
    dsn = config.get("pipeline", {}).get("database_connection", "")
    if not dsn or "<password>" in dsn or "<user>" in dsn:
        return ("fail", "pipeline.database_connection is a placeholder - "
                        "set real credentials in the per-machine config")
    try:
        asyncio.run(_try_connect(dsn, timeout))
    except Exception as e:  # report any failure, never the DSN itself
        return ("fail", f"cannot connect to the pipeline database: {type(e).__name__}: {e}")
    return ("ok", "pipeline database reachable")


def run_preflight(config: dict) -> int:
    """Print every check; return 0 unless a check failed."""
    results = check_paths(config) + check_annotations(config)
    if config.get("listener", {}).get("use_mock"):
        results.append(("ok", "listener.use_mock is true - database check skipped"))
    else:
        results.append(check_database(config))
    for level, message in results:
        print(f"[{level.upper():4}] {message}")
    return 1 if any(level == "fail" for level, _ in results) else 0
