#!/bin/sh
set -eu

BACKUP_INTERVAL_SECONDS=${BACKUP_INTERVAL_SECONDS:-86400}
case "$BACKUP_INTERVAL_SECONDS" in
  ''|*[!0-9]*|0)
    echo "BACKUP_INTERVAL_SECONDS must be a positive integer" >&2
    exit 1
    ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

while :; do
  sh "$SCRIPT_DIR/backup-postgres.sh"
  sleep "$BACKUP_INTERVAL_SECONDS"
done
