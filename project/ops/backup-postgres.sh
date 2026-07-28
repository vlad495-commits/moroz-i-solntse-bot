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

require POSTGRES_DB
require POSTGRES_USER
require BACKUP_ENCRYPTION_KEY

BACKUP_DIR="${BACKUP_DIR:-/backups/postgres}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
raw="$BACKUP_DIR/$POSTGRES_DB-$timestamp.dump"
encrypted="$raw.enc"

pg_dump --format=custom --no-owner --no-acl --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$raw"
openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENCRYPTION_KEY -in "$raw" -out "$encrypted"
sha256sum "$encrypted" > "$encrypted.sha256"
rm -f "$raw"

find "$BACKUP_DIR" -name "$POSTGRES_DB-*.dump.enc" -type f -mtime "+$BACKUP_RETENTION_DAYS" -delete
find "$BACKUP_DIR" -name "$POSTGRES_DB-*.dump.enc.sha256" -type f -mtime "+$BACKUP_RETENTION_DAYS" -delete

echo "$encrypted"
