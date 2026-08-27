# Automatic and observable collection

## Operator command

Start the dashboard and automatic worker together against an explicitly chosen
database:

```bash
KC_NEWS_RADAR_DB=./data/live.db kc-news-radar-run
```

Startup prints the resolved database path, whether selection was explicit,
dashboard address, enabled state, cadence, stale threshold, and next expected
cycle. The default cadence is 900 seconds (15 minutes). The first cycle is due
immediately. Each later cycle is scheduled from the previous cycle's
completion, preventing tight retries or a backlog of catch-up executions.

Manual collection remains available and observable:

```bash
KC_NEWS_RADAR_DB=./data/live.db kc-news-radar-collect
```

Disable automatic cycles without changing code:

```bash
KC_NEWS_RADAR_DB=./data/live.db \
KC_NEWS_RADAR_COLLECTION_ENABLED=0 \
kc-news-radar-run
```

`kc-news-radar-serve` remains a dashboard-only compatibility command. It does
not start a worker; the Collection view warns when automatic collection is
configured but no responsive worker is evidenced.

## Scheduler architecture

The scheduler is one small daemon thread in the local FastAPI process. It has
no Redis, Celery, cloud scheduler, or external database. The thread controls
cadence only. Correctness comes from the SQLite lease shared by every caller.

The worker stores an operational heartbeat and next expected cycle in
`scheduler_state`. This is a mutable status projection, not proof a collection
occurred. Append-only run events are the authority for execution history.

## Run records

Every request gets a UUID and an immutable row in `collection_runs` containing
its trigger (`MANUAL`, `SCHEDULED`, or `DEMO`), requested time, and scheduled
time where applicable. State changes append to `collection_run_events`:

- `RUNNING`
- `COMPLETED`
- `PARTIAL_FAILURE`
- `FAILED`
- `ABANDONED`
- `BLOCKED_OVERLAP`

Terminal events record attempted/succeeded/failed source counts, fetched and
updated item counts, pipeline summary, failure summary, and whether the cycle
completed cleanly. `collection_source_results` records each source's outcome,
status, item count, latency, and failure category. Historical events and source
results are never updated.

Inspect them through:

```text
GET /api/collection/status
GET /api/collection/runs?limit=20
GET /api/collection/runs/{run_id}
```

The API and mutation endpoints retain the existing local-machine trust
boundary. There is no authentication in this milestone and the server binds to
`127.0.0.1` by default.

## Overlap protection

Before touching a source, a cycle opens `BEGIN IMMEDIATE` and attempts to own
the singleton `collection_lease`. A non-expired lease causes the new request to
record `BLOCKED_OVERLAP`, including the blocking run ID, and return without
executing a collector or pipeline. This protects scheduler-vs-scheduler,
manual-vs-scheduled, and separate-process invocations. The lease is refreshed
after every bounded source attempt and defaults to 1,800 seconds.

## Failure and interruption semantics

A normal source failure becomes a per-source `FAILED` result. `FetchError` is
classified as `SOURCE_UNAVAILABLE`; other escaped adapter errors are
`ADAPTER_FAILURE`. Other collectors continue. A successful fetch with zero
items is `ZERO_ITEMS`, with the collector's `HEALTHY` or `DEGRADED` status
preserved, and is not mislabeled as an unavailable source.

If pipeline or orchestration code fails, the run records `FAILED` and releases
its lease where possible. A later scheduled cycle remains eligible.

If the process disappears, already committed source rows and earlier immutable
forecast/evidence rows remain valid. The run has no success event. While its
lease remains unexpired, another invocation records an overlap block rather
than guessing that external work is safe to repeat. Once the lease expires,
startup recovery or the next acquisition appends `ABANDONED`; it never rewrites
the earlier `RUNNING` event or blindly replays the uncertain cycle.

## Freshness

Freshness comes from the completion time of the latest persisted `COMPLETED` or
`PARTIAL_FAILURE` run, not browser load time. The default stale threshold is
3,600 seconds (one hour). States are:

- `FRESH`
- `FRESH_PARTIAL_FAILURE`
- `STALE`
- `NO_COMPLETED_RUN`

Per-source states use the latest persisted source-run result and distinguish
`FRESH`, `FRESH_ZERO_ITEMS`, `DEGRADED`, `FAILING`, and `STALE`. The dashboard
shows last request, attempt, completion, successful cycle, evidence age,
current lease, next cycle, and explicit warnings.

## Configuration and limits

- `KC_NEWS_RADAR_COLLECTION_ENABLED` — strict boolean, default `1`
- `KC_NEWS_RADAR_COLLECTION_CADENCE_SECONDS` — 30–86,400, default `900`
- `KC_NEWS_RADAR_STALE_AFTER_SECONDS` — 60–604,800, default `3600`
- `KC_NEWS_RADAR_COLLECTION_LEASE_SECONDS` — 60–86,400, default `1800`
- `KC_NEWS_RADAR_DB` — exact database path; explicit selection is recommended

This is a single-host service. It does not provide distributed leader
election, notifications, browser automation, remote authentication, or a
guarantee that the host itself is continuously available.
