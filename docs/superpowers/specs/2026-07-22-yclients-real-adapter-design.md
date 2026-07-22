# Real YCLIENTS Booking Adapter Design

Дата: 2026-07-22  
Статус: одобрен пользователем в исходном design gate

## Цель

Добавить реальный `BookingPort` для YCLIENTS поверх уже проверенных PostgreSQL state machines. Адаптер получает актуальные слоты, создаёт запись с минимальными клиентскими данными, получает, переносит и отменяет её через официальный API. Неопределённый результат изменяющего запроса никогда не повторяется автоматически и превращается существующим `BookingService` в durable `booking_outcome_unknown` + `admin_attention_required`.

## Границы

В scope входят:

- официальный availability flow `book_dates` → `book_staff` → `book_times`;
- `book_check` непосредственно перед create/reschedule;
- protected `records/record` create/get/update/delete с Partner + User Token;
- restart-safe deterministic opaque slot ID;
- минимальное расширение create-команды: имя, телефон, optional comment и явное согласие на обработку ПД;
- stdlib HTTP transport, таймаут и process-local rate limit `5 req/s` + `200 req/min`;
- fake HTTP contract tests без credentials;
- связная fail-closed проверка real adapter → `BookingService` → PostgreSQL event;
- Docker sandbox-smoke для выделенного test-филиала после local/fake gate.

Не входят Telegram booking UI, intent router, общий reconciliation/admin tooling, LLM guardrails, scheduler/notifications, production release и DB downgrade. Публичные `book_record` endpoints не используются.

## Официальный контракт

Источник — embedded OpenAPI 3.0.3 на `https://developers.yclients.com/ru/`, base URL `https://api.yclients.com`.

- Все запросы отправляют `Accept: application/vnd.yclients.v2+json`.
- Availability и `book_check`: `Authorization: Bearer <PartnerToken>`.
- Protected record CRUD: `Authorization: Bearer <PartnerToken>, User <UserToken>`.
- `GET /api/v1/book_dates/{company_id}` принимает `service_ids`, optional `staff_id`, `date_from`, `date_to`.
- `GET /api/v1/book_staff/{company_id}` принимает `service_ids` и optional `datetime`.
- `GET /api/v1/book_times/{company_id}/{staff_id}/{date}` принимает `service_ids`.
- `POST /api/v1/book_check/{company_id}` принимает `appointments` с `services`, `staff_id`, Unix `datetime`; коды `433`, `436`, `437`, `438` означают недоступный слот.
- `POST /api/v1/records/{company_id}` создаёт запись и возвращает `201`.
- `GET /api/v1/record/{company_id}/{record_id}` возвращает запись с `200`.
- `PUT /api/v1/record/{company_id}/{record_id}` изменяет запись и возвращает `201`.
- `DELETE /api/v1/record/{company_id}/{record_id}` возвращает `204`.
- `Idempotency-Key` и provider-side idempotency не документированы.

Официальная схема и пример расходятся для `book_dates.booking_dates` и некоторых datetime-полей: схема показывает date strings, примеры — Unix timestamps. Парсер принимает оба официально опубликованных варианта, затем нормализует их в timezone-aware `datetime`.

## Domain contract

`BookingPort` и `ExternalBooking` сохраняются. `CreateBooking` минимально расширяется обязательными полями:

```python
@dataclass(frozen=True, slots=True)
class CreateBooking:
    customer_id: str
    slot_id: str
    idempotency_key: str
    customer_name: str
    customer_phone: str
    personal_data_processing_allowed: bool
    comment: str | None = None
```

`customer_id` — внутренний непрозрачный идентификатор владения, не телефон и не YCLIENTS client ID. До `executing` create-flow проверяет непустые имя/телефон и `personal_data_processing_allowed is True`. При отсутствии согласия внешний вызов не выполняется, scenario остаётся в не-mutating состоянии и возвращает `next_action="request_personal_data_consent"`.

В protected create payload передаются только имя, телефон, optional comment, `client_agreements.is_personal_data_processing_allowed=true` и `is_newsletter_allowed=false`. Вся клиентская база не читается. `send_sms=false`, чтобы интеграция сама не создавала несогласованные сообщения.

## Slot ID

YCLIENTS не предоставляет готовый ID временного слота. Адаптер кодирует versioned compact JSON в URL-safe base64 с префиксом `yclients:v1:`. Payload содержит только provider-neutral значения, необходимые для mutation после restart:

```json
{"services":[331],"staff":6544,"start":1785315600,"duration":3600}
```

JSON сериализуется с фиксированным порядком ключей и без пробелов. Один и тот же слот всегда получает один ID. Decode проверяет prefix/version, типы, положительные numeric IDs, timezone-aware instant и duration; invalid/tampered value fail-closed как `SlotUnavailable`. In-memory slot cache не используется.

## Ownership mapping

Protected record payload использует документированное поле `api_id` как внешний correlation ID: `moroz:v1:<base64(customer_id)>`. Оно не является provider idempotency и не используется для retry. `customer_id` обязан быть внутренним непрозрачным ID без ПД.

Create/get/update извлекают один и тот же internal owner из `api_id`. Запись без валидного `moroz:v1:` marker не считается принадлежащей боту и fail-closed возвращает `BookingNotFound`; поиск по телефону или чтение общей клиентской базы не выполняются.

## Availability flow

