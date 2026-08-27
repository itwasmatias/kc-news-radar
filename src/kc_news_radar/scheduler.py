"""Small in-process cadence worker backed by the cross-process SQLite lease."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import db as dbmod
from .collection_runtime import CollectionCycleResult, execute_collection_cycle
from .config import Settings, load_settings
from .models import CollectionTrigger


log = logging.getLogger("kc_news_radar.scheduler")


def next_run_after(completed_at: datetime, cadence_seconds: int) -> datetime:
    return completed_at + timedelta(seconds=cadence_seconds)


class CollectionScheduler:
    """Single-thread cadence controller; SQLite provides process-wide exclusion."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        cycle_runner: Callable[..., CollectionCycleResult] = execute_collection_cycle,
        now_fn: Callable[[], datetime] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.cycle_runner = cycle_runner
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.worker_id = worker_id or str(uuid.uuid4())
        self.next_run_at: datetime | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def initialize(self) -> list[str]:
        dbmod.init_db(self.settings.db_path)
        now = self.now_fn()
        with dbmod.connect(self.settings.db_path) as conn:
            recovered = dbmod.recover_abandoned_collection_runs(conn, now=now)
            self.next_run_at = now if self.settings.collection_enabled else None
            with dbmod.transaction(conn):
                dbmod.upsert_scheduler_state(
                    conn,
                    configured_enabled=self.settings.collection_enabled,
                    worker_state="IDLE" if self.settings.collection_enabled else "DISABLED",
                    cadence_seconds=self.settings.collection_cadence_seconds,
                    stale_after_seconds=self.settings.stale_after_seconds,
                    next_run_at=self.next_run_at,
                    heartbeat_at=now,
                    worker_id=self.worker_id,
                )
        return recovered

    def _persist_state(self, worker_state: str, now: datetime) -> None:
        with dbmod.connect(self.settings.db_path) as conn:
            with dbmod.transaction(conn):
                dbmod.upsert_scheduler_state(
                    conn,
                    configured_enabled=self.settings.collection_enabled,
                    worker_state=worker_state,
                    cadence_seconds=self.settings.collection_cadence_seconds,
                    stale_after_seconds=self.settings.stale_after_seconds,
                    next_run_at=self.next_run_at,
                    heartbeat_at=now,
                    worker_id=self.worker_id,
                )

    def tick(self, *, now: datetime | None = None) -> CollectionCycleResult | None:
        """Run one due cycle. Tests call this directly without waiting."""
        now = now or self.now_fn()
        if not self.settings.collection_enabled:
            self.next_run_at = None
            self._persist_state("DISABLED", now)
            return None
        if self.next_run_at is None:
            self.next_run_at = now
        if now < self.next_run_at:
            self._persist_state("IDLE", now)
            return None

        scheduled_for = self.next_run_at
        self._persist_state("COLLECTING", now)
        result = self.cycle_runner(
            trigger_type=CollectionTrigger.SCHEDULED,
            scheduled_for=scheduled_for,
            settings=self.settings,
            reset_demo=False,
        )
        completed_at = self.now_fn()
        self.next_run_at = next_run_after(
            completed_at, self.settings.collection_cadence_seconds
        )
        self._persist_state("IDLE", completed_at)
        return result

    def run_forever(self) -> None:
        if self.next_run_at is None and self.settings.collection_enabled:
            self.initialize()
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # Whole-cycle failures are normally returned as durable FAILED
                # results. This protects later cadence cycles from scheduler bugs.
                log.exception("scheduled collection tick failed")
                now = self.now_fn()
                self.next_run_at = next_run_after(
                    now, self.settings.collection_cadence_seconds
                )
                self._persist_state("ERROR_WAITING", now)
            now = self.now_fn()
            wait_seconds = 30.0
            if self.next_run_at is not None:
                wait_seconds = max(
                    0.1,
                    min(30.0, (self.next_run_at - now).total_seconds()),
                )
            self._stop_event.wait(wait_seconds)

    def start(self) -> None:
        self.initialize()
        if not self.settings.collection_enabled:
            return
        self._thread = threading.Thread(
            target=self.run_forever,
            name="kc-news-radar-collector",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        now = self.now_fn()
        self.next_run_at = None
        self._persist_state("STOPPED", now)


def run_service() -> None:
    """Console entry point: dashboard plus automatic collection worker."""
    import uvicorn

    settings = load_settings()
    selection = "explicit KC_NEWS_RADAR_DB" if settings.db_path_explicit else "default path"
    scheduler = CollectionScheduler(settings=settings)
    print(f"KC News Radar database ({selection}): {settings.db_path}", flush=True)
    print(
        f"automatic collection: {'ENABLED' if settings.collection_enabled else 'DISABLED'}; "
        f"cadence={settings.collection_cadence_seconds}s; "
        f"stale-after={settings.stale_after_seconds}s",
        flush=True,
    )
    print(f"dashboard: http://{settings.host}:{settings.port}", flush=True)
    if not settings.db_path_explicit:
        print(
            "WARNING: default database selection; set KC_NEWS_RADAR_DB to select a verified live database.",
            flush=True,
        )
    scheduler.start()
    print(
        f"next expected collection: {scheduler.next_run_at.isoformat() if scheduler.next_run_at else 'disabled'}",
        flush=True,
    )
    try:
        uvicorn.run(
            "kc_news_radar.app:app",
            host=settings.host,
            port=settings.port,
            log_level="info",
        )
    finally:
        scheduler.stop()


__all__ = ["CollectionScheduler", "next_run_after", "run_service"]


if __name__ == "__main__":
    run_service()
