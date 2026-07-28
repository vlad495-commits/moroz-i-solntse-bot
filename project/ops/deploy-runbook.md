# Deploy Runbook

No push or merge is part of this runbook. Source state must already be reviewed and intentionally delivered to the server.

```bash
cd /opt/moroz-i-solntse-bot/project
git pull --ff-only
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml --profile ops run --rm ops-check
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml --profile migration run --rm migrate alembic -c /app/alembic.ini upgrade head
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml build bot worker scheduler admin
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis rabbitmq bot worker scheduler admin caddy
pwsh ./ops/smoke.ps1
```

Record image IDs, migration head, smoke output and operator name in the release notes.
