"""Explainable, deterministic scoring for signals and forecasts.

Two independent 0–100 scores are computed for every story candidate:

* ``experimental_likelihood_score``  — how likely, given the observed public
  signals, that the underlying development advances within the forecast
  horizon. **This is not a calibrated probability.** See docs/EDITORIAL_SAFETY.md.
* ``editorial_relevance_score``      — how newsworthy this development is to a
  public-service newsroom, independent of whether it advances. Uses only
  public-service factors (audience reach, fiscal magnitude, government
  accountability, safety, transportation, education). Does **not** use clicks,
  outrage potential, partisan advantage, or demographic targeting.

Both scores are the sum of documented positive components. A ``ScoreBreakdown``
records every component so the UI can show a bulleted "why this is elevated"
explanation.

Scoring model version:

    heuristic-v0.1

Change the constant in kc_news_radar/__init__.py when weights or components change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..models import Beat, SignalType

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ScoreBreakdown:
    total: int
    components: list[tuple[int, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "components": [{"weight": w, "reason": r} for w, r in self.components],
        }


# ---------------------------------------------------------------------------
# Experimental likelihood — how likely the story develops in the horizon
# ---------------------------------------------------------------------------


def experimental_likelihood_score(
    *,
    signal_type: SignalType,
    beat: Beat,
    hours_to_event: int | None,
    evidence_count: int,
    distinct_sources: int,
    agenda_recently_updated: bool,
    high_magnitude: bool,
    severity: str | None = None,
) -> ScoreBreakdown:
    """Return an explainable 0–100 experimental score.

    Deterministic: same inputs → same output. No randomness, no LLM.
    """
    components: list[tuple[int, str]] = []
    total = 0

    if signal_type == SignalType.SCHEDULED_CATALYST:
        components.append((20, "scheduled public event within forecast horizon"))
        total += 20
    if signal_type == SignalType.UNUSUAL_AGENDA_ITEM:
        components.append((15, "official agenda item detected"))
        total += 15
    if signal_type == SignalType.SEVERE_WEATHER_CHANGE:
        components.append((15, "active NWS alert"))
        total += 15
    if signal_type == SignalType.HIGH_IMPACT_PUBLIC_ACTION:
        components.append((15, "high-impact public-safety indicator"))
        total += 15
    if signal_type == SignalType.MULTI_SOURCE_CONVERGENCE:
        components.append((12, "same story observed from multiple public sources"))
        total += 12
    if signal_type == SignalType.DEVELOPMENT_DEAL_ACTIVITY:
        components.append((15, "development-deal indicator (stadium / TIF / Port KC / bond)"))
        total += 15

    if agenda_recently_updated:
        components.append((12, "agenda revised recently"))
        total += 12

    if high_magnitude:
        components.append((10, "unusually high fiscal or policy magnitude"))
        total += 10

    if distinct_sources >= 2:
        components.append((11, f"{distinct_sources} distinct public sources reference this issue"))
        total += 11

    if evidence_count >= 3:
        components.append((6, f"{evidence_count} supporting public evidence items"))
        total += 6

    if hours_to_event is not None:
        if hours_to_event <= 24:
            components.append((8, "near-term timing (within 24h)"))
            total += 8
        elif hours_to_event <= 72:
            components.append((5, "near-term timing (within 72h)"))
            total += 5

    if severity:
        low_sev = severity.lower()
        if low_sev in {"extreme", "severe"}:
            components.append((8, f"NWS severity: {severity}"))
            total += 8
        elif low_sev == "moderate":
            components.append((4, f"NWS severity: {severity}"))
            total += 4

    total = max(0, min(100, total))
    return ScoreBreakdown(total=total, components=components)


# ---------------------------------------------------------------------------
# Editorial relevance — public-service importance
# ---------------------------------------------------------------------------

# Rough population-reach heuristic per beat/geography, purely for scoring
# defensibility. Not a substitute for editorial judgment; documented.
_BEAT_BASE_RELEVANCE = {
    Beat.LOCAL_GOVERNMENT: 55,
    Beat.STATE_GOVERNMENT: 40,
    Beat.POLITICS_ELECTIONS: 50,
    Beat.EDUCATION: 60,
    Beat.HEALTH: 65,
    Beat.TRANSPORTATION: 55,
    Beat.WEATHER_ENVIRONMENT: 55,
    Beat.ECONOMY_BUSINESS: 50,
    Beat.HOUSING_DEVELOPMENT: 60,
    Beat.PUBLIC_SAFETY: 70,
    Beat.ARTS_CULTURE: 30,
    Beat.REGIONAL: 45,
    Beat.OTHER: 25,
}


def editorial_relevance_score(
    *,
    beat: Beat,
    high_magnitude: bool,
    affects_multiple_jurisdictions: bool,
    public_safety_indicator: bool,
    government_accountability: bool,
    infrastructure_impact: bool,
) -> ScoreBreakdown:
    components: list[tuple[int, str]] = []
    base = _BEAT_BASE_RELEVANCE.get(beat, 25)
    components.append((base, f"public-service base weight for beat {beat.value}"))
    total = base

    if high_magnitude:
        components.append((12, "significant public expenditure or policy magnitude"))
        total += 12
    if affects_multiple_jurisdictions:
        components.append((6, "affects multiple metro jurisdictions"))
        total += 6
    if public_safety_indicator:
        components.append((8, "public-safety relevance"))
        total += 8
    if government_accountability:
        components.append((6, "government accountability significance"))
        total += 6
    if infrastructure_impact:
        components.append((5, "infrastructure or transportation impact"))
        total += 5

    total = max(0, min(100, total))
    return ScoreBreakdown(total=total, components=components)


# ---------------------------------------------------------------------------
# Priority — used for ranking on the dashboard
# ---------------------------------------------------------------------------


def priority_score(likelihood: int, relevance: int) -> int:
    """A transparent 0–100 dashboard rank combining likelihood and relevance.

    Weighted mean: 45% likelihood, 55% relevance.  Relevance carries slightly
    more weight because a newsroom would rather triage highly-relevant slightly
    less-likely developments than the reverse.
    """
    score = round(0.45 * likelihood + 0.55 * relevance)
    return max(0, min(100, int(score)))


__all__ = [
    "ScoreBreakdown",
    "experimental_likelihood_score",
    "editorial_relevance_score",
    "priority_score",
]
