"""Descriptive forecast-outcome summaries for the editorial validation loop.

Only explicitly resolved forecasts with an explicit target version are
included. These counts describe the recorded sample; they do not establish
calibration, statistical significance, or predictive superiority.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from typing import Any

from . import db as dbmod


MIN_INFORMATIVE_SAMPLE = 5


def build_performance_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    resolutions = dbmod.list_resolutions(conn)
    included: list[tuple[dict[str, Any], dict[str, Any]]] = []
    excluded_without_version = 0

    for resolution in resolutions:
        version = resolution.get("forecast_version")
        if version is None:
            excluded_without_version += 1
            continue
        forecast = dbmod.get_forecast_version(
            conn, resolution["forecast_id"], int(version)
        )
        if forecast is not None:
            included.append((resolution, forecast))

    denominator = len(included)
    outcome_counts = Counter(r["outcome"] for r, _ in included)
    model_groups: dict[str, Counter[str]] = defaultdict(Counter)
    signal_groups: dict[str, Counter[str]] = defaultdict(Counter)

    for resolution, forecast in included:
        outcome = resolution["outcome"]
        model_groups[forecast["model_version"]][outcome] += 1
        explanation = forecast.get("explanation") or {}
        signal_types = explanation.get("signal_types") or []
        if not signal_types and explanation.get("signal_type"):
            signal_types = [explanation["signal_type"]]
        if not signal_types:
            signal_types = [part for part in forecast["event_type"].split("+") if part]
        for signal_type in sorted(set(signal_types)):
            signal_groups[signal_type][outcome] += 1

    def rows(groups: dict[str, Counter[str]], key_name: str) -> list[dict[str, Any]]:
        result = []
        for key in sorted(groups):
            counts = dict(sorted(groups[key].items()))
            result.append({
                key_name: key,
                "resolved_count": sum(counts.values()),
                "overall_resolved_denominator": denominator,
                "outcomes": counts,
            })
        return result

    sufficient = denominator >= MIN_INFORMATIVE_SAMPLE
    if denominator == 0:
        evidence_note = "Insufficient resolved evidence: no version-targeted forecast outcomes are recorded."
    elif not sufficient:
        evidence_note = (
            f"Insufficient resolved evidence for stable interpretation: only {denominator} "
            f"resolved forecasts are recorded; at least {MIN_INFORMATIVE_SAMPLE} are requested for a basic descriptive view."
        )
    else:
        evidence_note = (
            f"Descriptive counts from {denominator} resolved forecasts. The sample may be selective and does not establish calibration, significance, or predictive superiority."
        )

    return {
        "resolved_forecast_count": denominator,
        "denominator": denominator,
        "minimum_informative_sample": MIN_INFORMATIVE_SAMPLE,
        "sufficient_resolved_evidence": sufficient,
        "excluded_legacy_resolutions_without_version": excluded_without_version,
        "by_outcome": [
            {"outcome": outcome, "resolved_count": count, "denominator": denominator}
            for outcome, count in sorted(outcome_counts.items())
        ],
        "by_model_version": rows(model_groups, "model_version"),
        "by_signal_type": rows(signal_groups, "signal_type"),
        "evidence_note": evidence_note,
        "scientific_note": (
            "Experimental scores are not calibrated probabilities. These are recorded outcome counts only."
        ),
    }


__all__ = ["MIN_INFORMATIVE_SAMPLE", "build_performance_summary"]
