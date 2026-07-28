# Rollback Runbook

Rollback app containers to the previous image first. Treat schema as forward-only unless a fresh backup exists and restore has been rehearsed.

```bash
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml stop bot worker scheduler admin
docker image tag previous-bot-image moroz-bot:rollback
docker image tag previous-worker-image moroz-worker:rollback
docker image tag previous-scheduler-image moroz-scheduler:rollback
docker image tag previous-admin-image moroz-admin:rollback
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml up -d bot worker scheduler admin
pwsh ./ops/smoke.ps1
```

Do not run destructive downgrade without backup evidence and an explicit incident decision. If data restore is required, restore into a separate database first:

```bash
RESTORE_TARGET_DB=moroz_restore docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml exec postgres sh /ops/restore-postgres.sh /backups/postgres/<file>.dump.enc
```

Only after inspection may the operator decide how to swap traffic or databases.
