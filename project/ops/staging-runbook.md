# Staging runbook

Этот runbook выполняют только в изолированном checkout `/opt/moroz-staging` на текущем VPS. Он не разрешает действия над production или общими контейнерами, ingress, credentials и доменами. Рабочая папка для команд — `/opt/moroz-staging/project`, защищённый файл настроек — `/opt/moroz-staging/.env`, временное состояние — только `/opt/moroz-staging/tmp/`.

## 1. Fail-closed prerequisites

До начала должны быть выделены отдельные staging bot ID/token, `STAGING_DOMAIN` и DNS-запись, доступ к серверу, отдельный staging LLM credential и права на Docker daemon. Оператор должен владеть `/opt/moroz-staging`, а на диске должно быть не меньше 2 GiB свободного места.

```bash
cd /opt/moroz-staging/project
test "$(git rev-parse --show-toplevel)" = /opt/moroz-staging
test "$(realpath ..)" = /opt/moroz-staging
test -O /opt/moroz-staging
test "$(df -Pk /opt/moroz-staging | awk 'NR==2 {print $4}')" -ge 2097152
docker info >/dev/null
```

Остановиться с blocker, если checkout, владелец, bot identity, credential, DNS, Docker или место не подтверждены. Никакие staging-команды ниже не выполнять с production bot token, доменом или credentials.

## 2. Read-only inventory

Задать в текущей сессии только несекретный `STAGING_DOMAIN`, затем выполнить инвентаризацию. Не печатать и не дампить environment.

```bash
cd /opt/moroz-staging/project
if test -f ../.env; then
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml ls
fi
docker ps --format '{{.Names}} {{.Status}} {{.Ports}}'
ss -ltn
getent ahostsv4 "$STAGING_DOMAIN"
test "$(realpath ..)" = /opt/moroz-staging
```

Если `.env` ещё отсутствует, Compose inventory здесь пропускается и обязательно повторяется после раздела 3. Остановиться с identity, DNS или port blocker при любой неоднозначности. Не останавливать и не перенастраивать найденные production/shared процессы.

## 3. Protected secrets

На первом развёртывании создать файл только на сервере. Команды пишут случайные значения прямо в защищённый `.env`; значения не передаются через аргументы, не выводятся и не копируются в evidence.

```bash
cd /opt/moroz-staging/project
test ! -e /opt/moroz-staging/.env
umask 077
install -m 600 /dev/null /opt/moroz-staging/.env
printf 'POSTGRES_PASSWORD=' >> /opt/moroz-staging/.env
openssl rand -hex 32 >> /opt/moroz-staging/.env
printf '\nREDIS_PASSWORD=' >> /opt/moroz-staging/.env
openssl rand -hex 32 >> /opt/moroz-staging/.env
printf '\nRABBITMQ_PASSWORD=' >> /opt/moroz-staging/.env
openssl rand -hex 32 >> /opt/moroz-staging/.env
printf '\nTELEGRAM_WEBHOOK_SECRET=' >> /opt/moroz-staging/.env
openssl rand -hex 32 >> /opt/moroz-staging/.env
printf '\n' >> /opt/moroz-staging/.env
chmod 600 /opt/moroz-staging/.env
```

Отдельно через защищённый редактор добавить staging bot ID/token, `STAGING_DOMAIN`, `STAGING_PUBLIC_URL`, staging LLM credential, имена пользователей и внутренние connection settings. Не читать файл в терминал. Если `.env` уже существует, пропустить генерацию, проверить `chmod 600` и менять только нужные поля через защищённый редактор. Неполный или общий credential — blocker.

## 4. Config, build и image evidence

```bash
cd /opt/moroz-staging/project
export STAGING_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml ls
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml config --quiet bot worker migrate postgres redis rabbitmq caddy staging-webhook staging-smoke
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml build bot worker migrate
docker image inspect --format '{{.Config.User}}' "moroz-staging-bot:${STAGING_IMAGE_TAG}" "moroz-staging-worker:${STAGING_IMAGE_TAG}" "moroz-staging-migrate:${STAGING_IMAGE_TAG}"
docker image inspect --format '{{.Id}}' "moroz-staging-bot:${STAGING_IMAGE_TAG}" "moroz-staging-worker:${STAGING_IMAGE_TAG}" "moroz-staging-migrate:${STAGING_IMAGE_TAG}"
```

Сохранить commit, три image ID/digest и `.Config.User`, но не полный inspect. Пустой/неожиданный user, config failure или build failure — blocker.

