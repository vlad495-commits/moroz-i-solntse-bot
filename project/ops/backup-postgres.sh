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
raw="$(mktemp /tmp/moroz-backup.dump.XXXXXX)"
run_suffix="$(basename "$raw" | sed 's/^moroz-backup\.dump\.//')"
encrypted="$BACKUP_DIR/$POSTGRES_DB-$timestamp-$run_suffix.dump.enc"
encrypted_partial="$BACKUP_DIR/.$POSTGRES_DB-$timestamp-$run_suffix.dump.enc.partial"
checksum="$encrypted.sha256"
checksum_partial="$BACKUP_DIR/.$POSTGRES_DB-$timestamp-$run_suffix.dump.enc.sha256.partial"
published=0

cleanup() {
  rm -f "$raw" "$encrypted_partial" "$checksum_partial"
  if [ "$published" -ne 1 ]; then
    rm -f "$encrypted" "$checksum"
  fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

pg_dump --format=custom --no-owner --no-acl --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$raw"
openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENCRYPTION_KEY -in "$raw" -out "$encrypted_partial"
mv "$encrypted_partial" "$encrypted"
sha256sum "$encrypted" > "$checksum_partial"
mv "$checksum_partial" "$checksum"

find "$BACKUP_DIR" -name "$POSTGRES_DB-*.dump.enc" -type f -mtime "+$BACKUP_RETENTION_DAYS" -delete
find "$BACKUP_DIR" -name "$POSTGRES_DB-*.dump.enc.sha256" -type f -mtime "+$BACKUP_RETENTION_DAYS" -delete
find "$BACKUP_DIR" -name ".$POSTGRES_DB-*.partial" -type f -mtime +0 -delete

published=1
echo "$encrypted"
