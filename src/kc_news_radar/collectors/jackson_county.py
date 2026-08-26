"""Jackson County, Missouri — County Legislature via Legistar InSite web API.

Jackson County publishes its legislative record (resolutions, ordinances,
communications) through its Legistar tenant. The public InSite web API
(``webapi.legistar.com/v1/jacksonco/``) exposes matters with structured
fields — file number, type, status, controlling body, intro date, agenda
date, passed/enactment date, and title. That is a cleaner and more
newsroom-useful signal than the county's news RSS (which is fronted by a
CDN that blocks automated access).

The adapter is bounded to the ``PAGE_SIZE`` most-recently-modified matters
so a single run cannot walk the entire history.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

LEGISTAR_CLIENT = "jacksonco"
MATTERS_URL = f"https://webapi.legistar.com/v1/{LEGISTAR_CLIENT}/Matters"
PORTAL_HOST = "https://jacksongov.legistar.com"
PAGE_SIZE = 50

_FISCAL_KEYWORDS = ("tax", "assess", "millage", "budget", "appropriat", "levy", "bond")
_HEALTH_KEYWORDS = ("health department", "public health", "outbreak", "vaccin", "cotb", "wic")
_HOUSING_KEYWORDS = ("housing", "zoning", "urban renewal", "development plan", "tif")
_TRANSPORT_KEYWORDS = ("road", "bridge", "transit", "transport", "highway", "airport")


class JacksonCountyCollector(BaseCollector):
    name = "jackson_county"
    label = "Jackson County, MO — County Legislature (Legistar)"
    source_type = "government_meeting"

    def fetch(self, client: httpx.Client) -> Any:
        params = {
            "$top": str(PAGE_SIZE),
            "$orderby": "MatterLastModifiedUtc desc",
        }
        try:
            resp = client.get(MATTERS_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.json()

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        return parse_legistar_matters(
            raw,
            source_name=self.name,
            legistar_client=LEGISTAR_CLIENT,
            portal_host=PORTAL_HOST,
            geography="Jackson County, MO",
            geography_prefix="Jackson County",
            retrieved_at=retrieved_at,
        )


def parse_legistar_matters(
    raw: Any,
    *,
    source_name: str,
    legistar_client: str,
    portal_host: str,
    geography: str,
    geography_prefix: str,
    retrieved_at: datetime,
) -> list[SourceItem]:
    if not isinstance(raw, list):
        return []
    items: list[SourceItem] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        matter_id = row.get("MatterId")
        matter_guid = row.get("MatterGuid")
        matter_file = row.get("MatterFile")
        matter_title_raw = row.get("MatterTitle") or row.get("MatterName")
        if matter_id is None or not matter_title_raw:
            continue

        matter_title = _normalize_title(matter_title_raw)
        matter_type = _clean(row.get("MatterTypeName"))
        matter_status = _clean(row.get("MatterStatusName"))
        matter_body = _clean(row.get("MatterBodyName"))

        intro_date = _parse_legistar_dt(row.get("MatterIntroDate"))
        agenda_date = _parse_legistar_dt(row.get("MatterAgendaDate"))
        passed_date = _parse_legistar_dt(row.get("MatterPassedDate"))
        enactment_date = _parse_legistar_dt(row.get("MatterEnactmentDate"))
        last_modified = _parse_legistar_dt(row.get("MatterLastModifiedUtc"))

        published_at = intro_date or last_modified
        event_at = agenda_date or passed_date or enactment_date or intro_date

        prefix_parts = [matter_type, matter_file]
        prefix = " ".join(p for p in prefix_parts if p)
        title = f"{prefix}: {matter_title}" if prefix else matter_title

        excerpt_bits: list[str] = []
        if matter_body:
            excerpt_bits.append(f"Body: {matter_body}")
        if matter_status:
            excerpt_bits.append(f"Status: {matter_status}")
        if agenda_date:
            excerpt_bits.append(f"Agenda: {agenda_date.date().isoformat()}")
        if enactment_date:
            excerpt_bits.append(f"Enacted: {enactment_date.date().isoformat()}")
        excerpt_bits.append(matter_title)
        excerpt = " · ".join(b for b in excerpt_bits if b)[:800]

        canonical_url = (
            f"{portal_host}/LegislationDetail.aspx?ID={matter_id}"
            f"&GUID={matter_guid}" if matter_guid else f"{portal_host}/LegislationDetail.aspx?ID={matter_id}"
        )

        beat = _classify_beat(matter_title, matter_body or "")

        items.append(
            SourceItem(
                source_name=source_name,
                external_id=f"legistar:{legistar_client}:{matter_id}",
                canonical_url=canonical_url,
                title=title[:400],
                excerpt=excerpt or None,
                published_at=published_at,
                event_at=event_at,
                retrieved_at=retrieved_at,
                geography=(f"{geography_prefix} — {matter_body}" if matter_body else geography),
                beat=beat,
                content_hash=content_hash(
                    str(matter_id),
                    matter_file or "",
                    matter_title[:400],
                    matter_status or "",
                    agenda_date.isoformat() if agenda_date else "",
                ),
                metadata={
                    "matter_id": matter_id,
                    "matter_guid": matter_guid,
                    "matter_file": matter_file,
                    "matter_type": matter_type,
                    "matter_status": matter_status,
                    "matter_body": matter_body,
                    "intro_date": intro_date.isoformat() if intro_date else None,
                    "agenda_date": agenda_date.isoformat() if agenda_date else None,
                    "passed_date": passed_date.isoformat() if passed_date else None,
                    "enactment_date": enactment_date.isoformat() if enactment_date else None,
                    "raw_category": "legistar_matter",
                },
            )
        )
    return items


def _classify_beat(title: str, body: str) -> Beat:
    # Prefer subject-matter classifications (health/housing/transport) over
    # a fiscal keyword hit — most Legistar matters have a fiscal dimension,
    # but the subject is what makes it newsworthy.
    low = f"{title} {body}".lower()
    if any(k in low for k in _HEALTH_KEYWORDS):
        return Beat.HEALTH
    if any(k in low for k in _HOUSING_KEYWORDS):
        return Beat.HOUSING_DEVELOPMENT
    if any(k in low for k in _TRANSPORT_KEYWORDS):
        return Beat.TRANSPORTATION
    if any(k in low for k in _FISCAL_KEYWORDS):
        return Beat.ECONOMY_BUSINESS
    return Beat.LOCAL_GOVERNMENT


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    v = value.strip()
    return v or None


def _normalize_title(value: str) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    # Legistar titles often start with "Sponsor: ..." — keep the field but tidy whitespace.
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


def _parse_legistar_dt(s: Any) -> datetime | None:
    """Parse Legistar timestamps like ``2026-08-26T18:41:47.057`` (assumed UTC)."""
    if not isinstance(s, str) or not s.strip():
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
