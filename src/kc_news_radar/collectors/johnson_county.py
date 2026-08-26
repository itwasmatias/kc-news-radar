"""Johnson County, Kansas — Board of County Commissioners / county news.

Uses the Johnson County public news RSS. Commission agenda-level ingestion is
a Tier-2 enhancement using the county's Legistar / CivicClerk integration; the
news feed reliably surfaces meeting notices and major policy announcements.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

FEED_URL = "https://www.jocogov.org/rss.xml"

_BOCC_KEYWORDS = ("commission", "bocc", "meeting", "agenda", "resolution", "ordinance")
_HEALTH_KEYWORDS = ("health department", "public health", "outbreak", "vaccin")
_EDUCATION_KEYWORDS = ("school", "district", "education", "curriculum")


class JohnsonCountyCollector(BaseCollector):
    name = "johnson_county"
    label = "Johnson County, KS — public notices"
    source_type = "government_news"

    def fetch(self, client: httpx.Client) -> Any:
        try:
            resp = client.get(FEED_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.text

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        if not raw:
            return []
        soup = BeautifulSoup(raw, "lxml-xml")
        items: list[SourceItem] = []
        for entry in soup.find_all("item"):
            title = _text(entry.find("title")) or "Johnson County announcement"
            link = _text(entry.find("link"))
            guid = _text(entry.find("guid")) or link or title
            desc_html = _text(entry.find("description")) or ""
            description = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)
            pub = _text(entry.find("pubDate"))
            published_at = _parse_rfc2822(pub)

            beat = _classify_beat(f"{title} {description}")

            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=guid,
                    canonical_url=link,
                    title=title[:400],
                    excerpt=description[:800] if description else None,
                    published_at=published_at,
                    event_at=published_at,
                    retrieved_at=retrieved_at,
                    geography="Johnson County, KS",
                    beat=beat,
                    content_hash=content_hash(guid, title, description[:500]),
                    metadata={},
                )
            )
        return items


def _classify_beat(text: str) -> Beat:
    low = text.lower()
    if any(k in low for k in _BOCC_KEYWORDS):
        return Beat.LOCAL_GOVERNMENT
    if any(k in low for k in _HEALTH_KEYWORDS):
        return Beat.HEALTH
    if any(k in low for k in _EDUCATION_KEYWORDS):
        return Beat.EDUCATION
    return Beat.LOCAL_GOVERNMENT


def _text(node: Any) -> str | None:
    if node is None:
        return None
    return (node.get_text() if hasattr(node, "get_text") else str(node)).strip() or None


def _parse_rfc2822(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
