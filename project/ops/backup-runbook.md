# Postgres Backup And Restore

Backups are encrypted custom-format `pg_dump` artifacts. Keep `BACKUP_ENCRYPTION_KEY` only on the server or in the operator password vault, never in Git or logs.

## Automated daily backup

Production Compose runs the `backup` service continuously. It creates one backup immediately after PostgreSQL becomes healthy and then every `BACKUP_INTERVAL_SECONDS` (default `86400`, once a day). Confirm the service and the newest encrypted artifact:

The unencrypted custom dump exists only in the backup container's tmpfs. Its configurable cap is `BACKUP_TMPFS_SIZE` (default `1g`); keep it above the observed `pg_dump` size and monitor backup failures as the database grows. Encrypted partial files use unique names on the backup volume and are never treated as completed artifacts.

```bash
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml ps backup
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml logs --tail=50 backup
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec backup ls -l /backups/postgres
```

The commands below are manual drill/incident controls; they are not the daily scheduler.

## Backup manually

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
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec -e RESTORE_TARGET_DB=moroz_restore postgres sh /ops/restore-postgres.sh /backups/postgres/<file>.dump.enc
```

Never set `RESTORE_TARGET_DB` equal to `POSTGRES_DB`. Swap or production recovery is a manual incident decision after the restored database is inspected.
