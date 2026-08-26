"""Collector parse() tests + failure isolation. No live HTTP.

We hit each collector's parse() directly with fixture bytes and assert on
normalized SourceItem shape. failure isolation is exercised by asserting that
a collector whose fetch() raises still yields a FAILED health row and does
not raise from the top-level runner.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from kc_news_radar.collectors.base import (
    BaseCollector,
    FetchError,
    content_hash,
    run_all_collectors,
)
from kc_news_radar.collectors.flykc import FlyKCCollector
from kc_news_radar.collectors.jackson_county import JacksonCountyCollector
from kc_news_radar.collectors.johnson_county import JohnsonCountyCollector
from kc_news_radar.collectors.kcmo import KCMOClerkLegistarCollector, KCMOCollector
from kc_news_radar.collectors.nws import NWSCollector
from kc_news_radar.collectors.ridekc import RideKCCollector
from kc_news_radar.collectors.usgs import USGSQuakeCollector
from kc_news_radar.models import Beat, SourceItem, SourceStatus


NOW = datetime.now(timezone.utc)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> Any:
    with (FIXTURES / name).open() as f:
        return json.load(f)


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# Individual parsers
# ---------------------------------------------------------------------------


def test_kcmo_311_parse_produces_items():
    fixture = [
        {
            "case_id": "CASE-1",
            "request_type": "Dangerous Building",
            "neighborhood": "Westport",
            "council_district": "4",
            "street_address": "123 Fake St",  # must NOT appear in dashboard fields
            "description": "Repeat structural concerns",
            "status": "open",
            "creation_date": "2026-08-01T12:00:00",
        }
    ]
    items = KCMOCollector().parse(fixture, retrieved_at=NOW)
    assert len(items) == 1
    it = items[0]
    assert it.source_name == "kcmo_open_data"
    # Aggregate geography, not street.
    assert "Westport" in (it.geography or "")
    assert "123 Fake St" not in (it.title or "")
    assert "123 Fake St" not in (it.excerpt or "")
    # Street is retained in metadata under a private key.
    assert it.metadata.get("street_address_private") == "123 Fake St"


def test_kcmo_311_ignores_rows_without_case_id():
    fixture = [{"request_type": "Trash"}]  # no case_id
    items = KCMOCollector().parse(fixture, retrieved_at=NOW)
    assert items == []


def test_nws_parse_empty_features():
    fixture = {"features": []}
    items = NWSCollector().parse(fixture, retrieved_at=NOW)
    assert items == []


def test_nws_parse_severe_alert():
    fixture = {
        "features": [
            {
                "id": "urn:oid:1",
                "properties": {
                    "id": "alert-1",
                    "event": "Severe Thunderstorm Warning",
                    "headline": "SVR for Jackson",
                    "description": "Damaging wind.",
                    "severity": "Severe",
                    "urgency": "Immediate",
                    "certainty": "Observed",
                    "areaDesc": "Jackson, MO",
                    "sent": "2026-08-01T12:00:00+00:00",
                    "effective": "2026-08-01T12:00:00+00:00",
                    "onset": "2026-08-01T12:30:00+00:00",
                    "expires": "2026-08-01T14:00:00+00:00",
                },
            }
        ]
    }
    items = NWSCollector().parse(fixture, retrieved_at=NOW)
    assert len(items) == 1
    assert items[0].metadata.get("severity") == "Severe"


def test_usgs_parse_geojson():
    fixture = {
        "features": [
            {
                "id": "us1",
                "properties": {
                    "mag": 3.4,
                    "place": "32km ENE of Cape Girardeau, MO",
                    "time": 1700000000000,
                    "url": "https://example.invalid",
                    "type": "earthquake",
                },
                "geometry": {"coordinates": [-89.0, 37.0, 5.0]},
            }
        ]
    }
    items = USGSQuakeCollector().parse(fixture, retrieved_at=NOW)
    assert len(items) == 1
    assert items[0].source_name == "usgs_quakes"


def test_johnson_county_parse_rss():
    xml = (
        b"<?xml version='1.0'?>"
        b"<rss><channel><item>"
        b"<title>Test JoCo item</title>"
        b"<link>https://example.invalid/1</link>"
        b"<guid>joco-1</guid>"
        b"<description>Description.</description>"
        b"<pubDate>Mon, 01 Aug 2026 12:00:00 +0000</pubDate>"
        b"</item></channel></rss>"
    )
    items = JohnsonCountyCollector().parse(xml, retrieved_at=NOW)
    assert len(items) == 1
    assert items[0].source_name == "johnson_county"
    assert items[0].external_id


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class _BoomCollector(BaseCollector):
    name = "boom"
    label = "Always fails"
    source_type = "test"

    def fetch(self, client: httpx.Client) -> Any:
        raise FetchError("simulated upstream failure")

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return []


class _StaticCollector(BaseCollector):
    name = "static"
    label = "Static ok"
    source_type = "test"

    def fetch(self, client: httpx.Client) -> Any:
        return {"ok": True}

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return [
            SourceItem(
                source_name=self.name,
                external_id="x",
                title="t",
                retrieved_at=retrieved_at,
                content_hash=content_hash("x"),
            )
        ]


def test_failing_collector_marked_failed_but_does_not_raise():
    result = _BoomCollector().run()
    assert result.health.status == SourceStatus.FAILED
    assert result.items == []
    assert "simulated" in (result.health.error_message or "")


def test_one_failure_does_not_stop_the_rest():
    results = run_all_collectors([_BoomCollector(), _StaticCollector()])
    by_name = {r.source_name: r for r in results}
    assert by_name["boom"].health.status == SourceStatus.FAILED
    assert by_name["static"].health.status == SourceStatus.HEALTHY
    assert len(by_name["static"].items) == 1


class _EmptyOKCollector(BaseCollector):
    name = "empty_ok"
    label = "Empty OK"
    source_type = "test"
    empty_ok = True

    def fetch(self, client: httpx.Client) -> Any:
        return {"features": []}

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return []


def test_empty_ok_stays_healthy_when_empty():
    r = _EmptyOKCollector().run()
    assert r.health.status == SourceStatus.HEALTHY
    assert r.health.item_count == 0


class _EmptyBadCollector(BaseCollector):
    name = "empty_bad"
    label = "Empty bad"
    source_type = "test"

    def fetch(self, client: httpx.Client) -> Any:
        return None

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return []


def test_default_empty_is_degraded():
    r = _EmptyBadCollector().run()
    assert r.health.status == SourceStatus.DEGRADED


# ---------------------------------------------------------------------------
# Dedupe helper (content_hash)
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic_and_sensitive():
    a = content_hash("id", "title", "excerpt")
    b = content_hash("id", "title", "excerpt")
    c = content_hash("id", "title", "excerpt-2")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# Repaired-adapter regression tests (RideKC, FlyKC, Jackson County, KCMO Clerk)
# ---------------------------------------------------------------------------


def test_ridekc_parses_current_html_alerts():
    items = RideKCCollector().parse(_load_text("ridekc_alerts.html"), retrieved_at=NOW)
    assert len(items) == 2  # duplicate permalink deduped; page chrome ignored
    titles = [it.title for it in items]
    assert "Holmes Bridge Construction Reroutes" in titles
    assert any("Wine Walk" in t for t in titles)

    holmes = next(it for it in items if it.title == "Holmes Bridge Construction Reroutes")
    assert holmes.canonical_url.endswith(
        "/kcata/news/service-bulletin-holmes-bridge-construction-reroutes/"
    )
    assert holmes.source_name == "ridekc"
    assert holmes.beat == Beat.TRANSPORTATION
    assert holmes.metadata["cause"] == "Construction"
    assert holmes.metadata["effect"] == "Detour"
    assert holmes.metadata["expires_text"] == "8/27/2026"
    # event_at is the parsed expiration (UTC-normalized)
    assert holmes.event_at == datetime(2026, 8, 27, tzinfo=timezone.utc)
    # Route numbers surface either from href badges or free-text mentions.
    assert holmes.metadata["routes"], "expected route mentions on Holmes alert"

    wine = next(it for it in items if "Wine Walk" in it.title)
    assert "18" in wine.metadata["routes"]  # from /routes/18/ href in fixture
    assert wine.canonical_url.startswith("https://")


def test_ridekc_parse_is_idempotent_on_duplicate_input():
    raw = _load_text("ridekc_alerts.html")
    first = RideKCCollector().parse(raw, retrieved_at=NOW)
    second = RideKCCollector().parse(raw, retrieved_at=NOW)
    # Same permalinks in both runs, same content hashes → idempotent collection.
    assert [it.external_id for it in first] == [it.external_id for it in second]
    assert [it.content_hash for it in first] == [it.content_hash for it in second]


def test_ridekc_parse_survives_malformed_html():
    assert RideKCCollector().parse("<html>", retrieved_at=NOW) == []
    assert RideKCCollector().parse("", retrieved_at=NOW) == []
    assert RideKCCollector().parse(None, retrieved_at=NOW) == []


def test_flykc_parses_prismic_general_content_pages():
    items = FlyKCCollector().parse(_load_json("flykc_prismic_search.json"), retrieved_at=NOW)
    assert len(items) == 2  # third fixture row has no uid → skipped
    uids = [it.metadata["uid"] for it in items]
    assert "world-cup-2026" in uids
    assert "new-terminal-project-awards" in uids

    world_cup = next(it for it in items if it.metadata["uid"] == "world-cup-2026")
    assert world_cup.title == "2026 FIFA World Cup\u2122"
    assert world_cup.canonical_url == "https://flykc.com/world-cup-2026"
    assert world_cup.beat == Beat.TRANSPORTATION
    assert world_cup.geography == "Kansas City International Airport"
    assert world_cup.published_at == datetime(
        2026, 5, 27, 20, 53, 37, tzinfo=timezone.utc
    )
    # last_publication_date should populate event_at
    assert world_cup.event_at == datetime(
        2026, 6, 16, 20, 57, 11, tzinfo=timezone.utc
    )
    # excerpt comes from meta_description
    assert world_cup.excerpt and "2026 FIFA World Cup" in world_cup.excerpt

    # Rich-text title (list of blocks) is coerced to a plain string.
    terminal = next(it for it in items if it.metadata["uid"] == "new-terminal-project-awards")
    assert terminal.title == "New Terminal Project Awards"


def test_flykc_parse_is_idempotent():
    raw = _load_json("flykc_prismic_search.json")
    first = FlyKCCollector().parse(raw, retrieved_at=NOW)
    second = FlyKCCollector().parse(raw, retrieved_at=NOW)
    assert [it.external_id for it in first] == [it.external_id for it in second]
    assert [it.content_hash for it in first] == [it.content_hash for it in second]


def test_flykc_parse_survives_malformed_json():
    assert FlyKCCollector().parse({}, retrieved_at=NOW) == []
    assert FlyKCCollector().parse({"results": "not a list"}, retrieved_at=NOW) == []
    assert FlyKCCollector().parse(None, retrieved_at=NOW) == []


def test_jackson_county_parses_legistar_matters():
    items = JacksonCountyCollector().parse(
        _load_json("legistar_jacksonco_matters.json"), retrieved_at=NOW
    )
    assert len(items) == 2  # null MatterId row skipped
    ids = [it.external_id for it in items]
    assert "legistar:jacksonco:19293" in ids
    assert "legistar:jacksonco:8254" in ids

    resolution_22204 = next(it for it in items if it.external_id.endswith(":19293"))
    assert resolution_22204.title.startswith("Resolution 22204:")
    assert resolution_22204.canonical_url == (
        "https://jacksongov.legistar.com/LegislationDetail.aspx?ID=19293"
        "&GUID=158B4A27-3B14-404B-8842-B2B9E706D2FF"
    )
    assert resolution_22204.beat == Beat.HEALTH  # "public health related" in title
    assert resolution_22204.event_at == datetime(2026, 3, 9, tzinfo=timezone.utc)
    assert resolution_22204.metadata["matter_status"] == "Passed"
    assert resolution_22204.metadata["matter_body"] == "Budget Committee"


def test_jackson_county_parse_is_idempotent():
    raw = _load_json("legistar_jacksonco_matters.json")
    first = JacksonCountyCollector().parse(raw, retrieved_at=NOW)
    second = JacksonCountyCollector().parse(raw, retrieved_at=NOW)
    assert [it.external_id for it in first] == [it.external_id for it in second]
    assert [it.content_hash for it in first] == [it.content_hash for it in second]


def test_jackson_county_parse_survives_malformed_json():
    assert JacksonCountyCollector().parse({"not": "a list"}, retrieved_at=NOW) == []
    assert JacksonCountyCollector().parse(None, retrieved_at=NOW) == []


def test_kcmo_clerk_legistar_parses_matters():
    items = KCMOClerkLegistarCollector().parse(
        _load_json("legistar_kansascity_matters.json"), retrieved_at=NOW
    )
    assert len(items) == 2
    roy_blunt = next(it for it in items if it.external_id.endswith(":50892"))
    assert roy_blunt.title.startswith("Ordinance 260718:")
    assert "Roy Blunt Luminary Park" in roy_blunt.title
    assert roy_blunt.canonical_url == (
        "https://kansascity.legistar.com/LegislationDetail.aspx?ID=50892"
        "&GUID=9805D51E-C5C2-4E82-A9BF-ACEE4F9559E8"
    )
    assert roy_blunt.event_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert roy_blunt.metadata["matter_status"] == "Agenda Ready"
    assert roy_blunt.metadata["matter_body"] == "Finance, Governance and Public Safety Committee"
    # Fiscal magnitude keyword classifies this to ECONOMY_BUSINESS
    assert roy_blunt.beat in (Beat.ECONOMY_BUSINESS, Beat.LOCAL_GOVERNMENT)
    # Title carries no stray carriage returns.
    assert "\r" not in roy_blunt.title
    assert "\n" not in roy_blunt.title


def test_kcmo_clerk_legistar_parse_is_idempotent():
    raw = _load_json("legistar_kansascity_matters.json")
    first = KCMOClerkLegistarCollector().parse(raw, retrieved_at=NOW)
    second = KCMOClerkLegistarCollector().parse(raw, retrieved_at=NOW)
    assert [it.external_id for it in first] == [it.external_id for it in second]
    assert [it.content_hash for it in first] == [it.content_hash for it in second]


def test_all_repaired_adapters_normalize_canonical_url_and_timestamps():
    """Every repaired adapter produces UTC-aware datetimes and https:// URLs."""
    all_items: list[SourceItem] = []
    all_items.extend(
        RideKCCollector().parse(_load_text("ridekc_alerts.html"), retrieved_at=NOW)
    )
    all_items.extend(
        FlyKCCollector().parse(_load_json("flykc_prismic_search.json"), retrieved_at=NOW)
    )
    all_items.extend(
        JacksonCountyCollector().parse(
            _load_json("legistar_jacksonco_matters.json"), retrieved_at=NOW
        )
    )
    all_items.extend(
        KCMOClerkLegistarCollector().parse(
            _load_json("legistar_kansascity_matters.json"), retrieved_at=NOW
        )
    )
    assert all_items, "expected fixture-driven items across all four adapters"
    for it in all_items:
        assert it.canonical_url and it.canonical_url.startswith("https://"), (
            f"non-https URL: {it.canonical_url}"
        )
        assert it.retrieved_at.tzinfo is not None
        if it.event_at is not None:
            assert it.event_at.tzinfo is not None
        if it.published_at is not None:
            assert it.published_at.tzinfo is not None


class _RepairedAdapterFailsOnFetch(BaseCollector):
    """Simulates any repaired adapter losing its upstream and failing cleanly."""

    name = "boom_repaired"
    label = "Simulated repaired-adapter failure"
    source_type = "test"

    def fetch(self, client: httpx.Client) -> Any:
        raise FetchError("upstream unreachable")

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return []


def test_repaired_adapter_upstream_failure_stays_isolated():
    r = _RepairedAdapterFailsOnFetch().run()
    assert r.health.status == SourceStatus.FAILED
    assert "upstream" in (r.health.error_message or "").lower()
    # And confirms failure isolation still applies in the wider runner.
    results = run_all_collectors([_RepairedAdapterFailsOnFetch(), _StaticCollector()])
    by_name = {r.source_name: r for r in results}
    assert by_name["boom_repaired"].health.status == SourceStatus.FAILED
    assert by_name["static"].health.status == SourceStatus.HEALTHY
