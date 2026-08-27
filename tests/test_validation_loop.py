"""Closed-loop editorial validation: provenance, feedback, outcomes, metrics."""

from __future__ import annotations

from fastapi.testclient import TestClient

from kc_news_radar import db as dbmod
from kc_news_radar.demo import load_demo_fixtures
from kc_news_radar.performance import build_performance_summary
from kc_news_radar.pipeline.forecasting import run_pipeline


def _demo_database(path):
    dbmod.init_db(path)
    conn = dbmod.connect(path)
    load_demo_fixtures(conn)
    run_pipeline(conn)
    forecast = dbmod.list_forecasts_latest(conn)[0]
    conn.close()
    return forecast


def test_forecast_evidence_captures_signals_and_public_source_provenance(tmp_db_path):
    forecast = _demo_database(tmp_db_path)
    conn = dbmod.connect(tmp_db_path)
    rows = dbmod.get_forecast_evidence(conn, forecast["forecast_id"], forecast["version"])
    conn.close()

    assert rows
    row = rows[0]
    assert row["signal_type"]
    assert row["signal_summary"]
    assert row["source_name"]
    assert row["external_id"]
    assert row["retrieved_at"]
    assert row["content_hash"]
    assert "street_address_private" not in row["public_metadata"]


def test_historical_evidence_is_not_replaced_by_mutated_current_item(tmp_db_path):
    forecast = _demo_database(tmp_db_path)
    conn = dbmod.connect(tmp_db_path)
    before = dbmod.get_forecast_evidence(conn, forecast["forecast_id"], forecast["version"])
    assert before
    source_item_id = before[0]["source_item_id"]
    original_title = before[0]["item_title"]
    conn.execute(
        "UPDATE source_items SET title=?, content_hash=? WHERE id=?",
        ("CHANGED AFTER FORECAST ISSUANCE", "later-content-hash", source_item_id),
    )
    conn.commit()
    after = dbmod.get_forecast_evidence(conn, forecast["forecast_id"], forecast["version"])
    current = conn.execute("SELECT title FROM source_items WHERE id=?", (source_item_id,)).fetchone()[0]
    conn.close()

    assert current == "CHANGED AFTER FORECAST ISSUANCE"
    assert after[0]["item_title"] == original_title
    assert after == before


def test_api_detail_returns_captured_evidence_and_boundaries(tmp_db_path, monkeypatch):
    forecast = _demo_database(tmp_db_path)
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod

    body = TestClient(appmod.app).get(f"/api/forecasts/{forecast['forecast_id']}").json()
    assert body["evidence_status"] == "CAPTURED_AT_ISSUANCE"
    assert body["evidence_version"] == forecast["version"]
    assert body["supporting_evidence"]
    assert body["supporting_evidence"][0]["source_records"]
    assert any("do not by themselves establish" in note for note in body["evidence_limitations"])


def test_legacy_forecast_fails_closed_when_snapshot_is_absent(tmp_db_path, monkeypatch):
    forecast = _demo_database(tmp_db_path)
    conn = dbmod.connect(tmp_db_path)
    conn.execute(
        "DELETE FROM forecast_evidence_snapshots WHERE forecast_id=?",
        (forecast["forecast_id"],),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod

    body = TestClient(appmod.app).get(f"/api/forecasts/{forecast['forecast_id']}").json()
    assert body["evidence_status"] == "LEGACY_NOT_CAPTURED"
    assert body["supporting_evidence"] == []
    assert any("not substituted" in note for note in body["evidence_limitations"])


def test_resolution_api_targets_version_persists_and_preserves_forecast(tmp_db_path, monkeypatch):
    forecast = _demo_database(tmp_db_path)
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod

    conn = dbmod.connect(tmp_db_path)
    before = tuple(conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id=? AND version=?",
        (forecast["forecast_id"], forecast["version"]),
    ).fetchone())
    conn.close()

    client = TestClient(appmod.app)
    response = client.post(
        f"/api/forecasts/{forecast['forecast_id']}/resolution",
        json={
            "forecast_version": forecast["version"],
            "outcome": "CONFIRMED",
            "evidence": "Synthetic demo public record confirms the scheduled action.",
            "notes": "Deterministic acceptance demonstration.",
        },
    )
    assert response.status_code == 201
    assert response.json()["forecast_immutable"] is True

    reopened = dbmod.connect(tmp_db_path)
    resolution = dbmod.get_resolution(reopened, forecast["forecast_id"])
    after = tuple(reopened.execute(
        "SELECT * FROM forecasts WHERE forecast_id=? AND version=?",
        (forecast["forecast_id"], forecast["version"]),
    ).fetchone())
    reopened.close()
    assert resolution["forecast_version"] == forecast["version"]
    assert resolution["outcome"] == "CONFIRMED"
    assert after == before

    duplicate = client.post(
        f"/api/forecasts/{forecast['forecast_id']}/resolution",
        json={"forecast_version": forecast["version"], "outcome": "AMBIGUOUS", "evidence": "correction"},
    )
    assert duplicate.status_code == 409


