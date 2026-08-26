"""Record forecast resolutions without mutating original forecasts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import db as dbmod
from ..models import Outcome, Resolution


def record_resolution(
    conn: sqlite3.Connection,
    forecast_id: str,
    outcome: Outcome,
    evidence: str,
    notes: str | None = None,
    resolved_at: datetime | None = None,
) -> Resolution:
    """Persist a resolution row. Original forecast rows are untouched."""
    resolved_at = resolved_at or datetime.now(timezone.utc)
    r = Resolution(
        forecast_id=forecast_id,
        resolved_at=resolved_at,
        outcome=outcome,
        evidence=evidence,
        notes=notes,
    )
    dbmod.insert_resolution(conn, r)
    conn.commit()
    return r


def forecast_status_after_now(forecast_row: dict[str, Any], now: datetime) -> str:
    """Reported status for the dashboard, not the persisted row.

    ``forecasts.status`` is set at issuance time and never changed. But the
    dashboard wants to say EXPIRED when the horizon has passed with no
    resolution row. This is a *display-time* calculation only.
    """
    horizon_end = forecast_row.get("horizon_end")
    if isinstance(horizon_end, str):
        try:
            horizon_end = datetime.fromisoformat(horizon_end)
        except ValueError:
            horizon_end = None
    if horizon_end and horizon_end.tzinfo is None:
        horizon_end = horizon_end.replace(tzinfo=timezone.utc)

    if forecast_row.get("resolved"):
        return "RESOLVED"
    if horizon_end and horizon_end < now:
        return "EXPIRED"
    return forecast_row.get("status", "OPEN")
