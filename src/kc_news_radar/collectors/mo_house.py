"""Missouri House of Representatives — bill action list.

Public html endpoint that lists current bill activity. State legislative
actions matter for the KC metro (Jackson/Clay/Platte counties). This is a
supplementary state-government adapter added because Jackson County's
CivicPlus RSS is CDN-blocked.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..models import Beat, SourceItem
from .base import BaseCollector, FetchError, content_hash

BILL_LIST_URL = "https://house.mo.gov/BillList.aspx"

_BILL_RE = re.compile(r"^[HS][BRJC]?\s*\d+", re.IGNORECASE)


class MissouriHouseCollector(BaseCollector):
    name = "mo_house"
    label = "Missouri House — bill list"
    source_type = "state_government"

    def fetch(self, client: httpx.Client) -> Any:
        try:
            resp = client.get(BILL_LIST_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc
        return resp.text

    #: Maximum bills to normalize per run. Prevents flooding the DB from a
    #: full-session listing that returns hundreds of rows.
    MAX_ITEMS = 60

    def parse(self, raw: Any, *, retrieved_at: datetime) -> list[SourceItem]:
        if not raw:
            return []
        soup = BeautifulSoup(raw, "lxml")
        items: list[SourceItem] = []

        for row in soup.select("tr"):
            if len(items) >= self.MAX_ITEMS:
                break
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            bill_no = cells[0]
            if not _BILL_RE.match(bill_no):
                continue
            # Assume: [bill_no, sponsor, description, ...]
            desc = " · ".join(c for c in cells[1:5] if c)[:800]
            link = None
            a = row.find("a", href=True)
            if a and a["href"]:
                href = a["href"]
                if href.startswith("http"):
                    link = href
                else:
                    link = "https://house.mo.gov/" + href.lstrip("/")

            items.append(
                SourceItem(
                    source_name=self.name,
                    external_id=bill_no.replace(" ", ""),
                    canonical_url=link,
                    title=f"MO {bill_no}",
                    excerpt=desc or None,
                    published_at=None,
                    event_at=None,
                    retrieved_at=retrieved_at,
                    geography="State of Missouri",
                    beat=Beat.STATE_GOVERNMENT,
                    content_hash=content_hash(bill_no, desc[:400]),
                    metadata={"raw_row": cells[:6]},
                )
            )
        return items
