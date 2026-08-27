"""SQLite persistence layer.

Uses the standard-library sqlite3 module directly (no ORM). All timestamps
are stored as ISO-8601 UTC strings with explicit offsets; the caller is
responsible for parsing back into tz-aware datetimes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    Forecast,
    ForecastStatus,
    Outcome,
    Resolution,
    Signal,
    SourceHealth,
    SourceItem,
    SourceStatus,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT NOT NULL,
    excerpt TEXT,
    published_at TEXT,
    event_at TEXT,
    retrieved_at TEXT NOT NULL,
    geography TEXT,
    beat TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source_name, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_hash ON source_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_beat ON source_items(beat);
CREATE INDEX IF NOT EXISTS idx_items_event_at ON source_items(event_at);

CREATE TABLE IF NOT EXISTS source_health (
    source_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_attempt TEXT NOT NULL,
    last_success TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    geography TEXT,
    beat TEXT NOT NULL,
    novelty_score INTEGER NOT NULL,
    local_impact_score INTEGER NOT NULL,
    evidence_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN'
);
CREATE INDEX IF NOT EXISTS idx_signals_beat ON signals(beat);

CREATE TABLE IF NOT EXISTS signal_evidence (
    signal_id INTEGER NOT NULL,
    source_item_id INTEGER NOT NULL,
    relationship TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (signal_id, source_item_id),
    FOREIGN KEY (signal_id) REFERENCES signals(id),
    FOREIGN KEY (source_item_id) REFERENCES source_items(id)
);

-- Append-only. A row is one (forecast_id, version) fact. Never UPDATE.
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    claim TEXT NOT NULL,
    event_type TEXT NOT NULL,
    geography TEXT,
    beat TEXT NOT NULL,
    likelihood_score INTEGER NOT NULL,
    editorial_relevance_score INTEGER NOT NULL,
    priority_score INTEGER NOT NULL,
    status TEXT NOT NULL,
    model_version TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    PRIMARY KEY (forecast_id, version)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_status ON forecasts(status);
CREATE INDEX IF NOT EXISTS idx_forecasts_issued ON forecasts(issued_at);

-- Immutable historical provenance captured when a forecast version is issued.
-- signal_id/source_item_id are provenance labels, not foreign keys: current
-- signals are recomputed and source_items can change after issuance.
CREATE TABLE IF NOT EXISTS forecast_evidence_snapshots (
    forecast_id TEXT NOT NULL,
    forecast_version INTEGER NOT NULL,
    signal_id INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    signal_title TEXT NOT NULL,
    signal_summary TEXT NOT NULL,
    signal_created_at TEXT NOT NULL,
    signal_novelty_score INTEGER NOT NULL,
    signal_local_impact_score INTEGER NOT NULL,
    source_item_id INTEGER NOT NULL,
    relationship TEXT NOT NULL,
    evidence_weight INTEGER NOT NULL DEFAULT 1,
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_url TEXT,
    item_title TEXT NOT NULL,
    item_excerpt TEXT,
    published_at TEXT,
    event_at TEXT,
    retrieved_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    geography TEXT,
    beat TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    public_metadata_json TEXT NOT NULL,
    PRIMARY KEY (forecast_id, forecast_version, signal_id, source_item_id),
    FOREIGN KEY (forecast_id, forecast_version)
        REFERENCES forecasts(forecast_id, version)
);
CREATE INDEX IF NOT EXISTS idx_forecast_evidence_lookup
    ON forecast_evidence_snapshots(forecast_id, forecast_version);

CREATE TABLE IF NOT EXISTS resolutions (
    forecast_id TEXT PRIMARY KEY,
    resolved_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    evidence TEXT NOT NULL,
    notes TEXT
);

-- Adds explicit version targeting without destructively changing the existing
-- resolution table. New resolutions are written to both tables atomically.
CREATE TABLE IF NOT EXISTS resolution_targets (
    forecast_id TEXT PRIMARY KEY,
    forecast_version INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (forecast_id, forecast_version)
        REFERENCES forecasts(forecast_id, version),
    FOREIGN KEY (forecast_id) REFERENCES resolutions(forecast_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("Naive datetime rejected — all timestamps must be tz-aware")
    return dt.astimezone(timezone.utc).isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# source_items
# ---------------------------------------------------------------------------

def upsert_source_item(conn: sqlite3.Connection, item: SourceItem) -> tuple[int, bool]:
    """Insert or update a source item.

    Deduplication: (source_name, external_id) uniquely identifies an item.
    If the content_hash matches the existing row, only last_seen_at is bumped
    and updated=False is returned. If the hash differs, the record is updated
    and updated=True is returned (this represents a legitimate content change).

    Returns (row_id, was_updated).
    """
    now_iso = _iso(item.retrieved_at)
    existing = conn.execute(
        "SELECT id, content_hash FROM source_items WHERE source_name=? AND external_id=?",
        (item.source_name, item.external_id),
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO source_items (
                source_name, external_id, canonical_url, title, excerpt,
                published_at, event_at, retrieved_at, geography, beat,
                content_hash, metadata_json, first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item.source_name,
                item.external_id,
                item.canonical_url,
                item.title,
                item.excerpt,
                _iso(item.published_at),
                _iso(item.event_at),
                now_iso,
                item.geography,
                item.beat.value,
                item.content_hash,
                json.dumps(item.metadata, sort_keys=True, default=str),
                now_iso,
                now_iso,
            ),
        )
        return int(cur.lastrowid), False

    row_id = int(existing["id"])
    if existing["content_hash"] == item.content_hash:
        conn.execute(
            "UPDATE source_items SET last_seen_at=? WHERE id=?",
            (now_iso, row_id),
        )
        return row_id, False

    conn.execute(
        """
        UPDATE source_items SET
            canonical_url=?, title=?, excerpt=?, published_at=?, event_at=?,
            geography=?, beat=?, content_hash=?, metadata_json=?, last_seen_at=?
        WHERE id=?
        """,
        (
            item.canonical_url,
            item.title,
            item.excerpt,
            _iso(item.published_at),
            _iso(item.event_at),
            item.geography,
            item.beat.value,
            item.content_hash,
            json.dumps(item.metadata, sort_keys=True, default=str),
            now_iso,
            row_id,
        ),
    )
    return row_id, True


def list_source_items(
    conn: sqlite3.Connection, limit: int = 200, source_name: str | None = None
) -> list[dict[str, Any]]:
    if source_name:
        rows = conn.execute(
            "SELECT * FROM source_items WHERE source_name=? ORDER BY COALESCE(event_at, published_at, retrieved_at) DESC LIMIT ?",
            (source_name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM source_items ORDER BY COALESCE(event_at, published_at, retrieved_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
    return d


def count_source_items(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0])


# ---------------------------------------------------------------------------
# source_health
# ---------------------------------------------------------------------------

def upsert_source_health(conn: sqlite3.Connection, health: SourceHealth) -> None:
    conn.execute(
        """
        INSERT INTO source_health (
            source_name, status, last_attempt, last_success,
            item_count, error_message, latency_ms
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(source_name) DO UPDATE SET
            status=excluded.status,
            last_attempt=excluded.last_attempt,
            last_success=COALESCE(excluded.last_success, source_health.last_success),
            item_count=excluded.item_count,
            error_message=excluded.error_message,
            latency_ms=excluded.latency_ms
        """,
        (
            health.source_name,
            health.status.value,
            _iso(health.last_attempt),
            _iso(health.last_success),
            health.item_count,
            health.error_message,
            health.latency_ms,
        ),
    )


def list_source_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM source_health ORDER BY source_name").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------

def insert_signal(conn: sqlite3.Connection, signal: Signal, evidence: list[tuple[int, str, int]]) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            created_at, updated_at, signal_type, title, summary,
            geography, beat, novelty_score, local_impact_score,
            evidence_count, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _iso(signal.created_at),
            _iso(signal.updated_at),
            signal.signal_type.value,
            signal.title,
            signal.summary,
            signal.geography,
            signal.beat.value,
            signal.novelty_score,
            signal.local_impact_score,
            signal.evidence_count,
            signal.status,
        ),
    )
    signal_id = int(cur.lastrowid)
    for source_item_id, relationship, weight in evidence:
        conn.execute(
            "INSERT OR IGNORE INTO signal_evidence (signal_id, source_item_id, relationship, weight) VALUES (?,?,?,?)",
            (signal_id, source_item_id, relationship, weight),
        )
    return signal_id


def list_signals(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def clear_signals(conn: sqlite3.Connection) -> None:
    """Signals are derived from source_items and are recomputed each collect run."""
    conn.execute("DELETE FROM signal_evidence")
    conn.execute("DELETE FROM signals")


# ---------------------------------------------------------------------------
# forecasts (append-only)
# ---------------------------------------------------------------------------

def latest_forecast_version(conn: sqlite3.Connection, forecast_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) FROM forecasts WHERE forecast_id=?", (forecast_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def insert_forecast(conn: sqlite3.Connection, forecast: Forecast) -> None:
    """Insert one immutable (forecast_id, version) row. Never overwrites prior versions."""
    existing = conn.execute(
        "SELECT 1 FROM forecasts WHERE forecast_id=? AND version=?",
        (forecast.forecast_id, forecast.version),
    ).fetchone()
    if existing is not None:
        raise ValueError(
            f"forecast {forecast.forecast_id} version {forecast.version} already exists — historical rows are immutable"
        )
    conn.execute(
        """
        INSERT INTO forecasts (
            forecast_id, version, issued_at, horizon_start, horizon_end,
            claim, event_type, geography, beat, likelihood_score,
            editorial_relevance_score, priority_score, status,
            model_version, explanation_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            forecast.forecast_id,
            forecast.version,
            _iso(forecast.issued_at),
            _iso(forecast.horizon_start),
            _iso(forecast.horizon_end),
            forecast.claim,
            forecast.event_type,
            forecast.geography,
            forecast.beat.value,
            forecast.likelihood_score,
            forecast.editorial_relevance_score,
            forecast.priority_score,
            forecast.status.value,
            forecast.model_version,
            json.dumps(forecast.explanation, sort_keys=True, default=str),
        ),
    )


