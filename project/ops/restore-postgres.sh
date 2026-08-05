#!/bin/sh
set -eu

require() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "$name is required" >&2
    exit 1
  fi
}

if [ $# -ne 1 ]; then
  echo "usage: restore-postgres.sh BACKUP.dump.enc" >&2
  exit 1
fi

require POSTGRES_DB
require POSTGRES_USER
require RESTORE_TARGET_DB
require BACKUP_ENCRYPTION_KEY

if [ "$RESTORE_TARGET_DB" = "$POSTGRES_DB" ]; then
  echo "RESTORE_TARGET_DB must differ from POSTGRES_DB" >&2
  exit 1
fi

backup="$1"
if [ ! -f "$backup" ]; then
  echo "backup file not found: $backup" >&2
  exit 1
fi

tmp_dump="$(mktemp)"
trap 'rm -f "$tmp_dump"' EXIT

openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -in "$backup" -out "$tmp_dump"
createdb --username "$POSTGRES_USER" "$RESTORE_TARGET_DB"
pg_restore --clean --if-exists --no-owner --no-acl --username "$POSTGRES_USER" --dbname "$RESTORE_TARGET_DB" "$tmp_dump"
psql --username "$POSTGRES_USER" --dbname "$RESTORE_TARGET_DB" --tuples-only --command "SELECT version_num FROM alembic_version;"
