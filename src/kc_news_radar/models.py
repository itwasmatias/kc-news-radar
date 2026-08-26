"""Pydantic models for normalized records shared across the pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Beat(str, Enum):
    LOCAL_GOVERNMENT = "LOCAL_GOVERNMENT"
    STATE_GOVERNMENT = "STATE_GOVERNMENT"
    POLITICS_ELECTIONS = "POLITICS_ELECTIONS"
    EDUCATION = "EDUCATION"
    HEALTH = "HEALTH"
    TRANSPORTATION = "TRANSPORTATION"
    WEATHER_ENVIRONMENT = "WEATHER_ENVIRONMENT"
    ECONOMY_BUSINESS = "ECONOMY_BUSINESS"
    HOUSING_DEVELOPMENT = "HOUSING_DEVELOPMENT"
    PUBLIC_SAFETY = "PUBLIC_SAFETY"
    ARTS_CULTURE = "ARTS_CULTURE"
    REGIONAL = "REGIONAL"
    OTHER = "OTHER"


class SourceStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class SignalType(str, Enum):
    NEW_ITEM = "NEW_ITEM"
    ITEM_UPDATED = "ITEM_UPDATED"
    UNUSUAL_AGENDA_ITEM = "UNUSUAL_AGENDA_ITEM"
    SCHEDULED_CATALYST = "SCHEDULED_CATALYST"
    MULTI_SOURCE_CONVERGENCE = "MULTI_SOURCE_CONVERGENCE"
    SEVERE_WEATHER_CHANGE = "SEVERE_WEATHER_CHANGE"
    SERVICE_DISRUPTION = "SERVICE_DISRUPTION"
    DEADLINE_APPROACHING = "DEADLINE_APPROACHING"
    REPEATED_ENTITY_ACTIVITY = "REPEATED_ENTITY_ACTIVITY"
    HIGH_IMPACT_PUBLIC_ACTION = "HIGH_IMPACT_PUBLIC_ACTION"
    # 311 community signal (aggregate resident service-request patterns).
    # Not a claim about verified facts — represents a resident-reported pattern only.
    COMMUNITY_311_TREND = "COMMUNITY_311_TREND"
    # Development-deal indicator (TIF, stadium, Port KC, bond authorization).
    DEVELOPMENT_DEAL_ACTIVITY = "DEVELOPMENT_DEAL_ACTIVITY"


class ForecastStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


class Outcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    NOT_OCCURRED = "NOT_OCCURRED"
    AMBIGUOUS = "AMBIGUOUS"
    EXPIRED_UNRESOLVED = "EXPIRED_UNRESOLVED"


class SourceItem(BaseModel):
    """Normalized item from a public information source.

    A normalized SourceItem is what every collector produces regardless of
    whether the source was HTML, JSON, RSS, or ICS. Fields are optional where
    the underlying source does not provide them; nothing is invented.
    """

    model_config = ConfigDict(extra="forbid")

    source_name: str
    external_id: str
    canonical_url: str | None = None
    title: str
    excerpt: str | None = None
    published_at: datetime | None = None
    event_at: datetime | None = None
    retrieved_at: datetime
    geography: str | None = None
    beat: Beat = Beat.OTHER
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str
    status: SourceStatus
    last_attempt: datetime
    last_success: datetime | None
    item_count: int
    error_message: str | None = None
    latency_ms: int


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    created_at: datetime
    updated_at: datetime
    signal_type: SignalType
    title: str
    summary: str
    geography: str | None
    beat: Beat
    novelty_score: int
    local_impact_score: int
    evidence_count: int
    status: str = "OPEN"


class Forecast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forecast_id: str
    version: int
    issued_at: datetime
    horizon_start: datetime
    horizon_end: datetime
    claim: str
    event_type: str
    geography: str | None
    beat: Beat
    likelihood_score: int
    editorial_relevance_score: int
    priority_score: int
    status: ForecastStatus
    model_version: str
    explanation: dict[str, Any]


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forecast_id: str
    resolved_at: datetime
    outcome: Outcome
    evidence: str
    notes: str | None = None
