"""Build forecast rows from detected signals and persist them via the ledger.

For each underlying source item (a real-world public development) we aggregate
all signal types that reference it and produce ONE forecast per item — the
same forecast_id across runs so the ledger can append immutable versions.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import SCORING_MODEL_VERSION
from .. import db as dbmod
from ..models import Beat, Forecast, ForecastStatus, Signal, SignalType
from ..ledger.forecasts import upsert_forecast_version
from .signals import DetectedSignal, detect_signals
from .scoring import (
    editorial_relevance_score,
    experimental_likelihood_score,
    priority_score,
)


FORECAST_HORIZON_HOURS = 72
FORECAST_MIN_LIKELIHOOD = 25


def forecast_id_for_signal(signal: Signal, evidence_ids: list[int]) -> str:
    """Legacy per-signal id — retained for tests that assert per-signal identity."""
    primary_evidence = str(evidence_ids[0]) if evidence_ids else ""
    key = f"{signal.signal_type.value}|{signal.beat.value}|{primary_evidence}|{signal.title.lower()[:80]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _forecast_id_for_item(item: dict) -> str:
    """Stable per-source-item forecast id — same real-world thing, one forecast."""
    key = f"{item['source_name']}|{item['external_id']}"
    return "f-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]


def build_forecast(
    detected: DetectedSignal,
    *,
    now: datetime,
    distinct_sources: int,
    agenda_recently_updated: bool,
    high_magnitude: bool,
    severity: str | None,
    hours_to_event: int | None,
) -> tuple[Forecast, dict]:
    """Legacy per-signal builder — kept for tests. See ``run_pipeline`` for the
    aggregated per-item builder used in production.
    """
    signal = detected.signal
    likelihood_bd = experimental_likelihood_score(
        signal_type=signal.signal_type,
        beat=signal.beat,
        hours_to_event=hours_to_event,
        evidence_count=signal.evidence_count,
        distinct_sources=distinct_sources,
        agenda_recently_updated=agenda_recently_updated,
        high_magnitude=high_magnitude,
        severity=severity,
    )
    relevance_bd = editorial_relevance_score(
        beat=signal.beat,
        high_magnitude=high_magnitude,
        affects_multiple_jurisdictions=(distinct_sources >= 2),
        public_safety_indicator=(signal.signal_type == SignalType.HIGH_IMPACT_PUBLIC_ACTION or signal.beat == Beat.PUBLIC_SAFETY),
        government_accountability=(signal.beat in {Beat.LOCAL_GOVERNMENT, Beat.STATE_GOVERNMENT, Beat.POLITICS_ELECTIONS}),
        infrastructure_impact=(signal.beat in {Beat.TRANSPORTATION, Beat.HOUSING_DEVELOPMENT}),
    )
    prio = priority_score(likelihood_bd.total, relevance_bd.total)
    horizon_end = now + timedelta(hours=FORECAST_HORIZON_HOURS)
    fid = forecast_id_for_signal(signal, detected.evidence_ids)
    forecast = Forecast(
        forecast_id=fid,
        version=1,
        issued_at=now,
        horizon_start=now,
        horizon_end=horizon_end,
        claim=_claim_for(signal),
        event_type=signal.signal_type.value,
        geography=signal.geography,
        beat=signal.beat,
        likelihood_score=likelihood_bd.total,
        editorial_relevance_score=relevance_bd.total,
        priority_score=prio,
        status=ForecastStatus.OPEN,
        model_version=SCORING_MODEL_VERSION,
        explanation={
            "likelihood": likelihood_bd.as_dict(),
            "editorial_relevance": relevance_bd.as_dict(),
            "priority_formula": "0.45*likelihood + 0.55*relevance",
            "signal_type": signal.signal_type.value,
            "notes": "Experimental score — not a calibrated probability.",
        },
    )
    return forecast, {"likelihood": likelihood_bd, "relevance": relevance_bd}


def _claim_for(signal: Signal) -> str:
    """Phrase the forecast around a public process, never as an allegation about a person."""
    if signal.signal_type == SignalType.SCHEDULED_CATALYST:
        return f"{signal.beat.value.replace('_', ' ').title()} public action may develop following the scheduled event: {signal.title}."
    if signal.signal_type == SignalType.SEVERE_WEATHER_CHANGE:
        return f"Weather situation may evolve: {signal.title}."
    if signal.signal_type == SignalType.HIGH_IMPACT_PUBLIC_ACTION:
        return f"Public-safety situation warrants monitoring: {signal.title}."
    if signal.signal_type == SignalType.MULTI_SOURCE_CONVERGENCE:
        return f"Multiple public sources reference: {signal.title}. Development possible within 72h."
    if signal.signal_type == SignalType.UNUSUAL_AGENDA_ITEM:
        return f"Agenda item with unusual magnitude may advance: {signal.title}."
    if signal.signal_type == SignalType.REPEATED_ENTITY_ACTIVITY:
        return f"Repeated public activity in {signal.geography or 'this area'} may indicate a developing pattern."
    if signal.signal_type == SignalType.ITEM_UPDATED:
        return f"Public record updated — situation may be evolving: {signal.title}."
    return f"Public development to monitor: {signal.title}."


def _claim_for_item(item: dict, signal_types: set[SignalType]) -> str:
    beat = (item.get("beat") or "OTHER").replace("_", " ").lower()
    title = item.get("title", "").strip()
    if SignalType.SEVERE_WEATHER_CHANGE in signal_types:
        return f"Weather situation may evolve: {title}."
    if SignalType.HIGH_IMPACT_PUBLIC_ACTION in signal_types:
        return f"Public-safety situation warrants monitoring: {title}."
    if SignalType.DEVELOPMENT_DEAL_ACTIVITY in signal_types and SignalType.SCHEDULED_CATALYST in signal_types:
        return f"Scheduled action on a public development deal may advance: {title}."
    if SignalType.DEVELOPMENT_DEAL_ACTIVITY in signal_types:
        return f"Development-deal activity worth monitoring (stadium / TIF / Port KC / bond): {title}."
    if SignalType.SCHEDULED_CATALYST in signal_types and SignalType.UNUSUAL_AGENDA_ITEM in signal_types:
        return f"Scheduled {beat} action of unusual magnitude may advance: {title}."
    if SignalType.SCHEDULED_CATALYST in signal_types:
        return f"Scheduled {beat} public action may develop: {title}."
    if SignalType.UNUSUAL_AGENDA_ITEM in signal_types:
        return f"Agenda item with unusual magnitude may advance: {title}."
    if SignalType.MULTI_SOURCE_CONVERGENCE in signal_types:
        return f"Multiple public sources reference: {title}. Development possible within 72h."
    if SignalType.ITEM_UPDATED in signal_types:
        return f"Public record updated — situation may be evolving: {title}."
    return f"Public development to monitor: {title}."


def _parse_dt(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
    try:
        dt = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def run_pipeline(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    forecast_min_likelihood: int = FORECAST_MIN_LIKELIHOOD,
) -> dict:
    """Recompute signals and one forecast per underlying source item.

    Returns a small summary suitable for CLI output.
    """
    now = now or datetime.now(timezone.utc)
    items = dbmod.list_source_items(conn, limit=500)
    item_by_id = {it["id"]: it for it in items}

    dbmod.clear_signals(conn)
    detected = detect_signals(items, now=now)

    # Persist signals.
    signals_written = 0
    for det in detected:
        weight = 1
        evidence_triples = [
            (eid, det.signal.signal_type.value, weight) for eid in det.evidence_ids
        ]
        dbmod.insert_signal(conn, det.signal, evidence_triples)
        signals_written += 1

    # Signal types that meaningfully imply a "development to forecast".
    forecastable = {
        SignalType.SCHEDULED_CATALYST,
        SignalType.UNUSUAL_AGENDA_ITEM,
        SignalType.SEVERE_WEATHER_CHANGE,
        SignalType.HIGH_IMPACT_PUBLIC_ACTION,
        SignalType.MULTI_SOURCE_CONVERGENCE,
        SignalType.ITEM_UPDATED,
        SignalType.DEVELOPMENT_DEAL_ACTIVITY,
        # COMMUNITY_311_TREND intentionally excluded from primary forecasting:
        # 311 patterns are resident-reported and not calibrated for prediction.
        # They still surface as signals in the Watch column and Emerging tab.
    }

    # Aggregate signals per primary source item.
    per_item: dict[int, dict] = {}
    for det in detected:
        if det.signal.signal_type not in forecastable:
            continue
        for eid in det.evidence_ids:
            if eid not in item_by_id:
                continue
            slot = per_item.setdefault(
                eid,
                {
                    "signal_types": set(),
                    "evidence_ids": set(),
                    "sources": set(),
                    "sample_signal": det.signal,
                },
            )
            slot["signal_types"].add(det.signal.signal_type)
            slot["evidence_ids"].update(det.evidence_ids)
            for e in det.evidence_ids:
                if e in item_by_id:
                    slot["sources"].add(item_by_id[e]["source_name"])
            # Prefer the highest-impact signal as the "sample" for beat/geography defaults.
            if det.signal.local_impact_score > slot["sample_signal"].local_impact_score:
                slot["sample_signal"] = det.signal

    forecasts_written = 0
    for primary_id, slot in per_item.items():
        item = item_by_id[primary_id]
        signal_types: set[SignalType] = slot["signal_types"]
        evidence_ids = sorted(slot["evidence_ids"])
        distinct_sources = len(slot["sources"])

        event_at = _parse_dt(item.get("event_at"))
        hours_to_event = int((event_at - now).total_seconds() / 3600) if event_at and event_at > now else None

        first_seen = _parse_dt(item.get("first_seen_at"))
        last_seen = _parse_dt(item.get("last_seen_at"))
        agenda_updated = bool(first_seen and last_seen and (last_seen - first_seen).total_seconds() > 3600)

        haystack = f"{item.get('title', '')}\n{item.get('excerpt') or ''}"
        high_magnitude = (
            SignalType.UNUSUAL_AGENDA_ITEM in signal_types
            or SignalType.HIGH_IMPACT_PUBLIC_ACTION in signal_types
            or SignalType.DEVELOPMENT_DEAL_ACTIVITY in signal_types
        )

        severity = None
        meta = item.get("metadata") or {}
        if meta.get("severity"):
            severity = meta["severity"]

        beat = Beat(item.get("beat") or Beat.OTHER.value)

        # Aggregate likelihood: apply each contributing signal_type's boost.
        # We compute per-type, sum, cap at 100.
        components: list[tuple[int, str]] = []
        total = 0
        for st in signal_types:
            bd = experimental_likelihood_score(
                signal_type=st,
                beat=beat,
                hours_to_event=hours_to_event if st == SignalType.SCHEDULED_CATALYST else None,
                evidence_count=len(evidence_ids),
                distinct_sources=distinct_sources,
                agenda_recently_updated=False,   # applied once below
                high_magnitude=False,             # applied once below
                severity=severity if st == SignalType.SEVERE_WEATHER_CHANGE else None,
            )
            # Only pick up the per-signal-type-specific components, not the shared ones,
            # which we add once below to avoid double-counting.
            per_type_reasons = [c for c in bd.components if not _is_shared(c[1])]
            for c in per_type_reasons:
                components.append(c)
                total += c[0]

        # Shared components (added once)
        if distinct_sources >= 2:
            components.append((11, f"{distinct_sources} distinct public sources reference this issue"))
            total += 11
        if len(evidence_ids) >= 3:
            components.append((6, f"{len(evidence_ids)} supporting public evidence items"))
            total += 6
        if hours_to_event is not None:
            if hours_to_event <= 24:
                components.append((8, "near-term timing (within 24h)"))
                total += 8
            elif hours_to_event <= 72:
                components.append((5, "near-term timing (within 72h)"))
                total += 5
        if agenda_updated:
            components.append((12, "agenda revised recently"))
            total += 12
        if high_magnitude:
            components.append((10, "unusually high fiscal or policy magnitude"))
            total += 10
        if severity:
            low = severity.lower()
            if low in {"extreme", "severe"}:
                components.append((8, f"NWS severity: {severity}"))
                total += 8
            elif low == "moderate":
                components.append((4, f"NWS severity: {severity}"))
                total += 4

        total = max(0, min(100, total))

        if total < forecast_min_likelihood:
            continue

        relevance_bd = editorial_relevance_score(
            beat=beat,
            high_magnitude=high_magnitude,
            affects_multiple_jurisdictions=(distinct_sources >= 2),
            public_safety_indicator=(SignalType.HIGH_IMPACT_PUBLIC_ACTION in signal_types or beat == Beat.PUBLIC_SAFETY),
            government_accountability=(beat in {Beat.LOCAL_GOVERNMENT, Beat.STATE_GOVERNMENT, Beat.POLITICS_ELECTIONS}),
            infrastructure_impact=(beat in {Beat.TRANSPORTATION, Beat.HOUSING_DEVELOPMENT}),
        )
        prio = priority_score(total, relevance_bd.total)

        fid = _forecast_id_for_item(item)
        forecast = Forecast(
            forecast_id=fid,
            version=1,  # actual version assigned by upsert
            issued_at=now,
            horizon_start=now,
            horizon_end=now + timedelta(hours=FORECAST_HORIZON_HOURS),
            claim=_claim_for_item(item, signal_types),
            event_type="+".join(sorted(st.value for st in signal_types)),
            geography=item.get("geography"),
            beat=beat,
            likelihood_score=total,
            editorial_relevance_score=relevance_bd.total,
            priority_score=prio,
            status=ForecastStatus.OPEN,
            model_version=SCORING_MODEL_VERSION,
            explanation={
                "likelihood": {"total": total, "components": [{"weight": w, "reason": r} for w, r in components]},
                "editorial_relevance": relevance_bd.as_dict(),
                "priority_formula": "0.45*likelihood + 0.55*relevance",
                "signal_types": sorted(st.value for st in signal_types),
                "distinct_sources": distinct_sources,
                "notes": "Experimental score — not a calibrated probability.",
            },
        )
        upsert_forecast_version(conn, forecast)
        forecasts_written += 1

    conn.commit()
    return {
        "signals_written": signals_written,
        "forecasts_written": forecasts_written,
        "items_considered": len(items),
    }


_SHARED_REASON_MARKERS = (
    "distinct public sources reference",
    "supporting public evidence items",
    "near-term timing",
    "agenda revised recently",
    "unusually high fiscal",
    "NWS severity",
)


def _is_shared(reason: str) -> bool:
    r = reason.lower()
    return any(marker.lower() in r for marker in _SHARED_REASON_MARKERS)
