"""Scoring: deterministic, explainable, includes new Amendment-2 signal types."""

from __future__ import annotations

from kc_news_radar.models import Beat, SignalType
from kc_news_radar.pipeline.scoring import (
    editorial_relevance_score,
    experimental_likelihood_score,
    priority_score,
)


def test_experimental_likelihood_deterministic():
    kw = dict(
        signal_type=SignalType.SCHEDULED_CATALYST,
        beat=Beat.LOCAL_GOVERNMENT,
        hours_to_event=24,
        evidence_count=3,
        distinct_sources=2,
        agenda_recently_updated=True,
        high_magnitude=True,
    )
    a = experimental_likelihood_score(**kw)
    b = experimental_likelihood_score(**kw)
    assert a.total == b.total
    assert a.components == b.components


def test_likelihood_zero_when_nothing_matches():
    bd = experimental_likelihood_score(
        signal_type=SignalType.NEW_ITEM,
        beat=Beat.OTHER,
        hours_to_event=None,
        evidence_count=0,
        distinct_sources=1,
        agenda_recently_updated=False,
        high_magnitude=False,
    )
    assert bd.total == 0


def test_likelihood_component_reasons_are_human_readable():
    bd = experimental_likelihood_score(
        signal_type=SignalType.SEVERE_WEATHER_CHANGE,
        beat=Beat.WEATHER_ENVIRONMENT,
        hours_to_event=6,
        evidence_count=1,
        distinct_sources=1,
        agenda_recently_updated=False,
        high_magnitude=False,
        severity="Severe",
    )
    reasons = [r for _, r in bd.components]
    assert any("NWS" in r for r in reasons)
    assert bd.total > 0


def test_development_deal_signal_scored():
    """Amendment 2: DEVELOPMENT_DEAL_ACTIVITY must contribute score weight."""
    bd = experimental_likelihood_score(
        signal_type=SignalType.DEVELOPMENT_DEAL_ACTIVITY,
        beat=Beat.HOUSING_DEVELOPMENT,
        hours_to_event=None,
        evidence_count=1,
        distinct_sources=1,
        agenda_recently_updated=False,
        high_magnitude=False,
    )
    assert bd.total >= 15
    reasons = [r for _, r in bd.components]
    assert any("development-deal" in r.lower() for r in reasons)


def test_relevance_reflects_beat_and_boosts():
    bd = editorial_relevance_score(
        beat=Beat.PUBLIC_SAFETY,
        high_magnitude=True,
        affects_multiple_jurisdictions=True,
        public_safety_indicator=True,
        government_accountability=False,
        infrastructure_impact=False,
    )
    assert bd.total > 70
    reasons = [r for _, r in bd.components]
    assert any("base weight" in r for r in reasons)


def test_priority_is_weighted_mean():
    assert priority_score(0, 100) == round(0.55 * 100)
    assert priority_score(100, 0) == round(0.45 * 100)
    assert priority_score(50, 50) == 50


def test_priority_clamped():
    assert 0 <= priority_score(200, 200) <= 100
    assert priority_score(-10, -10) == 0