def list_forecasts_latest(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """Return the latest version of each forecast_id."""
    rows = conn.execute(
        """
        SELECT f.*
        FROM forecasts f
        INNER JOIN (
            SELECT forecast_id, MAX(version) AS max_v
            FROM forecasts
            GROUP BY forecast_id
        ) latest ON latest.forecast_id = f.forecast_id AND latest.max_v = f.version
        ORDER BY f.issued_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_forecast_row(r) for r in rows]


def get_forecast_versions(conn: sqlite3.Connection, forecast_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id=? ORDER BY version ASC",
        (forecast_id,),
    ).fetchall()
    return [_forecast_row(r) for r in rows]


def get_forecast_version(
    conn: sqlite3.Connection, forecast_id: str, version: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id=? AND version=?",
        (forecast_id, version),
    ).fetchone()
    return _forecast_row(row) if row else None


def _forecast_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["explanation"] = json.loads(d.pop("explanation_json") or "{}")
    return d


# ---------------------------------------------------------------------------
# immutable forecast evidence snapshots
# ---------------------------------------------------------------------------

def insert_forecast_evidence_snapshot(
    conn: sqlite3.Connection,
    *,
    forecast_id: str,
    forecast_version: int,
    signal_id: int,
    signal: Signal,
    source_item: dict[str, Any],
    relationship: str,
    evidence_weight: int = 1,
) -> None:
    """Capture the evidence exactly as persisted when a version is issued."""
    metadata = {
        key: value
        for key, value in (source_item.get("metadata") or {}).items()
        if not key.endswith("_private")
    }
    conn.execute(
        """
        INSERT INTO forecast_evidence_snapshots (
            forecast_id, forecast_version, signal_id, signal_type,
            signal_title, signal_summary, signal_created_at,
            signal_novelty_score, signal_local_impact_score,
            source_item_id, relationship, evidence_weight, source_name,
            external_id, canonical_url, item_title, item_excerpt,
            published_at, event_at, retrieved_at, first_seen_at, last_seen_at,
            geography, beat, content_hash, public_metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            forecast_id,
            forecast_version,
            signal_id,
            signal.signal_type.value,
            signal.title,
            signal.summary,
            _iso(signal.created_at),
            signal.novelty_score,
            signal.local_impact_score,
            source_item["id"],
            relationship,
            evidence_weight,
            source_item["source_name"],
            source_item["external_id"],
            source_item.get("canonical_url"),
            source_item["title"],
            source_item.get("excerpt"),
            source_item.get("published_at"),
            source_item.get("event_at"),
            source_item["retrieved_at"],
            source_item["first_seen_at"],
            source_item["last_seen_at"],
            source_item.get("geography"),
            source_item["beat"],
            source_item["content_hash"],
            json.dumps(metadata, sort_keys=True, default=str),
        ),
    )


def get_forecast_evidence(
    conn: sqlite3.Connection, forecast_id: str, forecast_version: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM forecast_evidence_snapshots
        WHERE forecast_id=? AND forecast_version=?
        ORDER BY signal_type, signal_id, source_name, external_id
        """,
        (forecast_id, forecast_version),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["public_metadata"] = json.loads(item.pop("public_metadata_json") or "{}")
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# resolutions
# ---------------------------------------------------------------------------

def insert_resolution(conn: sqlite3.Connection, resolution: Resolution) -> None:
    version = get_forecast_version(conn, resolution.forecast_id, resolution.forecast_version)
    if version is None:
        raise ValueError(
            f"unknown forecast {resolution.forecast_id!r} version {resolution.forecast_version}"
        )
    latest = latest_forecast_version(conn, resolution.forecast_id)
    if resolution.forecast_version != latest:
        raise ValueError(
            f"forecast version {resolution.forecast_version} is not the latest version {latest}"
        )
    if get_resolution(conn, resolution.forecast_id) is not None:
        raise ValueError(f"forecast {resolution.forecast_id!r} is already resolved")
    conn.execute(
        """
        INSERT INTO resolutions (forecast_id, resolved_at, outcome, evidence, notes)
        VALUES (?,?,?,?,?)
        """,
        (
            resolution.forecast_id,
            _iso(resolution.resolved_at),
            resolution.outcome.value,
            resolution.evidence,
            resolution.notes,
        ),
    )
    conn.execute(
        """
        INSERT INTO resolution_targets (forecast_id, forecast_version, recorded_at)
        VALUES (?,?,?)
        """,
        (
            resolution.forecast_id,
            resolution.forecast_version,
            _iso(resolution.resolved_at),
        ),
    )


def get_resolution(conn: sqlite3.Connection, forecast_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT r.*, t.forecast_version
        FROM resolutions r
        LEFT JOIN resolution_targets t ON t.forecast_id = r.forecast_id
        WHERE r.forecast_id=?
        """,
        (forecast_id,),
    ).fetchone()
    return dict(row) if row else None


def list_resolutions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, t.forecast_version
        FROM resolutions r
        LEFT JOIN resolution_targets t ON t.forecast_id = r.forecast_id
        ORDER BY r.resolved_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------

def insert_feedback(
    conn: sqlite3.Connection, subject_type: str, subject_id: str, label: str, note: str | None
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO feedback (subject_type, subject_id, label, note, created_at) VALUES (?,?,?,?,?)",
        (subject_type, subject_id, label, note, now),
    )
    return int(cur.lastrowid)


def list_feedback(
    conn: sqlite3.Connection, *, subject_type: str | None = None, subject_id: str | None = None
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if subject_type is not None:
        clauses.append("subject_type=?")
        params.append(subject_type)
    if subject_id is not None:
        clauses.append("subject_id=?")
        params.append(subject_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM feedback{where} ORDER BY created_at DESC", params
    ).fetchall()
    return [dict(row) for row in rows]
