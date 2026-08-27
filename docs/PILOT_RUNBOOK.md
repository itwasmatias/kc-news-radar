# Local pilot runbook

This runbook operates one KC News Radar process and one explicitly selected
SQLite database on a Fedora/PavilionOS host. The systemd service uses the
existing in-process 15-minute scheduler; it does not add a second scheduler.
The process also holds a lock keyed to the database path, so a second
`kc-news-radar-run` for the same database exits instead of becoming another
service instance.

## One-time setup

From the reviewed pilot checkout:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
mkdir -p /home/matias/kc-news-radar-pilot/data
scripts/install-user-service.sh /home/matias/kc-news-radar-pilot/data/pilot.db
```

The installer creates a user unit and, only when absent, a mode-0600
`~/.config/kc-news-radar/pilot.env`. It runs `daemon-reload` but deliberately
does not enable or start the service. Review that file before the first start.
Use absolute paths. Do not point pilot operation at `data/acceptance.db`,
`data/demo.db`, `data/kc_news_radar.db`, or any saved historical database.

The supported configuration keys are:

- `KC_NEWS_RADAR_DB`: explicit pilot SQLite path.
- `KC_NEWS_RADAR_BACKUP_DIR`: directory used only for Radar backups.
- `KC_NEWS_RADAR_BACKUP_RETENTION_COUNT`: successful backups retained; default 14.
- `KC_NEWS_RADAR_COLLECTION_ENABLED`: strict `1` or `0`; default `1`.
- `KC_NEWS_RADAR_COLLECTION_CADENCE_SECONDS`: completion-based cadence; default 900.
- `KC_NEWS_RADAR_STALE_AFTER_SECONDS`: stale evidence threshold; default 3600.
- `KC_NEWS_RADAR_COLLECTION_LEASE_SECONDS`: cross-process collection lease; default 1800.
- `KC_NEWS_RADAR_SHUTDOWN_GRACE_SECONDS`: bounded scheduler stop grace; default 30.
- `KC_NEWS_RADAR_HOST` and `KC_NEWS_RADAR_PORT`: local dashboard bind; defaults `127.0.0.1:8765`.

## Service operation

```bash
systemctl --user enable --now kc-news-radar.service
systemctl --user stop kc-news-radar.service
systemctl --user restart kc-news-radar.service
systemctl --user status kc-news-radar.service
journalctl --user -u kc-news-radar.service -n 200 --no-pager
journalctl --user -u kc-news-radar.service -f
```

The unit restarts after unexpected failure, waits 15 seconds between attempts,
and allows at most five starts in five minutes. Normal operation needs no root.
For operation after logout, the owner may enable user lingering once with the
host administrator: `loginctl enable-linger matias`.

On `systemctl stop`, SIGTERM first prevents any new cycle from starting. An
active bounded cycle gets the configured grace period. If it finishes, its
real terminal state is committed. If the process must exit first, no success is
invented: the committed `RUNNING` event and any committed per-source rows remain,
and startup recovery later appends `ABANDONED` after its lease is no longer valid.
The unit's stop timeout covers the maximum supported 300-second application
grace plus shutdown overhead; the default application grace remains 30 seconds.

## Status and health

Load the same environment as the service, then run either human-readable or
JSON health:

```bash
set -a
. "$HOME/.config/kc-news-radar/pilot.env"
set +a
.venv/bin/python -m kc_news_radar.operations health
.venv/bin/python -m kc_news_radar.operations health --json
```

Exit code 0 means the supported service process lock is active, the database
passes SQLite `quick_check`, completed evidence is fresh, enabled scheduling is
responsive, the latest attempt is not failed or abandoned, and no source is
failing, stale, or degraded. Exit code 2 means the
command completed but operational health is not healthy. A running process alone
does not make the result healthy. The served endpoints are:

```text
GET http://127.0.0.1:8765/api/health
GET http://127.0.0.1:8765/api/collection/status
GET http://127.0.0.1:8765/api/collection/runs
```

`FRESH` means the newest completed evidence is within the configured threshold.
`FRESH_PARTIAL_FAILURE` means the cycle completed but at least one source failed.
`STALE` means the last usable completed evidence is too old.
`NO_COMPLETED_RUN` means there is no completed cycle to treat as current.
Per-source `FAILING`, `STALE`, and `DEGRADED` states are non-healthy; inspect the
source message and run history. `FRESH_ZERO_ITEMS` is a successful empty fetch,
not proof that a source published nothing of editorial significance.

`BLOCKED_OVERLAP` means a requested cycle did no collector work because another
unexpired database lease existed. It remains in history. `ABANDONED` means a
previous `RUNNING` cycle lost a valid lease before it could prove completion;
its outcome is uncertain, committed source evidence is preserved, and Radar does
not blindly replay it. A later normal scheduled cycle is new work, not a rewrite.

## Manual collection and safe disablement

```bash
.venv/bin/python -m kc_news_radar.collect
.venv/bin/python -m kc_news_radar.collect --source nws_kc --verbose
```

Manual and scheduled collection share the same SQLite lease. To disable new
automatic cycles while keeping the dashboard available, set
`KC_NEWS_RADAR_COLLECTION_ENABLED=0` in `pilot.env`, then run:

```bash
systemctl --user restart kc-news-radar.service
```

Confirm `Automatic collection: DISABLED` with the health command. Do not start a
separate timer or cron job to replace the built-in cadence.

## Backup and retention

The backup command reads the live source through SQLite's online backup API,
writes a standalone database, reopens it read-only, runs `quick_check`, reports
both paths, and then retains the newest configured number of successful Radar
backup files belonging to the current source database stem. Backups for other
Radar databases sharing the directory, unrelated files, and partial files are
not retention candidates.

```bash
.venv/bin/python -m kc_news_radar.operations backup
.venv/bin/python -m kc_news_radar.operations backup --retain 30
```

Backups are named with the source stem and a UTC timestamp under the configured
backup directory. A nonzero exit means no verified backup should be assumed.
Inspect service/command logs for `SQLite backup succeeded` or the failure.

## Restore

Restore is intentionally a stopped-service operator procedure, not an automatic
action:

1. Stop the service and confirm it is inactive.
2. Run one final backup of the current pilot DB if it is readable.
3. Run the health command against the chosen backup by temporarily setting
   `KC_NEWS_RADAR_DB` to its exact path; require `Database quick_check: ok`.
4. Move the current pilot DB and any same-name `-wal`/`-shm` sidecars into a new,
   explicitly named quarantine directory. Do not delete them.
5. Copy the verified standalone backup to the exact configured pilot DB path.
6. Start the service and check health, run history, database path, and logs.

Never restore over or mutate an acceptance, demo, historical, or saved pilot
database. Do not copy the live database file while Radar is running; use the
backup command.

## Reboot/crash recovery

The enabled user service starts after reboot (with user lingering where needed).
Startup checks all committed `RUNNING` runs. A run whose lease is absent or
expired receives a new append-only `ABANDONED` event; already committed source
results remain. Because the new service first proves exclusive ownership of the
per-database service lock, it can also immediately abandon a still-valid lease
identified as belonging to the prior crashed service. A still-valid manual
collector lease is not guessed dead and can temporarily block a new cycle until
expiry. Health and run history expose the latest startup recovery and abandoned
run. Completion-based scheduling performs at most one immediate cycle on restart
and does not replay missed cadence intervals.

## Known limitations

- Single Fedora/PavilionOS host only; no distributed leader election or cloud HA.
- Localhost API has no authentication. Do not expose it to an untrusted network.
- A long in-flight source request is allowed only the configured shutdown grace;
  forced exit leaves truthful incomplete evidence for later abandonment.
- Manual collection lease validity is time-based. After a separate manual
  collector crash, a still-valid lease is honored until expiry rather than
  risking blind replay.
- Backup scheduling is an operator/systemd-timer decision; this milestone adds
  the safe command but does not silently create a timer.
- No notifications, smarter forecasting, or Morning Newsroom Snapshot are added.
