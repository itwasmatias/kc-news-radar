# Editorial Safety

This document is the load-bearing contract between the KC News Radar and
the newsroom that uses it. If any section here conflicts with how a score
is being used in practice, the score is being used wrong.

## The scores are not probabilities

The `experimental_likelihood_score` and `editorial_relevance_score` are
**0–100 heuristic scores**, not calibrated probabilities. Nothing in the
system calibrates them against real-world outcome frequencies. Every API
response and every UI card carries the label:

> Experimental scores are not calibrated probabilities.

If a downstream user (or a downstream model) starts treating these as
probabilities, the resulting decisions inherit no statistical warranty
whatsoever. The scoring model version (`heuristic-v0.1`) is embedded in
every stored forecast so that historical rows remain interpretable.

## What the two scores mean

- **Experimental likelihood** — a sum of positive component weights that
  correspond to observed public-signal features (a scheduled event within
  72h, an unusual agenda match, multi-source convergence, etc.). It is a
  *transparent aggregate*, not a probability.
- **Editorial relevance** — public-service importance, independent of
  whether the story advances. Based on beat, fiscal magnitude,
  multi-jurisdiction reach, safety, accountability, and infrastructure
  impact. Not clicks. Not outrage. Not partisan advantage.

`priority_score = round(0.45 * likelihood + 0.55 * relevance)`. Relevance
is weighted slightly higher because a newsroom would rather triage a
highly-relevant less-likely development than the reverse.

## What the radar does not do

- **No claims about individuals.** Forecast claims are phrased around
  *public processes*, not people ("Agenda item with unusual magnitude may
  advance…", not "Alderman X will vote yes"). Signal summaries follow the
  same rule.
- **No reporter assignments.** The Morning Strategy Brief's `QUESTIONS`
  block is phrased as public-process discussion prompts ("Does today's
  local-government development merit advance reporting: …?"), never as
  "assign this to reporter X."
- **No 311-as-fact.** 311 items are aggregated into pattern signals
  (`COMMUNITY_311_TREND`), never surfaced as claims about the underlying
  property or resident. Every 311-derived summary is explicitly labeled
  as a resident-reported pattern. Street-level location data never
  reaches dashboard-visible fields.
- **No person-level immigration data.** See `docs/DATA_SOURCES.md`. If
  and when EOIR ingestion is enabled, only aggregate docket trends are
  eligible to surface.
- **No LLM in the scoring path.** Every score is a deterministic sum of
  documented components. Rerunning `run_pipeline` on the same DB produces
  the same scores.

## Auditability

The forecast ledger is append-only. Once a forecast row is written for a
given `(forecast_id, version)`, it is never mutated. If the underlying
public-signal profile shifts, a new version is appended. This means:

- A newsroom manager can go back and see exactly what the radar said,
  when, and how much its assessment moved between versions.
- Resolutions live in a separate table and never touch the forecast row.
- Explanation JSON is stored with each version, so the "why" travels with
  the score.

## When to disable a signal

If a detector begins surfacing items that violate any rule above (person-
level profiling, mislabeled 311 as fact, treating a heuristic score as a
probability), open a task and disable the detector wiring in
`pipeline/signals.py` before shipping the fix. Silent behavior is worse
than a missing signal.

## Editorial judgment remains with the newsroom

The radar is a *scanning aid*, not an editor. Every priority ranking is
an input to a human editorial decision, not a substitute for one.
