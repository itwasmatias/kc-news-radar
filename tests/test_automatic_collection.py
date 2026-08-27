"""Deterministic offline tests for observable automatic collection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kc_news_radar import db as dbmod
from kc_news_radar.collection_runtime import CollectionCycleResult, execute_collection_cycle
from kc_news_radar.config import load_settings
from kc_news_radar.freshness import build_collection_status
from kc_news_radar.models import (
    Beat,
    CollectionRunState,
    CollectionTrigger,
    SourceHealth,
    SourceItem,
    SourceStatus,
)
from kc_news_radar.collectors.base import CollectorResult, content_hash
from kc_news_radar.scheduler import CollectionScheduler, next_run_after


NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def _settings(monkeypatch, path, *, enabled=True, demo=False, cadence=300, stale=900):
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(path))
    monkeypatch.setenv("KC_NEWS_RADAR_COLLECTION_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("KC_NEWS_RADAR_COLLECTION_CADENCE_SECONDS", str(cadence))
    monkeypatch.setenv("KC_NEWS_RADAR_STALE_AFTER_SECONDS", str(stale))
    monkeypatch.setenv("KC_NEWS_RADAR_COLLECTION_LEASE_SECONDS", "120")
    monkeypatch.setenv("KC_NEWS_RADAR_DEMO", "1" if demo else "0")
    return load_settings()


def _item(source="test_source", external_id="one", retrieved_at=NOW):
    return SourceItem(
        source_name=source,
        external_id=external_id,
        canonical_url=f"https://example.invalid/{source}/{external_id}",
        title="Public meeting notice",
        excerpt="A public body posted a meeting notice.",
        published_at=retrieved_at,
        event_at=retrieved_at + timedelta(hours=12),
        retrieved_at=retrieved_at,
        geography="Kansas City metro",
        beat=Beat.LOCAL_GOVERNMENT,
        content_hash=content_hash(source, external_id),
        metadata={},
    )


class StaticCollector:
    def __init__(self, name, *, status=SourceStatus.HEALTHY, items=None, error=None):
        self.name = name
        self.calls = 0
        self.status = status
        self.items = list(items or [])
        self.error = error

    def run(self):
        self.calls += 1
        return CollectorResult(
            source_name=self.name,
            items=self.items,
            health=SourceHealth(
                source_name=self.name,
                status=self.status,
                last_attempt=NOW,
                last_success=NOW if self.status != SourceStatus.FAILED else None,
                item_count=len(self.items),
                error_message=self.error,
                latency_ms=7,
            ),
        )


class RaisingCollector:
    name = "system_boom"
    calls = 0

    def run(self):
        self.calls += 1
        raise RuntimeError("unexpected runner failure")


def test_collection_run_completion_and_source_history_survive_reopen(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    collector = StaticCollector("alpha", items=[_item("alpha")])
    result = execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        settings=settings,
        collectors=[collector],
        now_fn=lambda: NOW,
        run_id="run-complete",
        owner_token="owner-complete",
    )
    assert result.state == CollectionRunState.COMPLETED
    assert result.sources_attempted == result.sources_succeeded == 1

    reopened = dbmod.connect(tmp_db_path)
    run = dbmod.get_collection_run(reopened, "run-complete")
    reopened.close()
    assert run["state"] == "COMPLETED"
    assert run["completed_cleanly"] is True
    assert [event["state"] for event in run["events"]] == ["RUNNING", "COMPLETED"]
    assert run["sources"][0]["outcome"] == "SUCCEEDED"
    assert run["pipeline_result"]["items_considered"] == 1


def test_source_failure_and_zero_items_are_distinct_and_other_sources_continue(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    unavailable = StaticCollector(
        "unavailable", status=SourceStatus.FAILED,
        error="FetchError: upstream unavailable",
    )
    adapter_failure = StaticCollector(
        "adapter_failure", status=SourceStatus.FAILED,
        error="ValueError: malformed upstream document",
    )
    quiet = StaticCollector("quiet", status=SourceStatus.HEALTHY, items=[])
    healthy = StaticCollector("healthy", items=[_item("healthy")])
    result = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        scheduled_for=NOW,
        settings=settings,
        collectors=[unavailable, RaisingCollector(), adapter_failure, quiet, healthy],
        now_fn=lambda: NOW,
        run_id="run-partial",
        owner_token="owner-partial",
    )
    assert result.state == CollectionRunState.PARTIAL_FAILURE
    assert (result.sources_attempted, result.sources_succeeded, result.sources_failed) == (5, 2, 3)
    assert healthy.calls == 1
    with dbmod.connect(tmp_db_path) as conn:
        sources = {row["source_name"]: row for row in dbmod.get_collection_run(conn, "run-partial")["sources"]}
    assert sources["unavailable"]["failure_kind"] == "SOURCE_UNAVAILABLE"
    assert sources["adapter_failure"]["failure_kind"] == "ADAPTER_FAILURE"
    assert sources["system_boom"]["failure_kind"] == "ADAPTER_FAILURE"
    assert sources["quiet"]["outcome"] == "ZERO_ITEMS"
    assert sources["quiet"]["source_status"] == "HEALTHY"


def test_later_cycle_succeeds_after_whole_run_failure(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    failed = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        settings=settings,
        collectors=[StaticCollector("alpha", items=[_item("alpha")])],
        pipeline_runner=lambda conn, now: (_ for _ in ()).throw(RuntimeError("pipeline failure")),
        now_fn=lambda: NOW,
        run_id="run-failed",
        owner_token="owner-failed",
    )
    assert failed.state == CollectionRunState.FAILED
    recovered = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        settings=settings,
        collectors=[StaticCollector("healthy", items=[_item("healthy")])],
        now_fn=lambda: NOW + timedelta(minutes=5),
        run_id="run-recovered",
        owner_token="owner-recovered",
    )
    assert recovered.state == CollectionRunState.COMPLETED


def test_manual_vs_scheduled_overlap_is_blocked_and_observable(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    dbmod.init_db(tmp_db_path)
    with dbmod.connect(tmp_db_path) as conn:
        held = dbmod.acquire_collection_run(
            conn,
            run_id="manual-active",
            owner_token="manual-owner",
            trigger_type=CollectionTrigger.MANUAL,
            requested_at=NOW,
            scheduled_for=None,
            lease_seconds=120,
        )
    assert held["acquired"] is True
    collector = StaticCollector("must-not-run", items=[_item("must-not-run")])
    blocked = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        scheduled_for=NOW,
        settings=settings,
        collectors=[collector],
        now_fn=lambda: NOW + timedelta(seconds=1),
        run_id="scheduled-blocked",
        owner_token="scheduled-owner",
    )
    assert blocked.state == CollectionRunState.BLOCKED_OVERLAP
    assert blocked.blocked_by_run_id == "manual-active"
    assert collector.calls == 0
    with dbmod.connect(tmp_db_path) as conn:
        run = dbmod.get_collection_run(conn, "scheduled-blocked")
    assert run["state"] == "BLOCKED_OVERLAP"
    assert run["blocked_by_run_id"] == "manual-active"


def test_startup_marks_expired_incomplete_run_abandoned(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    dbmod.init_db(tmp_db_path)
    old = NOW - timedelta(hours=1)
    with dbmod.connect(tmp_db_path) as conn:
        dbmod.acquire_collection_run(
            conn,
            run_id="interrupted",
            owner_token="dead-owner",
            trigger_type=CollectionTrigger.SCHEDULED,
            requested_at=old,
            scheduled_for=old,
            lease_seconds=60,
        )
    scheduler = CollectionScheduler(settings=settings, now_fn=lambda: NOW, worker_id="restart")
    recovered = scheduler.initialize()
    assert recovered == ["interrupted"]
    with dbmod.connect(tmp_db_path) as conn:
        run = dbmod.get_collection_run(conn, "interrupted")
        lease = dbmod.current_collection_lease(conn, now=NOW)
    assert run["state"] == "ABANDONED"
    assert run["completed_cleanly"] is False
    assert lease is None


def test_scheduler_executes_due_cycle_and_calculates_next_without_sleep(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path, cadence=300)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return CollectionCycleResult(
            run_id="scheduled-one", state=CollectionRunState.COMPLETED,
            acquired=True, blocked_by_run_id=None, sources_attempted=1,
            sources_succeeded=1, sources_failed=0, items_collected=1,
            items_updated=0, pipeline_result={}, failure_summary=None,
        )

    scheduler = CollectionScheduler(
        settings=settings, cycle_runner=runner, now_fn=lambda: NOW, worker_id="worker",
    )
    scheduler.initialize()
    result = scheduler.tick(now=NOW)
    assert result.run_id == "scheduled-one"
    assert calls[0]["trigger_type"] == CollectionTrigger.SCHEDULED
    assert calls[0]["scheduled_for"] == NOW
    assert scheduler.next_run_at == next_run_after(NOW, 300)
    assert scheduler.tick(now=NOW + timedelta(seconds=299)) is None


def test_scheduler_disabled_never_invokes_cycle(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path, enabled=False)
    calls = []
    scheduler = CollectionScheduler(
        settings=settings,
        cycle_runner=lambda **kwargs: calls.append(kwargs),
        now_fn=lambda: NOW,
        worker_id="disabled-worker",
    )
    scheduler.initialize()
    assert scheduler.tick(now=NOW) is None
    assert calls == []
    with dbmod.connect(tmp_db_path) as conn:
        state = dbmod.get_scheduler_state(conn)
    assert state["worker_state"] == "DISABLED"
    assert state["next_run_at"] is None


def test_freshness_uses_completed_run_time_and_detects_staleness(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path, stale=300)
    execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        settings=settings,
        collectors=[StaticCollector("alpha", items=[_item("alpha")])],
        now_fn=lambda: NOW,
        run_id="freshness-run",
        owner_token="freshness-owner",
    )
    with dbmod.connect(tmp_db_path) as conn:
        fresh = build_collection_status(conn, settings=settings, now=NOW + timedelta(minutes=4))
        stale = build_collection_status(conn, settings=settings, now=NOW + timedelta(minutes=6))
    assert fresh["freshness_state"] == "FRESH"
    assert stale["freshness_state"] == "STALE"
    assert stale["last_attempt_started_at"] == NOW.isoformat()
    assert stale["last_completed_at"] == NOW.isoformat()
    assert stale["last_successful_at"] == NOW.isoformat()


def test_collection_observability_api_and_dashboard(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path)
    execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        settings=settings,
        collectors=[StaticCollector("alpha", items=[_item("alpha")])],
        now_fn=lambda: NOW,
        run_id="api-run",
        owner_token="api-owner",
    )
    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)
    status = client.get("/api/collection/status")
    history = client.get("/api/collection/runs")
    detail = client.get("/api/collection/runs/api-run")
    html = client.get("/").text
    assert status.status_code == history.status_code == detail.status_code == 200
    assert status.json()["last_completed_run_id"] == "api-run"
    assert history.json()["runs"][0]["run_id"] == "api-run"
    assert detail.json()["sources"][0]["source_name"] == "alpha"
    assert "Automatic collection" in html
    assert "collectionRunsBody" in html


def test_scheduled_demo_cycles_are_offline_visible_and_do_not_mix_real_data(tmp_db_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_db_path, demo=True)
    first = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        scheduled_for=NOW,
        settings=settings,
        now_fn=lambda: NOW,
        run_id="demo-one",
        owner_token="demo-owner-one",
    )
    second = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        scheduled_for=NOW + timedelta(minutes=5),
        settings=settings,
        now_fn=lambda: NOW + timedelta(minutes=5),
        run_id="demo-two",
        owner_token="demo-owner-two",
    )
    assert first.state == second.state == CollectionRunState.COMPLETED
    with dbmod.connect(tmp_db_path) as conn:
        runs = dbmod.list_collection_runs(conn)
        titles = [row["title"] for row in dbmod.list_source_items(conn, limit=500)]
        forecasts = dbmod.list_forecasts_latest(conn)
    assert len(runs) == 2
    assert {run["trigger_type"] for run in runs} == {"DEMO"}
    assert titles and all(title.startswith("[DEMO DATA]") for title in titles)
    assert forecasts


def test_scheduled_demo_refuses_database_containing_real_items(tmp_db_path, monkeypatch):
    live_settings = _settings(monkeypatch, tmp_db_path, demo=False)
    execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        settings=live_settings,
        collectors=[StaticCollector("live", items=[_item("live")])],
        now_fn=lambda: NOW,
        run_id="live-first",
        owner_token="live-owner",
    )
    demo_settings = replace(live_settings, demo_mode=True)
    refused = execute_collection_cycle(
        trigger_type=CollectionTrigger.SCHEDULED,
        settings=demo_settings,
        now_fn=lambda: NOW + timedelta(minutes=5),
        run_id="demo-refused",
        owner_token="demo-refused-owner",
    )
    assert refused.state == CollectionRunState.FAILED
    assert "non-demo source items" in refused.failure_summary
    with dbmod.connect(tmp_db_path) as conn:
        titles = [row["title"] for row in dbmod.list_source_items(conn)]
    assert titles == ["Public meeting notice"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("KC_NEWS_RADAR_COLLECTION_ENABLED", "perhaps"),
        ("KC_NEWS_RADAR_COLLECTION_CADENCE_SECONDS", "5"),
        ("KC_NEWS_RADAR_STALE_AFTER_SECONDS", "not-a-number"),
    ],
)
def test_invalid_collection_configuration_fails_closed(tmp_db_path, monkeypatch, key, value):
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        load_settings()
