"""Local operator commands for health, SQLite backup, and service setup."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db as dbmod
from .config import Settings, load_settings
from .freshness import build_collection_status
from .service_lock import service_instance_running


log = logging.getLogger("kc_news_radar.operations")
BACKUP_PREFIX = "kc-news-radar-backup-"
BACKUP_SUFFIX = ".sqlite3"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _quick_check(conn: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    return "ok" if rows == ["ok"] else "; ".join(rows)


def _safe_backup_stem(source_db: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", source_db.stem).strip("-.") or "database"


def _backup_files(backup_dir: Path, *, source_stem: str | None = None) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    source_pattern = (
        re.compile(
            rf"^{re.escape(BACKUP_PREFIX)}{re.escape(source_stem)}-"
            rf"\d{{8}}T\d{{6}}(?:\.\d{{6}})?Z{re.escape(BACKUP_SUFFIX)}$"
        )
        if source_stem is not None
        else None
    )
    return sorted(
        (
            entry
            for entry in backup_dir.iterdir()
            if entry.is_file()
            and not entry.is_symlink()
            and entry.name.startswith(BACKUP_PREFIX)
            and entry.name.endswith(BACKUP_SUFFIX)
            and (source_pattern is None or source_pattern.fullmatch(entry.name))
            and _verified_backup_file(entry)
        ),
        key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
        reverse=True,
    )


def _verified_backup_file(path: Path) -> bool:
    try:
        with closing(_read_only_connection(path)) as conn:
            return _quick_check(conn) == "ok"
    except (OSError, sqlite3.Error):
        return False


def prune_backups(
    backup_dir: Path,
    *,
    source_db: Path,
    retain_count: int,
) -> list[Path]:
    """Delete only older verified backups belonging to one source database."""
    if retain_count < 1:
        raise ValueError("retain_count must be at least 1")
    resolved_dir = backup_dir.resolve()
    source_stem = _safe_backup_stem(source_db)
    removed: list[Path] = []
    for candidate in _backup_files(resolved_dir, source_stem=source_stem)[retain_count:]:
        if candidate.parent.resolve() != resolved_dir:
            raise RuntimeError(f"refusing retention outside backup directory: {candidate}")
        candidate.unlink()
        removed.append(candidate)
    return removed


def latest_backup(backup_dir: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    files = _backup_files(backup_dir.resolve())
    if not files:
        return None
    now = now or datetime.now(timezone.utc)
    newest = files[0]
    modified_at = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
    return {
        "path": str(newest),
        "modified_at": modified_at.isoformat(),
        "age_hours": round(max(0.0, (now - modified_at).total_seconds()) / 3600, 2),
        "size_bytes": newest.stat().st_size,
    }


def create_backup(
    source_db: Path,
    backup_dir: Path,
    *,
    retain_count: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create, verify, and retain an online SQLite backup without source writes."""
    source_db = source_db.resolve()
    backup_dir = backup_dir.resolve()
    if not source_db.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_db}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    safe_stem = _safe_backup_stem(source_db)
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = backup_dir / f"{BACKUP_PREFIX}{safe_stem}-{stamp}{BACKUP_SUFFIX}"
    partial = destination.with_suffix(destination.suffix + ".partial")
    if destination.exists() or partial.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")

    try:
        with closing(_read_only_connection(source_db)) as source, closing(sqlite3.connect(partial)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
            target_check = _quick_check(target)
            if target_check != "ok":
                raise RuntimeError(f"backup quick_check failed: {target_check}")
        os.replace(partial, destination)
        with closing(_read_only_connection(destination)) as verified:
            verified_check = _quick_check(verified)
            if verified_check != "ok":
                raise RuntimeError(f"reopened backup quick_check failed: {verified_check}")
    except Exception:
        log.exception("SQLite backup failed source=%s destination=%s", source_db, destination)
        if partial.exists():
            partial.unlink()
        if destination.exists():
            destination.unlink()
        raise

    removed = prune_backups(
        backup_dir,
        source_db=source_db,
        retain_count=retain_count,
    )
    log.info("SQLite backup succeeded source=%s destination=%s", source_db, destination)
    return {
        "ok": True,
        "source_db": str(source_db),
        "backup_path": str(destination),
        "verified": True,
        "quick_check": "ok",
        "size_bytes": destination.stat().st_size,
        "created_at": now.isoformat(),
        "retention_count": retain_count,
        "pruned_paths": [str(path) for path in removed],
    }


def build_operator_health(
    settings: Settings,
    *,
    now: datetime | None = None,
    process_alive: bool | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if process_alive is None:
        process_alive = service_instance_running(settings.db_path)
    database: dict[str, Any] = {
        "path": str(settings.db_path),
        "selection": "explicit" if settings.db_path_explicit else "default",
        "exists": settings.db_path.is_file(),
        "quick_check": None,
        "identity": None,
        "error": None,
    }
    status: dict[str, Any] | None = None
    try:
        stat = settings.db_path.stat()
        database["identity"] = f"device={stat.st_dev};inode={stat.st_ino}"
        database["size_bytes"] = stat.st_size
        with closing(_read_only_connection(settings.db_path)) as conn:
            database["quick_check"] = _quick_check(conn)
            status = build_collection_status(conn, settings=settings, now=now)
    except Exception as exc:
        database["error"] = f"{type(exc).__name__}: {exc}"

    backup_warning = None
    try:
        latest = latest_backup(settings.backup_dir, now=now)
    except Exception as exc:
        latest = None
        backup_warning = f"Backup status unavailable: {type(exc).__name__}: {exc}"
    if status is None:
        return {
            "healthy": False,
            "generated_at": now.isoformat(),
            "process_alive": process_alive,
            "runtime_status": "PROCESS_ALIVE" if process_alive else "NOT_RUNNING",
            "database": database,
            "latest_backup": latest,
            "warnings": [
                warning for warning in (
                    "Database health and persisted collection state could not be read.",
                    backup_warning,
                ) if warning
            ],
        }

    bad_sources = [
        row for row in status["sources"]
        if row["freshness_state"] in {"FAILING", "STALE", "DEGRADED"}
    ]
    latest_state = status.get("latest_run_state")
    scheduler_ok = not settings.collection_enabled or status["worker_responsive"]
    healthy = bool(
        database["quick_check"] == "ok"
        and process_alive
        and status["freshness_state"] == "FRESH"
        and scheduler_ok
        and not bad_sources
        and latest_state not in {"FAILED", "ABANDONED", "PARTIAL_FAILURE"}
    )
    runtime_status = "PROCESS_ALIVE" if process_alive else "NOT_RUNNING"
    warnings = list(status["warnings"])
    if not process_alive:
        warnings.append("No active KC News Radar service process lock is observable.")
    if backup_warning:
        warnings.append(backup_warning)
    return {
        "healthy": healthy,
        "generated_at": now.isoformat(),
        "process_alive": process_alive,
        "runtime_status": runtime_status,
        "database": database,
        "automatic_collection_enabled": settings.collection_enabled,
        "scheduler_state": status["worker_state"],
        "scheduler_responsive": status["worker_responsive"],
        "current_run": status.get("current_run"),
        "last_attempted_collection": status.get("last_attempted_run"),
        "last_completed_collection": status.get("last_completed_run"),
        "last_fully_successful_collection": status.get("last_successful_run"),
        "freshness_state": status["freshness_state"],
        "source_problem_count": len(bad_sources),
        "source_problems": bad_sources,
        "active_lease": status.get("active_lease"),
        "next_scheduled_cycle": status["next_scheduled_run"],
        "startup_recovery": status.get("startup_recovery"),
        "latest_abandoned_run": status.get("latest_abandoned_run"),
        "latest_backup": latest,
        "warnings": warnings,
    }


def format_health(report: dict[str, Any]) -> str:
    database = report["database"]
    lines = [
        f"KC News Radar health: {'HEALTHY' if report['healthy'] else 'NOT HEALTHY'}",
        f"Runtime: {report['runtime_status']}",
        f"Database: {database['path']}",
        f"Database quick_check: {database.get('quick_check') or 'FAILED/UNAVAILABLE'}",
    ]
    if "freshness_state" not in report:
        lines.append(f"Database error: {database.get('error')}")
        return "\n".join(lines)
    lines.extend([
        f"Automatic collection: {'ENABLED' if report['automatic_collection_enabled'] else 'DISABLED'}",
        f"Scheduler: {report['scheduler_state']} (responsive={report['scheduler_responsive']})",
        f"Current run: {(report['current_run'] or {}).get('run_id', 'none')}",
        f"Last attempted: {(report['last_attempted_collection'] or {}).get('requested_at', 'none')}",
        f"Last completed: {(report['last_completed_collection'] or {}).get('completed_at', 'none')}",
        f"Last fully successful: {(report['last_fully_successful_collection'] or {}).get('completed_at', 'none')}",
        f"Freshness: {report['freshness_state']}",
        f"Source problems: {report['source_problem_count']}",
        f"Active lease: {(report['active_lease'] or {}).get('run_id', 'none')}",
        f"Next scheduled cycle: {report['next_scheduled_cycle'] or 'none'}",
        f"Startup recovered abandoned runs: {(report['startup_recovery'] or {}).get('recovered_count', 0)}",
        f"Latest abandoned run: {(report['latest_abandoned_run'] or {}).get('run_id', 'none')}",
        f"Latest backup: {(report['latest_backup'] or {}).get('path', 'none')}",
    ])
    for source in report["source_problems"]:
        lines.append(
            f"  source {source['source_name']}: {source['freshness_state']}"
            + (f" ({source['message']})" if source.get("message") else "")
        )
    for warning in report["warnings"]:
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kc-news-radar operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    health_parser = subparsers.add_parser("health", help="inspect operational health")
    health_parser.add_argument("--json", action="store_true", help="emit JSON")
    backup_parser = subparsers.add_parser("backup", help="create a verified SQLite backup")
    backup_parser.add_argument("--backup-dir", type=Path)
    backup_parser.add_argument("--retain", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()

    try:
        if args.command == "health":
            report = build_operator_health(settings)
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_health(report))
            return 0 if report["healthy"] else 2
        result = create_backup(
            settings.db_path,
            args.backup_dir or settings.backup_dir,
            retain_count=args.retain or settings.backup_retention_count,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        log.error("operation failed: %s", exc)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "build_operator_health",
    "create_backup",
    "format_health",
    "latest_backup",
    "prune_backups",
]


if __name__ == "__main__":
    raise SystemExit(main())
