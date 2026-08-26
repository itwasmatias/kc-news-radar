"""Pydantic model validation — enums, extra=forbid, required fields."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from kc_news_radar.models import (
    Beat,
    Forecast,
    ForecastStatus,
    Signal,
    SignalType,
    SourceItem,
    SourceStatus,
)


def test_source_item_forbids_unknown_fields():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        SourceItem(
            source_name="x",
            external_id="e",
            title="t",
            retrieved_at=now,
            content_hash="h",
            bogus_field="rejected",  # type: ignore[call-arg]
        )


def test_source_item_defaults():
    now = datetime.now(timezone.utc)
    item = SourceItem(
        source_name="x", external_id="e", title="t",
        retrieved_at=now, content_hash="h",
    )
    assert item.beat == Beat.OTHER
    assert item.metadata == {}


def test_new_signal_types_present():
    # Amendment 2 additions must remain valid enum members.
    assert SignalType.COMMUNITY_311_TREND.value == "COMMUNITY_311_TREND"
    assert SignalType.DEVELOPMENT_DEAL_ACTIVITY.value == "DEVELOPMENT_DEAL_ACTIVITY"


def test_forecast_requires_scoring_fields():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        Forecast(
            forecast_id="f1", version=1, issued_at=now,
            horizon_start=now, horizon_end=now, claim="c", event_type="e",
            geography=None, beat=Beat.LOCAL_GOVERNMENT,
            # missing likelihood_score
            editorial_relevance_score=50,
            priority_score=50,
            status=ForecastStatus.OPEN,
            model_version="heuristic-v0.1",
            explanation={},
        )


def test_source_status_enum_values():
    assert {s.value for s in SourceStatus} == {"HEALTHY", "DEGRADED", "FAILED", "DISABLED"}
