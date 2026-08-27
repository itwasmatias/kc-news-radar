"""Deterministic local operational-hardening tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kc_news_radar import db as dbmod
from kc_news_radar.collection_runtime import CollectionCycleResult, execute_collection_cycle
from kc_news_radar.config import load_settings
from kc_news_radar.models import CollectionRunState, CollectionTrigger, SourceStatus
from kc_news_radar.operations import (
    BACKUP_PREFIX,
    BACKUP_SUFFIX,
    build_operator_health,
    create_backup,
    prune_backups,
)
from kc_news_radar.scheduler import CollectionScheduler
from kc_news_radar.service_lock import (
    ServiceAlreadyRunningError,
    ServiceInstanceLock,
    service_instance_running,
)

from test_automatic_collection import NOW, StaticCollector, _item, _settings


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _completed_result(run_id="scheduled"):
    return CollectionCycleResult(
        run_id=run_id,
        state=CollectionRunState.COMPLETED,
        acquired=True,
        blocked_by_run_id=None,
        sources_attempted=1,
        sources_succeeded=1,
        sources_failed=0,
        items_collected=1,
        items_updated=0,
        pipeline_result={},
        failure_summary=None,
    )


def test_online_backup_is_standalone_verified_and_source_unchanged(tmp_db_path, tmp_path):
    with dbmod.connect(tmp_db_path) as live:
        live.execute("CREATE TABLE pilot_marker (value TEXT NOT NULL)")
        live.execute("INSERT INTO pilot_marker VALUES ('expected')")
        live.commit()
        before_hash = _sha256(tmp_db_path)
        before_stat = tmp_db_path.stat()
        result = create_backup(
            tmp_db_path,
            tmp_path / "backups",
            retain_count=3,
            now=NOW,
        )
        assert live.execute("SELECT value FROM pilot_marker").fetchone()[0] == "expected"

    backup_path = tmp_path / "backups" / result["backup_path"].split("/")[-1]
    assert result["verified"] is True
    assert not backup_path.with_name(backup_path.name + "-wal").exists()
    with dbmod.connect(backup_path) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT value FROM pilot_marker").fetchone()[0] == "expected"
    assert _sha256(tmp_db_path) == before_hash
    assert tmp_db_path.stat().st_size == before_stat.st_size
    assert tmp_db_path.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_retention_removes_only_designated_old_backups(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    candidates = []
    for index in range(4):
        path = backup_dir / f"{BACKUP_PREFIX}pilot-2026082{index}T000000Z{BACKUP_SUFFIX}"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (value INTEGER)")
            conn.execute("INSERT INTO marker VALUES (?)", (index,))
        os.utime(path, (index + 1, index + 1))
        candidates.append(path)
    unrelated = backup_dir / "newsroom-notes.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    partial = backup_dir / f"{BACKUP_PREFIX}unfinished{BACKUP_SUFFIX}.partial"
    partial.write_text("preserve", encoding="utf-8")
    corrupt = backup_dir / f"{BACKUP_PREFIX}corrupt{BACKUP_SUFFIX}"
    corrupt.write_text("not sqlite", encoding="utf-8")

    removed = prune_backups(
        backup_dir,
        source_db=tmp_path / "pilot.db",
        retain_count=2,
    )

    assert {path.name for path in removed} == {candidates[0].name, candidates[1].name}
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert partial.read_text(encoding="utf-8") == "preserve"
    assert corrupt.read_text(encoding="utf-8") == "not sqlite"
    assert candidates[2].exists() and candidates[3].exists()


def test_retention_for_one_database_cannot_delete_other_database_backups(tmp_path):
    backup_dir = tmp_path / "shared-backups"
    backup_dir.mkdir()
    pilot_db = tmp_path / "pilot.db"
    dbmod.init_db(pilot_db)

    def make_verified_backup(stem, timestamp, modified_at):
        path = backup_dir / f"{BACKUP_PREFIX}{stem}-{timestamp}{BACKUP_SUFFIX}"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (source_stem TEXT NOT NULL)")
            conn.execute("INSERT INTO marker VALUES (?)", (stem,))
        os.utime(path, (modified_at, modified_at))
        return path

    pilot_backups = [
        make_verified_backup("pilot", f"2026082{index}T000000.000000Z", index + 10)
        for index in range(4)
    ]
    acceptance_backups = [
        make_verified_backup("acceptance", f"2026082{index}T010000.000000Z", index + 1)
        for index in range(3)
    ]
    acceptance_before = {
        path.name: _sha256(path)
        for path in acceptance_backups
    }

    result = create_backup(
        pilot_db,
        backup_dir,
        retain_count=2,
        now=NOW,
    )

    assert {Path(path).name for path in result["pruned_paths"]} == {
        pilot_backups[0].name,
        pilot_backups[1].name,
        pilot_backups[2].name,
    }
    assert pilot_backups[3].exists()
    assert Path(result["backup_path"]).exists()
    assert {
        path.name: _sha256(path)
        for path in acceptance_backups
    } == acceptance_before


def test_service_instance_lock_rejects_second_process_owner(tmp_db_path):
    first = ServiceInstanceLock(tmp_db_path)
    second = ServiceInstanceLock(tmp_db_path)
    first.acquire()
    try:
        assert service_instance_running(tmp_db_path) is True
        with pytest.raises(ServiceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    assert service_instance_running(tmp_db_path) is False
    second.acquire()
    second.release()


def test_shutdown_gate_allows_active_cycle_to_finish_and_starts_no_new_cycle(
    tmp_db_path, monkeypatch
):
    settings = replace(_settings(monkeypatch, tmp_db_path), shutdown_grace_seconds=2)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(2)
        return _completed_result()

    scheduler = CollectionScheduler(settings=settings, cycle_runner=runner, now_fn=lambda: NOW)
    scheduler.start()
    assert entered.wait(1)
    stop_result = []
    stopper = threading.Thread(target=lambda: stop_result.append(scheduler.stop()))
    stopper.start()
    for _ in range(100):
        with dbmod.connect(tmp_db_path) as conn:
            if dbmod.get_scheduler_state(conn)["worker_state"] == "STOPPING":
                break
        time.sleep(0.01)
    else:
        pytest.fail("scheduler never persisted STOPPING")
    release.set()
    stopper.join(2)

    assert stop_result == [True]
    assert len(calls) == 1
    assert scheduler.tick(now=NOW + timedelta(hours=1)) is None
    with dbmod.connect(tmp_db_path) as conn:
        assert dbmod.get_scheduler_state(conn)["worker_state"] == "STOPPED"


def test_idle_scheduler_stop_finishes_with_terminal_stopped_state(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    completed = threading.Event()

    def runner(**kwargs):
        completed.set()
        return _completed_result("idle-stop")

    scheduler = CollectionScheduler(settings=settings, cycle_runner=runner, now_fn=lambda: NOW)
    scheduler.start()
    assert completed.wait(1)
    assert scheduler.stop() is True
    with dbmod.connect(tmp_db_path) as conn:
        state = dbmod.get_scheduler_state(conn)
    assert state["worker_state"] == "STOPPED"
    assert state["next_run_at"] is None


def test_startup_recovery_preserves_committed_source_evidence(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    expired = NOW - timedelta(minutes=10)
    with dbmod.connect(tmp_db_path) as conn:
        dbmod.acquire_collection_run(
            conn,
            run_id="interrupted-with-source",
            owner_token="dead-owner",
            trigger_type=CollectionTrigger.SCHEDULED,
            requested_at=expired,
            scheduled_for=expired,
            lease_seconds=60,
        )
        with dbmod.transaction(conn):
            dbmod.insert_collection_source_result(
                conn,
                run_id="interrupted-with-source",
                source_name="alpha",
                attempted_at=expired,
                completed_at=expired + timedelta(seconds=1),
                outcome="SUCCEEDED",
                source_status="HEALTHY",
                item_count=2,
                items_updated=0,
                latency_ms=5,
                failure_kind=None,
                message=None,
            )

    scheduler = CollectionScheduler(settings=settings, now_fn=lambda: NOW, worker_id="restart")
    assert scheduler.initialize() == ["interrupted-with-source"]
    with dbmod.connect(tmp_db_path) as conn:
        run = dbmod.get_collection_run(conn, "interrupted-with-source")
        recovery = dbmod.latest_startup_recovery(conn)
    assert run["state"] == "ABANDONED"
    assert run["completed_cleanly"] is False
    assert run["sources"][0]["source_name"] == "alpha"
    assert recovery["recovered_run_ids"] == ["interrupted-with-source"]


def test_startup_immediately_recovers_prior_service_lease_but_honors_manual_lease(
    tmp_db_path, monkeypatch
):
    settings = _settings(monkeypatch, tmp_db_path)
    with dbmod.connect(tmp_db_path) as conn:
        dbmod.acquire_collection_run(
            conn,
            run_id="crashed-service",
            owner_token="service:old-worker:opaque",
            trigger_type=CollectionTrigger.SCHEDULED,
            requested_at=NOW,
            scheduled_for=NOW,
            lease_seconds=120,
        )
    scheduler = CollectionScheduler(
        settings=settings,
        now_fn=lambda: NOW + timedelta(seconds=5),
        worker_id="new-worker",
        prior_service_owner_proven_dead=True,
    )
    assert scheduler.initialize() == ["crashed-service"]

    with dbmod.connect(tmp_db_path) as conn:
        dbmod.acquire_collection_run(
            conn,
            run_id="manual-owner",
            owner_token="opaque-manual-token",
            trigger_type=CollectionTrigger.MANUAL,
            requested_at=NOW + timedelta(seconds=6),
            scheduled_for=None,
            lease_seconds=120,
        )
    later = CollectionScheduler(
        settings=settings,
        now_fn=lambda: NOW + timedelta(seconds=10),
        worker_id="another-worker",
    )
    assert later.initialize() == []
    with dbmod.connect(tmp_db_path) as conn:
        assert dbmod.get_collection_run(conn, "manual-owner")["state"] == "RUNNING"
        assert dbmod.current_collection_lease(conn, now=NOW + timedelta(seconds=10))["active"]


def test_health_reports_healthy_stale_failure_running_and_abandoned(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path, stale=300)
    scheduler = CollectionScheduler(settings=settings, now_fn=lambda: NOW, worker_id="health-worker")
    scheduler.initialize()
    execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        settings=settings,
        collectors=[StaticCollector("alpha", items=[_item("alpha")])],
        now_fn=lambda: NOW,
        run_id="healthy-run",
        owner_token="healthy-owner",
    )
    healthy = build_operator_health(settings, now=NOW, process_alive=True)
    assert healthy["healthy"] is True
    assert healthy["freshness_state"] == "FRESH"

    stale = build_operator_health(settings, now=NOW + timedelta(minutes=6))
    assert stale["healthy"] is False
    assert stale["freshness_state"] == "STALE"

    execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        settings=settings,
        collectors=[StaticCollector("alpha", status=SourceStatus.FAILED, error="FetchError: down")],
        now_fn=lambda: NOW + timedelta(minutes=1),
        run_id="partial-run",
        owner_token="partial-owner",
    )
    partial = build_operator_health(settings, now=NOW + timedelta(minutes=1))
    assert partial["healthy"] is False
    assert partial["freshness_state"] == "FRESH_PARTIAL_FAILURE"
    assert partial["source_problems"][0]["freshness_state"] == "FAILING"

    running_at = NOW + timedelta(minutes=2)
    with dbmod.connect(tmp_db_path) as conn:
        dbmod.acquire_collection_run(
            conn,
            run_id="currently-running",
            owner_token="active-secret-token",
            trigger_type=CollectionTrigger.SCHEDULED,
            requested_at=running_at,
            scheduled_for=running_at,
            lease_seconds=60,
        )
    running = build_operator_health(settings, now=running_at)
    assert running["current_run"]["run_id"] == "currently-running"
    assert "owner_token" not in running["active_lease"]

    recovered = CollectionScheduler(
        settings=settings,
        now_fn=lambda: running_at + timedelta(minutes=2),
        worker_id="recovery-worker",
    )
    assert recovered.initialize() == ["currently-running"]
    abandoned = build_operator_health(settings, now=running_at + timedelta(minutes=2))
    assert abandoned["latest_abandoned_run"]["run_id"] == "currently-running"
    assert abandoned["startup_recovery"]["recovered_count"] == 1


def test_restart_schedules_one_immediate_cycle_without_catch_up(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path, cadence=300)
    first_calls = []
    first = CollectionScheduler(
        settings=settings,
        cycle_runner=lambda **kwargs: first_calls.append(kwargs) or _completed_result("first"),
        now_fn=lambda: NOW,
        worker_id="first-worker",
    )
    first.initialize()
    first.tick(now=NOW)

    restart_now = NOW + timedelta(hours=2)
    restart_calls = []
    restarted = CollectionScheduler(
        settings=settings,
        cycle_runner=lambda **kwargs: restart_calls.append(kwargs) or _completed_result("restart"),
        now_fn=lambda: restart_now,
        worker_id="restart-worker",
    )
    restarted.initialize()
    restarted.tick(now=restart_now)
    restarted.tick(now=restart_now + timedelta(seconds=299))

    assert len(first_calls) == 1
    assert len(restart_calls) == 1
    assert restarted.next_run_at == restart_now + timedelta(seconds=300)


def test_health_json_is_serializable(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    report = build_operator_health(settings, now=NOW)
    json.dumps(report, sort_keys=True)


def test_operational_endpoints_distinguish_liveness_from_data_health(tmp_db_path, monkeypatch):
    _settings(monkeypatch, tmp_db_path)
    from kc_news_radar import app as appmod

    client = TestClient(appmod.app)
    health = client.get("/api/health").json()
    status = client.get("/api/collection/status").json()

    assert health["process_alive"] is True
    assert health["ok"] is False
    assert health["operational_healthy"] is False
    assert status["process_alive"] is True
    assert status["database_path"] == str(tmp_db_path)
    assert status["freshness_state"] == "NO_COMPLETED_RUN"
