# PostgreSQL ownership и `moroz_booking_key` для YCLIENTS

**Дата:** 2026-07-22  
**Статус:** одобрено пользователем  
**Исправляет:** ошибочную трактовку YCLIENTS `api_id` как writable correlation field

## Цель

PostgreSQL — единственный источник истины о владельце записи. YCLIENTS хранит только непрозрачный корреляционный ключ в дополнительном поле записи `moroz_booking_key`. Ключ позволяет проверить целостность provider record и найти запись после неопределённого create, но сам не определяет владельца без PostgreSQL.

## Доказанный provider mismatch

Instrumented sandbox create вернул HTTP `201` и создал запись, однако YCLIENTS заменил submitted `api_id`. Protected GET подтвердил согласованные ID/comment/services/staff/datetime/duration, но returned `api_id` отличался от owner marker; adapter GET корректно завершился fail-closed. Официальная схема определяет `api_id` как идентификатор внешней системы, `comment` как пользовательский комментарий, а `custom_fields` как сохраняемые дополнительные поля компании.

## Выбранный ключ

- `booking_key` — UUID исходного create-сценария `booking_scenarios.id`.
- UUID уже существует и durable сохранён до внешнего POST; новый генератор не нужен.
- В YCLIENTS ключ сериализуется строкой в `custom_fields.moroz_booking_key`.
- Ключ не содержит телефон, имя, Telegram ID или иной ПД.
- Ключ не является provider idempotency key и не разрешает retry mutation.

## PostgreSQL

Новая additive migration `0006_yclients_booking_key` добавляет `bookings.booking_key UUID`.

1. Для существующих локальных строк поле backfill-ится собственным `bookings.id`, потому что старые provider records не имели нового custom field.
2. После backfill колонка становится `NOT NULL` и `UNIQUE`.
3. Новая create-запись получает `booking_key=scenario.id`.
4. При update по `external_id` repository не меняет `customer_id` и `booking_key`.
5. Mismatch существующего `external_id ↔ customer_id ↔ booking_key` завершает transaction fail-closed; provider response не может переписать local ownership.

Snapshot может содержать строковое представление `booking_key`, но ключ не попадает в пользовательские ответы или обычные логи. Booking events сохраняют только минимальные статусы и external correlation, как сейчас.

## Domain и BookingPort

- `CreateBooking` получает обязательный `booking_key: UUID` отдельно от local `idempotency_key`.
- `ExternalBooking` получает обязательный `booking_key: UUID`.
- `GetBooking` становится отдельной командой: `external_id`, trusted local `customer_id`, trusted local `booking_key`.
- `RescheduleBooking` и `CancelBooking` получают trusted local `customer_id` и `booking_key`.
- `BookingService` на create передаёт `scenario.id`; на change передаёт значения из locked local `bookings` snapshot.

Provider никогда не возвращает authoritative `customer_id`: adapter строит `ExternalBooking.customer_id` только из trusted command/local context после exact проверки custom field.

## YCLIENTS wire contract

### Create

- Удалить ownership marker из `api_id`; arbitrary `api_id` больше не отправляется и не читается.
- Добавить `custom_fields: {"moroz_booking_key": "<uuid>"}`.
- Пользовательский `comment` остаётся без служебных префиксов и сохраняется как сейчас.
- HTTP `201` принимается только при unambiguous record и exact returned `moroz_booking_key`.

### Get

- Protected exact GET выполняется по `external_id`.
- Adapter проверяет record ID и exact `custom_fields.moroz_booking_key` против trusted command.
- Отсутствующий, malformed или чужой key даёт `BookingNotFound`; malformed provider structure — `BookingTemporaryError`.
- `customer_id` берётся только из trusted command.

### Reschedule

- Перед mutation protected GET проверяет exact local key/customer context.
- PUT сохраняет весь provider `custom_fields`, заменяя только `moroz_booking_key` тем же trusted UUID; чужие дополнительные поля не теряются.
- `api_id` не переносится как ownership.
- Один PUT, без retry; response снова обязан содержать exact key.

### Cancel

- Перед DELETE protected GET проверяет exact `moroz_booking_key`.
- Затем выполняется ровно один DELETE, без retry.
- Неопределённый GET не приводит к DELETE; неопределённый DELETE остаётся `BookingOutcomeUnknown`.

## Unknown outcome и reconciliation

- Перед create PostgreSQL checkpoint `executing` уже durable содержит scenario UUID/customer/state.
- Любой неопределённый mutation outcome по-прежнему создаёт durable `booking_outcome_unknown` и `admin_attention_required`; автоматического mutation retry нет.
- Read-only reconciliation может ограниченно получить records в известном slot/date window и локально выбрать exact `custom_fields.moroz_booking_key`.
- `0` или `>1` совпадений остаются escalation; ровно одно совпадение является evidence для ручного/будущего отдельного reconciliation workflow, но текущая фаза не добавляет production-admin.
- Local idempotency остаётся на unique `booking_scenarios.idempotency_key`; `booking_key` не дедуплицирует разные business intents.

## Legacy и fail-closed

- Старые local bookings получают уникальный PostgreSQL key, но provider records без custom field не считаются автоматически совместимыми.
- Попытка изменить legacy provider record без exact `moroz_booking_key` завершается fail-closed и требует ручной проверки.
- Текущая synthetic sandbox record без нового field должна быть удалена только после отдельного explicit consent.

## TDD и verification

1. Migration RED/GREEN: backfill, `NOT NULL`, uniqueness, head.
2. Repository RED/GREEN: immutable `customer_id + booking_key`, rollback on mismatch.
3. Domain/service RED/GREEN: create UUID propagation и change из locked local snapshot.
4. Fake HTTP RED/GREEN: create/get/PUT/DELETE exact custom field, preservation чужих custom fields, отсутствие ownership `api_id`, no retries.
5. Smoke RED/GREEN: duplicate/reconciliation scan по `moroz_booking_key`, redacted output.
6. Booking regression, full Docker suite, independent review/fix-loop.
7. Внешний gate: пользователь создаёт additional record field с code `moroz_booking_key` в test-филиале; затем отдельный consented smoke create/get/reschedule/get/cancel и exact zero-active verification.

## Не входит в scope

- Provider idempotency, blind retries.
- Чтение общей клиентской базы.
- Production admin/reconciliation UI.
- LLM guardrails, scheduler/notifications и production release.
- Merge/push без отдельного разрешения.
