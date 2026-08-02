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
| External YCLIENTS read-only | `docker compose --env-file ../.env --profile yclients-readonly run --rm yclients-readonly` | — | company scope not confirmed | — | **NOT RUN — awaiting explicit company/sandbox confirmation** |

## Сквозная матрица доказательств

| Сценарий | Минимальное доказательство | Статус |
|---|---|---|
| create → get → reschedule → get → cancel | mock E2E, затем permission-gated sandbox lifecycle и reconciliation | mock готов; sandbox pending |
| duplicate/replay | одинаковый update/callback, одна mutation, terminal replay | local mock готов |
| race двух пользователей | один confirmed, второй slot unavailable | local mock готов |
| чужая запись/action | owner binding, protected GET, отсутствие раскрытия деталей | local mock готов |
| timeout / 429 / 5xx / malformed | fail-closed, нет текста об успехе и confirmed snapshot | local fake готов |
| outcome unknown | durable escalation/human mode и GET-only reconciliation | local fake готов |
| Telegram UI | opaque callback, exact confirmation, create/change/cancel | local mock готов |
| PostgreSQL | inbox/outbox/actions/events/escalations/audit replay и atomicity | local integration готов |
| scheduler/reminders | только confirmed owned snapshot; replace/skip после reschedule/cancel | отдельный post-booking gate pending |

Финальный отчёт должен для каждой строки добавить точную Docker-команду, node IDs, timestamp, pass count и санитизированные DB assertions. Незапущенные внешние проверки остаются явно `NOT RUN`.
