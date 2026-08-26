"""Morning Strategy Brief — the primary newsroom deliverable.

Purpose (per PRODUCT STEERING AMENDMENT):

    Reduce the amount of information gathering and mental preparation
    required before newsroom editorial-strategy meetings.

The brief is intentionally short. It surfaces ~5 highest-value items rather
than dozens. Additional signals remain accessible via secondary views.

Structure returned:

    {
      "generated_at":  ISO-8601 datetime,
      "generated_at_local": "Wednesday, Aug. 26 — 6:42 AM CDT",
      "top_priorities": [ up to 5 story candidates ],
      "new":       [ meaningful new developments ],
      "changed":   [ existing developments with materially changed scores ],
      "resolved":  [ resolutions since last brief ],
      "watch":     [ noteworthy below-the-threshold signals ],
      "next_72h":  [ scheduled catalysts within 72h ],
      "questions": [ evidence-backed editorial discussion prompts ],
      "beat_momentum": [ per-beat momentum summary ],
      "disclaimer": "Radar reflects only currently configured and successfully collected public sources.",
    }

All fields are safe to render into HTML/JSON; the frontend is expected to
HTML-escape values on display.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import NEWSROOM_TZ
from .. import db as dbmod
from ..ledger.resolution import forecast_status_after_now


BRIEF_TOP_N = 5
CHANGED_DELTA = 5
WATCH_MAX = 8
NEXT_72H_MAX = 8
QUESTIONS_MAX = 5

DISCLAIMER = (
    "Radar reflects only currently configured and successfully collected public sources. "
    "Experimental scores are not calibrated probabilities. Editorial judgment remains with the newsroom."
)


def _to_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_string(dt: datetime) -> str:
    local = dt.astimezone(NEWSROOM_TZ)
    return local.strftime("%A, %b. %-d — %-I:%M %p %Z")


def build_brief(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)

    forecasts = dbmod.list_forecasts_latest(conn, limit=200)
    resolutions = {r["forecast_id"]: r for r in dbmod.list_resolutions(conn)}
    signals = dbmod.list_signals(conn, limit=200)
    items = dbmod.list_source_items(conn, limit=500)
    health = dbmod.list_source_health(conn)

    # Enrich forecasts with display status and version count.
    forecast_versions_cache: dict[str, list[dict]] = {}
    enriched_forecasts: list[dict[str, Any]] = []
    for f in forecasts:
        versions = forecast_versions_cache.setdefault(
            f["forecast_id"], dbmod.get_forecast_versions(conn, f["forecast_id"])
        )
        resolution = resolutions.get(f["forecast_id"])
        f = dict(f)
        f["resolved"] = bool(resolution)
        f["resolution"] = resolution
        f["display_status"] = forecast_status_after_now(f, now)
        f["version_count"] = len(versions)
        # score delta vs previous version
        if len(versions) >= 2:
            prev = versions[-2]
            f["delta_likelihood"] = int(f["likelihood_score"]) - int(prev["likelihood_score"])
            f["delta_relevance"] = int(f["editorial_relevance_score"]) - int(prev["editorial_relevance_score"])
        else:
            f["delta_likelihood"] = 0
            f["delta_relevance"] = 0
        enriched_forecasts.append(f)

    # ------------------------------------------------------------------
    # Top priorities — ~5 highest priority open forecasts
    # ------------------------------------------------------------------
    open_forecasts = [f for f in enriched_forecasts if f["display_status"] == "OPEN"]
    open_forecasts.sort(key=lambda f: (int(f["priority_score"]), int(f["likelihood_score"])), reverse=True)
    top_priorities = [_forecast_card(f) for f in open_forecasts[:BRIEF_TOP_N]]

    # ------------------------------------------------------------------
    # NEW — forecasts on their first version, issued in the last 24h
    # ------------------------------------------------------------------
    day_ago = now - timedelta(hours=24)
    new_items = [
        _forecast_card(f)
        for f in open_forecasts
        if f["version_count"] == 1 and _to_dt(f["issued_at"]) and _to_dt(f["issued_at"]) >= day_ago
    ][:BRIEF_TOP_N]

    # ------------------------------------------------------------------
    # CHANGED — likelihood/relevance shifted by CHANGED_DELTA in latest version
    # ------------------------------------------------------------------
    changed_items = [
        _forecast_card(f)
        for f in open_forecasts
        if abs(f["delta_likelihood"]) >= CHANGED_DELTA or abs(f["delta_relevance"]) >= CHANGED_DELTA
    ][:BRIEF_TOP_N]

    # ------------------------------------------------------------------
    # RESOLVED — forecasts with recent resolutions
    # ------------------------------------------------------------------
    resolved_items = []
    for f in enriched_forecasts:
        r = f.get("resolution")
        if not r:
            continue
        rdt = _to_dt(r.get("resolved_at"))
        if rdt and rdt < now - timedelta(days=7):
            continue
        resolved_items.append({
            "forecast_id": f["forecast_id"],
            "claim": f["claim"],
            "outcome": r["outcome"],
            "resolved_at": r["resolved_at"],
            "evidence": r["evidence"][:200] if r.get("evidence") else "",
        })

    # ------------------------------------------------------------------
    # WATCH — high-novelty signals that did not clear the forecast threshold
    # ------------------------------------------------------------------
    forecast_evidence_titles = {f["claim"].lower() for f in enriched_forecasts}
    watch: list[dict[str, Any]] = []
    for sig in signals:
        if sig["signal_type"] in {"REPEATED_ENTITY_ACTIVITY", "MULTI_SOURCE_CONVERGENCE", "UNUSUAL_AGENDA_ITEM"}:
            watch.append({
                "signal_id": sig["id"],
                "title": sig["title"],
                "summary": sig["summary"],
                "beat": sig["beat"],
                "signal_type": sig["signal_type"],
                "novelty_score": sig["novelty_score"],
                "local_impact_score": sig["local_impact_score"],
            })
        if len(watch) >= WATCH_MAX:
            break

    # ------------------------------------------------------------------
    # NEXT 72 HOURS — scheduled public events within 72h from source_items
    # ------------------------------------------------------------------
    horizon = now + timedelta(hours=72)
    next_72h: list[dict[str, Any]] = []
    for it in items:
        event_at = _to_dt(it.get("event_at"))
        if not event_at or not (now <= event_at <= horizon):
            continue
        beat = it.get("beat")
        if beat not in {"LOCAL_GOVERNMENT", "STATE_GOVERNMENT", "POLITICS_ELECTIONS", "EDUCATION", "HOUSING_DEVELOPMENT", "WEATHER_ENVIRONMENT", "TRANSPORTATION"}:
            continue
        next_72h.append({
            "title": it["title"],
            "beat": beat,
            "event_at": it["event_at"],
            "geography": it.get("geography"),
            "source_name": it["source_name"],
            "canonical_url": it.get("canonical_url"),
            "hours_out": int((event_at - now).total_seconds() // 3600),
        })
    next_72h.sort(key=lambda e: e["hours_out"])
    next_72h = next_72h[:NEXT_72H_MAX]

    # ------------------------------------------------------------------
    # QUESTIONS WORTH DISCUSSING — evidence-backed editorial prompts
    # These prompts are phrased as questions about public process. They never
    # allege facts about individuals and never propose reporter assignments.
    # ------------------------------------------------------------------
    questions = _generate_questions(top_priorities, next_72h)

    # ------------------------------------------------------------------
    # Beat momentum — per-beat directional summary derived from signals + forecasts
    # ------------------------------------------------------------------
    beat_momentum = _beat_momentum(enriched_forecasts, signals, items)

    healthy = sum(1 for h in health if h["status"] == "HEALTHY")
    degraded = sum(1 for h in health if h["status"] == "DEGRADED")
    failed = sum(1 for h in health if h["status"] == "FAILED")

    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "generated_at_local": _local_string(now),
        "top_priorities": top_priorities,
        "new": new_items,
        "changed": changed_items,
        "resolved": resolved_items,
        "watch": watch,
        "next_72h": next_72h,
        "questions": questions,
        "beat_momentum": beat_momentum,
        "sources_summary": {
            "healthy": healthy,
            "degraded": degraded,
            "failed": failed,
            "total": len(health),
        },
        "disclaimer": DISCLAIMER,
        "scientific_note": "Experimental score — not a calibrated probability.",
        "scoring_model_version": open_forecasts[0]["model_version"] if open_forecasts else "heuristic-v0.1",
    }


def _forecast_card(f: dict) -> dict:
    return {
        "forecast_id": f["forecast_id"],
        "claim": f["claim"],
        "beat": f["beat"],
        "geography": f.get("geography"),
        "likelihood_score": f["likelihood_score"],
        "editorial_relevance_score": f["editorial_relevance_score"],
        "priority_score": f["priority_score"],
        "horizon_end": f["horizon_end"],
        "issued_at": f["issued_at"],
        "version": f["version"],
        "version_count": f["version_count"],
        "delta_likelihood": f["delta_likelihood"],
        "delta_relevance": f["delta_relevance"],
        "explanation": f.get("explanation", {}),
        "display_status": f["display_status"],
        "model_version": f["model_version"],
    }


def _generate_questions(top: list[dict], next_72h: list[dict]) -> list[dict]:
    questions: list[dict] = []
    for f in top[:QUESTIONS_MAX]:
        beat = str(f["beat"]).replace("_", " ").lower()
        prompt = f"Does today's {beat} development merit advance reporting: \"{_short(f['claim'])}\"?"
        questions.append({
            "prompt": prompt,
            "why": f["claim"],
            "beat": f["beat"],
            "supporting_forecast": f["forecast_id"],
        })
    for cat in next_72h[:2]:
        prompt = f"Is coverage prepared for the scheduled event in ~{cat['hours_out']}h: \"{_short(cat['title'])}\"?"
        questions.append({
            "prompt": prompt,
            "why": f"Scheduled event within {cat['hours_out']}h; source: {cat['source_name']}",
            "beat": cat["beat"],
            "supporting_source_url": cat.get("canonical_url"),
        })
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[dict] = []
    for q in questions:
        if q["prompt"] in seen:
            continue
        seen.add(q["prompt"])
        unique.append(q)
    return unique[:QUESTIONS_MAX]


def _short(s: str) -> str:
    return (s[:110] + "…") if len(s) > 110 else s


def _beat_momentum(forecasts: list[dict], signals: list[dict], items: list[dict]) -> list[dict]:
    """A tiny per-beat momentum summary. Deterministic, derived from counts."""
    beats: dict[str, dict[str, Any]] = {}
    for f in forecasts:
        b = f["beat"]
        row = beats.setdefault(b, {"beat": b, "forecast_count": 0, "signal_count": 0, "item_count": 0,
                                    "sum_priority": 0, "sum_relevance": 0, "next_catalyst": None,
                                    "next_catalyst_hours": None})
        row["forecast_count"] += 1
        row["sum_priority"] += int(f["priority_score"])
        row["sum_relevance"] += int(f["editorial_relevance_score"])

    for s in signals:
        b = s["beat"]
        row = beats.setdefault(b, {"beat": b, "forecast_count": 0, "signal_count": 0, "item_count": 0,
                                    "sum_priority": 0, "sum_relevance": 0, "next_catalyst": None,
                                    "next_catalyst_hours": None})
        row["signal_count"] += 1

    now = datetime.now(timezone.utc)
    for it in items:
        b = it.get("beat")
        if not b:
            continue
        row = beats.setdefault(b, {"beat": b, "forecast_count": 0, "signal_count": 0, "item_count": 0,
                                    "sum_priority": 0, "sum_relevance": 0, "next_catalyst": None,
                                    "next_catalyst_hours": None})
        row["item_count"] += 1
        event_at = _to_dt(it.get("event_at"))
        if event_at and event_at > now:
            hours = int((event_at - now).total_seconds() / 3600)
            if row["next_catalyst_hours"] is None or hours < row["next_catalyst_hours"]:
                row["next_catalyst_hours"] = hours
                row["next_catalyst"] = it["title"][:120]

    out: list[dict] = []
    for row in beats.values():
        # Simple momentum symbol: more forecasts + signals => stronger arrow
        activity = row["forecast_count"] * 2 + row["signal_count"]
        if activity >= 6:
            arrow = "↑↑"
        elif activity >= 3:
            arrow = "↑"
        elif activity >= 1:
            arrow = "→"
        else:
            arrow = "·"
        avg_relevance = int(row["sum_relevance"] / row["forecast_count"]) if row["forecast_count"] else 0
        relevance_label = "HIGH" if avg_relevance >= 70 else "MEDIUM" if avg_relevance >= 50 else "LOW"
        out.append({
            "beat": row["beat"],
            "momentum": arrow,
            "editorial_relevance": relevance_label,
            "next_catalyst": row["next_catalyst"],
            "next_catalyst_hours": row["next_catalyst_hours"],
            "forecast_count": row["forecast_count"],
            "signal_count": row["signal_count"],
            "item_count": row["item_count"],
        })
    # Sort by (relevance rank, activity) desc
    _relev_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    out.sort(key=lambda r: (_relev_rank[r["editorial_relevance"]], r["forecast_count"] + r["signal_count"]), reverse=True)
    return out


__all__ = ["build_brief", "DISCLAIMER"]
