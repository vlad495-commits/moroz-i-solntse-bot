# Phase 3 Staging Design

**Status:** дизайн одобрен пользователем 2026-07-18; реализация ещё не начата.

**Goal:** подготовить на текущем VPS минимальный изолированный staging-контур Reliable Telegram Pipeline и доказать его работу через настоящий Telegram test bot, не затрагивая общие или production-контейнеры.

## Источники правды и границы

Дизайн следует `AGENTS.md`, `План реализации.md`, `Дорожная карта.md`, `ТЗ и архитектура.md` и актуальным планам в `docs/superpowers/plans/`. Учебная лестница и `project/.step` не используются.

В Phase 3 входят только staging-инфраструктура, HTTPS webhook, изолированный запуск, live smoke, относящиеся к staging recovery-проверки и runbook. YCLIENTS, guardrails, новые scheduler-функции, production admin, monitoring/alerts, backup/restore и production release остаются в последующих фазах.

## Принятый подход

Staging размещается на текущем VPS в отдельном checkout и всегда запускается с явным Compose project name `moroz-staging`. Ingress выбирается после read-only preflight:

1. Если 80/443 свободны, staging запускает собственный Caddy в том же Compose project.
2. Если 80/443 уже заняты существующим Caddy, его конфигурация сначала валидируется, затем получает только staging route без остановки общих контейнеров. Bot публикуется только на loopback-порт.
3. Если порты заняты неизвестным ingress или безопасное изменение невозможно, server ingress считается внешним blocker. Tunnel и новый ingress-провайдер не добавляются.

## Компоненты и изоляция

Рабочий поток:

```text
Telegram
  -> HTTPS ingress
  -> bot:/telegram/webhook
  -> PostgreSQL inbox + Redis buffer
  -> RabbitMQ
  -> worker
  -> LLM
  -> PostgreSQL outbox
  -> Telegram send
```

В staging запускаются только `bot`, `worker`, `postgres`, `redis`, `rabbitmq` и выбранный ingress. `admin` и `scheduler` не запускаются.

Команда всегда использует base Compose вместе со staging override и явным namespace:

```powershell
docker compose -p moroz-staging `
  -f docker-compose.yml `
  -f docker-compose.staging.yml config --quiet
```

Project name изолирует контейнеры, сеть и named volumes. PostgreSQL, Redis и RabbitMQ не публикуют host-порты. При собственном Caddy bot доступен только по service discovery `bot:8081`. При существующем host Caddy bot получает `127.0.0.1:${STAGING_BOT_PORT:-18081}:8081`; публичный bind запрещён, а занятый loopback-порт блокирует запуск.

Каждый checkout имеет собственный ignored `.env` с правами только технического пользователя. Staging credentials и data не переиспользуются из общего или production-контура. Собственные Python-образы работают non-root. Официальные infrastructure images закрепляются по версии; runtime user/capabilities проверяются до запуска. Caddy получает read-only конфигурацию и отдельные project-scoped persistent data/config volumes.

## HTTPS ingress

Публично маршрутизируется только точный путь `/telegram/webhook`. Admin, OpenAPI, container healthchecks, stores и queue наружу не публикуются. Все остальные пути получают `404`.

Caddy использует staging domain из server environment, автоматически получает и обновляет публичный TLS-сертификат, проксирует webhook в bot и сохраняет Telegram secret header без логирования его значения. Перед reload выполняется `caddy validate`; невалидная конфигурация не применяется.

Preflight проверяет:

- staging checkout и Compose project targets;
- отсутствие действий над другими Compose project names;
- DNS staging domain на текущий VPS;
- доступность 80/443 либо тип существующего ingress;
- отсутствие публичных port mappings у stores;
- доступность Docker daemon и минимум 2 GiB свободного места перед build.

## Секреты и Telegram identity gate

На VPS криптографическим генератором создаются отдельные PostgreSQL, Redis и RabbitMQ credentials и `TELEGRAM_WEBHOOK_SECRET`. Webhook secret имеет длину 32–64 символа и содержит только разрешённые Telegram символы `[A-Za-z0-9_-]`. Значения не печатаются, не попадают в аргументы процессов, Git, Docker image, логи или evidence.

