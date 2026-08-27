# Architecture

## Module layout

```
src/kc_news_radar/
├── __init__.py          # package version + SCORING_MODEL_VERSION constant
├── app.py               # FastAPI: /api/* endpoints + static frontend mount
├── collect.py           # CLI: `kc-news-radar-collect` entry point
├── collection_runtime.py # one leased, observable collection cycle
├── scheduler.py         # small cadence thread + combined service command
├── freshness.py         # run-derived freshness and source state
├── config.py            # environment-driven Settings dataclass + Chicago TZ
├── db.py                # sqlite3-based persistence (no ORM)
├── demo.py              # deterministic offline fixtures for demo mode
├── models.py            # Pydantic models + enums (Beat, SignalType, …)
├── collectors/          # one file per public source adapter
│   ├── base.py          # BaseCollector, CollectorResult, run_all_collectors
│   ├── kcmo.py          # KCMO 311 open-data (Socrata) + legacy RSS
│   ├── johnson_county.py
│   ├── nws.py, nws_afd.py
│   ├── usgs.py
│   ├── mo_house.py
│   └── ridekc.py, flykc.py, jackson_county.py   # repaired live public interfaces
├── pipeline/
│   ├── normalize.py     # text normalization + Jaccard helpers
│   ├── dedupe.py        # near-duplicate grouping by title similarity
│   ├── signals.py       # deterministic signal detectors
│   ├── scoring.py       # experimental_likelihood + editorial_relevance
│   ├── forecasting.py   # aggregate signals per item → Forecast rows
│   └── briefing.py      # Morning Strategy Brief payload
├── ledger/
│   ├── forecasts.py     # append-only versioning + material-delta gating
│   └── resolution.py    # forecast resolutions + display-time status
├── performance.py       # descriptive outcome counts from resolved forecasts
└── web/                 # static HTML/JS/CSS dashboard
```

## Data flow

```
Public sources         Collectors                 Pipeline
────────────────       ─────────────              ─────────────────────
Socrata 311, NWS,  →   fetch() + parse()   →      normalize / dedupe
USGS, JoCo RSS,        (per-adapter                        │
MO House, etc.          failure isolation)                 ▼
                                                    detect_signals()
                                                           │
                                                           ▼
                          Forecast ledger  ◄─────  run_pipeline()
                          (append-only)                    │
                                                           ▼
                        Morning Strategy Brief   ◄─── build_brief()
                                (FastAPI)
```

The pipeline is designed so that each layer can be tested in isolation with
plain Python dicts and does not require network access.

## Storage schema (SQLite)

- **`source_items`** — one row per unique `(source_name, external_id)`.
  Deduplicated by `content_hash`; when the hash changes, the row is
  updated in place (that is what "the source updated its content" means).
- **`source_health`** — per-adapter status (`HEALTHY | DEGRADED | FAILED |
  DISABLED`), last attempt, last success, item count, latency, error.
- **`signals`** + **`signal_evidence`** — the current detected signals plus
  their supporting `source_items`. Signals are recomputed every collect run
  (see `db.clear_signals`); missing items simply stop generating signals.
- **`forecasts`** — **append-only.** Primary key `(forecast_id, version)`.
  A new version is written only when the underlying likelihood or relevance
  changes by at least `MATERIAL_SCORE_DELTA` (5 points). Prior rows are
  never mutated — `insert_forecast` raises if you try.
- **`forecast_evidence_snapshots`** — immutable signal and public-source
  snapshots captured only when a new forecast version is appended. IDs remain
  provenance labels rather than live foreign-key joins because signals are
  recomputed and source rows can later change. Private metadata keys are not
  copied into the dashboard-visible snapshot.
- **`resolutions`** — one row per forecast recording an outcome
  (`CONFIRMED | NOT_OCCURRED | AMBIGUOUS | EXPIRED_UNRESOLVED`). Never
  modifies the forecast row itself.
- **`resolution_targets`** — the exact forecast version resolved. New
  resolutions are single-assignment and target the latest persisted version.
- **`feedback`** — newsroom-manager reactions on items/signals/forecasts.
- **`collection_runs`** — immutable run request identity and trigger metadata.
- **`collection_run_events`** — append-only lifecycle facts (`RUNNING`,
  `COMPLETED`, `PARTIAL_FAILURE`, `FAILED`, `ABANDONED`, or
  `BLOCKED_OVERLAP`). Completion counters and pipeline summaries live here.
- **`collection_source_results`** — one immutable result per attempted source
  and run, distinguishing failures, degraded zero-item parses, and legitimate
  empty feeds.
- **`collection_lease`** — singleton expiring SQLite lease used by all manual
  and scheduled callers for cross-process exclusion.
- **`scheduler_state`** — mutable operational projection for worker heartbeat,
  cadence, and next expected cycle; run events remain execution authority.

Timestamps are stored as ISO-8601 UTC strings; the DB layer refuses naive
datetimes. Display code (only) formats to `America/Chicago`.

## Failure isolation

Each collector's `run()` catches every exception and returns a
`SourceHealth` row with status `FAILED` plus an error message. A failing
collector cannot crash the run. `run_all_collectors()` also wraps `run()`
one level up as belt-and-braces defense.

An adapter that fetches successfully but returns zero items is marked
`DEGRADED` — unless it declares `empty_ok = True` (used for alert feeds
like NWS and USGS where quiet periods are legitimate), in which case it
stays `HEALTHY`.

## Explainability

Every score is a sum of documented positive components. Both
`experimental_likelihood_score` and `editorial_relevance_score` return a
`ScoreBreakdown` whose `.components` list becomes the "why this is
elevated" bullet list in the dashboard. `SCORING_MODEL_VERSION`
(`heuristic-v0.1`) is embedded in every forecast row so historical rows
remain interpretable when the scoring rules change.

## Immutability

The forecast ledger is the compliance backbone. `ledger.forecasts`:

- Compares the current run's likelihood/relevance against the most recent
  stored version.
- If either delta is at least `MATERIAL_SCORE_DELTA`, appends a new
  `(forecast_id, version+1)` row.
- Otherwise makes no change.
- Never rewrites history. If someone tries to insert an existing
  `(forecast_id, version)`, `db.insert_forecast` raises `ValueError`.

Evidence snapshots and resolutions follow the same rule: a new forecast
version receives its own evidence snapshot, and an outcome is stored beside—not
inside—the forecast. Legacy forecasts without snapshots return an explicit
`LEGACY_NOT_CAPTURED` boundary instead of a join to mutable current content.

## Automatic collection boundary

`kc-news-radar-run` starts FastAPI and one small cadence thread in the same
process. This avoids external scheduling infrastructure, but the thread is not
the concurrency authority. `collection_runtime.execute_collection_cycle`
atomically acquires `collection_lease` under `BEGIN IMMEDIATE`, so a manual CLI
in another process cannot overlap it. Each source result commits separately;
the signal/forecast/evidence pipeline retains its existing atomic commit.
Interrupted runs keep earlier committed facts, remain `RUNNING` until the lease
expires, and become `ABANDONED` on startup recovery or the next acquisition.
