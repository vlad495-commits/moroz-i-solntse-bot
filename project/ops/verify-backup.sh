#!/bin/sh
set -eu

if [ $# -ne 1 ]; then
  echo "usage: verify-backup.sh BACKUP.dump.enc" >&2
  exit 1
fi

if [ -z "${BACKUP_ENCRYPTION_KEY:-}" ]; then
  echo "BACKUP_ENCRYPTION_KEY is required" >&2
  exit 1
fi

backup="$1"
checksum="$backup.sha256"
tmp_dump="$(mktemp)"
trap 'rm -f "$tmp_dump"' EXIT

sha256sum -c "$checksum"
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -in "$backup" -out "$tmp_dump"
pg_restore --list "$tmp_dump" >/dev/null
echo "backup verification passed"
