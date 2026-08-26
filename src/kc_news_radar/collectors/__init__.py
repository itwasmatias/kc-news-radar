"""Source adapters for public Kansas City-area information."""

from __future__ import annotations

from .base import BaseCollector, CollectorResult, FetchError
from .flykc import FlyKCCollector
from .jackson_county import JacksonCountyCollector
from .johnson_county import JohnsonCountyCollector
from .kcmo import KCMOClerkLegistarCollector, KCMOCollector
from .mo_house import MissouriHouseCollector
from .nws import NWSCollector
from .nws_afd import NWSAreaForecastCollector
from .ridekc import RideKCCollector
from .usgs import USGSQuakeCollector

# Order controls dashboard listing.
ALL_COLLECTORS: list[type[BaseCollector]] = [
    KCMOCollector,                # KCMO 311 open-data
    KCMOClerkLegistarCollector,   # KCMO City Council (Clerk / Legistar)
    JacksonCountyCollector,       # Jackson County MO Legislature (Legistar)
    JohnsonCountyCollector,       # Johnson County KS public news RSS
    NWSCollector,                 # NWS active alerts
    NWSAreaForecastCollector,     # NWS AFD narrative
    USGSQuakeCollector,           # USGS central-US quakes
    MissouriHouseCollector,       # MO House bill list
    RideKCCollector,              # RideKC service alerts (HTML)
    FlyKCCollector,               # KCI Airport (Prismic)
]


def build_all(*, http_timeout: float, user_agent: str) -> list[BaseCollector]:
    return [cls(http_timeout=http_timeout, user_agent=user_agent) for cls in ALL_COLLECTORS]


__all__ = [
    "ALL_COLLECTORS",
    "BaseCollector",
    "CollectorResult",
    "FetchError",
    "FlyKCCollector",
    "JacksonCountyCollector",
    "JohnsonCountyCollector",
    "KCMOClerkLegistarCollector",
    "KCMOCollector",
    "MissouriHouseCollector",
    "NWSAreaForecastCollector",
    "NWSCollector",
    "RideKCCollector",
    "USGSQuakeCollector",
    "build_all",
]
