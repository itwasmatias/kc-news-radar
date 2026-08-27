"""Collection and evidence freshness derived from durable run records."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db as dbmod
from .config import Settings


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _age_hours(now: datetime, value: str | None) -> float | None:
    timestamp = _dt(value)
    if timestamp is None:
        return None
    return round(max(0.0, (now - timestamp).total_seconds()) / 3600, 2)


def build_collection_status(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    runs = dbmod.list_collection_runs(conn, limit=200)
    lease = dbmod.current_collection_lease(conn, now=now)
    scheduler = dbmod.get_scheduler_state(conn)
    startup_recovery = dbmod.latest_startup_recovery(conn)

    started = [run for run in runs if run.get("started_at")]
    finished = [
        run for run in runs
        if run["state"] in {"COMPLETED", "PARTIAL_FAILURE", "FAILED"}
    ]
    evidence_runs = [
        run for run in runs
        if run["state"] in {"COMPLETED", "PARTIAL_FAILURE"}
    ]
    successful = [run for run in runs if run["state"] == "COMPLETED"]
    abandoned = [run for run in runs if run["state"] == "ABANDONED"]
    latest_finished = finished[0] if finished else None
    latest_evidence_run = evidence_runs[0] if evidence_runs else None
    evidence_at = latest_evidence_run.get("completed_at") if latest_evidence_run else None
    evidence_age_hours = _age_hours(now, evidence_at)
    stale_after_hours = round(settings.stale_after_seconds / 3600, 2)
    if evidence_age_hours is None:
        freshness_state = "NO_COMPLETED_RUN"
    elif evidence_age_hours * 3600 > settings.stale_after_seconds:
        freshness_state = "STALE"
    elif latest_evidence_run and latest_evidence_run["state"] == "PARTIAL_FAILURE":
        freshness_state = "FRESH_PARTIAL_FAILURE"
    else:
        freshness_state = "FRESH"

    scheduler_projection = scheduler or {
        "configured_enabled": settings.collection_enabled,
        "worker_state": "NOT_STARTED",
        "cadence_seconds": settings.collection_cadence_seconds,
        "stale_after_seconds": settings.stale_after_seconds,
        "next_run_at": None,
        "heartbeat_at": None,
        "worker_id": None,
    }
    heartbeat_age = _age_hours(now, scheduler_projection.get("heartbeat_at"))
    heartbeat_limit_hours = max(90, settings.collection_cadence_seconds * 2) / 3600
    worker_responsive = bool(
        scheduler_projection.get("heartbeat_at")
        and heartbeat_age is not None
        and heartbeat_age <= heartbeat_limit_hours
        and scheduler_projection.get("worker_state") not in {"STOPPED", "DISABLED"}
    )
    lease_heartbeat_age = _age_hours(now, lease.get("heartbeat_at") if lease else None)
    if lease and lease["active"] and lease_heartbeat_age is not None:
        worker_responsive = worker_responsive or lease_heartbeat_age <= (90 / 3600)

    source_rows = []
    for row in dbmod.latest_source_run_results(conn):
        attempt_age_hours = _age_hours(now, row["completed_at"])
        source_evidence_age_hours = _age_hours(now, row.get("last_successful_at"))
        if row["outcome"] == "FAILED":
            state = "FAILING"
        elif source_evidence_age_hours is not None and source_evidence_age_hours * 3600 > settings.stale_after_seconds:
            state = "STALE"
        elif row["source_status"] == "DEGRADED":
            state = "DEGRADED"
        elif row["outcome"] == "ZERO_ITEMS":
            state = "FRESH_ZERO_ITEMS"
        else:
            state = "FRESH"
        source_rows.append({
            **row,
            "attempt_age_hours": attempt_age_hours,
            "evidence_age_hours": source_evidence_age_hours,
            "freshness_state": state,
        })

    warnings: list[str] = []
    if freshness_state == "NO_COMPLETED_RUN":
        warnings.append("No completed observable collection run exists for this database.")
    elif freshness_state == "STALE":
        warnings.append(
            f"Displayed evidence is {evidence_age_hours} hours old, beyond the {stale_after_hours}-hour stale threshold."
        )
    elif freshness_state == "FRESH_PARTIAL_FAILURE":
        warnings.append("The latest completed run had one or more source failures.")
    if settings.collection_enabled and not worker_responsive:
        warnings.append("Automatic collection is configured on, but no responsive scheduler worker is evidenced.")
    if runs and runs[0]["state"] == "RUNNING" and not (lease and lease["active"]):
        warnings.append("An incomplete run has no active lease and requires startup recovery evidence.")

    active_lease = None
    current_run = None
    if lease and lease["active"]:
        active_lease = {
            key: value for key, value in lease.items()
            if key not in {"owner_token", "singleton_id"}
        }
        current_run = dbmod.get_collection_run(conn, lease["run_id"])

    return {
        "database_path": str(settings.db_path),
        "database_selection": "explicit" if settings.db_path_explicit else "default",
        "automatic_collection_enabled": settings.collection_enabled,
        "worker_state": scheduler_projection["worker_state"],
        "worker_responsive": worker_responsive,
        "cadence_seconds": settings.collection_cadence_seconds,
        "stale_after_seconds": settings.stale_after_seconds,
        "stale_after_hours": stale_after_hours,
        "next_scheduled_run": (
            scheduler_projection.get("next_run_at") if settings.collection_enabled else None
        ),
        "scheduler_heartbeat_at": scheduler_projection.get("heartbeat_at"),
        "collection_running": bool(lease and lease["active"]),
        "active_run_id": lease["run_id"] if lease and lease["active"] else None,
        "active_lease": active_lease,
        "current_run": current_run,
        "latest_run_state": runs[0]["state"] if runs else None,
        "last_attempted_run": runs[0] if runs else None,
        "last_requested_at": runs[0]["requested_at"] if runs else None,
        "last_attempt_started_at": started[0]["started_at"] if started else None,
        "last_completed_at": latest_finished.get("completed_at") if latest_finished else None,
        "last_completed_run_id": latest_finished["run_id"] if latest_finished else None,
        "last_completed_run": latest_finished,
        "last_successful_at": successful[0].get("completed_at") if successful else None,
        "last_successful_run_id": successful[0]["run_id"] if successful else None,
        "last_successful_run": successful[0] if successful else None,
        "latest_abandoned_run": abandoned[0] if abandoned else None,
        "startup_recovery": startup_recovery,
        "evidence_at": evidence_at,
        "evidence_age_hours": evidence_age_hours,
        "freshness_state": freshness_state,
        "recent_run_count": len(runs),
        "sources": source_rows,
        "warnings": warnings,
    }


__all__ = ["build_collection_status"]
