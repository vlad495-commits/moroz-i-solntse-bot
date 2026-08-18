# Плановая retention-очистка — дизайн

## Контекст и решение

После отключения legacy polling функция `project/llm/db.py::cleanup_old_records`
перестала вызываться ежедневно. Настройка `DATA_RETENTION_DAYS` существует, но
runtime больше не обеспечивает её фактическое исполнение.

Сохраняем текущую отраслево разумную политику проекта: `1095` дней по умолчанию,
настраиваемое значение из окружения и полное отключение при значении `<= 0`.
Эта поставка не утверждает юридический срок вместо собственника и не расширяет
состав удаляемых данных: автоматически очищаются только `messages` и
`token_usage`, как в действующем контракте `cleanup_old_records`.

Плановая очистка проходит через уже существующий durable-контур:

```text
worker startup
  -> scheduler_jobs(kind=retention_cleanup, daily UTC bucket)
  -> scheduler pump
  -> RabbitMQ scheduler_job
  -> worker
  -> PostgreSQL transaction
  -> messages + token_usage
```

Отдельный cron, новый контейнер, новая очередь, миграция и новая зависимость не
добавляются.

## Компоненты

### Доменный модуль retention

Новый `project/src/moroz/retention.py` содержит:

- `RETENTION_CLEANUP_KIND = "retention_cleanup"`;
- `retention_job(now)` — одну задачу на UTC-сутки с пустым payload и
  идемпотентным ключом `retention_cleanup:<YYYY-MM-DD>`;
- `delete_expired_records(connection, retention_days)` — явные параметризованные
  DELETE для `messages` и `token_usage`, возвращающие только счётчики строк;
- `RetentionCleanupCoordinator` — постановку текущей/следующей задачи и запуск
  очистки;
- `RetentionCleanupError` с единственным безопасным кодом
  `retention_cleanup_failed`.

`delete_expired_records` принимает уже открытую PostgreSQL connection. Поэтому
worker выполняет оба DELETE в одной транзакции, а legacy `cleanup_old_records`
переиспользует ту же функцию через свой текущий pool без второго SQL-контура.

### Планирование

Singleton worker на старте вызывает `ensure_current(now)`. Для положительного
`DATA_RETENTION_DAYS` coordinator ставит задачу на начало текущих UTC-суток.
Если она ещё не исполнялась, scheduler подхватит её немедленно; повторный startup
не создаст дубль благодаря unique `idempotency_key`.

Перед исполнением текущей задачи coordinator ставит задачу на следующие
UTC-сутки. Так временный сбой очистки не разрывает ежедневную цепочку, а retry
текущей задачи остаётся в штатном scheduler/RabbitMQ-контуре. При
`DATA_RETENTION_DAYS <= 0` новая задача не создаётся; уже поставленная задача
завершается как `skipped(retention_disabled)` без DELETE и без продолжения цепочки.

Значение retention не сохраняется в payload. Worker применяет текущую runtime-
настройку, поэтому изменение окружения не оставляет старое число в durable job.

### Worker routing

`retention_cleanup` обрабатывается как системная scheduler-задача до веток,
которым нужны booking/YCLIENTS зависимости. `MessageTaskHandler` получает один
`retention_cleanup` coordinator и передаёт его в существующий
`handle_scheduler_job`.

Успех завершается обычным `JobResult.sent()`. Ошибка PostgreSQL преобразуется в
`RetentionCleanupError`; в `scheduler_jobs.last_error_code`, alerts и logs
попадает только allowlisted `retention_cleanup_failed`, без SQL, значений полей,
идентификаторов и текста сообщений.

## Транзакция и границы удаления

Оба DELETE выполняются в одной PostgreSQL-транзакции с параметром типа integer и
границей `created_at < now() - make_interval(days => $1)`. Ошибка второго DELETE
откатывает первый. Имена таблиц не принимаются извне и не интерполируются.

Удаляются:

- `messages` старше настроенного срока;
- `token_usage` старше настроенного срока.

Не удаляются этой задачей:

- inbox/outbox, consent, booking, scheduler, escalation и audit записи;
- Redis-контекст;
- данные YCLIENTS;
- backup-архивы.

Полное удаление одного клиента уже реализовано отдельным owner-only контуром.
Расширение автоматического retention на остальные сущности требует отдельной
таблицы сроков и юридического согласования, поэтому в эту поставку не входит.

## Конфигурация и эксплуатация

`DATA_RETENTION_DAYS` добавляется в allowlist окружения worker в базовом Compose.
Scheduler не получает эту настройку: он только публикует due jobs, удаление
выполняет worker. Значение по умолчанию остаётся `1095` в существующем
`project/llm/config.py` и Compose.

Новые секреты, provider-вызовы, staging/production mutations и ручная кнопка
очистки не требуются.

## Проверки

1. Unit: UTC bucket, стабильный idempotency key, disabled mode, постановка
   следующей задачи, безопасная ошибка и routing без booking dependencies.
2. PostgreSQL integration: просроченные строки обеих таблиц удаляются, свежие и
   контрольные строки сохраняются, ошибка второго DELETE откатывает первый.
3. Worker/runtime: startup гарантирует текущую retention-задачу, Compose передаёт
   только нужную настройку, существующие YCLIENTS coordinators не меняют поведение.
4. Regression: legacy `cleanup_old_records` использует общий DELETE-контракт,
   scheduler/worker и privacy/delete-customer тесты проходят.
5. Финальный Docker gate: focused tests, полный suite, compile/config checks и
   `git diff --check`.

## Критерии готовности

- при `1095` worker гарантирует не более одной retention-задачи на UTC-сутки;
- задача действительно удаляет только просроченные `messages` и `token_usage`;
- при `<=0` нет DELETE и ежедневная цепочка прекращается;
- повторная доставка и restart не создают дополнительное удаление или job;
- частичное удаление между двумя таблицами невозможно;
- ошибки и observability не содержат ПД, SQL details и секретов;
- дорожная карта и changelog отражают фактические проверки, без утверждения о
  production rollout до отдельного deploy.
