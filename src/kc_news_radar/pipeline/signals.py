"""Deterministic signal detection over stored source items.

A "signal" here is a short-lived, human-inspectable indicator that something
in a public source has changed in a way potentially worth a journalist's
attention. Signal generation runs after every collection cycle. All signals
are recomputed from source_items each run (see db.clear_signals) so that
missing or resolved items simply disappear.

Signal types implemented:

* ``NEW_ITEM``                  — a source item first observed within the
                                  configured recency window.
* ``ITEM_UPDATED``              — a source item whose content_hash changed
                                  between runs.
* ``SCHEDULED_CATALYST``        — a future-dated public event (agenda item,
                                  meeting, deadline) approaching within 72h.
* ``SEVERE_WEATHER_CHANGE``     — an NWS alert with elevated severity.
* ``UNUSUAL_AGENDA_ITEM``       — heuristic keyword hit for large fiscal or
                                  policy items in local-government sources.
* ``MULTI_SOURCE_CONVERGENCE``  — near-duplicate titles across two or more
                                  distinct sources.
* ``REPEATED_ENTITY_ACTIVITY``  — same recognizable entity (council district,
                                  neighborhood, agency) appears in multiple
                                  new items within the window.
* ``HIGH_IMPACT_PUBLIC_ACTION`` — public-safety or significant infrastructure.
* ``COMMUNITY_311_TREND``       — aggregate resident-reported (311) pattern:
                                  volume spike within a category, or geographic
                                  concentration within a neighborhood / council
                                  district. Aggregate geography only; never
                                  household-level. Explicitly labeled as a
                                  resident-reported signal, not a verified fact.
* ``DEVELOPMENT_DEAL_ACTIVITY`` — development-deal indicators (Royals/Chiefs
                                  stadium activity, TIF, Port KC, bond
                                  authorization, land acquisition, development
                                  agreement). Distinct from generic unusual
                                  agenda items so newsrooms can watch the
                                  category directly.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..models import Beat, Signal, SignalType
from .dedupe import group_by_title_similarity
from .normalize import clean_text, normalized_title

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RECENCY_HOURS = 72
CATALYST_HORIZON_HOURS = 72

# Keywords that suggest an agenda item is unusually consequential.
# Includes major-development-deal patterns (Royals/Chiefs/Port KC/TIF/bond
# authorizations) per user-research amendment.
_HIGH_MAGNITUDE_TERMS = (
    r"\$\s?\d{1,3}(?:,\d{3})+",       # $12,000,000 style
    r"\d+\s*million",
    r"budget amendment",
    r"tax increment",
    r"\btif\b",
    r"rezone",
    r"eminent domain",
    r"emergency ordinance",
    r"appropriat",
    r"bond\s+(?:issue|authorization)",
    # development-deal indicators
    r"royals",
    r"chiefs",
    r"stadium",
    r"port\s*kc",
    r"development\s+agreement",
    r"land\s+acquisition",
    r"public\s+financing",
    r"revenue\s+bond",
)
_HIGH_MAGNITUDE_RE = re.compile("|".join(_HIGH_MAGNITUDE_TERMS), re.IGNORECASE)

_HIGH_SAFETY_TERMS = re.compile(
    r"shoot|homicide|explos|hazmat|evacuat|active\s+shooter|building\s+collapse|water\s+main\s+break",
    re.IGNORECASE,
)

# Curated development-deal patterns. These are the newsroom's early-warning
# triggers for large public-private deals. Matching is intentionally narrow so
# the signal stays specific — generic "development" or "downtown" wouldn't.
_DEV_DEAL_TERMS = (
    r"royals",
    r"chiefs",
    r"stadium",
    r"arrowhead",
    r"kauffman\s+stadium",
    r"port\s*kc",
    r"\btif\b",
    r"tax\s+increment\s+financ",
    r"bond\s+(?:issue|authorization|referendum)",
    r"development\s+agreement",
    r"land\s+acquisition",
    r"public\s+financing",
    r"revenue\s+bond",
    r"eminent\s+domain",
)
_DEV_DEAL_RE = re.compile("|".join(_DEV_DEAL_TERMS), re.IGNORECASE)

# 311 volume-spike threshold: at least this many resident reports in the
# recency window for the same request category (across the metro) or the
# same aggregate geography (neighborhood / council district) constitutes an
# aggregate signal worth surfacing to a newsroom manager.
_311_CATEGORY_MIN = 5
_311_GEOGRAPHY_MIN = 4


@dataclass
class DetectedSignal:
    """A signal plus the source_item row IDs that support it."""

    signal: Signal
    evidence_ids: list[int]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def detect_signals(
    items: list[dict], *, now: datetime | None = None
) -> list[DetectedSignal]:
    """Return signals derived from the given normalized source_items.

    ``items`` is a list of dicts as produced by ``db.list_source_items`` —
    each has: id, source_name, title, excerpt, beat, event_at, published_at,
    retrieved_at, first_seen_at, last_seen_at, geography, metadata, ...

    The function is deterministic given identical inputs; ``now`` may be
    injected for tests.
    """
    now = _tz(now) if now is not None else datetime.now(timezone.utc)
    recency_cutoff = now - timedelta(hours=RECENCY_HOURS)

    parsed = [_prepare(i) for i in items]
    signals: list[DetectedSignal] = []

    signals.extend(_new_item_signals(parsed, recency_cutoff))
    signals.extend(_updated_item_signals(parsed, recency_cutoff))
    signals.extend(_scheduled_catalyst_signals(parsed, now))
    signals.extend(_severe_weather_signals(parsed, now))
    signals.extend(_unusual_agenda_signals(parsed, recency_cutoff))
    signals.extend(_high_impact_signals(parsed, recency_cutoff))
    signals.extend(_multi_source_convergence_signals(parsed, recency_cutoff))
    signals.extend(_repeated_entity_signals(parsed, recency_cutoff))
    signals.extend(_community_311_signals(parsed, recency_cutoff))
    signals.extend(_development_deal_signals(parsed, recency_cutoff))

    # Deterministic ordering: by (signal_type, novelty desc, title)
    signals.sort(
        key=lambda s: (
            s.signal.signal_type.value,
            -s.signal.novelty_score,
            s.signal.title.lower(),
        )
    )
    return signals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tz(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _prepare(item: dict) -> dict:
    out = dict(item)
    out["event_at"] = _parse(item.get("event_at"))
    out["published_at"] = _parse(item.get("published_at"))
    out["retrieved_at"] = _parse(item.get("retrieved_at"))
    out["first_seen_at"] = _parse(item.get("first_seen_at"))
    out["last_seen_at"] = _parse(item.get("last_seen_at"))
    out["title"] = clean_text(item.get("title"))
    out["excerpt"] = clean_text(item.get("excerpt"))
    return out


def _parse(v):
    if v is None or isinstance(v, datetime):
        return _tz(v) if isinstance(v, datetime) else None
    try:
        return _tz(datetime.fromisoformat(v))
    except (TypeError, ValueError):
        return None


def _mk_signal(
    *,
    signal_type: SignalType,
    title: str,
    summary: str,
    geography: str | None,
    beat: Beat,
    novelty: int,
    impact: int,
    evidence: list[int],
    now: datetime,
) -> DetectedSignal:
    novelty = max(0, min(100, novelty))
    impact = max(0, min(100, impact))
    return DetectedSignal(
        signal=Signal(
            id=None,
            created_at=now,
            updated_at=now,
            signal_type=signal_type,
            title=title[:280],
            summary=summary[:800],
            geography=(geography or None),
            beat=beat,
            novelty_score=novelty,
            local_impact_score=impact,
            evidence_count=len(evidence),
            status="OPEN",
        ),
        evidence_ids=list(evidence),
    )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _new_item_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    out: list[DetectedSignal] = []
    for it in items:
        first_seen = it.get("first_seen_at")
        if not first_seen or first_seen < cutoff:
            continue
        if it.get("last_seen_at") and it["last_seen_at"] > first_seen + timedelta(hours=1):
            # Not brand-new anymore.
            continue
        out.append(
            _mk_signal(
                signal_type=SignalType.NEW_ITEM,
                title=f"New: {it['title'][:120]}",
                summary=(it.get("excerpt") or "New public-source item observed.")[:400],
                geography=it.get("geography"),
                beat=Beat(it.get("beat") or Beat.OTHER.value),
                novelty=70,
                impact=45,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _updated_item_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    out: list[DetectedSignal] = []
    for it in items:
        first_seen = it.get("first_seen_at")
        last_seen = it.get("last_seen_at")
        if not (first_seen and last_seen):
            continue
        if last_seen < cutoff:
            continue
        # A row is considered "updated" if last_seen is meaningfully after first_seen.
        if (last_seen - first_seen) < timedelta(hours=1):
            continue
        out.append(
            _mk_signal(
                signal_type=SignalType.ITEM_UPDATED,
                title=f"Updated: {it['title'][:120]}",
                summary=(it.get("excerpt") or "Source item content changed since last collection.")[:400],
                geography=it.get("geography"),
                beat=Beat(it.get("beat") or Beat.OTHER.value),
                novelty=60,
                impact=40,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _scheduled_catalyst_signals(items: list[dict], now: datetime) -> list[DetectedSignal]:
    horizon = now + timedelta(hours=CATALYST_HORIZON_HOURS)
    out: list[DetectedSignal] = []
    for it in items:
        event_at = it.get("event_at")
        if not event_at or event_at < now or event_at > horizon:
            continue
        beat = Beat(it.get("beat") or Beat.OTHER.value)
        if beat not in {Beat.LOCAL_GOVERNMENT, Beat.STATE_GOVERNMENT, Beat.EDUCATION, Beat.POLITICS_ELECTIONS, Beat.HOUSING_DEVELOPMENT}:
            continue
        hours_out = int((event_at - now).total_seconds() / 3600)
        out.append(
            _mk_signal(
                signal_type=SignalType.SCHEDULED_CATALYST,
                title=f"Scheduled: {it['title'][:120]}",
                summary=f"Scheduled public event in about {hours_out}h. Source excerpt: {it.get('excerpt') or ''}"[:400],
                geography=it.get("geography"),
                beat=beat,
                novelty=55,
                impact=60,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _severe_weather_signals(items: list[dict], now: datetime) -> list[DetectedSignal]:
    out: list[DetectedSignal] = []
    for it in items:
        if it.get("source_name") not in {"nws_kc", "nws_afd_eax"}:
            continue
        meta = it.get("metadata") or {}
        severity = (meta.get("severity") or "").lower()
        event = meta.get("event") or "weather event"
        if not severity and it.get("source_name") == "nws_kc":
            continue
        novelty = 55
        impact = 55
        if severity in {"severe", "extreme"}:
            novelty = 90
            impact = 90
        elif severity in {"moderate"}:
            novelty = 70
            impact = 70
        out.append(
            _mk_signal(
                signal_type=SignalType.SEVERE_WEATHER_CHANGE,
                title=f"NWS: {event} — {it.get('geography') or 'KC metro'}",
                summary=(it.get("excerpt") or "NWS alert.")[:400],
                geography=it.get("geography"),
                beat=Beat.WEATHER_ENVIRONMENT,
                novelty=novelty,
                impact=impact,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _unusual_agenda_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    out: list[DetectedSignal] = []
    for it in items:
        beat = Beat(it.get("beat") or Beat.OTHER.value)
        if beat not in {Beat.LOCAL_GOVERNMENT, Beat.STATE_GOVERNMENT, Beat.HOUSING_DEVELOPMENT}:
            continue
        haystack = f"{it.get('title')}\n{it.get('excerpt') or ''}"
        if not _HIGH_MAGNITUDE_RE.search(haystack):
            continue
        first_seen = it.get("first_seen_at")
        if first_seen and first_seen < cutoff - timedelta(days=7):
            continue
        out.append(
            _mk_signal(
                signal_type=SignalType.UNUSUAL_AGENDA_ITEM,
                title=f"Unusual agenda item: {it['title'][:100]}",
                summary=f"Fiscal or policy magnitude indicators detected. {it.get('excerpt') or ''}"[:400],
                geography=it.get("geography"),
                beat=beat,
                novelty=70,
                impact=80,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _high_impact_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    out: list[DetectedSignal] = []
    for it in items:
        first_seen = it.get("first_seen_at")
        if first_seen and first_seen < cutoff:
            continue
        haystack = f"{it.get('title')}\n{it.get('excerpt') or ''}"
        if not _HIGH_SAFETY_TERMS.search(haystack):
            continue
        beat = Beat(it.get("beat") or Beat.OTHER.value)
        if beat == Beat.OTHER:
            beat = Beat.PUBLIC_SAFETY
        out.append(
            _mk_signal(
                signal_type=SignalType.HIGH_IMPACT_PUBLIC_ACTION,
                title=f"High-impact: {it['title'][:120]}",
                summary=(it.get("excerpt") or "Public-safety indicator detected.")[:400],
                geography=it.get("geography"),
                beat=beat,
                novelty=80,
                impact=85,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _multi_source_convergence_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    """Same story reported by ≥2 distinct sources becomes one convergence signal."""
    recent = [
        it for it in items if (it.get("first_seen_at") or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    groups = group_by_title_similarity(recent)
    id_to_item = {it["id"]: it for it in recent}
    out: list[DetectedSignal] = []
    for g in groups:
        members = [id_to_item[m] for m in g.member_ids]
        sources = {m["source_name"] for m in members}
        if len(sources) < 2:
            continue
        rep = members[0]
        out.append(
            _mk_signal(
                signal_type=SignalType.MULTI_SOURCE_CONVERGENCE,
                title=f"Multi-source: {rep['title'][:120]}",
                summary=f"Same or similar story observed from sources: {', '.join(sorted(sources))}",
                geography=rep.get("geography"),
                beat=Beat(rep.get("beat") or Beat.OTHER.value),
                novelty=75,
                impact=70,
                evidence=[m["id"] for m in members],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _repeated_entity_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    """If the same council district / neighborhood / agency appears >=3 times, surface it."""
    recent = [
        it for it in items if (it.get("first_seen_at") or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    entity_to_items: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for it in recent:
        meta = it.get("metadata") or {}
        # Try several entity slots.
        for key in ("neighborhood", "council_district", "issuingOffice", "raw_category"):
            v = meta.get(key)
            if v:
                entity_to_items[(key, str(v))].append(it)
                break
        # Geography as fallback entity
        geo = it.get("geography")
        if geo:
            entity_to_items[("geography", geo)].append(it)

    out: list[DetectedSignal] = []
    for (kind, value), members in entity_to_items.items():
        if len(members) < 3:
            continue
        # Skip geographies that are the same as the entire metro (too broad).
        if kind == "geography" and value.lower() in {"kansas city metro", "kansas city, mo", "state of missouri"}:
            continue
        rep = members[0]
        beats = Counter(m.get("beat") for m in members)
        top_beat = beats.most_common(1)[0][0] or Beat.OTHER.value
        out.append(
            _mk_signal(
                signal_type=SignalType.REPEATED_ENTITY_ACTIVITY,
                title=f"Repeated activity: {value[:80]}",
                summary=f"{len(members)} related public-source items involve {kind}={value}.",
                geography=value if kind == "geography" else rep.get("geography"),
                beat=Beat(top_beat),
                novelty=65,
                impact=60,
                evidence=[m["id"] for m in members],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _community_311_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    """Aggregate 311 patterns from ``kcmo_open_data``.

    Two aggregate detectors, both privacy-preserving:

    * ``category_spike`` — many recent reports of the same request type across
      the metro (e.g. 8 dangerous-building reports in 72h).
    * ``geography_concentration`` — many recent reports concentrated in the
      same neighborhood or council district, regardless of category.

    Only aggregate geography (neighborhood / council district) is used. Street
    addresses live in metadata (``street_address_private``) and never surface
    to the dashboard. Every signal summary explicitly labels this as a
    resident-reported pattern, not a verified fact.
    """
    recent = [
        it for it in items
        if it.get("source_name") == "kcmo_open_data"
        and (it.get("first_seen_at") or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    if not recent:
        return []

    by_category: dict[str, list[dict]] = defaultdict(list)
    by_geo: dict[str, list[dict]] = defaultdict(list)
    for it in recent:
        meta = it.get("metadata") or {}
        cat = (meta.get("request_type") or "").strip()
        if cat:
            by_category[cat].append(it)
        # Prefer neighborhood; fall back to council district. Never street.
        neighborhood = (meta.get("neighborhood") or "").strip()
        council = meta.get("council_district")
        geo_key = None
        if neighborhood:
            geo_key = f"Neighborhood: {neighborhood}"
        elif council:
            geo_key = f"Council district {council}"
        if geo_key:
            by_geo[geo_key].append(it)

    out: list[DetectedSignal] = []
    disclaimer = (
        "Resident-reported pattern from 311 open-data submissions. "
        "Not verified facts about any household or address."
    )

    for cat, members in by_category.items():
        if len(members) < _311_CATEGORY_MIN:
            continue
        beat = Beat(members[0].get("beat") or Beat.OTHER.value)
        geos = sorted({
            (m.get("metadata") or {}).get("neighborhood")
            or ((f"Council district {(m.get('metadata') or {}).get('council_district')}")
                if (m.get("metadata") or {}).get("council_district") else None)
            or "Kansas City, MO"
            for m in members
        })
        out.append(
            _mk_signal(
                signal_type=SignalType.COMMUNITY_311_TREND,
                title=f"311 category spike: {cat} ({len(members)} reports, 72h)",
                summary=(
                    f"{len(members)} resident 311 reports of category '{cat}' "
                    f"in the last {RECENCY_HOURS}h. Aggregate areas involved: "
                    f"{', '.join(geos[:6])}. {disclaimer}"
                )[:800],
                geography="Kansas City, MO",
                beat=beat,
                novelty=60,
                impact=55,
                evidence=[m["id"] for m in members],
                now=datetime.now(timezone.utc),
            )
        )

    for geo, members in by_geo.items():
        if len(members) < _311_GEOGRAPHY_MIN:
            continue
        beats = Counter(m.get("beat") for m in members)
        top_beat_val = beats.most_common(1)[0][0] or Beat.LOCAL_GOVERNMENT.value
        cats = Counter((m.get("metadata") or {}).get("request_type") for m in members)
        top_cats = [c for c, _ in cats.most_common(3) if c]
        out.append(
            _mk_signal(
                signal_type=SignalType.COMMUNITY_311_TREND,
                title=f"311 geographic concentration: {geo} ({len(members)} reports, 72h)",
                summary=(
                    f"{len(members)} resident 311 reports concentrated in {geo} "
                    f"in the last {RECENCY_HOURS}h. Most common categories: "
                    f"{', '.join(top_cats) or 'various'}. {disclaimer}"
                )[:800],
                geography=geo,
                beat=Beat(top_beat_val),
                novelty=60,
                impact=55,
                evidence=[m["id"] for m in members],
                now=datetime.now(timezone.utc),
            )
        )
    return out


def _development_deal_signals(items: list[dict], cutoff: datetime) -> list[DetectedSignal]:
    """Flag items whose title/excerpt matches curated development-deal patterns.

    Emitted as a dedicated signal type so newsroom managers can watch the
    dev-deal category directly (Royals, Chiefs, TIF, Port KC, bond
    authorization). These items may also trigger ``UNUSUAL_AGENDA_ITEM``; that
    overlap is deliberate — the two views serve different scanning tasks.
    """
    out: list[DetectedSignal] = []
    for it in items:
        first_seen = it.get("first_seen_at")
        if first_seen and first_seen < cutoff:
            continue
        haystack = f"{it.get('title')}\n{it.get('excerpt') or ''}"
        matches = sorted({m.group(0).lower() for m in _DEV_DEAL_RE.finditer(haystack)})
        if not matches:
            continue
        beat = Beat(it.get("beat") or Beat.OTHER.value)
        if beat == Beat.OTHER:
            beat = Beat.HOUSING_DEVELOPMENT
        out.append(
            _mk_signal(
                signal_type=SignalType.DEVELOPMENT_DEAL_ACTIVITY,
                title=f"Dev-deal indicator: {it['title'][:110]}",
                summary=(
                    f"Development-deal keywords matched: {', '.join(matches)}. "
                    f"{it.get('excerpt') or ''}"
                )[:400],
                geography=it.get("geography"),
                beat=beat,
                novelty=70,
                impact=80,
                evidence=[it["id"]],
                now=datetime.now(timezone.utc),
            )
        )
    return out


__all__ = [
    "DetectedSignal",
    "detect_signals",
    "RECENCY_HOURS",
    "CATALYST_HORIZON_HOURS",
]


def summarize_items_for_signal(items: Iterable[dict]) -> str:
    lines = []
    for it in items:
        title = normalized_title(it.get("title"))
        lines.append(f"- {title}")
    return "\n".join(lines)
