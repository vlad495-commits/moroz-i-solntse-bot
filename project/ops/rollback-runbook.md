# Rollback Runbook

Rollback app containers to the previous image first. Treat schema as forward-only unless a fresh backup exists and restore has been rehearsed.

```bash
set -eu
. /opt/moroz-release-state/rollback-images.env
export BOT_IMAGE="${PREVIOUS_BOT_IMAGE:?set immutable previous bot image}"
export WORKER_IMAGE="${PREVIOUS_WORKER_IMAGE:?set immutable previous worker image}"
export SCHEDULER_IMAGE="${PREVIOUS_SCHEDULER_IMAGE:?set immutable previous scheduler image}"
export ADMIN_IMAGE="${PREVIOUS_ADMIN_IMAGE:?set immutable previous admin image}"
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build bot worker scheduler admin
pwsh ./ops/smoke.ps1
```

The four exported values are the exact image references Compose reads from `docker-compose.prod.yml`; record immutable tags or digests before every release. After the incident command, unset `BOT_IMAGE`, `WORKER_IMAGE`, `SCHEDULER_IMAGE`, and `ADMIN_IMAGE` before any normal candidate deployment.

Do not run destructive downgrade without backup evidence and an explicit incident decision. If data restore is required, restore into a separate database first:

```bash
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec -e RESTORE_TARGET_DB=moroz_restore postgres sh /ops/restore-postgres.sh /backups/postgres/<file>.dump.enc
```

Only after inspection may the operator decide how to swap traffic or databases.
