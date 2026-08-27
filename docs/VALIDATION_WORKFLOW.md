# Closed-loop editorial validation

## What a forecast means

A forecast is an immutable, versioned statement that a public development may
advance within its stated horizon. Its likelihood and editorial-relevance
scores are deterministic heuristic totals, not probabilities. The radar helps
an editor decide what to inspect; it does not assign reporters or make claims
about individuals.

## Inspecting evidence

Select a claim in **Forecast Ledger**. For forecast versions issued by the
closed-loop pipeline, the detail workspace shows:

- each contributing signal and the material summary that triggered it;
- each supporting public-source record, its source and canonical URL;
- when the record was retrieved, first observed, and last observed at issuance;
- the content hash that identifies the persisted snapshot; and
- explicit evidence limitations.

These are immutable issuance-time snapshots. Current signal rows are
recomputed, and current source rows can change, so neither is used to recreate
historical evidence. A forecast created before snapshot support is labeled
`LEGACY_NOT_CAPTURED`. Missing fields remain missing. Private metadata fields,
including the retained internal 311 street-address field, are not copied to the
dashboard evidence snapshot.

## Editorial feedback

The forecast detail workspace accepts one of a constrained set of newsroom
labels such as `USEFUL`, `WATCH`, `ALREADY_KNEW`, `NOT_NEWSWORTHY`, or
`INCORRECT`, plus an optional short note. Feedback is append-only and does not
change the forecast or its score. It records how the newsroom experienced the
radar output.

## Recording an outcome

An unresolved forecast may receive one outcome:

- `CONFIRMED`
- `NOT_OCCURRED`
- `AMBIGUOUS`
- `EXPIRED_UNRESOLVED`

The request names the latest forecast version explicitly and requires
supporting evidence or an explanatory record. Optional notes can preserve
editorial context. The outcome is stored separately from the forecast. The
original version remains byte-for-byte unchanged. Duplicate resolution or an
attempt to resolve a non-latest version fails closed; v0.1 provides no silent
overwrite or correction operation.

## Performance summary

The **Validation** tab counts only outcomes with an explicit target version. It
groups those counts by outcome, scoring-model version, and contributing signal
type, and always displays the resolved denominator. It does not call a
confirmation rate “accuracy,” treat a score as a probability, claim
calibration, or infer statistical significance. Zero outcomes are reported as
no resolved evidence; fewer than five are labeled insufficient for stable
interpretation.

## Selecting a database safely

Choose the database explicitly for both collection and serving:

```bash
KC_NEWS_RADAR_DB=./data/review-2026-08-26.db kc-news-radar-collect
KC_NEWS_RADAR_DB=./data/review-2026-08-26.db kc-news-radar-serve
```

For a deterministic synthetic demonstration:

```bash
KC_NEWS_RADAR_DB=./data/demo.db KC_NEWS_RADAR_DEMO=1 kc-news-radar-collect
KC_NEWS_RADAR_DB=./data/demo.db kc-news-radar-serve
```

Startup prints the resolved database path and whether it was explicitly
selected. The dashboard warns about implicit default selection, stale
collection evidence, or collector-name drift. No existing database is chosen
because it looks newer, and demo mode modifies only the selected database.