Staging использует только отдельный Telegram test token. До `setWebhook` one-shot tool выполняет `getMe` и сравнивает полученный bot ID с обязательным ожидаемым staging bot ID. Несовпадение останавливает операцию до изменения webhook. Production/common bot ID и token не передаются staging-контейнерам.

LLM-вызов выполняется staging credential, разрешённым для синтетического теста. Если такого credential нет, live LLM gate остаётся blocker; fake LLM не засчитывается как live evidence.

## Последовательность запуска

Запуск fail-closed:

1. Выполнить read-only server, Docker, DNS и ports preflight.
2. Создать server-only staging environment без вывода значений.
3. Выполнить `docker compose ... config --quiet`.
4. Собрать bot, worker и migrate images с тегом проверяемого Git commit; записать image digests без environment.
5. Проверить non-root runtime и отсутствие secret-shaped данных в image history/config.
6. Запустить только staging PostgreSQL, Redis и RabbitMQ; дождаться healthchecks.
7. Запустить immutable migration image с `alembic upgrade head`.
8. Отдельно проверить, что staging database находится на Alembic head.
9. Запустить bot, worker и ingress; дождаться healthchecks.
10. Проверить публичный TLS, `404` на постороннем пути и `403` для webhook без/с неверным secret header.
11. Выполнить identity gate и только затем Telegram `setWebhook`.
12. Проверить `getWebhookInfo`: ожидаемый HTTPS URL, разрешённые updates, отсутствие свежей ошибки доставки.

Bot staging healthcheck проверяет реальный HTTP listener по внутреннему непубличному endpoint, а не только `/proc`. Worker сохраняет текущий readiness marker и bounded shutdown. Existing `stop_grace_period: 30s` остаётся окончательной process-level границей.

## Управление webhook

One-shot staging tool запускается только через Compose profile, получает явный allowlist token/expected bot ID/public URL/webhook secret без LLM credential и поддерживает безопасные операции `set`, `status` и `delete`. Smoke/recovery operation получает отдельный allowlist database connection/public URL/webhook secret без Telegram token и LLM credential. Tool выводит только success/failure, агрегатные counts, pending count и безопасную error category.

`setWebhook` устанавливает:

- `https://${STAGING_DOMAIN}/telegram/webhook`;
- тот же `TELEGRAM_WEBHOOK_SECRET`, который проверяет bot;
- `allowed_updates=["message", "callback_query"]`;
- `max_connections=5` для одного staging-оператора;
- `drop_pending_updates=false` по умолчанию.

Удаление pending updates допустимо только при первичном подключении заведомо нового disposable test bot и отдельном явном флаге. Обычный deploy, rollback, stop и повторный `setWebhook` pending updates не удаляют.

## Live smoke и duplicate evidence

Consented оператор использует отдельный test bot и фиксированную синтетическую фразу `staging canary: проверка ответа` без ПД. До теста снимаются только агрегатные baseline counts.

Ожидаемый live flow:

1. Telegram доставляет update через публичный HTTPS ingress.
2. Bot проверяет secret header до JSON parse.
3. PostgreSQL сохраняет одну inbox-запись.
4. Redis/RabbitMQ передают durable task worker.
5. Worker выполняет один LLM-вызов, создаёт одну `token_usage` и один outbound.
6. Telegram получает один ответ, outbound становится `sent`.

Evidence выводит только дельты `1 inbox / 1 token_usage / 1 sent outbound` и безопасные статусы. Text, payload, chat/user IDs, token, secret, DSN и provider response не выводятся.

Для duplicate gate one-shot tool читает нормализованную inbox-запись внутри staging, в памяти восстанавливает минимальный Telegram update с тем же `update_id` и повторно отправляет его через публичный HTTPS ingress с тем же secret header. Сырой Telegram payload не сохраняется. После повтора дельты остаются `1/1/1`; второй LLM-вызов и outbound запрещены.

## Recovery и degradation

Все команды содержат `-p moroz-staging` и перед действием проверяют target project.

### Worker restart

Через HTTPS создаётся новый синтетический pending update без ПД с зарезервированным отрицательным `update_id`, который Telegram не использует для настоящих updates. Worker останавливается bounded-командой и запускается снова. Проверяется readiness transition, восстановление pending work, ровно один дополнительный LLM-вызов/outbound и отсутствие blind resend уже обработанных update.

