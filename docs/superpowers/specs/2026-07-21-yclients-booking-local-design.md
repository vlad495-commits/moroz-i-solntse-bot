# Локальный дизайн YCLIENTS Booking

Дата: 2026-07-21
Статус: одобрен пользователем как вариант A

## Цель

Собрать и проверить локальное ядро записи, переноса и отмены через единый `BookingPort`, не привязывая домен к неподтверждённому HTTP-контракту YCLIENTS. Результат этой части — рабочие state machines, durable PostgreSQL-checkpoints, локальная идемпотентность, mock adapter и Docker-тесты. Real YCLIENTS adapter и live/sandbox gate остаются открытыми.

## Границы

В локальную часть входят:

- доменные immutable dataclasses для слотов, команд, внешней записи, идентификации и сценария;
- `BookingPort` с именами методов из мастер-плана;
- in-memory `MockYclientsAdapter` для deterministic unit/E2E-проверок;
- additive Alembic revision после `0004_pipeline_order_claim`;
- `booking_scenarios`, `bookings`, `booking_events`;
- state machines создания, переноса и отмены;
- повторная проверка слота непосредственно перед изменяющим вызовом;
- правило трёх часов для переноса и отмены;
- durable `admin_attention_required` event при эскалации;
- fail-closed при неопределённом результате внешнего вызова или восстановлении из `executing`.

Не входят:

- real HTTP adapter, endpoint paths, auth headers, provider payloads и provider-specific error mapping;
- запрос или хранение реальных YCLIENTS credentials;
- Telegram intent routing и пользовательский booking UI;
- общий escalation framework, production-admin, scheduler/notifications и LLM security;
- provider-side idempotency, пока она не подтверждена официальным контрактом или sandbox evidence.

## Компоненты

### Domain

`models.py` содержит значения без I/O:

- `SlotQuery` — услуги, временной диапазон и optional staff;
- `Slot` — внешний slot ID, услуги, staff, start и duration;
- `CreateBooking`, `RescheduleBooking`, `CancelBooking` — команды с локальным `idempotency_key`;
- `ExternalBooking` — нормализованный локальный snapshot внешнего результата;
- `BookingIdentity` — подтверждённый customer ID для управления существующей записью;
- `BookingScenario` — durable state с `kind`, `phase`, выбранным слотом и данными операции;
- `BookingEvent` — event type и безопасный payload без обязательных ПД;
- доменные ошибки `SlotUnavailable`, `BookingNotFound`, `BookingTemporaryError`, `BookingOutcomeUnknown`.

`ports.py` сохраняет контракт мастер-плана: `list_slots`, `create_booking`, `reschedule_booking`, `cancel_booking`, `get_booking`.

### Mock adapter

`MockYclientsAdapter` хранит слоты, записи и результаты операций в памяти. Он:

- возвращает только matching future slots;
- не возвращает занятые слоты;
- возвращает тот же результат для повторного локального idempotency key;
- не создаёт вторую запись при повторе;
- одинаково проверяет доступность для создания и переноса;
- делает повторную отмену с тем же ключом безопасным no-op.

Mock моделирует доменный контракт, а не выдуманный HTTP YCLIENTS.

### PostgreSQL repository

Revision `0005_booking_state` только добавляет новые таблицы и индексы. Предыдущий app artifact продолжает работать, потому что существующие таблицы и колонки не меняются.

`booking_scenarios` хранит текущий checkpoint и JSONB state. `bookings` хранит локальную копию подтверждённого внешнего результата. `booking_events` — append-only журнал переходов и эскалаций.

Repository выполняет checkpoint и event insert в одной транзакции. Финальный внешний snapshot, terminal phase и событие также сохраняются атомарно.

### State machines

Единый `BookingService.handle(scenario_id, confirmed, identity=None)` загружает scenario из PostgreSQL.

Общий порядок:

1. Без явного подтверждения внешний mutating call запрещён.
2. Для переноса/отмены подтверждённая identity сверяется и со scenario, и с владельцем актуального local booking snapshot; forged/stale scenario не получает детали и не вызывает port.
3. Все change-scenario одного `external_id` дополнительно сериализуются одним namespaced PostgreSQL advisory lock. Под этим lock перечитываются актуальные status, owner и start; правило трёх часов считается от актуального snapshot, а stale status/start fail-closed эскалируется до внешнего вызова.
4. Перед create/reschedule scenario checkpoint переводится в `executing`, после чего слот повторно проверяется через `list_slots`.
5. Потерянный слот переводит scenario в `collecting`, сохраняет событие и возвращает до трёх актуальных альтернатив без потери остальных данных.
6. Успешный вызов атомарно сохраняет local booking snapshot и terminal phase `confirmed`.
7. `BookingTemporaryError` и `BookingOutcomeUnknown` переводят scenario в `escalated`, добавляют `admin_attention_required` и не обещают слот.
8. Scenario, найденный в `executing` после перезапуска, не повторяет mutating call: результат считается неопределённым и эскалируется.
9. Повторная обработка terminal scenario возвращает сохранённый результат без нового внешнего вызова. Summary создаётся только из immutable terminal state самого scenario, поэтому более поздний перенос/отмена общего snapshot его не меняет.

## Идемпотентность

Локальный `idempotency_key` уникален в `booking_scenarios`. PostgreSQL сериализует один сценарий и отдельно aggregate одной внешней записи (`external_id`) через namespaced advisory locks; durable checkpoints остаются короткими транзакциями. Mock adapter также кеширует результаты по ключу.

Это не выдаётся за provider-side exactly-once. Crash после отправки запроса, но до сохранения ответа, остаётся outcome-unknown окном. Без документированной идемпотентности или однозначной provider reconciliation такой сценарий только эскалируется; автоматический повтор запрещён.

## Durable эскалация

До Production Admin отдельный общий task framework не создаётся. Эскалация представлена terminal phase `escalated` и append-only `booking_events.event_type = 'admin_attention_required'` с техническим `error_code`, scenario ID и безопасным контекстом. Следующая admin-фаза сможет читать эти события без изменения booking state machine.

## Проверка

- unit: фильтрация slots и идемпотентность всех mock mutations;
- integration: Alembic head, additive schema, repository checkpoints/events/terminal snapshots;
- E2E create: confirmation gate, lost-slot alternatives, durable success, repeat without duplicate;
- E2E change: ownership, old/new summary, three-hour rule, temporary failure, outcome unknown и recovery from `executing`;
- полный Docker suite и migration head;
- независимый task review после каждого логического commit и whole-branch review в конце.

## Внешний gate

После исчерпания локальной части для real adapter понадобятся официально подтверждённые auth/endpoint/error/rate-limit contracts, API-enabled test company, company ID, разрешённые service/staff fixtures и права на безопасное create/reschedule/cancel. До этого real adapter отсутствует, а YCLIENTS-фаза не объявляется live-complete.
