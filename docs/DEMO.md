# Demo mode

Demo mode loads a deterministic set of synthetic fixtures into the SQLite
database so the whole system can be exercised offline (no network, no
live public-source dependencies).

Every synthetic record has its title prefixed with `[DEMO DATA]` so it
cannot be mistaken for a real public-source item.

## Running

```bash
# Set the demo flag and collect. This wipes the DB and inserts fixtures.
KC_NEWS_RADAR_DB=./data/demo.db KC_NEWS_RADAR_DEMO=1 kc-news-radar-collect

# Start the dashboard.
KC_NEWS_RADAR_DB=./data/demo.db KC_NEWS_RADAR_DEMO=1 kc-news-radar-run
# → open http://127.0.0.1:8765
```

You'll see something like:

```
run <uuid>: COMPLETED; sources=10/10 succeeded, failed=0; items=16; updated=0
pipeline: 35 signals, 7 forecast versions from 16 items
```

With `kc-news-radar-run`, the first automatic cycle loads fixtures when the
selected database is empty. Later scheduled demo cycles reuse only the marked
synthetic records, refresh the deterministic pipeline, and append run history;
they never contact live sources or mix real records into the demo database.

## What the demo exercises

The fixture set is small but deliberately chosen to hit every code path
that matters:

- **`SCHEDULED_CATALYST`** — a KCMO housing appropriation, a Johnson
  County BOCC meeting, and a Missouri House education bill all have
  `event_at` inside the 72-hour horizon.
- **`UNUSUAL_AGENDA_ITEM`** — the KCMO housing item includes an
  `emergency ordinance` + `$12,000,000` keyword hit; the JoCo items
  include a `$45 million` appropriation.
- **`SEVERE_WEATHER_CHANGE`** — an NWS Severe Thunderstorm Watch fixture
  with `severity=Severe`.
- **`REPEATED_ENTITY_ACTIVITY`** — three related JoCo items share a
  `raw_category=bocc` entity.
- **`MULTI_SOURCE_CONVERGENCE`** — near-duplicate titles across
  different sources (when applicable).
- **`COMMUNITY_311_TREND`** — six synthetic 311 items in the same
  category (`dangerous building`) and neighborhood (`Westport`) trigger
  both the category-spike and geographic-concentration variants.
- **`DEVELOPMENT_DEAL_ACTIVITY`** — a Council resolution fixture
  referencing "Royals stadium development agreement" + "Port KC" + "TIF
  financing" trips the development-deal detector.
- **Source health mix** — the demo shows all ten Tier-1 adapters as
  `HEALTHY` (matching the current production reality after the four
  legislative / transit / airport adapters were rewritten against their
  live public interfaces). The source-health tab's `DEGRADED` and
  `FAILED` states remain exercised by `tests/test_collectors.py`.

## Resetting

Demo mode wipes the selected database's `source_items`, `source_health`,
`signals`, `signal_evidence`, `forecasts`, evidence snapshots, `resolutions`,
and feedback before it inserts. It never searches for or modifies other local
database files. If
you switch back to live mode later, delete or move the DB file first if
you want a clean slate:

```bash
rm data/kc_news_radar.db
kc-news-radar-collect             # live mode
```

## Why demo data is safe by construction

- Every title starts with `[DEMO DATA]`.
- The frontend HTML-escapes every rendered string.
- The prefix is preserved through signals, forecast claims, and the
  Morning Strategy Brief.
- Demo mode is exclusive: you either load the fixtures (with `wipe →
  insert`) or you collect from live adapters. The two are never mixed
  in the same DB in the same run.
