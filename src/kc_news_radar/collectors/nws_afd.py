"""NWS Area Forecast Discussion (AFD) — Pleasant Hill / Kansas City (EAX).

The AFD is the forecast office's plain-text narrative describing the reasoning
behind the current forecast. Meaningful shifts in AFD language (e.g., "increasing
confidence in severe threat") are a leading indicator days before an alert.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

AFD_LIST_URL = "https://api.weather.gov/products/types/AFD/locations/EAX"


class NWSAreaForecastCollector(BaseCollector):
    name = "nws_afd_eax"
    label = "NWS Area Forecast Discussion — Pleasant Hill (EAX)"
    source_type = "alert"

    def fetch(self, client: httpx.Client) -> Any:
        try:
            resp = client.get(AFD_LIST_URL, headers={"Accept": "application/ld+json"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        listing = resp.json()

        graph = listing.get("@graph") or []
        results: list[dict[str, Any]] = []
        for prod in graph[:5]:  # bound: newest few AFDs
            prod_url = prod.get("@id")
            if not prod_url:
                continue
            try:
                detail = client.get(prod_url, headers={"Accept": "application/ld+json"})
                detail.raise_for_status()
                results.append(detail.json())
            except httpx.HTTPError:
                continue
        return results

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        products = raw or []
        items: list[SourceItem] = []
        for prod in products:
            pid = prod.get("id") or prod.get("@id")
            if not pid:
                continue
            issued = _parse_iso(prod.get("issuanceTime"))
            text = (prod.get("productText") or "").strip()
            headline = _first_line(text) or "Area Forecast Discussion"
            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=str(pid),
                    canonical_url=prod.get("@id"),
                    title=f"AFD — {headline}"[:400],
                    excerpt=text[:800] if text else None,
                    published_at=issued,
                    event_at=issued,
                    retrieved_at=retrieved_at,
                    geography="Kansas City metro / WFO EAX",
                    beat=Beat.WEATHER_ENVIRONMENT,
                    content_hash=content_hash(str(pid), text[:400]),
                    metadata={"issuingOffice": prod.get("issuingOffice")},
                )
            )
        return items


def _first_line(text: str) -> str | None:
    for ln in text.splitlines():
        clean = ln.strip()
        if clean and not clean.startswith("000") and not clean.startswith("FXUS"):
            return clean[:120]
    return None


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
