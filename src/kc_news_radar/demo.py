"""Demo-mode synthetic fixtures.

When ``KC_NEWS_RADAR_DEMO=1`` the CLI loads these deterministic fixtures into
the DB instead of hitting live sources. The UI clearly labels every demo
record with ``DEMO DATA`` so it can never be mistaken for a real story.

Synthetic data is *never* mixed with live data. The demo entry point wipes
existing records before inserting fixtures.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import db as dbmod
from .collectors.base import content_hash
from .models import Beat, SourceHealth, SourceItem, SourceStatus


DEMO_TAG = "DEMO DATA"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_demo_fixtures(conn: sqlite3.Connection) -> None:
    """Wipe collector state and insert a small, deterministic synthetic set."""
    conn.execute("DELETE FROM resolution_targets")
    conn.execute("DELETE FROM resolutions")
    conn.execute("DELETE FROM feedback")
    conn.execute("DELETE FROM forecast_evidence_snapshots")
    conn.execute("DELETE FROM forecasts")
    conn.execute("DELETE FROM signal_evidence")
    conn.execute("DELETE FROM signals")
    conn.execute("DELETE FROM source_items")
    conn.execute("DELETE FROM source_health")
    conn.commit()

    now = _now()
    yesterday = now - timedelta(hours=20)
    tomorrow = now + timedelta(hours=18)
    two_days = now + timedelta(hours=44)

    fixtures = [
        SourceItem(
            source_name="kcmo_open_data",
            external_id="demo-311-001",
            canonical_url="https://example.invalid/demo/kcmo/1",
            title=f"[{DEMO_TAG}] 311: dangerous building report — Westport",
            excerpt=f"[{DEMO_TAG}] Repeat 311 case: vacant property with structural concerns. Council district 4. Status: assigned to Code Enforcement.",
            published_at=yesterday,
            event_at=yesterday,
            retrieved_at=now,
            geography="Westport",
            beat=Beat.HOUSING_DEVELOPMENT,
            content_hash=content_hash("demo-311-001"),
            metadata={"council_district": "4", "status": "assigned", "request_type": "dangerous building"},
        ),
        SourceItem(
            source_name="kcmo_open_data",
            external_id="demo-311-002",
            canonical_url="https://example.invalid/demo/kcmo/2",
            title=f"[{DEMO_TAG}] Council notice: housing action scheduled",
            excerpt=f"[{DEMO_TAG}] Council will consider ordinance for $12,000,000 housing appropriation. Emergency ordinance status. Meeting Thursday.",
            published_at=yesterday,
            event_at=tomorrow,
            retrieved_at=now,
            geography="Kansas City, MO",
            beat=Beat.LOCAL_GOVERNMENT,
            content_hash=content_hash("demo-311-002"),
            metadata={"council_district": "3", "amount": 12000000},
        ),
        SourceItem(
            source_name="johnson_county",
            external_id="demo-joco-001",
            canonical_url="https://example.invalid/demo/joco/1",
            title=f"[{DEMO_TAG}] Johnson County Commission — unusual agenda activity",
            excerpt=f"[{DEMO_TAG}] Three related agenda items regarding a $45 million infrastructure appropriation. BOCC meeting within 36 hours.",
            published_at=yesterday,
            event_at=now + timedelta(hours=36),
            retrieved_at=now,
            geography="Johnson County, KS",
            beat=Beat.LOCAL_GOVERNMENT,
            content_hash=content_hash("demo-joco-001"),
            metadata={"amount": 45000000, "raw_category": "bocc"},
        ),
        SourceItem(
            source_name="johnson_county",
            external_id="demo-joco-002",
            canonical_url="https://example.invalid/demo/joco/2",
            title=f"[{DEMO_TAG}] Johnson County Commission — related budget amendment",
            excerpt=f"[{DEMO_TAG}] Companion resolution to the $45 million infrastructure appropriation.",
            published_at=yesterday,
            event_at=now + timedelta(hours=36),
            retrieved_at=now,
            geography="Johnson County, KS",
            beat=Beat.LOCAL_GOVERNMENT,
            content_hash=content_hash("demo-joco-002"),
            metadata={"amount": 45000000, "raw_category": "bocc"},
        ),
        SourceItem(
            source_name="johnson_county",
            external_id="demo-joco-003",
            canonical_url="https://example.invalid/demo/joco/3",
            title=f"[{DEMO_TAG}] Johnson County Commission — public hearing notice for infrastructure",
            excerpt=f"[{DEMO_TAG}] Public hearing notice tied to the pending infrastructure appropriation.",
            published_at=yesterday,
            event_at=now + timedelta(hours=36),
            retrieved_at=now,
            geography="Johnson County, KS",
            beat=Beat.LOCAL_GOVERNMENT,
            content_hash=content_hash("demo-joco-003"),
            metadata={"raw_category": "bocc"},
        ),
        SourceItem(
            source_name="nws_kc",
            external_id="demo-nws-001",
            canonical_url="https://example.invalid/demo/nws/1",
            title=f"[{DEMO_TAG}] NWS Severe Thunderstorm Watch — Jackson, Clay, Cass",
            excerpt=f"[{DEMO_TAG}] Severe thunderstorms possible this evening with damaging wind and hail.",
            published_at=now - timedelta(hours=1),
            event_at=now + timedelta(hours=6),
            retrieved_at=now,
            geography="Jackson; Clay; Cass",
            beat=Beat.WEATHER_ENVIRONMENT,
            content_hash=content_hash("demo-nws-001"),
            metadata={"severity": "Severe", "event": "Severe Thunderstorm Watch", "urgency": "Expected"},
        ),
        SourceItem(
            source_name="ridekc",
            external_id="demo-ridekc-001",
            canonical_url="https://example.invalid/demo/ridekc/1",
            title=f"[{DEMO_TAG}] RideKC service disruption — Route 47",
            excerpt=f"[{DEMO_TAG}] Route 47 detour due to overnight water main work. Effect through Friday.",
            published_at=now - timedelta(hours=3),
            event_at=now - timedelta(hours=3),
            retrieved_at=now,
            geography="Kansas City metro (transit)",
            beat=Beat.TRANSPORTATION,
            content_hash=content_hash("demo-ridekc-001"),
            metadata={"route_hint": ["47"]},
        ),
        SourceItem(
            source_name="mo_house",
            external_id="demo-mo-hb42",
            canonical_url="https://example.invalid/demo/mo/hb42",
            title=f"[{DEMO_TAG}] MO HB 42 — education funding formula amendment",
            excerpt=f"[{DEMO_TAG}] Amends the education funding formula; committee vote scheduled tomorrow.",
            published_at=yesterday,
            event_at=tomorrow,
            retrieved_at=now,
            geography="State of Missouri",
            beat=Beat.STATE_GOVERNMENT,
            content_hash=content_hash("demo-mo-hb42"),
            metadata={"bill": "HB42"},
        ),
        SourceItem(
            source_name="usgs_quakes",
            external_id="demo-usgs-001",
            canonical_url="https://example.invalid/demo/usgs/1",
            title=f"[{DEMO_TAG}] M3.4 earthquake — 32km ENE of Cape Girardeau, MO",
            excerpt=f"[{DEMO_TAG}] Small earthquake felt in southeast Missouri. New Madrid seismic zone.",
            published_at=now - timedelta(hours=2),
            event_at=now - timedelta(hours=2),
            retrieved_at=now,
            geography="southeast Missouri",
            beat=Beat.WEATHER_ENVIRONMENT,
            content_hash=content_hash("demo-usgs-001"),
            metadata={"magnitude": 3.4},
        ),
        # Development-deal fixture — exercises DEVELOPMENT_DEAL_ACTIVITY detector.
        SourceItem(
            source_name="kcmo_open_data",
            external_id="demo-devdeal-001",
            canonical_url="https://example.invalid/demo/devdeal/1",
            title=f"[{DEMO_TAG}] Council resolution — Royals stadium development agreement discussion",
            excerpt=f"[{DEMO_TAG}] Council to discuss draft development agreement referencing Port KC land acquisition and TIF financing for proposed Royals stadium site.",
            published_at=yesterday,
            event_at=tomorrow,
            retrieved_at=now,
            geography="Kansas City, MO",
            beat=Beat.HOUSING_DEVELOPMENT,
            content_hash=content_hash("demo-devdeal-001"),
            metadata={"council_district": "3"},
        ),
    ]
    # 311 category-spike + geography-concentration fixture — exercises
    # COMMUNITY_311_TREND detector. All aggregated by neighborhood, never
    # by street. Category "dangerous building" repeats ≥5 times; neighborhood
    # "Westport" repeats ≥4 times.
    for i in range(1, 7):
        fixtures.append(
            SourceItem(
                source_name="kcmo_open_data",
                external_id=f"demo-311-spike-{i:03d}",
                canonical_url=f"https://example.invalid/demo/kcmo/spike/{i}",
                title=f"[{DEMO_TAG}] 311: dangerous building — Westport #{i}",
                excerpt=f"[{DEMO_TAG}] Resident-reported dangerous-building 311 case in Westport. Aggregate signal; not a verified fact about any property.",
                published_at=now - timedelta(hours=6 + i),
                event_at=now - timedelta(hours=6 + i),
                retrieved_at=now,
                geography="Westport",
                beat=Beat.HOUSING_DEVELOPMENT,
                content_hash=content_hash(f"demo-311-spike-{i}"),
                metadata={
                    "council_district": "4",
                    "neighborhood": "Westport",
                    "status": "open",
                    "request_type": "dangerous building",
                    "street_address_private": None,
                },
            )
        )

    for item in fixtures:
        dbmod.upsert_source_item(conn, item)

    # Insert some health records to mirror a realistic mix.
    now = _now()
    healths = [
        SourceHealth(source_name="kcmo_open_data", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=2, error_message=None, latency_ms=180),
        SourceHealth(source_name="kcmo_council_legistar", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=4, error_message=None, latency_ms=320),
        SourceHealth(source_name="jackson_county", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=3, error_message=None, latency_ms=340),
        SourceHealth(source_name="johnson_county", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=3, error_message=None, latency_ms=260),
        SourceHealth(source_name="nws_kc", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=1, error_message=None, latency_ms=210),
        SourceHealth(source_name="nws_afd_eax", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=0, error_message="empty response (no current items)", latency_ms=350),
        SourceHealth(source_name="usgs_quakes", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=1, error_message=None, latency_ms=190),
        SourceHealth(source_name="mo_house", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=1, error_message=None, latency_ms=430),
        SourceHealth(source_name="ridekc", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=2, error_message=None, latency_ms=470),
        SourceHealth(source_name="flykc", status=SourceStatus.HEALTHY, last_attempt=now, last_success=now, item_count=1, error_message=None, latency_ms=380),
    ]
    for h in healths:
        dbmod.upsert_source_health(conn, h)


__all__ = ["load_demo_fixtures", "DEMO_TAG"]
