"""Deterministic post-collection pipeline: normalize, dedupe, detect signals, score, forecast."""

from .signals import detect_signals
from .scoring import (
    ScoreBreakdown,
    editorial_relevance_score,
    experimental_likelihood_score,
    priority_score,
)
from .forecasting import build_forecast, forecast_id_for_signal, run_pipeline

__all__ = [
    "ScoreBreakdown",
    "build_forecast",
    "detect_signals",
    "editorial_relevance_score",
    "experimental_likelihood_score",
    "forecast_id_for_signal",
    "priority_score",
    "run_pipeline",
]
