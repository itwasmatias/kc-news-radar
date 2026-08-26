"""FastAPI application: read-oriented dashboard for KC News Radar.

Endpoints (all read-only):

    GET /api/health           liveness + settings summary
    GET /api/sources          per-source adapter status
    GET /api/signals          latest detected signals
    GET /api/forecasts        latest version of each forecast
    GET /api/forecasts/{id}   all versions of one forecast + resolution
    GET /api/brief            morning strategy brief (primary UI payload)
    GET /api/items            latest normalized source items
    POST /api/feedback        record newsroom feedback (label + optional note)

Security notes:

* The server binds to 127.0.0.1 by default (see config.load_settings).
* All responses are JSON. The frontend HTML-escapes external text on render.
* No arbitrary fetch, no shell exec, no auth (local-only prototype).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, SCORING_MODEL_VERSION
from . import db as dbmod
from .config import load_settings
from .ledger.resolution import forecast_status_after_now
from .pipeline.briefing import build_brief

log = logging.getLogger("kc_news_radar.app")

WEB_ROOT = Path(__file__).resolve().parent / "web"

app = FastAPI(
    title="Kansas City News Radar",
    version=__version__,
    description="Experimental early-warning system for local journalism (v0.1 prototype).",
)


def _conn():
    settings = load_settings()
    dbmod.init_db(settings.db_path)
    return dbmod.connect(settings.db_path)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    settings = load_settings()
    with _conn() as conn:
        counts = {
            "items": dbmod.count_source_items(conn),
            "signals": len(dbmod.list_signals(conn, limit=10_000)),
            "forecasts": len(dbmod.list_forecasts_latest(conn, limit=10_000)),
        }
    return {
        "ok": True,
        "version": __version__,
        "scoring_model_version": SCORING_MODEL_VERSION,
        "demo_mode": settings.demo_mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "scientific_note": "Experimental scores are not calibrated probabilities.",
    }


@app.get("/api/sources")
def api_sources() -> dict[str, Any]:
    with _conn() as conn:
        rows = dbmod.list_source_health(conn)
    return {"sources": rows, "count": len(rows)}


@app.get("/api/items")
def api_items(limit: int = Query(50, ge=1, le=500), source: str | None = None) -> dict[str, Any]:
    with _conn() as conn:
        rows = dbmod.list_source_items(conn, limit=limit, source_name=source)
    return {"items": rows, "count": len(rows)}


@app.get("/api/signals")
def api_signals(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    with _conn() as conn:
        rows = dbmod.list_signals(conn, limit=limit)
    return {"signals": rows, "count": len(rows)}


@app.get("/api/forecasts")
def api_forecasts(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _conn() as conn:
        rows = dbmod.list_forecasts_latest(conn, limit=limit)
        resolutions = {r["forecast_id"]: r for r in dbmod.list_resolutions(conn)}
    out = []
    for r in rows:
        r = dict(r)
        r["resolved"] = bool(resolutions.get(r["forecast_id"]))
        r["resolution"] = resolutions.get(r["forecast_id"])
        r["display_status"] = forecast_status_after_now(r, now)
        out.append(r)
    return {
        "forecasts": out,
        "count": len(out),
        "scientific_note": "Experimental scores are not calibrated probabilities.",
        "scoring_model_version": SCORING_MODEL_VERSION,
    }


@app.get("/api/forecasts/{forecast_id}")
def api_forecast_detail(forecast_id: str) -> dict[str, Any]:
    with _conn() as conn:
        versions = dbmod.get_forecast_versions(conn, forecast_id)
        if not versions:
            raise HTTPException(status_code=404, detail=f"unknown forecast_id {forecast_id!r}")
        resolution = dbmod.get_resolution(conn, forecast_id)
        # Evidence: source items joined via any signal referenced by the latest forecast
        # For simplicity, expose the most recent signals referencing this beat/geography.
    latest = versions[-1]
    return {
        "forecast_id": forecast_id,
        "versions": versions,
        "latest": latest,
        "resolution": resolution,
        "scientific_note": "Experimental score — not a calibrated probability.",
    }


@app.get("/api/brief")
def api_brief() -> dict[str, Any]:
    with _conn() as conn:
        return build_brief(conn)


class FeedbackIn(BaseModel):
    subject_type: str = Field(pattern="^(forecast|signal|item)$")
    subject_id: str
    label: str = Field(pattern="^(USEFUL|NOT_USEFUL|ALREADY_KNEW|WATCH|ASSIGNED_REPORTER|NOT_NEWSWORTHY|INCORRECT)$")
    note: str | None = Field(default=None, max_length=400)


@app.post("/api/feedback")
def api_feedback(payload: FeedbackIn) -> dict[str, Any]:
    with _conn() as conn:
        with dbmod.transaction(conn):
            new_id = dbmod.insert_feedback(
                conn, payload.subject_type, payload.subject_id, payload.label, payload.note
            )
    return {"ok": True, "id": new_id}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


app.mount("/web", StaticFiles(directory=str(WEB_ROOT)), name="web")


def serve() -> None:
    """Entry point for the console script."""
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "kc_news_radar.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    serve()
