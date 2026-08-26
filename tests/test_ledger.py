"""Forecast ledger: append-only, material-delta gating, resolutions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kc_news_radar import db as dbmod
from kc_news_radar.ledger.forecasts import (
    MATERIAL_SCORE_DELTA,
    compare_versions,
    upsert_forecast_version,
)
from kc_news_radar.ledger.resolution import forecast_status_after_now, record_resolution
from kc_news_radar.models import Beat, Forecast, ForecastStatus, Outcome


def _mk_forecast(fid="f-1", likelihood=40, relevance=50, now=None) -> Forecast:
    now = now or datetime.now(timezone.utc)
    return Forecast(
        forecast_id=fid, version=1, issued_at=now,
        horizon_start=now, horizon_end=now + timedelta(hours=72),
        claim="c", event_type="e", geography=None, beat=Beat.LOCAL_GOVERNMENT,
        likelihood_score=likelihood, editorial_relevance_score=relevance,
        priority_score=45, status=ForecastStatus.OPEN,
        model_version="heuristic-v0.1", explanation={},
    )


def test_first_upsert_writes_version_1(tmp_db):
    stored = upsert_forecast_version(tmp_db, _mk_forecast())
    assert stored.version == 1
    versions = dbmod.get_forecast_versions(tmp_db, "f-1")
    assert len(versions) == 1


def test_below_delta_is_noop(tmp_db):
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=40, relevance=50))
    # +2 on likelihood → below MATERIAL_SCORE_DELTA=5 → no new version
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=42, relevance=50))
    versions = dbmod.get_forecast_versions(tmp_db, "f-1")
    assert len(versions) == 1
    assert versions[0]["likelihood_score"] == 40  # historical row untouched


def test_material_delta_appends_new_version(tmp_db):
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=40, relevance=50))
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=40 + MATERIAL_SCORE_DELTA, relevance=50))
    versions = dbmod.get_forecast_versions(tmp_db, "f-1")
    assert len(versions) == 2
    assert versions[0]["likelihood_score"] == 40
    assert versions[1]["likelihood_score"] == 40 + MATERIAL_SCORE_DELTA


def test_prior_versions_are_never_mutated(tmp_db):
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=40, relevance=50))
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=80, relevance=50))
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=20, relevance=50))
    versions = dbmod.get_forecast_versions(tmp_db, "f-1")
    scores = [v["likelihood_score"] for v in versions]
    assert scores == [40, 80, 20]  # full history preserved, in order


def test_compare_versions_detects_change():
    a = {"likelihood_score": 40, "editorial_relevance_score": 50}
    b = {"likelihood_score": 40 + MATERIAL_SCORE_DELTA, "editorial_relevance_score": 50}
    cmp = compare_versions(a, b)
    assert cmp.changed
    assert cmp.delta_likelihood == MATERIAL_SCORE_DELTA


def test_compare_versions_below_delta_not_changed():
    a = {"likelihood_score": 40, "editorial_relevance_score": 50}
    b = {"likelihood_score": 42, "editorial_relevance_score": 51}
    assert compare_versions(a, b).changed is False


def test_resolution_does_not_touch_forecast_row(tmp_db):
    now = datetime.now(timezone.utc)
    upsert_forecast_version(tmp_db, _mk_forecast(likelihood=40, relevance=50, now=now))
    record_resolution(
        tmp_db, forecast_id="f-1", resolved_at=now + timedelta(hours=48),
        outcome=Outcome.CONFIRMED, evidence="published story link",
    )
    tmp_db.commit()
    versions = dbmod.get_forecast_versions(tmp_db, "f-1")
    # Persisted status untouched — only display status changes downstream.
    assert versions[0]["status"] == "OPEN"
    res = dbmod.get_resolution(tmp_db, "f-1")
    assert res is not None
    assert res["outcome"] == "CONFIRMED"


def test_forecast_status_after_now_expired():
    now = datetime.now(timezone.utc)
    row = {
        "status": "OPEN",
        "horizon_end": (now - timedelta(hours=1)).isoformat(),
    }
    assert forecast_status_after_now(row, now) == "EXPIRED"


def test_forecast_status_after_now_open():
    now = datetime.now(timezone.utc)
    row = {
        "status": "OPEN",
        "horizon_end": (now + timedelta(hours=10)).isoformat(),
    }
    assert forecast_status_after_now(row, now) == "OPEN"
