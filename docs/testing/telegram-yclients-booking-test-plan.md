# Тест-план Telegram → YCLIENTS booking flow

## Правила безопасности

- Все локальные прогоны выполняются только через Docker Compose.
- До отдельного подтверждения компании разрешены только fake transport и mock adapter. Реальный read-only запуск делает только GET.
- Sandbox create/reschedule/cancel запускаются лишь после отдельного явного разрешения, с фейковыми данными, bounded cleanup и reconciliation.
- В доказательства не попадают токены, телефоны, имена, URL с credentials, provider payload, external booking IDs или `moroz_booking_key`.

## Read-only gate YCLIENTS

Команда:

```bash
cd project && docker compose --env-file ../.env --profile yclients-readonly run --rm yclients-readonly
```

Обязательные переменные: `YCLIENTS_PARTNER_TOKEN`, `YCLIENTS_COMPANY_ID`, `YCLIENTS_SERVICE_ALLOWLIST`, `YCLIENTS_STAFF_ALLOWLIST`, `YCLIENTS_ENVIRONMENT_LABEL`. Опциональны `YCLIENTS_BASE_URL`, `YCLIENTS_TIMEZONE`, `YCLIENTS_TIMEOUT_SECONDS`. `YCLIENTS_USER_TOKEN`, Telegram-, LLM-, PostgreSQL-, Redis- и RabbitMQ-секреты этому сервису не передаются: availability GET использует partner bearer, а непрозрачный slot ID подписывается `local non-provider sentinel` и не выводится.

Успех — один JSON-объект с environment label, горизонтом `14`, настроенными service/staff IDs и агрегированными counts. Любая недоступность, redirect/неожиданный envelope, отсутствующий или дублированный configured ID, partial result либо неверная граница окна дают exit non-zero и только `{"ok":false}`.

| Проверка | Команда / тесты | Время (UTC+3) | Среда | Exit | Санитизированный результат |
|---|---|---:|---|---:|---|
| Local fake GET-only | `pytest tests/unit/booking/test_yclients_readonly_check.py tests/unit/test_worker.py tests/unit/test_migration_profile.py tests/contract/booking/test_yclients_catalog.py tests/contract/booking/test_yclients_http.py tests/contract/booking/test_yclients_adapter.py -q` через Compose test profile | 2026-08-02 04:43 | `local-fake` | 0 | `222 passed in 66.17s`; captured transport method set `{"GET"}`; exact 14-day window; raw requested-staff duplicates and malformed `bookable` fail before `/book_times`; no private fields |
| External YCLIENTS read-only | `docker compose --env-file <external ignored .env> --profile yclients-readonly run -T --rm yclients-readonly` | 2026-08-04 13:45 | `sandbox` | 0 | Fresh completion run: `ok=true`; horizon `14`; service/staff counts `1/1`; availability total `336`; configured IDs matched exactly; profile has no User/Telegram/LLM/DB/queue secrets; no mutation method |

## Permission-gated sandbox lifecycle

Команда выполняется только после отдельного явного разрешения на lifecycle именно тестовой компании и фейкового набора данных:

```bash
cd project && docker compose --env-file ../.env --profile yclients-smoke run --rm yclients-smoke
```

Перед любым mutation профиль fail-closed требует: `YCLIENTS_SANDBOX_CONSENT=I_UNDERSTAND_THIS_CREATES_TEST_BOOKINGS`, `YCLIENTS_ENVIRONMENT_LABEL=sandbox`, имя с префиксом `Synthetic Test `, телефон строго формата `+7000` и ещё семь цифр, а также `YCLIENTS_TEST_WINDOW_DAYS` как целое от `1` до `14`. Слоты читаются только в этом окне; нужны два разных будущих слота. Затем bounded GET reconciliation по свежему UUID обязан вернуть строго `matches=0`, `active_matches=0`; auth/transport/shape failure или неожиданное совпадение останавливают flow до первого POST.

Каждый run использует новый UUID/key. Успех требует точную цепочку `create → get → reschedule → get → cancel → final get → reconciliation`, где итог reconciliation равен `matches=1`, `active_matches=0`. При неизвестном результате mutation дальнейшие mutations запрещены: выполняется ровно одна GET-only reconciliation и результат остаётся для ручной проверки. После definite сбоя post-create разрешена только одна cleanup-cancel по exact external ID/key, затем одна GET-only reconciliation; её ошибка также остаётся ручной проверкой. JSON-итог не содержит token, name, phone, run UUID или external booking ID.

| Проверка | Команда / тесты | Время (UTC+3) | Среда | Exit | Санитизированный результат |
|---|---|---:|---|---:|---|
| Local Task 12 safety gate | `pytest tests/unit/booking/test_yclients_sandbox_smoke.py tests/contract/booking/test_yclients_adapter.py tests/unit/test_migration_profile.py -q` через Compose test profile | 2026-08-04 14:43 | `local-fake` | 0 | `167 passed in 59.30s`; exact consent/marker, ASCII fake identity, non-empty 1-day window, pre-mutation record-read gate, exact cancel ownership и single reconciliation/cleanup contracts covered |
| External sandbox lifecycle | `docker compose --env-file <external ignored .env> --profile yclients-smoke run -T --rm yclients-smoke` | 2026-08-04 14:42 | `sandbox` | 1 | Единственная разрешённая попытка: services/staff/slots `1/1/336`; records GET-preflight вернул definite provider failure (`HTTP 403` из отдельной санитизированной диагностики); create/get/reschedule/cancel все `not_started`, `manual_review_required=false`, mutations `0` |

## Сквозная матрица доказательств

| Сценарий | Минимальное доказательство | Статус |
|---|---|---|
| create → get → reschedule → get → cancel | mock E2E, затем permission-gated sandbox lifecycle и reconciliation | mock готов; sandbox blocked: User Token records access `403`, разрешённая попытка остановлена до mutation |
| duplicate/replay | одинаковый update/callback, одна mutation, terminal replay | local mock готов |
| race двух пользователей | один confirmed, второй slot unavailable | local mock готов |
| чужая запись/action | owner binding, protected GET, отсутствие раскрытия деталей | local mock готов |
| timeout / 429 / 5xx / malformed | fail-closed, нет текста об успехе и confirmed snapshot | local fake готов |
| outcome unknown | durable escalation/human mode и GET-only reconciliation | local fake готов |
| Telegram UI | opaque callback, exact confirmation, create/change/cancel | local mock готов |
| PostgreSQL | inbox/outbox/actions/events/escalations/audit replay и atomicity | local integration готов |
| scheduler/reminders | только confirmed owned snapshot; replace/skip после reschedule/cancel | отдельный post-booking gate pending |

Финальный отчёт должен для каждой строки добавить точную Docker-команду, node IDs, timestamp, pass count и санитизированные DB assertions. Незапущенные внешние проверки остаются явно `NOT RUN`.
