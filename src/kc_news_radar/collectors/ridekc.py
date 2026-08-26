"""RideKC / KCATA rider service alerts.

The RideKC public service-alerts page
(``https://www.ridekc.org/getting-around/service-alerts/`` which
redirects to the bare host) renders every active rider alert server-side
inside ``<div class="s4_notif_item">`` blocks. Each block contains an
``<a class="s4_notif_link">`` whose ``href`` is the underlying
``/kcata/news/…`` bulletin permalink, whose accessibility label carries
the alert title, whose ``<strong>`` children carry the cause and effect
labels, and whose surrounding paragraph carries the description.

That is the canonical current interface — there is no public RSS feed,
and we do not invent one.

This adapter parses the alerts page and normalizes each alert into a
``SourceItem`` suitable for downstream signal detection.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

ALERTS_URL = "https://www.ridekc.org/getting-around/service-alerts/"
_HOST = "https://ridekc.org"

_TITLE_PREFIX_RE = re.compile(
    r"^\s*Click to learn more about this service alert\s*:?\s*", re.IGNORECASE
)
_BULLETIN_RE = re.compile(r"/kcata/news/", re.IGNORECASE)
_ROUTE_HREF_RE = re.compile(r"/routes/(\d{1,3})(?:[/-]|$)", re.IGNORECASE)
_ROUTE_TEXT_RE = re.compile(r"\b(?:route|line)\s+(\d{1,3})\b", re.IGNORECASE)
# Some detail pages include "Expires MM/DD/YYYY" on the banner too.
_EXPIRES_RE = re.compile(
    r"Expires\s+(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{1,2}-\d{1,2})", re.IGNORECASE
)


class RideKCCollector(BaseCollector):
    name = "ridekc"
    label = "RideKC service alerts"
    source_type = "transit"

    def fetch(self, client: httpx.Client) -> Any:
        try:
            resp = client.get(ALERTS_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.text

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        if not isinstance(raw, str) or not raw.strip():
            return []
        soup = BeautifulSoup(raw, "html.parser")

        items: list[SourceItem] = []
        seen_permalinks: set[str] = set()

        for block in soup.find_all("div", class_="s4_notif_item"):
            if not isinstance(block, Tag):
                continue
            anchor = block.find("a", href=True)
            if not isinstance(anchor, Tag):
                continue
            href = anchor["href"]
            if not _BULLETIN_RE.search(href):
                continue
            permalink = urljoin(_HOST, href)
            if permalink in seen_permalinks:
                continue
            seen_permalinks.add(permalink)

            title = _extract_title(anchor)
            if not title:
                continue

            strongs = [t.get_text(" ", strip=True) for t in anchor.find_all("strong")]
            strongs = [s for s in strongs if s]
            cause = strongs[0] if strongs else None
            effect = strongs[1] if len(strongs) > 1 else None

            description = _extract_description(block, anchor)
            block_text = block.get_text(" ", strip=True)
            expires_text = _find(_EXPIRES_RE, block_text)
            expires_at = _parse_date(expires_text)
            routes = _extract_routes(block, description or "")

            excerpt_bits: list[str] = []
            if cause:
                excerpt_bits.append(f"Cause: {cause}")
            if effect:
                excerpt_bits.append(f"Effect: {effect}")
            if expires_text:
                excerpt_bits.append(f"Expires: {expires_text}")
            if routes:
                excerpt_bits.append(f"Routes: {', '.join(routes)}")
            if description:
                excerpt_bits.append(description)
            excerpt = " · ".join(b for b in excerpt_bits if b)[:800] or None

            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=permalink,
                    canonical_url=permalink,
                    title=title[:400],
                    excerpt=excerpt,
                    published_at=None,
                    event_at=expires_at,
                    retrieved_at=retrieved_at,
                    geography="Kansas City metro (transit)",
                    beat=Beat.TRANSPORTATION,
                    content_hash=content_hash(
                        permalink,
                        title,
                        cause or "",
                        effect or "",
                        (description or "")[:400],
                        expires_text or "",
                    ),
                    metadata={
                        "cause": cause,
                        "effect": effect,
                        "expires_text": expires_text,
                        "routes": routes,
                    },
                )
            )
        return items


def _extract_title(anchor: Tag) -> str | None:
    # Accessibility label: <span class="no_viz">Click to learn more about this service alert: {title}</span>
    span = anchor.find("span", class_="no_viz")
    if isinstance(span, Tag):
        text = span.get_text(" ", strip=True)
        stripped = _TITLE_PREFIX_RE.sub("", text).strip(" :·—-")
        if stripped:
            return stripped
    # Fallback: any text content that is not a <strong> label.
    parts: list[str] = []
    for child in anchor.children:
        if isinstance(child, Tag) and child.name in {"strong", "b"}:
            continue
        if isinstance(child, Tag):
            parts.append(child.get_text(" ", strip=True))
        elif isinstance(child, str):
            parts.append(child.strip())
    text = _one_line(" ".join(p for p in parts if p))
    text = _TITLE_PREFIX_RE.sub("", text).strip(" :·—-")
    return text or None


def _extract_description(block: Tag, anchor: Tag) -> str | None:
    """The description is the block text minus the anchor and any <strong> labels."""
    parent = anchor.parent
    if not isinstance(parent, Tag):
        parent = block
    # Build a string of every non-anchor sibling inside the paragraph, then
    # append any remaining sibling paragraphs inside the block.
    parts: list[str] = []
    for sib in parent.contents:
        if sib is anchor:
            continue
        if isinstance(sib, Tag):
            parts.append(sib.get_text(" ", strip=True))
        elif isinstance(sib, str):
            parts.append(sib.strip())
    text = _one_line(" ".join(p for p in parts if p))
    return text[:600] or None


def _extract_routes(block: Tag, description: str) -> list[str]:
    routes: set[str] = set()
    for a in block.find_all("a", href=True):
        m = _ROUTE_HREF_RE.search(a["href"])
        if m:
            routes.add(m.group(1))
    for m in _ROUTE_TEXT_RE.finditer(description or ""):
        routes.add(m.group(1))
    return sorted(routes, key=lambda x: int(x))


def _find(pattern: re.Pattern[str], text: str) -> str | None:
    if not text:
        return None
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1).strip() or None


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc)
    return None