## 5. Stores и migration

```bash
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml up -d postgres redis rabbitmq
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile migration run --rm migrate alembic upgrade head
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile migration run --rm migrate alembic current
```

Записать только Alembic head identifier. Ошибка migration или current не на head — blocker; схему назад не менять.

## 6. Ingress decision

Если inventory подтвердил, что TCP 80/443 свободны, сначала проверить собственный Caddy contract:

```bash
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-ingress run --rm --no-deps caddy validate --config /etc/caddy/Caddyfile
```

Если 80/443 заняты текущим ingress, не останавливать его. Сохранить кандидат только в `/opt/moroz-staging/tmp/Caddyfile.candidate`, проверить штатной validation-командой владельца ingress и добавить маршрут без остановки сервиса:

```caddy
{$STAGING_DOMAIN} {
	@telegram_webhook path /telegram/webhook

	handle @telegram_webhook {
		reverse_proxy 127.0.0.1:18081
	}

	handle {
		respond 404
	}
}
```

Если существующий ingress не может безопасно принять этот проверенный contract или его ownership неясен, остановиться с ingress blocker.

## 7. Apps, health и HTTPS

```bash
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml up -d --wait --wait-timeout 120 bot worker
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml ps --status running bot worker postgres redis rabbitmq
curl --fail --silent --show-error http://127.0.0.1:18081/openapi.json >/dev/null
```

При свободных 80/443 после healthy bot запустить собственный ingress:

```bash
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-ingress up -d --wait --wait-timeout 120 caddy
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml ps --status running caddy
```

Для собственного или существующего ingress проверить TLS, fallback `404` и webhook `403` с заведомо несекретным sentinel header:

```bash
test "$(curl --proto '=https' --tlsv1.2 --silent --show-error --output /dev/null --write-out '%{http_code}' "https://${STAGING_DOMAIN}/staging-unrelated-sentinel")" = 404
test "$(curl --proto '=https' --tlsv1.2 --silent --show-error --output /dev/null --write-out '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'X-Telegram-Bot-Api-Secret-Token: staging-invalid-sentinel' --data '{}' "https://${STAGING_DOMAIN}/telegram/webhook")" = 403
```

Timeout, unhealthy service, TLS failure или иной status — blocker.

## 8. Telegram webhook lifecycle

`staging-webhook` проверяет dedicated bot identity до Telegram Bot API операций `getWebhookInfo`, `setWebhook` и `deleteWebhook`. До первого `setWebhook` безопасный status с `action:status` и `ok:false` означает ожидаемое несовпадение, а не blocker. Любой `error_type`, иной action или иной exit code — blocker. Стандартная установка не очищает pending updates.

```bash
cd /opt/moroz-staging/project
set +e
webhook_status_json="$(
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-webhook status
)"
webhook_status_rc=$?
set -e
if test "$webhook_status_rc" -ne 0 && test "$webhook_status_rc" -ne 1; then
  unset webhook_status_json webhook_status_rc
  printf '%s\n' 'staging webhook initial status blocker' >&2
  exit 1
fi
case "$webhook_status_json" in
  *'"error_type"'*)
    unset webhook_status_json webhook_status_rc
    printf '%s\n' 'staging webhook initial status blocker' >&2
    exit 1
    ;;
  *'"action": "status"'*) ;;
  *)
    unset webhook_status_json webhook_status_rc
    printf '%s\n' 'staging webhook initial status blocker' >&2
    exit 1
    ;;
esac
case "$webhook_status_json" in
  *'"pending_update_count": 0'*) ;;
  *)
    unset webhook_status_json webhook_status_rc
    printf '%s\n' 'staging webhook initial status blocker' >&2
    exit 1
    ;;
esac
case "$webhook_status_json" in
  *'"has_last_error": false'*) ;;
  *)
    unset webhook_status_json webhook_status_rc
    printf '%s\n' 'staging webhook initial status blocker' >&2
    exit 1
    ;;
esac
unset webhook_status_json webhook_status_rc
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-webhook set
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-webhook status
```

Не выводить `webhook_status_json`. Initial `ok:false` допустим только по safe contract выше; после `set` финальный status обязан завершиться успешно. Identity mismatch, неожиданный pending count, `has_last_error:true` или Telegram error — blocker.

## 9. Consent и live canary

