"""Full offline pipeline: demo fixtures → signals → forecasts → brief → API.

Exercises the acceptance criteria that matter most:

* `kc-news-radar-collect` in demo mode produces signals and forecasts.
* Immutable forecast versions are appended by `run_pipeline` across runs.
* `build_brief` produces the Morning Strategy Brief structure with disclaimer.
* Amendment-2 signal types can flow through the full pipeline safely.
* Every API response labels experimental scores as non-probabilities.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from kc_news_radar import db as dbmod
from kc_news_radar.demo import load_demo_fixtures
from kc_news_radar.pipeline.briefing import build_brief
from kc_news_radar.pipeline.forecasting import run_pipeline


def test_demo_pipeline_produces_signals_and_forecasts(tmp_db):
    load_demo_fixtures(tmp_db)
    summary = run_pipeline(tmp_db)
    assert summary["signals_written"] > 0
    assert summary["forecasts_written"] > 0

    # Both Amendment-2 signal types should appear from the demo fixtures.
    rows = tmp_db.execute("SELECT DISTINCT signal_type FROM signals").fetchall()
    types = {r["signal_type"] for r in rows}
    assert "COMMUNITY_311_TREND" in types
    assert "DEVELOPMENT_DEAL_ACTIVITY" in types


def test_pipeline_rerun_never_mutates_prior_forecasts(tmp_db):
    load_demo_fixtures(tmp_db)
    run_pipeline(tmp_db)

    baseline = tmp_db.execute(
        "SELECT forecast_id, version, likelihood_score FROM forecasts ORDER BY forecast_id, version"
    ).fetchall()

    # Re-run with the same fixtures; no material score deltas expected.
    run_pipeline(tmp_db)

    after = tmp_db.execute(
        "SELECT forecast_id, version, likelihood_score FROM forecasts ORDER BY forecast_id, version"
    ).fetchall()

    # The historical rows must be byte-identical.
    baseline_rows = [tuple(r) for r in baseline]
    after_rows = [tuple(r) for r in after]
    # Every baseline row must still be present with the same score.
    baseline_by_key = {(r[0], r[1]): r[2] for r in baseline_rows}
    after_by_key = {(r[0], r[1]): r[2] for r in after_rows}
    for key, score in baseline_by_key.items():
        assert after_by_key.get(key) == score, f"row {key} was mutated"


def test_briefing_has_expected_structure_and_disclaimer(tmp_db):
    load_demo_fixtures(tmp_db)
    run_pipeline(tmp_db)
    brief = build_brief(tmp_db)
    for key in [
        "generated_at", "top_priorities", "new", "changed", "resolved",
        "watch", "next_72h", "questions", "beat_momentum", "sources_summary",
        "disclaimer", "scientific_note",
    ]:
        assert key in brief
    assert "not calibrated probabilities" in brief["disclaimer"].lower()
    assert brief["sources_summary"]["total"] > 0


def test_api_responses_carry_scientific_labeling(tmp_db_path, monkeypatch):
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))

    # Load demo data into the same DB path.
    conn = dbmod.connect(tmp_db_path)
    load_demo_fixtures(conn)
    run_pipeline(conn)
    conn.close()

    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)

    # /api/health — should call out that scores are not calibrated
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "calibrated" in body["scientific_note"].lower()

    # /api/forecasts — must include scientific note + scoring model version
    r = client.get("/api/forecasts")
    assert r.status_code == 200
    body = r.json()
    assert "calibrated" in body["scientific_note"].lower()
    assert body["scoring_model_version"].startswith("heuristic-")

    # /api/brief — must carry disclaimer
    r = client.get("/api/brief")
    assert r.status_code == 200
    body = r.json()
    assert "not calibrated" in body["disclaimer"].lower()


def test_api_feedback_validates_labels(tmp_db_path, monkeypatch):
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)

    # Valid label
    r = client.post("/api/feedback", json={
        "subject_type": "forecast", "subject_id": "f-abc",
        "label": "USEFUL", "note": "worth chasing",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Bogus label rejected
    r = client.post("/api/feedback", json={
        "subject_type": "forecast", "subject_id": "f-abc", "label": "BOGUS",
    })
    assert r.status_code == 422

    # Bogus subject_type rejected
    r = client.post("/api/feedback", json={
        "subject_type": "totally-invalid", "subject_id": "f-abc", "label": "USEFUL",
    })
    assert r.status_code == 422


def test_api_forecast_detail_404_on_unknown(tmp_db_path, monkeypatch):
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)
    r = client.get("/api/forecasts/does-not-exist")
    assert r.status_code == 404
