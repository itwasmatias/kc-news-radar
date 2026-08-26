"""SQLite persistence + immutable ledger + DB reopen preserves records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kc_news_radar import db as dbmod
from kc_news_radar.models import (
    Beat,
    Forecast,
    ForecastStatus,
    Signal,
    SignalType,
    SourceHealth,
    SourceItem,
    SourceStatus,
)


def _mk_item(**overrides) -> SourceItem:
    now = datetime.now(timezone.utc)
    base = dict(
        source_name="src", external_id="e-1", title="T",
        retrieved_at=now, content_hash="h1",
    )
    base.update(overrides)
    return SourceItem(**base)


def test_upsert_new_returns_updated_false(tmp_db):
    _id, updated = dbmod.upsert_source_item(tmp_db, _mk_item())
    assert _id > 0
    assert updated is False


def test_upsert_same_hash_is_noop_content(tmp_db):
    item = _mk_item()
    id1, u1 = dbmod.upsert_source_item(tmp_db, item)
    id2, u2 = dbmod.upsert_source_item(tmp_db, item)
    assert id1 == id2
    assert u1 is False and u2 is False


def test_upsert_different_hash_flags_updated(tmp_db):
    item = _mk_item()
    dbmod.upsert_source_item(tmp_db, item)
    updated_item = _mk_item(content_hash="h2", title="T (edited)")
    _id, was_updated = dbmod.upsert_source_item(tmp_db, updated_item)
    assert was_updated is True


def test_naive_datetime_rejected(tmp_db):
    naive = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError):
        dbmod.upsert_source_item(
            tmp_db,
            SourceItem(
                source_name="s", external_id="e", title="t",
                retrieved_at=naive, content_hash="h",
            ),
        )


def test_forecast_versions_are_immutable(tmp_db):
    now = datetime.now(timezone.utc)
    f1 = Forecast(
        forecast_id="fx", version=1, issued_at=now,
        horizon_start=now, horizon_end=now + timedelta(hours=72),
        claim="c", event_type="e", geography=None, beat=Beat.LOCAL_GOVERNMENT,
        likelihood_score=40, editorial_relevance_score=50, priority_score=45,
        status=ForecastStatus.OPEN, model_version="heuristic-v0.1", explanation={},
    )
    dbmod.insert_forecast(tmp_db, f1)
    with pytest.raises(ValueError, match="immutable"):
        dbmod.insert_forecast(tmp_db, f1)  # same (id, version)


def test_reopen_preserves_records(tmp_db_path):
    conn = dbmod.connect(tmp_db_path)
    now = datetime.now(timezone.utc)
    dbmod.upsert_source_item(
        conn,
        SourceItem(
            source_name="s", external_id="persist-1", title="Persisted",
            retrieved_at=now, content_hash="h",
        ),
    )
    dbmod.upsert_source_health(
        conn,
        SourceHealth(
            source_name="s", status=SourceStatus.HEALTHY,
            last_attempt=now, last_success=now, item_count=1,
            error_message=None, latency_ms=100,
        ),
    )
    conn.commit()
    conn.close()

    # Reopen and confirm records are still there.
    conn2 = dbmod.connect(tmp_db_path)
    items = dbmod.list_source_items(conn2)
    healths = dbmod.list_source_health(conn2)
    conn2.close()
    assert len(items) == 1
    assert items[0]["external_id"] == "persist-1"
    assert healths[0]["source_name"] == "s"
    assert healths[0]["status"] == "HEALTHY"


def test_signals_can_be_cleared(tmp_db):
    now = datetime.now(timezone.utc)
    dbmod.insert_signal(
        tmp_db,
        Signal(
            id=None, created_at=now, updated_at=now,
            signal_type=SignalType.NEW_ITEM, title="t", summary="s",
            geography=None, beat=Beat.OTHER,
            novelty_score=50, local_impact_score=50, evidence_count=0,
        ),
        evidence=[],
    )
    assert len(dbmod.list_signals(tmp_db)) == 1
    dbmod.clear_signals(tmp_db)
    assert dbmod.list_signals(tmp_db) == []