1. Проверить, что `SlotQuery.service_ids` и optional `staff_id` состоят из положительных numeric YCLIENTS IDs.
2. Запросить доступные даты через `book_dates` в локальной timezone филиала.
3. Запросить bookable staff через `book_staff`; при заданном `staff_id` оставить только точное совпадение.
4. Для каждой подходящей пары date/staff запросить `book_times`.
5. Преобразовать Unix `datetime` и `seance_length` в `Slot`, отфильтровать по точным aware границам `starts_after`/`starts_before`, service subset и staff.
6. Отсортировать по `starts_at`, затем `staff_id`; дубли удалить по opaque slot ID.

Ни один availability response не кэшируется в памяти: перед mutation source of truth перечитывается через `book_check`.

## Mutations

### Create

1. Decode slot ID.
2. Выполнить `book_check` с Partner token.
3. POST protected record с `save_if_busy=false`, slot staff/services/datetime/duration, минимальным `client`, agreements, comment и ownership `api_id`.
4. Принять только documented `201` + однозначный response record; построить `ExternalBooking` с internal `customer_id`.

### Get

GET protected record по exact external ID. Deleted record нормализуется как `status="cancelled"`; активный — `confirmed`. Slot ID восстанавливается из record services/staff/datetime/seance length, owner — из `api_id`.

### Reschedule

1. GET exact current record; записи без ownership marker не изменяются.
2. Decode target slot и выполнить `book_check`.
3. PUT exact record, сохранив client/comment/ownership fields и заменив staff/services/datetime/duration; `save_if_busy=false`.
4. Принять только documented `201` и вернуть нормализованный snapshot.

### Cancel

DELETE exact protected record. Только documented `204` означает успех.

## HTTP и ошибки

Transport — Python stdlib `urllib.request`, вызываемый через `asyncio.to_thread`; новая runtime dependency не добавляется. Каждый запрос имеет bounded timeout. Adapter и sandbox smoke используют один экземпляр process-local limiter с rolling окнами `5/1s` и `200/60s`. Это достаточно для текущего singleton worker; распределённый limiter не добавляется.

Автоматических retries нет, включая GET: официальный контракт не даёт достаточного основания для retry policy. Это минимальнее и безопаснее, чем угадывать повтор.

Классификация:

- availability/get transport, timeout, `429` и `5xx` → `BookingTemporaryError`;
- `book_check` transport/timeout/`5xx` → `BookingTemporaryError`, поскольку mutation ещё не отправлена;
- documented slot conflict codes → `SlotUnavailable`;
- protected `404` → `BookingNotFound`;
- mutation transport/timeout, `5xx`, malformed success или неожиданный status после отправки → `BookingOutcomeUnknown`;
- definite precondition/auth/validation response без признаков успеха → `BookingTemporaryError`, кроме documented slot/not-found mapping;
- `success=false`, malformed envelope или inconsistent record никогда не превращаются в success.

Ни mutation, ни outcome-unknown не повторяются. Существующий `BookingService` сохраняет escalation внутри aggregate lock, поэтому неопределённость durable блокирует sibling mutations.

## Config и секреты

`YclientsConfig.from_env()` читает:

- `YCLIENTS_PARTNER_TOKEN`;
- `YCLIENTS_USER_TOKEN`;
- `YCLIENTS_COMPANY_ID`;
- optional `YCLIENTS_BASE_URL` (default `https://api.yclients.com`);
- optional `YCLIENTS_TIMEZONE` (default `Europe/Moscow`);
- optional `YCLIENTS_TIMEOUT_SECONDS` (default `10`).

Пустые token/company values fail-fast. Tokens не появляются в exception text, repr, logs, tests или documentation. Compose передаёт их только worker/sandbox-smoke профилю из ignored `.env`/process environment; test/migrate/cutover не получают их.

## Проверка

Fake HTTP contract tests на stdlib local server проверяют exact paths/query/headers/bodies/statuses, envelope parsing, deterministic restart-safe slot IDs, consent gate, ownership marker, create/get/reschedule/cancel, documented conflicts, malformed replies, rate limits и один фактический mutation request при разрыве соединения.

E2E с disposable PostgreSQL связывает real adapter с fake HTTP: сервер принимает create body и обрывает ответ, adapter выдаёт `BookingOutcomeUnknown`, `BookingService` сохраняет terminal `escalated`, `booking_outcome_unknown` и один `admin_attention_required`; повтор scenario не делает второй POST.

Canonical verification: focused RED/GREEN, все booking/migration tests, полный fresh Docker suite, compile/import/config/static secret gates, task-specific cleanup и независимый whole-branch review/fix-loop.

## Sandbox smoke и completion gate

После local/fake completion отдельный Docker smoke, получающий values только из ignored `.env`, выполняет:

1. read configured test service/staff and available slots;
2. выбрать два разных будущих слота;
3. create synthetic test record с отдельным non-real phone/name и unique safe comment/api_id;
4. get exact record;
5. reschedule во второй слот;
6. get exact record и подтвердить новый instant;
7. cancel exact record;
8. подтвердить deleted/cancelled state и отсутствие второго record с тем же smoke correlation в узком date range без вывода чужих records/ПД;
9. вывести только redacted IDs/counts/statuses и удалить task-specific Compose artifacts.

Smoke прекращается fail-closed при любой неопределённости; blind cleanup mutation после unknown запрещена и требуется ручная проверка test-филиала. Phase становится live-complete только при сохранённом evidence успешного smoke и локального outcome-unknown E2E. Staging rollback gate закрывается в том же релизе только отдельным distinct app-image циклом `candidate → previous → candidate`, без DB downgrade.
