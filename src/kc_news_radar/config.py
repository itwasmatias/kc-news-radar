"""Runtime configuration for KC News Radar.

All values are read from environment variables with sensible defaults so the
application runs out-of-the-box on a developer laptop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "kc_news_radar.db"

NEWSROOM_TZ = ZoneInfo("America/Chicago")

USER_AGENT = (
    "KCNewsRadar/0.1 (+experimental newsroom-support prototype; contact via project README)"
)

HTTP_TIMEOUT_SECONDS = 15.0
MAX_EXCERPT_CHARS = 800


def _bool_env(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    db_path_explicit: bool
    demo_mode: bool
    host: str
    port: int
    user_agent: str
    http_timeout: float


def load_settings() -> Settings:
    configured_db = os.environ.get("KC_NEWS_RADAR_DB")
    db_path = Path(configured_db) if configured_db else DEFAULT_DB_PATH
    return Settings(
        db_path=db_path.resolve(),
        db_path_explicit=configured_db is not None,
        demo_mode=_bool_env("KC_NEWS_RADAR_DEMO", False),
        host=os.environ.get("KC_NEWS_RADAR_HOST", "127.0.0.1"),
        port=int(os.environ.get("KC_NEWS_RADAR_PORT", "8765")),
        user_agent=os.environ.get("KC_NEWS_RADAR_USER_AGENT", USER_AGENT),
        http_timeout=float(os.environ.get("KC_NEWS_RADAR_TIMEOUT", HTTP_TIMEOUT_SECONDS)),
    )
