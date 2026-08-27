"""FastAPI application and closed-loop dashboard for KC News Radar.

Endpoints:

    GET /api/health           liveness + settings summary
    GET /api/sources          per-source adapter status
    GET /api/signals          latest detected signals
    GET /api/forecasts        latest version of each forecast
    GET /api/forecasts/{id}   all versions of one forecast + resolution
    GET /api/brief            morning strategy brief (primary UI payload)
    GET /api/items            latest normalized source items
    POST /api/feedback        record newsroom feedback (label + optional note)
    POST /api/forecasts/{id}/resolution  record one immutable outcome
    GET /api/performance      descriptive resolved-forecast counts
    GET /api/collection/status current scheduler and freshness state
    GET /api/collection/runs   recent durable run history
    GET /api/collection/runs/{id} one run with events and source results

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
from .collectors import ALL_COLLECTORS
from .config import load_settings
from .ledger.resolution import forecast_status_after_now
from .ledger.resolution import record_resolution
from .models import Outcome
from .performance import build_performance_summary
from .freshness import build_collection_status
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


def _database_context(health_rows: list[dict[str, Any]]) -> dict[str, Any]:
    settings = load_settings()
    configured = {collector.name for collector in ALL_COLLECTORS}
    observed = {row["source_name"] for row in health_rows}
    unconfigured = sorted(observed - configured)
    missing = sorted(configured - observed)
    attempts = [
        datetime.fromisoformat(row["last_attempt"])
        for row in health_rows
        if row.get("last_attempt")
    ]
    latest = max(attempts) if attempts else None
    age_hours = (
        round((datetime.now(timezone.utc) - latest).total_seconds() / 3600, 1)
        if latest else None
    )
    warnings: list[str] = []
    if not settings.db_path_explicit:
        warnings.append(
            "Using the default database path. Set KC_NEWS_RADAR_DB to select a verified snapshot explicitly."
        )
    if not health_rows:
        warnings.append("This database has no collection-health evidence yet.")
    if age_hours is not None and age_hours * 3600 > settings.stale_after_seconds:
        warnings.append(f"The latest collection attempt is {age_hours} hours old.")
    if unconfigured:
        warnings.append(
            "This snapshot contains source identities not present in the current collector configuration: "
            + ", ".join(unconfigured)
        )
    if missing:
        warnings.append(
            "This snapshot has no health record for configured sources: " + ", ".join(missing)
        )
    return {
        "path": str(settings.db_path),
        "selection": "explicit" if settings.db_path_explicit else "default",
        "latest_collection_attempt": latest.isoformat() if latest else None,
        "age_hours": age_hours,
        "configured_source_mismatch": bool(unconfigured or missing),
        "unconfigured_sources": unconfigured,
        "missing_configured_sources": missing,
        "warnings": warnings,
        "safe_to_present_as_current": not warnings,
    }


def _group_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        signal = grouped.setdefault(
            row["signal_id"],
            {
                "signal_id": row["signal_id"],
                "signal_type": row["signal_type"],
                "title": row["signal_title"],
                "summary": row["signal_summary"],
                "created_at": row["signal_created_at"],
                "novelty_score": row["signal_novelty_score"],
                "local_impact_score": row["signal_local_impact_score"],
                "source_records": [],
            },
        )
        signal["source_records"].append({
            "source_item_id": row["source_item_id"],
            "source_name": row["source_name"],
            "external_id": row["external_id"],
            "canonical_url": row["canonical_url"],
            "title": row["item_title"],
            "excerpt": row["item_excerpt"],
            "published_at": row["published_at"],
            "event_at": row["event_at"],
            "retrieved_at": row["retrieved_at"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "geography": row["geography"],
            "beat": row["beat"],
            "content_hash": row["content_hash"],
            "metadata": row["public_metadata"],
            "relationship": row["relationship"],
            "weight": row["evidence_weight"],
        })
    return list(grouped.values())


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
        health_rows = dbmod.list_source_health(conn)
        contains_demo_data = bool(conn.execute(
            "SELECT 1 FROM source_items WHERE title LIKE '[DEMO DATA]%' LIMIT 1"
        ).fetchone())
    return {
        "ok": True,
        "version": __version__,
        "scoring_model_version": SCORING_MODEL_VERSION,
        "demo_mode": settings.demo_mode or contains_demo_data,
        "contains_demo_data": contains_demo_data,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "database": _database_context(health_rows),
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
        evidence_rows = dbmod.get_forecast_evidence(conn, forecast_id, latest_version := int(versions[-1]["version"]))
        feedback = dbmod.list_feedback(conn, subject_type="forecast", subject_id=forecast_id)
    latest = versions[-1]
    return {
        "forecast_id": forecast_id,
        "versions": versions,
        "latest": latest,
        "resolution": resolution,
        "supporting_evidence": _group_evidence(evidence_rows),
        "evidence_status": "CAPTURED_AT_ISSUANCE" if evidence_rows else "LEGACY_NOT_CAPTURED",
        "evidence_version": latest_version,
        "evidence_limitations": (
            [
                "Evidence is an immutable snapshot captured when this forecast version was issued.",
                "The records support the detected signals; they do not by themselves establish the forecast outcome.",
                "Fields absent from the public source remain absent.",
            ]
            if evidence_rows else
            [
                "This forecast predates immutable evidence snapshots.",
                "Current signals or current upstream content were not substituted for missing historical evidence.",
            ]
        ),
        "editorial_feedback": feedback,
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


class ResolutionIn(BaseModel):
    forecast_version: int = Field(ge=1)
    outcome: Outcome
    evidence: str = Field(min_length=3, max_length=2000)
    notes: str | None = Field(default=None, max_length=1000)


@app.post("/api/feedback")
def api_feedback(payload: FeedbackIn) -> dict[str, Any]:
    with _conn() as conn:
        with dbmod.transaction(conn):
            new_id = dbmod.insert_feedback(
                conn, payload.subject_type, payload.subject_id, payload.label, payload.note
            )
    return {"ok": True, "id": new_id}


@app.post("/api/forecasts/{forecast_id}/resolution", status_code=201)
def api_record_resolution(forecast_id: str, payload: ResolutionIn) -> dict[str, Any]:
    with _conn() as conn:
        try:
            with dbmod.transaction(conn):
                resolution = record_resolution(
                    conn,
                    forecast_id=forecast_id,
                    forecast_version=payload.forecast_version,
                    outcome=payload.outcome,
                    evidence=payload.evidence,
                    notes=payload.notes,
                )
        except ValueError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("unknown forecast") else 409
            raise HTTPException(status_code=status, detail=detail) from exc
    return {
        "ok": True,
        "resolution": resolution.model_dump(mode="json"),
        "forecast_immutable": True,
    }


@app.get("/api/performance")
def api_performance() -> dict[str, Any]:
    with _conn() as conn:
        return build_performance_summary(conn)


@app.get("/api/collection/status")
def api_collection_status() -> dict[str, Any]:
    settings = load_settings()
    with _conn() as conn:
        return build_collection_status(conn, settings=settings)


@app.get("/api/collection/runs")
def api_collection_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    with _conn() as conn:
        rows = dbmod.list_collection_runs(conn, limit=limit)
    return {"runs": rows, "count": len(rows)}


@app.get("/api/collection/runs/{run_id}")
def api_collection_run_detail(run_id: str) -> dict[str, Any]:
    with _conn() as conn:
        run = dbmod.get_collection_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown collection run {run_id!r}")
    return run


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
    selection = "explicit KC_NEWS_RADAR_DB" if settings.db_path_explicit else "default path"
    print(f"KC News Radar database ({selection}): {settings.db_path}", flush=True)
    if not settings.db_path_explicit:
        print(
            "WARNING: default database selection; set KC_NEWS_RADAR_DB to launch against a verified snapshot.",
            flush=True,
        )
    uvicorn.run(
        "kc_news_radar.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    serve()
