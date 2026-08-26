"""National Weather Service — Kansas City / Pleasant Hill (KEAX) office.

Uses the official NWS public JSON API. Documented at https://www.weather.gov/documentation/services-web-api.
The API is free, does not require authentication, and asks callers to identify
themselves via User-Agent — which the base collector already does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

# Kansas City metro NWS Zones covering the WFO Pleasant Hill (KEAX) county
# warning area for the immediate metro. This filter keeps the record set to
# the KC-metro rather than the entire two-state region.
KC_METRO_ZONES = [
    # Missouri
    "MOZ028",  # Platte
    "MOZ029",  # Clay
    "MOZ043",  # Jackson
    "MOZ037",  # Ray
    "MOZ042",  # Cass
    # Kansas
    "KSZ057",  # Wyandotte
    "KSZ102",  # Johnson (KS)
    "KSZ060",  # Leavenworth
]

ALERTS_URL = "https://api.weather.gov/alerts/active"


class NWSCollector(BaseCollector):
    name = "nws_kc"
    label = "National Weather Service — Kansas City"
    source_type = "alert"
    # An alerts feed with zero active alerts is a legitimate quiet period.
    empty_ok = True

    def fetch(self, client: httpx.Client) -> Any:
        params = {"zone": ",".join(KC_METRO_ZONES)}
        try:
            resp = client.get(ALERTS_URL, params=params, headers={"Accept": "application/geo+json"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.json()

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        features = (raw or {}).get("features") or []
        items: list[SourceItem] = []
        for feat in features:
            props = feat.get("properties") or {}
            ext_id = feat.get("id") or props.get("id")
            if not ext_id:
                continue
            headline = props.get("headline") or props.get("event") or "NWS alert"
            description = (props.get("description") or "").strip()
            severity = props.get("severity")
            event = props.get("event")
            areaDesc = props.get("areaDesc")
            sent = _parse_iso(props.get("sent"))
            onset = _parse_iso(props.get("onset")) or _parse_iso(props.get("effective"))

            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=str(ext_id),
                    canonical_url=props.get("@id") or feat.get("id"),
                    title=str(headline)[:400],
                    excerpt=description[:800] if description else None,
                    published_at=sent,
                    event_at=onset,
                    retrieved_at=retrieved_at,
                    geography=str(areaDesc)[:300] if areaDesc else "Kansas City metro",
                    beat=Beat.WEATHER_ENVIRONMENT,
                    content_hash=content_hash(str(ext_id), str(headline), description[:500], str(severity)),
                    metadata={
                        "severity": severity,
                        "event": event,
                        "certainty": props.get("certainty"),
                        "urgency": props.get("urgency"),
                        "expires": props.get("expires"),
                    },
                )
            )
        return items


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
