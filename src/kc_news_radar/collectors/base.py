"""Base collector contract and runner utilities."""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import SourceHealth, SourceItem, SourceStatus

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Raised when an adapter cannot fetch upstream content."""


@dataclass
class CollectorResult:
    source_name: str
    items: list[SourceItem]
    health: SourceHealth


class BaseCollector(ABC):
    """A public-source adapter.

    Every collector implements:

    * ``fetch()`` — a small, timeout-bounded network call. May raise
      :class:`FetchError`.
    * ``parse(raw)`` — deterministic transformation of raw bytes/JSON into a
      list of :class:`SourceItem`. Callable directly from tests.
    * ``run()`` — orchestrates the two above and returns a
      :class:`CollectorResult` including source health. One collector's failure
      must never crash the collection run; see :func:`run_all_collectors`.
    """

    #: Unique short name used as the source_name in stored records.
    name: str = "unknown"
    #: Human-readable label shown in the UI.
    label: str = "Unknown source"
    #: One of the SourceType categories: government_meeting, alert, news, transit.
    source_type: str = "generic"

    def __init__(self, *, http_timeout: float = 15.0, user_agent: str = "KCNewsRadar/0.1") -> None:
        self.http_timeout = http_timeout
        self.user_agent = user_agent

    # --- adapter API ------------------------------------------------------

    @abstractmethod
    def fetch(self, client: httpx.Client) -> Any:
        """Perform bounded network I/O and return raw response payload(s)."""

    @abstractmethod
    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        """Deterministically parse raw payloads into normalized items."""

    # --- runner -----------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.http_timeout,
            headers={"User-Agent": self.user_agent, "Accept": "*/*"},
            follow_redirects=True,
        )

    def run(self) -> CollectorResult:
        started = time.monotonic()
        attempt_at = datetime.now(timezone.utc)
        try:
            with self._client() as client:
                raw = self.fetch(client)
            items = self.parse(raw, retrieved_at=datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001 — we deliberately isolate every collector
            latency_ms = int((time.monotonic() - started) * 1000)
            log.warning("collector %s failed: %s", self.name, exc)
            return CollectorResult(
                source_name=self.name,
                items=[],
                health=SourceHealth(
                    source_name=self.name,
                    status=SourceStatus.FAILED,
                    last_attempt=attempt_at,
                    last_success=None,
                    item_count=0,
                    error_message=f"{type(exc).__name__}: {exc}",
                    latency_ms=latency_ms,
                ),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        # A successful fetch with zero items is HEALTHY when the adapter opts
        # in (e.g., an alerts feed is legitimately empty during quiet periods).
        # Otherwise, no-items is DEGRADED because something is likely wrong
        # with parsing.
        empty_is_healthy = getattr(self, "empty_ok", False)
        if items or empty_is_healthy:
            status = SourceStatus.HEALTHY
            error_message = None if items else "empty response (no current items)"
        else:
            status = SourceStatus.DEGRADED
            error_message = "no items parsed from response"
        return CollectorResult(
            source_name=self.name,
            items=items,
            health=SourceHealth(
                source_name=self.name,
                status=status,
                last_attempt=attempt_at,
                last_success=attempt_at,
                item_count=len(items),
                error_message=error_message,
                latency_ms=latency_ms,
            ),
        )


def content_hash(*parts: str | None) -> str:
    """Stable SHA-256 hash of the given text parts, used for dedupe."""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").strip().encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def run_all_collectors(collectors: list[BaseCollector]) -> list[CollectorResult]:
    """Run each collector, isolating failures.

    One collector raising or timing out must not prevent others from running.
    """
    results: list[CollectorResult] = []
    for c in collectors:
        try:
            results.append(c.run())
        except Exception as exc:  # extreme defensive: ``run`` already catches
            log.error("collector %s crashed outside run(): %s", c.name, exc)
            results.append(
                CollectorResult(
                    source_name=c.name,
                    items=[],
                    health=SourceHealth(
                        source_name=c.name,
                        status=SourceStatus.FAILED,
                        last_attempt=datetime.now(timezone.utc),
                        last_success=None,
                        item_count=0,
                        error_message=f"{type(exc).__name__}: {exc}",
                        latency_ms=0,
                    ),
                )
            )
    return results