def test_resolution_api_rejects_invalid_outcome_and_version(tmp_db_path, monkeypatch):
    forecast = _demo_database(tmp_db_path)
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)

    invalid_outcome = client.post(
        f"/api/forecasts/{forecast['forecast_id']}/resolution",
        json={"forecast_version": forecast["version"], "outcome": "MAYBE", "evidence": "no"},
    )
    assert invalid_outcome.status_code == 422
    invalid_version = client.post(
        f"/api/forecasts/{forecast['forecast_id']}/resolution",
        json={"forecast_version": 999, "outcome": "AMBIGUOUS", "evidence": "Public record remains unclear."},
    )
    assert invalid_version.status_code == 404


def test_feedback_submission_is_visible_in_forecast_detail(tmp_db_path, monkeypatch):
    forecast = _demo_database(tmp_db_path)
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)

    response = client.post("/api/feedback", json={
        "subject_type": "forecast",
        "subject_id": forecast["forecast_id"],
        "label": "USEFUL",
        "note": "Useful for the morning meeting.",
    })
    assert response.status_code == 200
    detail = client.get(f"/api/forecasts/{forecast['forecast_id']}").json()
    assert detail["editorial_feedback"][0]["label"] == "USEFUL"


def test_performance_summary_zero_and_resolved_behavior(tmp_db_path, monkeypatch):
    forecast = _demo_database(tmp_db_path)
    conn = dbmod.connect(tmp_db_path)
    zero = build_performance_summary(conn)
    conn.close()
    assert zero["denominator"] == 0
    assert zero["sufficient_resolved_evidence"] is False
    assert "no version-targeted" in zero["evidence_note"]

    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod
    client = TestClient(appmod.app)
    client.post(
        f"/api/forecasts/{forecast['forecast_id']}/resolution",
        json={"forecast_version": forecast["version"], "outcome": "NOT_OCCURRED", "evidence": "Forecast horizon ended without the action."},
    )
    summary = client.get("/api/performance").json()
    assert summary["denominator"] == 1
    assert summary["by_outcome"] == [{"outcome": "NOT_OCCURRED", "resolved_count": 1, "denominator": 1}]
    assert summary["by_model_version"][0]["resolved_count"] == 1
    assert summary["by_model_version"][0]["overall_resolved_denominator"] == 1
    assert summary["by_signal_type"]
    assert summary["by_signal_type"][0]["overall_resolved_denominator"] == 1
    assert summary["sufficient_resolved_evidence"] is False


def test_dashboard_contains_closed_loop_controls(tmp_db_path, monkeypatch):
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    from kc_news_radar import app as appmod
    html = TestClient(appmod.app).get("/").text
    assert "Forecast performance" in html
    assert "forecastDetail" in html
    assert "performanceSummary" in html


def test_demo_database_remains_visibly_identified_when_served_without_demo_env(tmp_db_path, monkeypatch):
    _demo_database(tmp_db_path)
    monkeypatch.setenv("KC_NEWS_RADAR_DB", str(tmp_db_path))
    monkeypatch.delenv("KC_NEWS_RADAR_DEMO", raising=False)
    from kc_news_radar import app as appmod

    health = TestClient(appmod.app).get("/api/health").json()
    assert health["contains_demo_data"] is True
    assert health["demo_mode"] is True
