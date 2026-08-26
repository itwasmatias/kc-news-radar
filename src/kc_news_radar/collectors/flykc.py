"""Kansas City International Airport (MCI / KCI) — aviation department publications.

FlyKC.com is a Gatsby SPA whose content is served by Prismic
(``flykc.cdn.prismic.io``). The Prismic ``newsroom`` document type contains
only a landing/hub document (no per-release documents); the aviation
department publishes its dated content — such as the 2026 FIFA World Cup
airport-operations page, new-terminal project awards, and seasonal
promotions — through the ``general_content_page`` type. We surface the most
recently published/updated general_content_page documents as the closest
publicly-available "aviation department publications" stream.

This is a narrow, honest adaptation: no RSS is invented, no bypass of
anti-bot controls is attempted, and the label makes it clear that these
are content-page publications from the aviation department rather than
formal press releases. See ``docs/DATA_SOURCES.md``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

PRISMIC_API = "https://flykc.cdn.prismic.io/api/v2"
PRISMIC_SEARCH = "https://flykc.cdn.prismic.io/api/v2/documents/search"
PRISMIC_TYPE = "general_content_page"
CANONICAL_HOST = "https://flykc.com"

# Bounded per-run page size.
PAGE_SIZE = 20


class FlyKCCollector(BaseCollector):
    name = "flykc"
    label = "KCI Airport (MCI) — aviation department publications"
    source_type = "aviation"

    def fetch(self, client: httpx.Client) -> Any:
        try:
            api = client.get(PRISMIC_API)
            api.raise_for_status()
            master_ref = _pick_master_ref(api.json())
            if not master_ref:
                raise FetchError("Prismic API root did not expose a master ref")

            params = {
                "ref": master_ref,
                "q": f'[[at(document.type,"{PRISMIC_TYPE}")]]',
                "pageSize": str(PAGE_SIZE),
                "orderings": "[document.first_publication_date desc]",
            }
            resp = client.get(PRISMIC_SEARCH, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.json()

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        if not isinstance(raw, dict):
            return []
        results = raw.get("results")
        if not isinstance(results, list):
            return []

        items: list[SourceItem] = []
        for doc in results:
            if not isinstance(doc, dict):
                continue
            uid = doc.get("uid")
            if not isinstance(uid, str) or not uid:
                continue
            data = doc.get("data") or {}
            if not isinstance(data, dict):
                data = {}

            title = _first_text(
                data.get("blue_hero_banner_title"),
                data.get("meta_title"),
            ) or f"MCI: {uid.replace('-', ' ')}"

            excerpt = _first_text(
                data.get("meta_description"),
            )

            first_pub = _parse_prismic_dt(doc.get("first_publication_date"))
            last_pub = _parse_prismic_dt(doc.get("last_publication_date"))

            canonical_url = f"{CANONICAL_HOST}/{uid}"
            external_id = f"prismic:{doc.get('id') or uid}"

            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=external_id,
                    canonical_url=canonical_url,
                    title=title[:400],
                    excerpt=(excerpt[:800] if excerpt else None),
                    published_at=first_pub,
                    event_at=last_pub or first_pub,
                    retrieved_at=retrieved_at,
                    geography="Kansas City International Airport",
                    beat=Beat.TRANSPORTATION,
                    content_hash=content_hash(
                        external_id,
                        title,
                        (excerpt or "")[:400],
                        (last_pub.isoformat() if last_pub else ""),
                    ),
                    metadata={
                        "uid": uid,
                        "prismic_type": PRISMIC_TYPE,
                        "first_publication_date": (
                            first_pub.isoformat() if first_pub else None
                        ),
                        "last_publication_date": (
                            last_pub.isoformat() if last_pub else None
                        ),
                    },
                )
            )
        return items


def _pick_master_ref(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    refs = payload.get("refs")
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if isinstance(ref, dict) and ref.get("isMasterRef") and isinstance(ref.get("ref"), str):
            return ref["ref"]
    for ref in refs:
        if isinstance(ref, dict) and ref.get("id") == "master" and isinstance(ref.get("ref"), str):
            return ref["ref"]
    return None


def _first_text(*candidates: Any) -> str | None:
    """Return the first non-empty plain-text value found among candidates.

    Prismic string fields are usually plain strings; rich-text fields are
    lists of ``{"type": ..., "text": ...}`` blocks. This helper accepts both.
    """
    for c in candidates:
        text = _coerce_text(c)
        if text:
            return text
    return None


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        joined = " ".join(parts).strip()
        return joined or None
    if isinstance(value, dict):
        t = value.get("text")
        if isinstance(t, str):
            s = t.strip()
            return s or None
    return None


def _parse_prismic_dt(s: Any) -> datetime | None:
    """Parse a Prismic timestamp like ``2026-05-27T20:53:37+0000``."""
    if not isinstance(s, str) or not s:
        return None
    # Prismic emits +0000 (no colon). Normalize for fromisoformat.
    text = s.strip()
    if len(text) >= 5 and (text.endswith("+0000") or text.endswith("-0000")):
        text = text[:-5] + "+00:00"
    elif len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
