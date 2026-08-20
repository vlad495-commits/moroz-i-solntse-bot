# Staging Release Privacy and Scheduler Fixes — Design

**Дата:** 2026-08-18  
**Статус:** одобрено владельцем продукта  
**Источник:** обязательный review draft PR №2 перед merge в `main`

## Цель

Закрыть два privacy-разрыва и включить фактическое исполнение периодических задач
на staging до merge и rollout актуального release candidate:

1. полное удаление локальных данных Telegram-клиента не должно оставлять или
   восстанавливать его запись из локальной YCLIENTS-проекции;
2. захваченный worker-ом outbound не должен отправиться после успешного удаления
   клиента;
3. retention, YCLIENTS booking projection и YCLIENTS catalog jobs должны реально
   проходить через scheduler на staging.

YCLIENTS остаётся внешним источником истины. Создание, перенос, отмена или удаление
записей YCLIENTS не входят в работу и запрещены.

## Выбранный подход

### 1. Durable suppression локальной YCLIENTS-проекции

Новая additive-миграция `0012_yclients_projection_suppression` создаёт таблицу
`yclients_projection_suppressions`:

- `external_id text PRIMARY KEY`;
- `created_at timestamptz NOT NULL DEFAULT now()`.

Таблица намеренно не хранит `chat_id`, имя, телефон, booking key, услуги или другие
клиентские данные. Один provider ID остаётся техническим tombstone, который не
позволяет повторно сохранить удалённые локальные данные.

Во время customer deletion до удаления `bookings` собираются связанные
`external_id`. В той же PostgreSQL-транзакции сервис:

1. вставляет их в suppression-таблицу через `ON CONFLICT DO NOTHING`;
2. удаляет соответствующие строки из `yclients_booking_projection`;
3. проверяет отсутствие этих IDs в проекции и наличие tombstone для каждого ID;
4. только затем удаляет локальные bookings/scenarios и остальные данные.

Повторное удаление остаётся идемпотентным. Suppression rows не очищаются
автоматически: provider ID считается непрозрачным стабильным идентификатором, а
удаление tombstone способно восстановить локальные данные при следующем sync.

`ProjectionRepository.replace()` внутри своей существующей транзакции получает
набор suppressed `external_id` и не вставляет такие строки в новый снимок. Reader
и HTTP-контракт не меняются: provider response по-прежнему полностью валидируется,
но подавленные записи не материализуются в локальной проекции.

Внешняя запись и карточка клиента в YCLIENTS не изменяются. Это уточняет прежний
контракт «удалить все локальные данные»: YCLIENTS может хранить запись, бот — нет.

### 2. PostgreSQL fence перед Telegram send

`MessageRepository` получает узкий async context manager для одного уже claimed
outbound. Он:

1. открывает PostgreSQL-транзакцию;
2. берёт тот же `pg_advisory_xact_lock(customer_lock_subject(chat_id))`, что
   customer deletion и message worker;
3. повторно читает outbound по точному UUID;
4. разрешает отправку только если строка существует, принадлежит тому же
   `channel/chat_id` и остаётся в статусе `sending`;
5. удерживает lock до завершения одного `telegram.send_message`.

Если deletion завершился раньше, durable row уже отсутствует и sender возвращает
`SKIPPED`. Если send получил lock раньше, deletion ждёт окончания provider call и
показывает успех только после него. Поэтому после успешного ответа удаления новый
send старого текста невозможен.

Post-send completion остаётся существующей отдельной операцией. Если deletion
удалит row между успешным send и completion, completion безопасно не восстановит
контент; сообщение было отправлено до завершения удаления. Network timeout и
unknown-delivery правила не ослабляются.

Новый Redis lock, очередь или delivery service не добавляются. PostgreSQL lock
переиспользует существующую customer serialization boundary.

### 3. Commit-pinned scheduler на staging

Staging Compose перестаёт назначать scheduler профиль
`disabled-in-staging`. Scheduler получает отдельный immutable image:

```text
moroz-staging-scheduler:${STAGING_IMAGE_TAG}
```

Image собирается из существующего scheduler Dockerfile и того же exact commit,
что bot/worker/admin/migrate. Scheduler не получает Telegram, LLM или YCLIENTS
credentials; только существующие DB/RabbitMQ настройки его base Compose contract.

Staging runbook включает scheduler в:

- candidate tag collision check и build;
- candidate image manifest и secret/image scan;
- rollout `up -d` и running/image-ID verification;
- safe log scan;
- rollback capture и restore.

