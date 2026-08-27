"""Small in-process cadence worker backed by the cross-process SQLite lease."""

from __future__ import annotations

import logging
import signal
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import db as dbmod
from .collection_runtime import CollectionCycleResult, execute_collection_cycle
from .config import Settings, load_settings
from .models import CollectionTrigger
from .service_lock import ServiceInstanceLock


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
        prior_service_owner_proven_dead: bool = False,
    ) -> None:
        self.settings = settings or load_settings()
        self.cycle_runner = cycle_runner
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.worker_id = worker_id or str(uuid.uuid4())
        self.prior_service_owner_proven_dead = prior_service_owner_proven_dead
        self.next_run_at: datetime | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_gate = threading.Lock()
        self._cycle_active = threading.Event()
        self._initialized = False
        self.recovered_run_ids: list[str] = []

    def initialize(self) -> list[str]:
        if self._initialized:
            return list(self.recovered_run_ids)
        dbmod.init_db(self.settings.db_path)
        now = self.now_fn()
        with dbmod.connect(self.settings.db_path) as conn:
            # run_service holds the per-DB process lock before initialize(). A
            # prior service-owned lease therefore cannot still have a live
            # owner, even when its time-based expiry is in the future. Manual
            # collector leases have no such prefix and remain honored.
            recovered = dbmod.recover_abandoned_collection_runs(
                conn,
                now=now,
                invalid_owner_token_prefix=(
                    "service:" if self.prior_service_owner_proven_dead else None
                ),
            )
            self.next_run_at = now if self.settings.collection_enabled else None
            with dbmod.transaction(conn):
                dbmod.record_startup_recovery(
                    conn,
                    startup_id=self.worker_id,
                    checked_at=now,
                    recovered_run_ids=recovered,
                )
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
        if recovered:
            log.warning("startup recovery marked abandoned runs=%s", ",".join(recovered))
        else:
            log.info("startup recovery found no abandoned runs")
        self.recovered_run_ids = list(recovered)
        self._initialized = True
        return list(recovered)

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
        with self._start_gate:
            if self._stop_event.is_set():
                return None
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
            self._cycle_active.set()

        self._persist_state("COLLECTING", now)
        try:
            result = self.cycle_runner(
                trigger_type=CollectionTrigger.SCHEDULED,
                scheduled_for=scheduled_for,
                settings=self.settings,
                owner_token=f"service:{self.worker_id}:{uuid.uuid4()}",
                reset_demo=False,
            )
        finally:
            self._cycle_active.clear()
        completed_at = self.now_fn()
        if self._stop_event.is_set():
            self.next_run_at = None
            self._persist_state("STOPPING", completed_at)
        else:
            self.next_run_at = next_run_after(
                completed_at, self.settings.collection_cadence_seconds
            )
            self._persist_state("IDLE", completed_at)
        return result

    def run_forever(self) -> None:
        if self.next_run_at is None and self.settings.collection_enabled:
            self.initialize()
        log.info("scheduler started worker_id=%s", self.worker_id)
        try:
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
        finally:
            self.next_run_at = None
            self._persist_state("STOPPED", self.now_fn())
            log.info("scheduler stopped worker_id=%s", self.worker_id)

    def start(self) -> None:
        self.initialize()
        if not self.settings.collection_enabled:
            log.info("scheduler disabled by configuration")
            return
        self._thread = threading.Thread(
            target=self.run_forever,
            name="kc-news-radar-collector",
            daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        """Close the new-cycle gate immediately without waiting for active work."""
        with self._start_gate:
            if self._stop_event.is_set():
                return
            now = self.now_fn()
            self.next_run_at = None
            if self._thread is None:
                self._stop_event.set()
                self._persist_state("STOPPED", now)
                return
            # Persist STOPPING before releasing the worker. The worker's
            # finally block is the sole later writer of terminal STOPPED.
            self._persist_state("STOPPING", now)
            self._stop_event.set()

    def stop(self) -> bool:
        """Request stop and return whether the scheduler exited in the grace period."""
        self.request_stop()
        if self._thread is None:
            return True
        if self._thread is not None:
            self._thread.join(timeout=self.settings.shutdown_grace_seconds)
        stopped = not self._thread.is_alive()
        if not stopped:
            log.warning(
                "scheduler did not stop within grace period seconds=%s active_cycle=%s",
                self.settings.shutdown_grace_seconds,
                self._cycle_active.is_set(),
            )
        return stopped


def run_service() -> None:
    """Console entry point: dashboard plus automatic collection worker."""
    import uvicorn

    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    selection = "explicit KC_NEWS_RADAR_DB" if settings.db_path_explicit else "default path"
    with ServiceInstanceLock(settings.db_path):
        scheduler = CollectionScheduler(
            settings=settings,
            prior_service_owner_proven_dead=True,
        )
        log.info(
            "process startup database=%s selection=%s automatic_collection=%s cadence=%s",
            settings.db_path,
            selection,
            settings.collection_enabled,
            settings.collection_cadence_seconds,
        )
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
            config = uvicorn.Config(
                "kc_news_radar.app:app",
                host=settings.host,
                port=settings.port,
                log_level="info",
                timeout_graceful_shutdown=settings.shutdown_grace_seconds,
            )
            server = uvicorn.Server(config)
            previous_handlers = {
                handled: signal.getsignal(handled)
                for handled in (signal.SIGTERM, signal.SIGINT)
            }

            def handle_service_signal(signum, frame) -> None:
                log.info("process received shutdown signal=%s", signum)
                scheduler.request_stop()
                server.handle_exit(signum, frame)

            for handled in previous_handlers:
                signal.signal(handled, handle_service_signal)
            try:
                server.run()
            finally:
                # Uvicorn restores and replays captured signals. Our outer
                # handler converts that replay into a normal return, keeping
                # scheduler cleanup reachable before process exit.
                for handled, previous in previous_handlers.items():
                    signal.signal(handled, previous)
        finally:
            scheduler.stop()
            log.info("process shutdown database=%s", settings.db_path)


__all__ = ["CollectionScheduler", "next_run_after", "run_service"]


if __name__ == "__main__":
    run_service()
