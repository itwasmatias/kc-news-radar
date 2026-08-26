"""USGS earthquake feed — Kansas City metro geographic filter.

Kansas City sits near the New Madrid Seismic Zone reach; the USGS public
earthquake API is a well-documented free JSON endpoint. Even a small local
event is newsworthy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

# Broader central-US window so occasional New Madrid events are captured.
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
BOX = {
    "minlatitude": "36.0",
    "maxlatitude": "41.0",
    "minlongitude": "-96.5",
    "maxlongitude": "-91.5",
    "orderby": "time",
    "limit": "50",
    "format": "geojson",
}


class USGSQuakeCollector(BaseCollector):
    name = "usgs_quakes"
    label = "USGS earthquakes — Central US"
    source_type = "alert"
    empty_ok = True  # no recent quakes is a legitimate quiet period

    def fetch(self, client: httpx.Client) -> Any:
        try:
            resp = client.get(USGS_URL, params=BOX)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.json()

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        features = (raw or {}).get("features") or []
        items: list[SourceItem] = []
        for feat in features:
            props = feat.get("properties") or {}
            ext_id = feat.get("id")
            if not ext_id:
                continue
            mag = props.get("mag")
            place = props.get("place") or "central US"
            time_ms = props.get("time")
            event_at = _from_ms(time_ms)
            title = f"M{mag} earthquake — {place}" if mag is not None else f"Seismic event — {place}"
            url = props.get("url")
            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=str(ext_id),
                    canonical_url=url,
                    title=title[:400],
                    excerpt=f"Magnitude {mag}. Location: {place}. Status: {props.get('status')}."[:800],
                    published_at=event_at,
                    event_at=event_at,
                    retrieved_at=retrieved_at,
                    geography=str(place)[:200],
                    beat=Beat.WEATHER_ENVIRONMENT,
                    content_hash=content_hash(str(ext_id), str(mag), str(place)),
                    metadata={"magnitude": mag, "type": props.get("type"), "tsunami": props.get("tsunami")},
                )
            )
        return items


def _from_ms(ms: int | float | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None
