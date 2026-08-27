"""One observable collection cycle with durable run and overlap evidence."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from . import db as dbmod
from .collectors import build_all
from .collectors.base import BaseCollector, CollectorResult
from .config import Settings, load_settings
from .demo import load_demo_fixtures
from .models import CollectionRunState, CollectionTrigger, SourceHealth, SourceStatus
from .pipeline.forecasting import run_pipeline


log = logging.getLogger("kc_news_radar.collection_runtime")


@dataclass(frozen=True)
class CollectionCycleResult:
    run_id: str
    state: CollectionRunState
    acquired: bool
    blocked_by_run_id: str | None
    sources_attempted: int
    sources_succeeded: int
    sources_failed: int
    items_collected: int
    items_updated: int
    pipeline_result: dict | None
    failure_summary: str | None


def _failure_kind(result: CollectorResult) -> str | None:
    if result.health.status != SourceStatus.FAILED:
        return None
    message = result.health.error_message or ""
    return "SOURCE_UNAVAILABLE" if message.startswith("FetchError:") else "ADAPTER_FAILURE"


def _source_outcome(result: CollectorResult) -> str:
    if result.health.status == SourceStatus.FAILED:
        return "FAILED"
    if result.health.item_count == 0:
        return "ZERO_ITEMS"
    return "SUCCEEDED"


def _run_collector_safely(
    collector: BaseCollector, now_fn: Callable[[], datetime]
) -> CollectorResult:
    try:
        return collector.run()
    except Exception as exc:  # defensive: BaseCollector.run normally catches
        attempted_at = now_fn()
        log.exception("collector %s escaped its isolation boundary", collector.name)
        return CollectorResult(
            source_name=collector.name,
            items=[],
            health=SourceHealth(
                source_name=collector.name,
                status=SourceStatus.FAILED,
                last_attempt=attempted_at,
                last_success=None,
                item_count=0,
                error_message=f"{type(exc).__name__}: {exc}",
                latency_ms=0,
            ),
        )


def _demo_results(conn: sqlite3.Connection) -> list[CollectorResult]:
    """Represent deterministic demo source health as source-run results."""
    from .models import SourceHealth

    return [
        CollectorResult(
            source_name=row["source_name"],
            items=[],
            health=SourceHealth(
                source_name=row["source_name"],
                status=SourceStatus(row["status"]),
                last_attempt=datetime.fromisoformat(row["last_attempt"]),
                last_success=(
                    datetime.fromisoformat(row["last_success"])
                    if row.get("last_success") else None
                ),
                item_count=int(row["item_count"]),
                error_message=row.get("error_message"),
                latency_ms=int(row["latency_ms"]),
            ),
        )
        for row in dbmod.list_source_health(conn)
    ]


def execute_collection_cycle(
    *,
    trigger_type: CollectionTrigger,
    scheduled_for: datetime | None = None,
    only_source: str | None = None,
    settings: Settings | None = None,
    collectors: Sequence[BaseCollector] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    run_id: str | None = None,
    owner_token: str | None = None,
    reset_demo: bool = False,
    pipeline_runner: Callable[..., dict] = run_pipeline,
) -> CollectionCycleResult:
    """Execute one bounded cycle. All callers share the same SQLite lease."""
    settings = settings or load_settings()
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    requested_at = now_fn()
    run_id = run_id or str(uuid.uuid4())
    owner_token = owner_token or str(uuid.uuid4())
    effective_trigger = CollectionTrigger.DEMO if settings.demo_mode else trigger_type

    dbmod.init_db(settings.db_path)
    with dbmod.connect(settings.db_path) as conn:
        acquisition = dbmod.acquire_collection_run(
            conn,
            run_id=run_id,
            owner_token=owner_token,
            trigger_type=effective_trigger,
            requested_at=requested_at,
            scheduled_for=scheduled_for,
            lease_seconds=settings.collection_lease_seconds,
        )
    if not acquisition["acquired"]:
        log.warning(
            "collection cycle blocked by active run run_id=%s blocked_by=%s",
            run_id,
            acquisition["blocked_by_run_id"],
        )
        return CollectionCycleResult(
            run_id=run_id,
            state=CollectionRunState.BLOCKED_OVERLAP,
            acquired=False,
            blocked_by_run_id=acquisition["blocked_by_run_id"],
            sources_attempted=0,
            sources_succeeded=0,
            sources_failed=0,
            items_collected=0,
            items_updated=0,
            pipeline_result=None,
            failure_summary="Another collection run holds the database lease.",
        )

    attempted = succeeded = failed = total_items = total_updated = 0
    pipeline_result: dict | None = None
    failures: list[str] = []
    log.info("collection cycle begin run_id=%s trigger=%s", run_id, effective_trigger.value)

    try:
        with dbmod.connect(settings.db_path) as conn:
            if settings.demo_mode:
                has_items = dbmod.count_source_items(conn) > 0
                if reset_demo or not has_items:
                    load_demo_fixtures(conn)
                elif conn.execute(
                    "SELECT 1 FROM source_items WHERE title NOT LIKE '[DEMO DATA]%' LIMIT 1"
                ).fetchone():
                    raise ValueError(
                        "demo scheduling refused because the selected database contains non-demo source items"
                    )
                results = _demo_results(conn)
            else:
                selected = list(collectors) if collectors is not None else build_all(
                    http_timeout=settings.http_timeout,
                    user_agent=settings.user_agent,
                )
                if only_source:
                    selected = [collector for collector in selected if collector.name == only_source]
                    if not selected:
                        raise ValueError(f"no collector named {only_source!r}")
                # Lazy execution lets each result and lease heartbeat commit
                # before the next adapter starts.
                results = (_run_collector_safely(collector, now_fn) for collector in selected)

            for result in results:
                attempted += 1
                source_updated = 0
                if settings.demo_mode:
                    total_items += result.health.item_count
                with dbmod.transaction(conn):
                    for item in result.items:
                        _, was_updated = dbmod.upsert_source_item(conn, item)
                        total_items += 1
                        if was_updated:
                            total_updated += 1
                            source_updated += 1
                    if not settings.demo_mode:
                        dbmod.upsert_source_health(conn, result.health)
                    outcome = _source_outcome(result)
                    if outcome == "FAILED":
                        failed += 1
                        failures.append(f"{result.source_name}: {result.health.error_message or 'failed'}")
                    else:
                        succeeded += 1
                    completed_at = now_fn()
                    dbmod.insert_collection_source_result(
                        conn,
                        run_id=run_id,
                        source_name=result.source_name,
                        attempted_at=result.health.last_attempt,
                        completed_at=completed_at,
                        outcome=outcome,
                        source_status=result.health.status.value,
                        item_count=result.health.item_count,
                        items_updated=source_updated,
                        latency_ms=result.health.latency_ms,
                        failure_kind=_failure_kind(result),
                        message=result.health.error_message,
                    )
                    dbmod.heartbeat_collection_lease(
                        conn,
                        run_id=run_id,
                        owner_token=owner_token,
                        now=completed_at,
                        lease_seconds=settings.collection_lease_seconds,
                    )

            pipeline_result = pipeline_runner(conn, now=now_fn())
            completed_at = now_fn()
            state = (
                CollectionRunState.PARTIAL_FAILURE if failed
                else CollectionRunState.COMPLETED
            )
            failure_summary = "; ".join(failures) if failures else None
            with dbmod.transaction(conn):
                dbmod.finish_collection_run(
                    conn,
                    run_id=run_id,
                    owner_token=owner_token,
                    completed_at=completed_at,
                    state=state,
                    sources_attempted=attempted,
                    sources_succeeded=succeeded,
                    sources_failed=failed,
                    items_collected=total_items,
                    items_updated=total_updated,
                    pipeline_result=pipeline_result,
                    failure_summary=failure_summary,
                    completed_cleanly=(failed == 0),
                )
    except Exception as exc:
        failure_summary = f"{type(exc).__name__}: {exc}"
        log.exception("collection run %s failed", run_id)
        try:
            with dbmod.connect(settings.db_path) as conn:
                with dbmod.transaction(conn):
                    dbmod.finish_collection_run(
                        conn,
                        run_id=run_id,
                        owner_token=owner_token,
                        completed_at=now_fn(),
                        state=CollectionRunState.FAILED,
                        sources_attempted=attempted,
                        sources_succeeded=succeeded,
                        sources_failed=failed,
                        items_collected=total_items,
                        items_updated=total_updated,
                        pipeline_result=pipeline_result,
                        failure_summary=failure_summary,
                        completed_cleanly=False,
                    )
        except Exception:
            log.exception("could not finalize failed collection run %s", run_id)
        return CollectionCycleResult(
            run_id=run_id,
            state=CollectionRunState.FAILED,
            acquired=True,
            blocked_by_run_id=None,
            sources_attempted=attempted,
            sources_succeeded=succeeded,
            sources_failed=failed,
            items_collected=total_items,
            items_updated=total_updated,
            pipeline_result=pipeline_result,
            failure_summary=failure_summary,
        )

    if failures:
        log.warning(
            "collection cycle source failures run_id=%s failed=%s summary=%s",
            run_id,
            failed,
            failure_summary,
        )
    log.info(
        "collection cycle end run_id=%s state=%s sources=%s/%s items=%s",
        run_id,
        state.value,
        succeeded,
        attempted,
        total_items,
    )
    return CollectionCycleResult(
        run_id=run_id,
        state=state,
        acquired=True,
        blocked_by_run_id=None,
        sources_attempted=attempted,
        sources_succeeded=succeeded,
        sources_failed=failed,
        items_collected=total_items,
        items_updated=total_updated,
        pipeline_result=pipeline_result,
        failure_summary=failure_summary,
    )


__all__ = ["CollectionCycleResult", "execute_collection_cycle"]
