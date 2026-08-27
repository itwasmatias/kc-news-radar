#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /absolute/path/to/pilot.db" >&2
    exit 2
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
python_path="$repo_dir/.venv/bin/python"
db_path=$1

if [[ "$db_path" != /* ]]; then
    echo "database path must be absolute: $db_path" >&2
    exit 2
fi
if [[ ! -x "$python_path" ]]; then
    echo "missing virtualenv Python: $python_path" >&2
    echo "create .venv and install the project before installing the service" >&2
    exit 2
fi
if [[ "$db_path" == *$'\n'* || "$db_path" == *$'\r'* ]]; then
    echo "database path must not contain newlines" >&2
    exit 2
fi
if [[ "$db_path" == *'|'* || "$db_path" == *'&'* || "$repo_dir" == *'|'* || "$repo_dir" == *'&'* ]]; then
    echo "repository and database paths must not contain | or &" >&2
    exit 2
fi

config_dir="$HOME/.config/kc-news-radar"
unit_dir="$HOME/.config/systemd/user"
mkdir -p -- "$config_dir" "$unit_dir"

escaped_db=${db_path//\\/\\\\}
escaped_db=${escaped_db//\"/\\\"}
backup_dir="$(dirname -- "$db_path")/backups"
escaped_backup=${backup_dir//\\/\\\\}
escaped_backup=${escaped_backup//\"/\\\"}

if [[ ! -e "$config_dir/pilot.env" ]]; then
    {
        printf 'KC_NEWS_RADAR_DB="%s"\n' "$escaped_db"
        printf 'KC_NEWS_RADAR_BACKUP_DIR="%s"\n' "$escaped_backup"
        printf '%s\n' \
            'KC_NEWS_RADAR_BACKUP_RETENTION_COUNT=14' \
            'KC_NEWS_RADAR_COLLECTION_ENABLED=1' \
            'KC_NEWS_RADAR_COLLECTION_CADENCE_SECONDS=900' \
            'KC_NEWS_RADAR_STALE_AFTER_SECONDS=3600' \
            'KC_NEWS_RADAR_COLLECTION_LEASE_SECONDS=1800' \
            'KC_NEWS_RADAR_SHUTDOWN_GRACE_SECONDS=30' \
            'KC_NEWS_RADAR_HOST=127.0.0.1' \
            'KC_NEWS_RADAR_PORT=8765'
    } > "$config_dir/pilot.env"
    chmod 600 "$config_dir/pilot.env"
else
    echo "preserving existing configuration: $config_dir/pilot.env"
fi

sed \
    -e "s|@WORKING_DIRECTORY@|$repo_dir|g" \
    -e "s|@PYTHON@|$python_path|g" \
    "$repo_dir/ops/systemd/kc-news-radar.service.in" \
    > "$unit_dir/kc-news-radar.service"

systemctl --user daemon-reload
echo "installed: $unit_dir/kc-news-radar.service"
echo "configuration: $config_dir/pilot.env"
echo "not enabled or started; review configuration, then run:"
echo "  systemctl --user enable --now kc-news-radar.service"
