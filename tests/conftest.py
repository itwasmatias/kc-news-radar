"""Shared test fixtures.

All tests run entirely offline. No collector-level HTTP is exercised in the
default suite; parsers are hit with local fixture bytes instead.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kc_news_radar import db as dbmod


@pytest.fixture()
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh initialized SQLite DB for a single test."""
    db_path = tmp_path / "test.db"
    dbmod.init_db(db_path)
    conn = dbmod.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a path to an initialized DB so tests can reopen it."""
    p = tmp_path / "reopen.db"
    dbmod.init_db(p)
    return p


@pytest.fixture()
def now_utc() -> datetime:
    return datetime.now(timezone.utc)