Для первого rollout scheduler сейчас отсутствует. Previous manifest сохраняет для
него явный marker `absent`. Rollback на такой previous release останавливает и
удаляет только candidate scheduler container, затем доказывает его отсутствие.
Если previous scheduler существовал, rollback использует его сохранённый immutable
image ID как для остальных runtime services. Candidate restore всегда возвращает
commit-pinned scheduler.

После rollout bounded technical smoke использует только synthetic system jobs и
подтверждает переход scheduler job в terminal state. Клиентские reminders,
feedback и любые YCLIENTS mutations не запускаются. Для YCLIENTS projection и
catalog разрешены только существующие GET-only readers.

## Рассмотренные альтернативы

### Убрать `client_name` из всей проекции

Проще по схеме, но ломает полезность read-only центра записей для всех клиентов и
не устраняет хранение времени/услуг удалённой записи. Отклонено.

### Проверять Redis marker непосредственно перед Telegram API

Уменьшает окно гонки, но не закрывает его: marker может появиться сразу после
проверки. Redis также не является durable источником outbound. Отклонено в пользу
существующего PostgreSQL customer lock.

### Оставить scheduler выключенным и запускать вручную

Не обеспечивает десятиминутную/часовую/ежедневную периодичность и снова оставляет
pending jobs без исполнителя. Отклонено.

## Миграция и совместимость

- Migration `0012` только добавляет suppression-таблицу; существующая проекция и
  customer tables не переписываются.
- Downgrade удаляет только suppression-таблицу, но production/staging rollback БД
  по-прежнему запрещён: старые app images обязаны работать на forward schema.
- Старый bot/worker/admin игнорирует новую таблицу.
- Candidate worker требует migration head до запуска.
- Projection replacement и customer deletion остаются атомарными.

## Ошибки и fail-closed поведение

- Ошибка вставки tombstone, удаления projection row или remnant-проверки откатывает
  всю customer deletion.
- Ошибка suppression lookup/write завершает projection sync безопасным
  `yclients_projection_write`, не заменяя прежний снимок.
- Ошибка pre-send fence не вызывает Telegram API и уходит в существующий retry/
  alert path.
- Неизвестный previous scheduler state блокирует rollback; догадки по tag запрещены.
- Неуспешный scheduler terminal-state smoke блокирует staging acceptance.

## Тестирование

### Customer deletion / projection

- migration shape и head `0012`;
- удаление клиента создаёт только tombstone по `external_id` и очищает projection;
- forced projection/tombstone error откатывает всё удаление;
- следующий полный projection sync не восстанавливает suppressed record;
- unrelated YCLIENTS records продолжают материализоваться;
- remnant check fail-closed требует `projection=0` и exact suppression coverage.

### Outbound race

- claimed outbound, удалённый до fence, возвращает `SKIPPED` без provider call;
- send, начавшийся первым, удерживает customer lock, deletion ждёт;
- deletion, начавшийся первой, удаляет row, после чего sender не отправляет;
- network/timeout/cancel/ordered-delivery regressions сохраняют прежнее поведение.

### Staging scheduler

- merged Compose содержит commit-pinned scheduler без disabled profile;
- scheduler image присутствует в candidate/previous manifests;
- previous `absent` и previous image rollback paths проверены отдельно;
- rollout/restore проверяют exact scheduler image ID и running state;
- documented technical smoke подтверждает terminal synthetic system job;
- scheduler environment не получает Telegram/LLM/YCLIENTS secrets.

### Финальный gate

- affected Docker suites;
- полный Docker suite без skips;
- migration head и backward compatibility gate;
- pinned Ruff `E9,F`, compileall, Compose config и `git diff --check`;
- независимый повторный code review PR range.

## Вне scope

- любые YCLIENTS mutations;
- удаление внешней карточки клиента или записи YCLIENTS;
- ручной refresh в админке;
- новый generic privacy framework;
- изменение production до отдельного явно разрешённого rollout;
- merge PR или staging deployment до зелёных тестов и review verdict Ready.

## Критерии готовности

1. Удалённые локальные данные не возвращаются после projection sync.
2. После успешного customer deletion старый outbound не может быть отправлен.
3. Staging scheduler работает на exact candidate image и проходит rollback.
4. Read-only YCLIENTS диагностика выводит только allowlisted status/evidence.
5. PR №2 получает повторный review `0 Critical / 0 Important` и только после этого
   может быть влит в `main` и развёрнут на staging.