### Временная потеря Redis

Останавливается только `moroz-staging` Redis. Через HTTPS подаётся новый синтетический update с другим отрицательным `update_id`. PostgreSQL inbox и durable fallback сохраняют запрос; worker продолжает DB/Rabbit recovery и создаёт ровно один ответ. Redis запускается снова, health восстанавливается, накопленные или уже обработанные updates повторно не вызывают LLM.

### Bounded shutdown

Worker прекращает intake, завершает или bounded-cancel in-flight work и останавливается не более чем за 30 секунд. Evidence содержит длительность и безопасный статус, но не raw logs. Общие контейнеры не затрагиваются.

## Rollback приложения

Bot, worker и migration images получают immutable tag на основе Git commit и фиксируемый digest. Перед deploy сохраняется предыдущий проверенный app tag.

Rollback меняет только bot/worker на предыдущие images и повторно проверяет health/webhook. PostgreSQL/Redis/RabbitMQ volumes сохраняются. `alembic downgrade`, удаление volumes и destructive schema rollback запрещены. Если предыдущий app несовместим с уже применённой схемой, rollback блокируется и используется forward fix.

В Phase 3 новых бизнес-миграций не планируется, поэтому rehearsal проверяет app-only rollback на неизменённой schema. Изменение Caddy откатывается отдельно только на ранее валидированную конфигурацию.

## Safe stop

Обычная остановка staging:

1. Проверить `getWebhookInfo` и дождаться нулевого pending count либо зафиксировать blocker.
2. Выполнить `deleteWebhook` без `drop_pending_updates`.
3. Остановить только Compose project `moroz-staging` с bounded timeout.
4. Сохранить volumes.

Удаление staging volumes/data не входит в stop и требует отдельного явного подтверждения с повторной проверкой абсолютных targets.

## Safe logs и evidence

Raw staging logs не копируются в changelog или сообщения. Проверка логов передаёт их через sanitizer, который выводит только количество и категории проблем. Запрещены token-like URL, Authorization/header values, DSN, passwords, prompt/user text и полный stack payload.

Evidence содержит:

- Git commit и image digests;
- Compose project name;
- migration head status;
- container health states;
- TLS/webhook safe status и pending count;
- агрегатные smoke/recovery deltas;
- bounded shutdown duration;
- rollback result;
- точные blockers без secret-shaped значений.

## Минимальные артефакты реализации

Ponytail full ограничивает реализацию следующими артефактами:

- `project/docker-compose.staging.yml` — только staging overrides, images, ingress и profile-only tool;
- `project/ops/staging/Caddyfile` — единственный webhook route;
- `project/ops/staging.py` — один stdlib/existing-dependency CLI для webhook, smoke/recovery evidence и safe output;
- `project/ops/staging-runbook.md` — точные launch/check/stop/rollback команды;
- один сфокусированный contract/test модуль плюс точечные изменения существующих Compose tests;
- обновления `Дорожная карта.md` и `changelog.md`.

Новые runtime dependencies, отдельный deployment framework, tunnel, orchestration service, monitoring stack и production config не добавляются.

## Проверки и критерии завершения

### Local/fake staging ready

- полный Docker pytest suite проходит в task-specific Compose project;
- staging Compose render/contract и Caddy validation проходят;
- migration fresh database до head проходит;
- fake ingress, wrong-secret, duplicate, worker restart и Redis-loss tests проходят;
- image user, bounded shutdown, safe logs и secret scans проходят;
- cleanup task-specific containers/volumes/networks/images даёт `0/0/0/0`.

### Live staging passed

- отдельный test bot identity подтверждён;
- staging DNS/TLS и `getWebhookInfo` корректны;
- consented live update даёт ровно один inbox, LLM call и sent outbound;
- duplicate update не меняет эти дельты;
- worker restart и Redis-loss recovery подтверждены в staging;
- app-only rollback проверен без DB downgrade;
- runbook-команды воспроизведены и evidence записано безопасно.

Phase 3 закрывается только после обоих gate. Если отсутствуют test token, DNS, server access или live LLM credential, выполняется вся доступная local/fake часть, а Phase 3 остаётся открытой с точным blocker. Fake success никогда не называется live evidence.
