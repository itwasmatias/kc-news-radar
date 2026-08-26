"""Immutable forecast versioning.

Rules:

* An existing (forecast_id, version) row is **never** modified.
* If the same forecast_id is re-issued with a materially different score,
  a **new** version row is appended.
* If nothing material has changed, no new row is appended.
* The whole history is queryable — misses stay visible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .. import db as dbmod
from ..models import Forecast


MATERIAL_SCORE_DELTA = 5


@dataclass
class ForecastComparison:
    changed: bool
    delta_likelihood: int
    delta_relevance: int


def compare_versions(a: dict, b: dict) -> ForecastComparison:
    dl = int(b.get("likelihood_score", 0)) - int(a.get("likelihood_score", 0))
    dr = int(b.get("editorial_relevance_score", 0)) - int(a.get("editorial_relevance_score", 0))
    changed = abs(dl) >= MATERIAL_SCORE_DELTA or abs(dr) >= MATERIAL_SCORE_DELTA
    return ForecastComparison(changed=changed, delta_likelihood=dl, delta_relevance=dr)


def upsert_forecast_version(conn: sqlite3.Connection, forecast: Forecast) -> Forecast:
    """Append a new version of forecast if materially changed, else no-op.

    Never mutates prior rows. Returns the concrete Forecast row actually stored
    (with its correct version number), or the most recent existing version if
    no change was material.
    """
    latest = dbmod.latest_forecast_version(conn, forecast.forecast_id)
    if latest > 0:
        prior_rows = dbmod.get_forecast_versions(conn, forecast.forecast_id)
        prior = prior_rows[-1]
        comparison = compare_versions(prior, forecast.model_dump())
        if not comparison.changed:
            # Nothing meaningful changed. Do not append a new version.
            return Forecast(
                **{
                    **prior,
                    "beat": prior["beat"],
                    "status": prior["status"],
                    "explanation": prior.get("explanation", {}),
                }
            )

    next_version = latest + 1
    new_forecast = forecast.model_copy(update={"version": next_version})
    dbmod.insert_forecast(conn, new_forecast)
    return new_forecast
