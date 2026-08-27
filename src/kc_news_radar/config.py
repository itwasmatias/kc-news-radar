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
DEFAULT_COLLECTION_CADENCE_SECONDS = 15 * 60
DEFAULT_STALE_AFTER_SECONDS = 60 * 60
DEFAULT_COLLECTION_LEASE_SECONDS = 30 * 60
DEFAULT_SHUTDOWN_GRACE_SECONDS = 30
DEFAULT_BACKUP_RETENTION_COUNT = 14


def _bool_env(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _strict_bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be one of 1/0, true/false, yes/no, or on/off")


def _bounded_int_env(key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    db_path: Path
    db_path_explicit: bool
    demo_mode: bool
    host: str
    port: int
    user_agent: str
    http_timeout: float
    collection_enabled: bool
    collection_cadence_seconds: int
    stale_after_seconds: int
    collection_lease_seconds: int
    shutdown_grace_seconds: int
    backup_dir: Path
    backup_retention_count: int


def load_settings() -> Settings:
    configured_db = os.environ.get("KC_NEWS_RADAR_DB")
    db_path = Path(configured_db) if configured_db else DEFAULT_DB_PATH
    resolved_db_path = db_path.resolve()
    configured_backup_dir = os.environ.get("KC_NEWS_RADAR_BACKUP_DIR")
    backup_dir = (
        Path(configured_backup_dir).resolve()
        if configured_backup_dir
        else resolved_db_path.parent / "backups"
    )
    return Settings(
        db_path=resolved_db_path,
        db_path_explicit=configured_db is not None,
        demo_mode=_bool_env("KC_NEWS_RADAR_DEMO", False),
        host=os.environ.get("KC_NEWS_RADAR_HOST", "127.0.0.1"),
        port=int(os.environ.get("KC_NEWS_RADAR_PORT", "8765")),
        user_agent=os.environ.get("KC_NEWS_RADAR_USER_AGENT", USER_AGENT),
        http_timeout=float(os.environ.get("KC_NEWS_RADAR_TIMEOUT", HTTP_TIMEOUT_SECONDS)),
        collection_enabled=_strict_bool_env("KC_NEWS_RADAR_COLLECTION_ENABLED", True),
        collection_cadence_seconds=_bounded_int_env(
            "KC_NEWS_RADAR_COLLECTION_CADENCE_SECONDS",
            DEFAULT_COLLECTION_CADENCE_SECONDS,
            minimum=30,
            maximum=86_400,
        ),
        stale_after_seconds=_bounded_int_env(
            "KC_NEWS_RADAR_STALE_AFTER_SECONDS",
            DEFAULT_STALE_AFTER_SECONDS,
            minimum=60,
            maximum=604_800,
        ),
        collection_lease_seconds=_bounded_int_env(
            "KC_NEWS_RADAR_COLLECTION_LEASE_SECONDS",
            DEFAULT_COLLECTION_LEASE_SECONDS,
            minimum=60,
            maximum=86_400,
        ),
        shutdown_grace_seconds=_bounded_int_env(
            "KC_NEWS_RADAR_SHUTDOWN_GRACE_SECONDS",
            DEFAULT_SHUTDOWN_GRACE_SECONDS,
            minimum=1,
            maximum=300,
        ),
        backup_dir=backup_dir,
        backup_retention_count=_bounded_int_env(
            "KC_NEWS_RADAR_BACKUP_RETENTION_COUNT",
            DEFAULT_BACKUP_RETENTION_COUNT,
            minimum=1,
            maximum=10_000,
        ),
    )
