"""Signal detection tests — including the two Amendment-2 signal types.

These tests exercise the deterministic detector functions directly on
prepared item dicts, mirroring what dbmod.list_source_items returns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kc_news_radar.models import Beat, SignalType
from kc_news_radar.pipeline.signals import RECENCY_HOURS, detect_signals


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _item(**overrides):
    base = {
        "id": 1,
        "source_name": "kcmo_open_data",
        "title": "t",
        "excerpt": None,
        "beat": Beat.OTHER.value,
        "geography": None,
        "event_at": None,
        "published_at": None,
        "retrieved_at": NOW,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "metadata": {},
    }
    base.update(overrides)
    return base


def _types(signals):
    return {s.signal.signal_type for s in signals}


def test_scheduled_catalyst_within_horizon():
    items = [
        _item(
            id=10,
            source_name="johnson_county",
            title="BOCC meeting",
            beat=Beat.LOCAL_GOVERNMENT.value,
            event_at=NOW + timedelta(hours=24),
        )
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.SCHEDULED_CATALYST in _types(signals)


def test_scheduled_catalyst_ignored_beyond_horizon():
    items = [
        _item(
            id=11,
            source_name="johnson_county",
            title="Distant meeting",
            beat=Beat.LOCAL_GOVERNMENT.value,
            event_at=NOW + timedelta(days=30),
        )
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.SCHEDULED_CATALYST not in _types(signals)


def test_severe_weather_produces_signal():
    items = [
        _item(
            id=12,
            source_name="nws_kc",
            title="Severe Thunderstorm Warning",
            beat=Beat.WEATHER_ENVIRONMENT.value,
            metadata={"severity": "Severe", "event": "Severe Thunderstorm Warning"},
        )
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.SEVERE_WEATHER_CHANGE in _types(signals)


def test_unusual_agenda_matches_high_magnitude_terms():
    items = [
        _item(
            id=13,
            source_name="johnson_county",
            title="$45 million appropriation resolution",
            excerpt="Emergency ordinance tax increment financing",
            beat=Beat.LOCAL_GOVERNMENT.value,
        )
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.UNUSUAL_AGENDA_ITEM in _types(signals)


def test_development_deal_signal_fires_on_royals_keyword():
    items = [
        _item(
            id=14,
            source_name="kcmo_open_data",
            title="Council discussion — Royals stadium development agreement",
            excerpt="Draft references Port KC and TIF financing.",
            beat=Beat.HOUSING_DEVELOPMENT.value,
        )
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.DEVELOPMENT_DEAL_ACTIVITY in _types(signals)


def test_development_deal_signal_silent_without_keywords():
    items = [
        _item(
            id=15,
            source_name="kcmo_open_data",
            title="Trash collection notice",
            excerpt="Weekly schedule.",
            beat=Beat.LOCAL_GOVERNMENT.value,
        )
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.DEVELOPMENT_DEAL_ACTIVITY not in _types(signals)


def test_community_311_trend_category_spike():
    # Need >=5 items in same request_type to trigger category spike.
    items = [
        _item(
            id=100 + i,
            source_name="kcmo_open_data",
            title=f"311: dangerous building {i}",
            beat=Beat.HOUSING_DEVELOPMENT.value,
            metadata={"request_type": "dangerous building", "neighborhood": f"n{i}"},
        )
        for i in range(6)
    ]
    signals = [s for s in detect_signals(items, now=NOW)
               if s.signal.signal_type == SignalType.COMMUNITY_311_TREND]
    # Both a category-spike and (since one geo is empty) may or may not emit
    # geography-concentration. At minimum the category spike must appear.
    assert any("category spike" in s.signal.title.lower() for s in signals)


def test_community_311_trend_labels_as_resident_reported():
    items = [
        _item(
            id=200 + i,
            source_name="kcmo_open_data",
            title=f"311: pothole {i}",
            beat=Beat.TRANSPORTATION.value,
            metadata={"request_type": "pothole", "neighborhood": "Waldo"},
        )
        for i in range(6)
    ]
    signals = [s for s in detect_signals(items, now=NOW)
               if s.signal.signal_type == SignalType.COMMUNITY_311_TREND]
    assert signals, "expected at least one 311 signal"
    combined = " ".join(s.signal.summary.lower() for s in signals)
    # Scientific-safety labeling is required per Amendment 2.
    assert "resident" in combined
    assert "not verified" in combined


def test_community_311_never_exposes_street_in_summary():
    items = [
        _item(
            id=300 + i,
            source_name="kcmo_open_data",
            title=f"311: rodent report {i}",
            beat=Beat.HEALTH.value,
            metadata={
                "request_type": "rodent",
                "neighborhood": "Westport",
                "street_address_private": "123 Fake St",
            },
        )
        for i in range(6)
    ]
    signals = [s for s in detect_signals(items, now=NOW)
               if s.signal.signal_type == SignalType.COMMUNITY_311_TREND]
    assert signals
    for s in signals:
        assert "123 Fake St" not in s.signal.title
        assert "123 Fake St" not in s.signal.summary


def test_community_311_only_triggers_from_kcmo_source():
    items = [
        _item(
            id=400 + i,
            source_name="some_other_source",
            title=f"case {i}",
            beat=Beat.OTHER.value,
            metadata={"request_type": "trash"},
        )
        for i in range(10)
    ]
    signals = detect_signals(items, now=NOW)
    assert SignalType.COMMUNITY_311_TREND not in _types(signals)


def test_recency_cutoff_excludes_old_items():
    old = NOW - timedelta(hours=RECENCY_HOURS + 10)
    items = [
        _item(
            id=500,
            source_name="johnson_county",
            title="ancient item",
            beat=Beat.LOCAL_GOVERNMENT.value,
            first_seen_at=old,
            last_seen_at=old,
        )
    ]
    signals = detect_signals(items, now=NOW)
    # NEW_ITEM must not fire — item is too old.
    assert SignalType.NEW_ITEM not in _types(signals)


def test_deterministic_output_order():
    items = [
        _item(id=1, source_name="a", title="Alpha",
              beat=Beat.LOCAL_GOVERNMENT.value,
              event_at=NOW + timedelta(hours=10)),
        _item(id=2, source_name="b", title="Bravo",
              beat=Beat.LOCAL_GOVERNMENT.value,
              event_at=NOW + timedelta(hours=20)),
    ]
    s1 = [s.signal.signal_type.value for s in detect_signals(items, now=NOW)]
    s2 = [s.signal.signal_type.value for s in detect_signals(items, now=NOW)]
    assert s1 == s2