1. Получить явное согласие тестировщика на обработку одного сообщения dedicated test bot.
2. Зафиксировать время и scope согласия без chat/user ID и текста.
3. Создать snapshot.
4. Тестировщик вручную отправляет боту точный текст: `staging canary: проверка ответа`.
5. Проверить доставку и replay.

```bash
cd /opt/moroz-staging/project
app_uid="$(docker run --rm --entrypoint id "moroz-staging-bot:${STAGING_IMAGE_TAG}" -u)"
app_gid="$(docker run --rm --entrypoint id "moroz-staging-bot:${STAGING_IMAGE_TAG}" -g)"
test -n "$app_uid" && test -n "$app_gid"
install -d -m 0700 -o "$app_uid" -g "$app_gid" /opt/moroz-staging/tmp
unset app_uid app_gid
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke snapshot --label live
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke verify --label live
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke replay-live
```

Каталог evidence остаётся mode 0700 и принадлежит non-root UID/GID app image. Нет согласия, лишние сообщения или неверные safe deltas — blocker.

## 10. Worker recovery

Остановить и запустить только exact staging container; stores и другие services не трогать.

```bash
docker stop --timeout 30 moroz-staging-worker-1
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke inject --label worker-restart
docker start moroz-staging-worker-1
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke verify --label worker-restart
```

Неожиданный container name или recovery timeout — blocker.

## 11. Redis recovery

```bash
redis_container=moroz-staging-redis-1
restore_redis() { docker start "$redis_container" >/dev/null 2>&1 || true; }
trap restore_redis EXIT HUP INT TERM
docker stop --timeout 30 moroz-staging-redis-1
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke inject --label redis-loss
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-smoke verify --label redis-loss
docker start moroz-staging-redis-1
trap - EXIT HUP INT TERM
for attempt in $(seq 1 30); do
  test "$(docker inspect -f '{{.State.Health.Status}}' moroz-staging-redis-1)" = healthy && break
  sleep 1
done
test "$(docker inspect -f '{{.State.Health.Status}}' moroz-staging-redis-1)" = healthy
```

`verify` обязан вернуть `1/1/1`, пока Redis ещё остановлен. Неожиданный container name, потеря persistent data или recovery timeout — blocker.

## 12. Safe logs

Raw stream не показывать и не сохранять. Передать его напрямую scanner, который выводит только safe aggregate counts:

```bash
cd /opt/moroz-staging/project
set -o pipefail
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml logs --no-color --since=10m bot worker caddy | docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run -T --rm staging-smoke scan-logs
```

Любой nonzero secret/PII/raw/traceback count — blocker.

## 13. Image-only rollback

Выбрать ранее записанный immutable image tag полного комплекта bot/worker/migrate. На первом staging-релизе предыдущего комплекта нет: это явный rollback blocker до следующего app release, а не разрешение использовать текущий tag как фиктивный откат. Stores сохраняются, schema command в rollback отсутствует.

```bash
cd /opt/moroz-staging/project
export STAGING_PREVIOUS_IMAGE_TAG='<previous-immutable-tag>'
export STAGING_IMAGE_TAG="$STAGING_PREVIOUS_IMAGE_TAG"
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml up -d --no-build --wait --wait-timeout 120 bot worker
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml ps --status running bot worker postgres redis rabbitmq
curl --fail --silent --show-error http://127.0.0.1:18081/openapi.json >/dev/null
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-webhook status
```

Неизвестный tag, missing image, unhealthy app или webhook mismatch — rollback blocker.

## 14. Safe stop

Сначала записать safe pending count, затем убрать webhook без очистки pending updates. Остановить только перечисленные staging services; persistent stores сохранить.

```bash
cd /opt/moroz-staging/project
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-webhook status
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml --profile staging-tools run --rm staging-webhook delete
docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml stop --timeout 30 caddy bot worker rabbitmq redis postgres
```

Webhook delete failure — blocker; не продолжать с догадками или операциями над другими projects.

## 15. Evidence

Evidence не содержит secret values, raw logs, message text, bot token, chat/user IDs или resolved environment.

| Поле | Безопасное значение |
|---|---|
| Commit | short commit ID |
| Images | bot/worker/migrate image ID или digest |
| Migration | Alembic head identifier |
| Health | service name + healthy/running boolean |
| Webhook | ok, pending count, has-last-error boolean |
| Smoke | label + inbox/LLM/sent delta counts |
| Recovery | label + safe delta counts + duration |
| Log scan | aggregate counters |
| Durations | seconds per checkpoint |
| Blockers | blocker type + timestamp, без приватных данных |
