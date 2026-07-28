# Postgres Backup And Restore

Backups are encrypted custom-format `pg_dump` artifacts. Keep `BACKUP_ENCRYPTION_KEY` only on the server or in the operator password vault, never in Git or logs.

## Backup

```bash
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec postgres sh /ops/backup-postgres.sh
```

Required env: `POSTGRES_DB`, `POSTGRES_USER`, `PGPASSWORD`, `BACKUP_ENCRYPTION_KEY`.

## Verify

```bash
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec postgres sh /ops/verify-backup.sh /backups/postgres/<file>.dump.enc
```

Verification checks the checksum, decrypts to a temporary file and asks `pg_restore --list` to parse the archive.

## Restore Drill

Restore only into a separate database first:

```bash
RESTORE_TARGET_DB=moroz_restore docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec postgres sh /ops/restore-postgres.sh /backups/postgres/<file>.dump.enc
```

Never set `RESTORE_TARGET_DB` equal to `POSTGRES_DB`. Swap or production recovery is a manual incident decision after the restored database is inspected.
