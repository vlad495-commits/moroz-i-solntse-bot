# Deploy Runbook

No push or merge is part of this runbook. Source state must already be reviewed and intentionally delivered to the server.

```bash
set -eu
cd /opt/moroz-i-solntse-bot/project

PREVIOUS_RELEASE="$(date -u +%Y%m%dT%H%M%SZ)"
PREVIOUS_BOT_IMAGE="moroz-i-solntse-bot:previous-${PREVIOUS_RELEASE}"
PREVIOUS_WORKER_IMAGE="moroz-i-solntse-worker:previous-${PREVIOUS_RELEASE}"
PREVIOUS_SCHEDULER_IMAGE="moroz-i-solntse-scheduler:previous-${PREVIOUS_RELEASE}"
PREVIOUS_ADMIN_IMAGE="moroz-i-solntse-admin:previous-${PREVIOUS_RELEASE}"

BOT_CONTAINER="$(docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml ps -q bot)"
WORKER_CONTAINER="$(docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml ps -q worker)"
SCHEDULER_CONTAINER="$(docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml ps -q scheduler)"
ADMIN_CONTAINER="$(docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml ps -q admin)"
test -n "$BOT_CONTAINER"
test -n "$WORKER_CONTAINER"
test -n "$SCHEDULER_CONTAINER"
test -n "$ADMIN_CONTAINER"

docker image tag "$(docker inspect --format '{{.Image}}' "$BOT_CONTAINER")" "$PREVIOUS_BOT_IMAGE"
docker image tag "$(docker inspect --format '{{.Image}}' "$WORKER_CONTAINER")" "$PREVIOUS_WORKER_IMAGE"
docker image tag "$(docker inspect --format '{{.Image}}' "$SCHEDULER_CONTAINER")" "$PREVIOUS_SCHEDULER_IMAGE"
docker image tag "$(docker inspect --format '{{.Image}}' "$ADMIN_CONTAINER")" "$PREVIOUS_ADMIN_IMAGE"

install -d -m 700 /opt/moroz-release-state
umask 077
ROLLBACK_STATE=/opt/moroz-release-state/rollback-images.env
ROLLBACK_TMP="${ROLLBACK_STATE}.tmp.$$"
trap 'rm -f "$ROLLBACK_TMP"' EXIT
cat > "$ROLLBACK_TMP" <<EOF
PREVIOUS_BOT_IMAGE=$PREVIOUS_BOT_IMAGE
PREVIOUS_WORKER_IMAGE=$PREVIOUS_WORKER_IMAGE
PREVIOUS_SCHEDULER_IMAGE=$PREVIOUS_SCHEDULER_IMAGE
PREVIOUS_ADMIN_IMAGE=$PREVIOUS_ADMIN_IMAGE
EOF
mv "$ROLLBACK_TMP" "$ROLLBACK_STATE"
trap - EXIT

git pull --ff-only
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml --profile ops run --rm ops-check
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml --profile migration run --rm migrate alembic -c /app/alembic.ini upgrade head
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml build bot worker scheduler admin backup
sudo sh ./ops/prepare-runtime-dirs.sh
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis rabbitmq bot worker scheduler admin backup caddy
pwsh ./ops/smoke.ps1
```

The deploy stops before `git pull` or build if any current app container/image is missing. Record `/opt/moroz-release-state/rollback-images.env`, migration head, smoke output and operator name in the release notes.
