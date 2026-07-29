# Staging admin for manual acceptance

## Goal

Подготовить один стабильный staging, соответствующий актуальному `main`, и открыть
существующую админку по HTTPS для ограниченной ручной приёмки первой версии.
Это не production-деплой и не полное ручное тестирование.

## Scope

В staging должны быть доступны Telegram-бот и существующая админка с историей
диалогов, паузой бота, логами и базовой статистикой. Scheduler, YCLIENTS smoke,
уведомления и дополнительные каналы остаются выключенными.

## Public contract

- `https://${STAGING_DOMAIN}/telegram/webhook` проксируется в bot.
- `https://${STAGING_DOMAIN}/healthz` проксируется в bot.
- `https://${STAGING_DOMAIN}/admin` перенаправляется на `/admin/`.
- `https://${STAGING_DOMAIN}/admin/*` проксируется в admin.
- Любой другой публичный путь возвращает `404`.
- Admin не публикует отдельный host-порт.

## Compose contract

Staging overlay включает `admin`, задаёт immutable image
`moroz-staging-admin:${STAGING_IMAGE_TAG}` и не включает `scheduler`.
Admin получает `ADMIN_ROOT_PATH=/admin`, secure-cookie и обязательные staging
credentials. Caddy ждёт healthy bot и admin.

Сборка staging создаёт четыре app images: bot, worker, migrate и admin. Secret
values не входят в image metadata, команды, evidence или Git.

## Credentials

`ADMIN_USERNAME`, `ADMIN_PASSWORD` и `ADMIN_SESSION_SECRET` создаются и хранятся
только в защищённом `/opt/moroz-staging/.env` с mode `600`. Слабые default values
не используются. Значения не печатаются в терминал и не попадают в отчёт.

## Deployment

После локального RED/GREEN и полного quality gate изменения коммитятся в `main`
и публикуются в `origin/main` по явному разрешению пользователя. На staging
фиксируется предыдущий commit и image set, затем выполняются Git update, сборка
immutable images, Alembic `upgrade head`, запуск stores и app-контейнеров,
валидация Caddy, запуск HTTPS ingress и установка webhook.

Если staging checkout не может безопасно перейти на `origin/main` fast-forward,
rollout останавливается до отдельного решения; история сервера не сбрасывается.

## Technical smoke

Smoke ограничивается следующими проверками:

- Alembic current совпадает с head;
- bot, worker, admin, PostgreSQL, Redis, RabbitMQ и Caddy healthy/running;
- loopback bot health отвечает успешно;
- HTTPS `/healthz` и `/admin/login` отвечают `200`;
- `/admin` перенаправляет на `/admin/`;
- webhook без секрета и с неверным sentinel secret отвечает `403`;
- посторонний URL отвечает `404`;
- Telegram webhook status корректен и не содержит last error;
- безопасный log scanner не находит secrets, PII, raw message text или traceback.

Live canary и полное ручное сценарное тестирование в этот rollout не входят.

## Rollback

До rollout сохранены:

- staging commit `b5ce49dd405bec817826e6e519effa6218329639`;
- immutable bot/worker/migrate image set `yclients-7e2ec278ed7`;
- защищённая копия staging `.env`;
- проверенный зашифрованный PostgreSQL dump.

App/config rollback возвращает checkout на предыдущий commit, останавливает новый
admin, поднимает предыдущие bot/worker images и прежний Caddy contract. PostgreSQL,
Redis и RabbitMQ сохраняются. Alembic downgrade не выполняется.

Если требуется восстановление данных, dump сначала разворачивается в отдельную
базу и проверяется. Замена рабочей staging-базы требует отдельного incident
решения и не является автоматической частью rollback.

## Completion criteria

Staging готов к ручной приёмке, только если текущий `main` развёрнут, миграция на
head, все необходимые контейнеры healthy, bot/admin доступны по HTTPS, webhook
корректен и технический smoke полностью зелёный. Любой непрошедший пункт явно
фиксируется как blocker без расширения scope и без несвязанных исправлений.
