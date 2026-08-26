"""Kansas City, Missouri — 311 open-data + City Clerk legislative record.

Two adapters live here:

* :class:`KCMOCollector` — the city's 311 service-request stream on the
  Kansas City open-data (Socrata) portal. Aggregate resident-reported
  service-request patterns are a legitimate newsroom signal about
  neighborhood conditions and city responsiveness. Street-level location
  data is retained in metadata but never surfaced in dashboard-visible
  fields; see ``docs/DATA_SOURCES.md``.

* :class:`KCMOClerkLegistarCollector` — the Kansas City Council / City
  Clerk legislative record system. The Clerk publishes the council's
  legislative record through Legistar (``kansascity.legistar.com``);
  the corresponding public InSite web API
  (``webapi.legistar.com/v1/kansascity/``) exposes structured Matter rows
  with file number, type, status, controlling body, intro date, agenda
  date, passed/enactment date, sponsor, and title. That is the correct
  legislative-monitoring surface for a newsroom radar — replacing the
  earlier RSS press-release feed which (a) was fronted by a CDN that
  blocked automated access, and (b) surfaced marketing press releases
  rather than legislative actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash
from .jackson_county import parse_legistar_matters

# Socrata dataset: 311 service requests (KCMO). Public open data.
KCMO_311_DATASET = "https://data.kcmo.org/resource/7at3-sxhp.json"
# Pull the most recent submissions.
KCMO_311_QUERY = "?$order=creation_date DESC&$limit=50"

# Kansas City City Clerk legislative record — Legistar tenant.
KCMO_LEGISTAR_CLIENT = "kansascity"
KCMO_LEGISTAR_MATTERS = f"https://webapi.legistar.com/v1/{KCMO_LEGISTAR_CLIENT}/Matters"
KCMO_LEGISTAR_PORTAL = "https://kansascity.legistar.com"
KCMO_LEGISTAR_PAGE_SIZE = 50


class KCMOCollector(BaseCollector):
    name = "kcmo_open_data"
    label = "Kansas City, MO — 311 service requests (data.kcmo.org)"
    source_type = "government_open_data"

    def fetch(self, client: httpx.Client) -> Any:
        try:
            resp = client.get(KCMO_311_DATASET + KCMO_311_QUERY)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.json()

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        rows = raw or []
        items: list[SourceItem] = []
        for row in rows:
            case_id = row.get("case_id") or row.get(":id")
            if not case_id:
                continue
            request_type = row.get("request_type") or "311 service request"
            neighborhood = row.get("neighborhood")
            council_district = row.get("council_district")
            street = row.get("street_address")  # kept only in metadata, not dashboard-visible
            description = row.get("description") or ""
            status = row.get("status")

            created = _parse_iso(row.get("creation_date"))
            # Privacy: prefer aggregate geography (neighborhood or council district)
            # over precise street address in dashboard-visible fields. Street
            # remains available in metadata for downstream investigation.
            aggregate_geo = neighborhood or (f"Council district {council_district}" if council_district else "Kansas City, MO")
            title = f"311: {request_type} — {aggregate_geo}"
            excerpt_bits = [description]
            if council_district:
                excerpt_bits.append(f"Council district: {council_district}")
            if status:
                excerpt_bits.append(f"Status: {status}")
            excerpt = " · ".join(b for b in excerpt_bits if b)[:800]

            beat = _classify_311(request_type)

            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=str(case_id),
                    canonical_url=f"https://data.kcmo.org/resource/7at3-sxhp.json?case_id={case_id}",
                    title=title[:400],
                    excerpt=excerpt or None,
                    published_at=created,
                    event_at=created,
                    retrieved_at=retrieved_at,
                    geography=aggregate_geo,
                    beat=beat,
                    content_hash=content_hash(str(case_id), title, description[:400], str(status)),
                    metadata={
                        "council_district": council_district,
                        "neighborhood": neighborhood,
                        "status": status,
                        "request_type": request_type,
                        # street_address is retained in metadata only. Do not
                        # display in dashboard cards; kept for downstream
                        # investigation by human reporters if needed.
                        "street_address_private": street,
                    },
                )
            )
        return items


_311_HOUSING = ("housing", "property", "code enforce", "vacant", "dangerous building", "rental")
_311_TRANSPORT = ("pothole", "street", "sidewalk", "traffic", "sign", "signal", "streetlight")
_311_SAFETY = ("weapon", "shooting", "gunshot", "abandon vehicle")
_311_HEALTH = ("mosquito", "rodent", "trash", "refuse", "sewage", "dead animal")


def _classify_311(request_type: str | None) -> Beat:
    low = (request_type or "").lower()
    if any(k in low for k in _311_HOUSING):
        return Beat.HOUSING_DEVELOPMENT
    if any(k in low for k in _311_TRANSPORT):
        return Beat.TRANSPORTATION
    if any(k in low for k in _311_SAFETY):
        return Beat.PUBLIC_SAFETY
    if any(k in low for k in _311_HEALTH):
        return Beat.HEALTH
    return Beat.LOCAL_GOVERNMENT


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


class KCMOClerkLegistarCollector(BaseCollector):
    """Kansas City Council / City Clerk legislative record via Legistar InSite API."""

    name = "kcmo_council_legistar"
    label = "Kansas City, MO — City Council (Clerk / Legistar)"
    source_type = "government_meeting"

    def fetch(self, client: httpx.Client) -> Any:
        params = {
            "$top": str(KCMO_LEGISTAR_PAGE_SIZE),
            "$orderby": "MatterLastModifiedUtc desc",
        }
        try:
            resp = client.get(KCMO_LEGISTAR_MATTERS, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.json()

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return parse_legistar_matters(
            raw,
            source_name=self.name,
            legistar_client=KCMO_LEGISTAR_CLIENT,
            portal_host=KCMO_LEGISTAR_PORTAL,
            geography="Kansas City, MO",
            geography_prefix="Kansas City",
            retrieved_at=retrieved_at,
        )
