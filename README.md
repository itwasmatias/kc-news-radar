# Kansas City News Radar (v0.1)

An experimental early-warning system for a Kansas City public radio newsroom
(KCUR-style). It watches a small set of Tier-1 public information sources for
Kansas City, Missouri and Kansas City metro civic developments, produces
explainable signals from what changes, and rolls the signals into a
**Morning Strategy Brief** designed to save a newsroom manager 30–60 minutes
of information gathering ahead of the daily editorial meeting.

> ⚠️ **This is a v0.1 prototype.** Every score it produces is an
> *experimental score*, not a calibrated probability. Editorial judgment
> remains with the newsroom. See `docs/EDITORIAL_SAFETY.md`.

---

## What it does

1. **Collects** normalized items from a small set of public data sources
   (KCMO 311 open-data, Johnson County news, NWS alerts, USGS earthquakes,
   Missouri House bills, NWS forecast discussions, KCMO and Jackson County
   Legistar records, RideKC alerts, and FlyKC aviation publications).
2. **Detects deterministic signals** — new items, updated items, scheduled
   catalysts, severe weather, unusual agenda items, multi-source convergence,
   repeated entity activity, high-impact public safety, 311 community
   trends, and development-deal activity (Royals/Chiefs/TIF/Port KC).
3. **Scores** each candidate story with two independent, explainable 0–100
   scores: an *experimental likelihood* and an *editorial relevance* (see
   `docs/EDITORIAL_SAFETY.md`).
4. **Appends** a forecast row per candidate story to an **immutable ledger**
   that lets a newsroom go back later and audit what the radar said, when,
   and how much its score moved between versions.
5. **Serves** a local FastAPI dashboard whose primary view is the Morning
   Strategy Brief (`NEW`, `CHANGED`, `RESOLVED`, `WATCH`, `NEXT 72H`,
   `QUESTIONS`), with a Beat Momentum overview and secondary tabs for
   signals, the forecast ledger, and source health.
6. **Closes the validation loop** by preserving the signal and public-source
   evidence captured for each new forecast version, accepting constrained
   newsroom feedback, recording one version-targeted outcome without changing
   the forecast, and reporting descriptive resolved-outcome counts.

---

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

# Try it with deterministic synthetic data (no network needed) in a named DB:
KC_NEWS_RADAR_DB=./data/demo.db KC_NEWS_RADAR_DEMO=1 kc-news-radar-collect
KC_NEWS_RADAR_DB=./data/demo.db kc-news-radar-serve  # http://127.0.0.1:8765
```

Every demo record is prefixed `[DEMO DATA]` so you can never confuse it with
a real public-source item.

To run against live public sources instead:

```bash
KC_NEWS_RADAR_DB=./data/live.db kc-news-radar-collect
kc-news-radar-collect --list     # list adapters
KC_NEWS_RADAR_DB=./data/live.db kc-news-radar-collect --source nws_kc --verbose
KC_NEWS_RADAR_DB=./data/live.db kc-news-radar-serve
```

---

## Running tests

```bash
pytest
```

The full suite runs entirely offline — no live HTTP is exercised.

---

## Configuration

Environment variables (all optional):

| Variable                    | Default                          | Purpose                              |
| --------------------------- | -------------------------------- | ------------------------------------ |
| `KC_NEWS_RADAR_DB`          | `./data/kc_news_radar.db`        | SQLite path                          |
| `KC_NEWS_RADAR_DEMO`        | `0`                              | `1` loads demo fixtures instead of live sources |
| `KC_NEWS_RADAR_HOST`        | `127.0.0.1`                      | Bind address for the web server      |
| `KC_NEWS_RADAR_PORT`        | `8765`                           | Web server port                      |
| `KC_NEWS_RADAR_TIMEOUT`     | `15.0`                           | Per-request HTTP timeout (seconds)   |
| `KC_NEWS_RADAR_USER_AGENT`  | `KCNewsRadar/0.1 (…)`            | User-Agent for all outbound HTTP     |

The database file is created on first use. Reopening it later preserves every
source item, signal, forecast version, evidence snapshot, resolution, and
feedback record. The default remains `data/kc_news_radar.db`, but startup
prints whether selection was explicit and the dashboard warns when the default,
stale collection evidence, or source-configuration drift is detected. For a
review or demonstration, always set `KC_NEWS_RADAR_DB` to the exact snapshot
you intend to inspect. Local database files are ignored and are never selected
by filename heuristics.

## Closed-loop editorial validation

Open **Forecast Ledger** and select a forecast claim. The detail workspace
shows the exact signal and source-record snapshots captured when the displayed
forecast version was issued. It never fills a historical gap with current
upstream content. Older ledger rows created before snapshot support say that
evidence is unavailable.

The same workspace accepts a constrained editorial feedback label and optional
note. An unresolved forecast can receive one outcome targeted at its displayed
latest version, with supporting evidence and optional notes. Resolution is
single-assignment: it creates a separate resolution record and never updates
the immutable forecast row.

The **Validation** tab reports resolved sample counts by outcome, scoring-model
version, and contributing signal type. Every view includes its denominator.
These are descriptive counts from recorded outcomes—not probabilities,
calibration, statistical significance, or evidence that one model is better.
With fewer than five resolved forecasts, the product explicitly labels the
sample insufficient for stable interpretation. See
`docs/VALIDATION_WORKFLOW.md` for the full evidence and safety contract.

---

## Documentation

- `docs/ARCHITECTURE.md` — module layout, data flow, storage schema
- `docs/DATA_SOURCES.md` — Tier-1 adapters, source URLs, and known failure modes
- `docs/EDITORIAL_SAFETY.md` — what the scores mean and, critically, don't
- `docs/DEMO.md` — running the demo without network access
- `docs/VALIDATION_WORKFLOW.md` — evidence, feedback, outcomes, and performance

---

## Isolation

This project lives entirely under `kc-news-radar/` and shares no code, no
database, no config, and no network state with any other project on the
same machine.
