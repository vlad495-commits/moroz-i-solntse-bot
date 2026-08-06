#!/bin/sh
set -eu

BACKUP_DIR=/backups/postgres
mkdir -p "$BACKUP_DIR"
chown -R postgres:postgres "$BACKUP_DIR"
chmod 0770 "$BACKUP_DIR"
find /tmp -maxdepth 1 -name 'moroz-backup.*' -type f -delete

exec gosu postgres "$@"
