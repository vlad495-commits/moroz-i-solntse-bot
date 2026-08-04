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
| New sandbox browser fixture | Авторизованный YCLIENTS UI, только санитизированные counts | 2026-08-04 18:18 | `sandbox` | 0 | `companies=1`; `services=1`; configured bookable `staff=1`; future 60-minute `slots>=2` inside 14 days; clients `0`; records `0`; no API/lifecycle mutation, IDs, tokens or ПД |
| New sandbox partner catalog GET | `yclients-readonly` через isolated Compose namespace | 2026-08-04 18:24 | `sandbox` | 0 | `ok=true`; services/staff `1/1`; future slots `322` in 14 days; no User/Telegram/LLM/DB/queue secrets and no mutation method |
| New sandbox records preflight GET | `yclients-sandbox-preflight` через тот же isolated namespace | 2026-08-04 18:25 | `sandbox` | 1 | Fail-closed `definite_provider_failure`; services/staff/slots `1/1/322`; `matches=0`, `active_matches=0`, `success=false`; provider mutations `0` |
| New sandbox records preflight after app connection | `yclients-sandbox-preflight` через isolated Compose namespace | 2026-08-04 20:10 | `sandbox` | 0 | Existing app connected to the new branch with approved record/custom-field rights; `success=true`; services/staff/slots `1/1/322`; `matches=0`, `active_matches=0`; provider mutations `0` |
| New sandbox affected regression | `pytest -q tests/unit/booking/test_yclients_sandbox_preflight.py tests/unit/booking/test_yclients_sandbox_smoke.py tests/contract/booking tests/e2e/booking/test_yclients_fail_closed.py` через isolated Compose namespace | 2026-08-04 20:12 | `local-fake` | 0 | `207 passed in 63.32s`; external provider mutations absent; exact cleanup `containers=0 volumes=0 networks=0 images=0` |
| New sandbox ownership field gate | `yclients-sandbox-preflight` после UI-создания двух hidden text record fields | 2026-08-04 20:44 | `sandbox` | 0 | GET-only: `fields/services/staff/slots=2/1/1/322`; exact `moroz_booking_key` + `moroz_customer_id` contract; `matches=0 active_matches=0 success=true`; provider mutations `0` |
| Ownership gate affected regression | preflight/smoke + contract/fail-closed, затем полный booking/Telegram/PostgreSQL/reminders набор через Docker | 2026-08-04 21:07 | `local-fake` | 0 | Review-fix focused `78 passed`; affected `225 passed in 66.13s`; full booking flow `624 passed in 563.20s`; exact key требует связанный customer marker, unrelated records без requested key игнорируются |
| Second new-sandbox lifecycle + permission diagnosis | один отдельно разрешённый `yclients-smoke`, затем только GET reconciliation/permissions | 2026-08-04 21:50 | `sandbox` | 1 | `fields/services/staff/slots=2/1/1/322`; create/get confirmed; reschedule definite provider failure; встроенный exact cleanup confirmed; reconciliation `matches=1 active_matches=0`, unknown отсутствует. Официальный GET permissions подтвердил root cause: `records_edit_date_and_master_access=false`; повторного lifecycle не было |
| Pre-mutation permission gate regression | TDD field+permission gate, affected booking/contract/fail-closed и booking/Telegram/PostgreSQL/reminders через Docker | 2026-08-04 21:54 | `local-fake` | 0 | RED `4 failed`; focused GREEN `12 passed`; affected `227 passed in 64.31s`; full functional `624 passed`, Redis-dependent Telegram ingress после readiness `11 passed`; final exact source `80 passed`; re-review `0/0/0 approve`; внешний GET-only gate при missing transfer right ожидаемо остановился `exit 1` до каталога/mutation; cleanup `0/0/0/0` |

## Permission-gated sandbox lifecycle

Команда выполняется только после отдельного явного разрешения на lifecycle именно тестовой компании и фейкового набора данных:

```bash
cd project && docker compose --env-file ../.env --profile yclients-smoke run --rm yclients-smoke
```

Перед любым mutation профиль fail-closed требует: `YCLIENTS_SANDBOX_CONSENT=I_UNDERSTAND_THIS_CREATES_TEST_BOOKINGS`, `YCLIENTS_ENVIRONMENT_LABEL=sandbox`, имя с префиксом `Synthetic Test `, телефон строго формата `+7000` и ещё семь цифр, а также `YCLIENTS_TEST_WINDOW_DAYS` как целое от `1` до `14`. До каталога он GET-only проверяет оба ownership field и полный набор реально используемых прав системного пользователя, включая отдельное `records_edit_date_and_master_access`; любой missing/non-boolean permission останавливает flow до первого POST. Слоты читаются только в заданном окне; нужны два разных будущих слота. Затем bounded GET reconciliation по свежему UUID обязан вернуть строго `matches=0`, `active_matches=0`; auth/transport/shape failure или неожиданное совпадение также останавливают flow до mutation.

Каждый run использует новый UUID/key. Успех требует точную цепочку `create → get → reschedule → get → cancel → final get → reconciliation`, где итог reconciliation равен `matches=1`, `active_matches=0`. При неизвестном результате mutation дальнейшие mutations запрещены: выполняется ровно одна GET-only reconciliation и результат остаётся для ручной проверки. После definite сбоя post-create разрешена только одна cleanup-cancel по exact external ID/key, затем одна GET-only reconciliation; её ошибка также остаётся ручной проверкой. JSON-итог не содержит token, name, phone, run UUID или external booking ID.

Safety clarification after the 2026-08-04 incident: исходное разрешение на lifecycle с bounded cleanup не считается разрешением на out-of-band DELETE после `mutation_outcome_unknown`. Даже если UI/GET показывают ровно одну запись с fake identity, test service/staff и smoke-comment, cleanup остаётся mutation и требует нового отдельного явного разрешения на exact synthetic-запись. До такого разрешения допустимы только read-only reconciliation и escalation/manual review.

| Проверка | Команда / тесты | Время (UTC+3) | Среда | Exit | Санитизированный результат |
|---|---|---:|---|---:|---|
| Local Task 12 safety gate | `pytest tests/unit/booking/test_yclients_sandbox_smoke.py tests/contract/booking/test_yclients_adapter.py tests/unit/test_migration_profile.py -q` через Compose test profile | 2026-08-04 14:43 | `local-fake` | 0 | `167 passed in 59.30s`; exact consent/marker, ASCII fake identity, non-empty 1-day window, pre-mutation record-read gate, exact cancel ownership и single reconciliation/cleanup contracts covered |
| External sandbox lifecycle | `docker compose --env-file <external ignored .env> --profile yclients-smoke run -T --rm yclients-smoke` | 2026-08-04 14:42 | `sandbox` | 1 | Единственная разрешённая попытка: services/staff/slots `1/1/336`; records GET-preflight вернул definite provider failure (`HTTP 403` из отдельной санитизированной диагностики); create/get/reschedule/cancel все `not_started`, `manual_review_required=false`, mutations `0` |
| New sandbox lifecycle + bounded cleanup | тот же permission-gated профиль, затем read-only reconciliation и manual exact cleanup | 2026-08-04 20:32 | `sandbox` | 1 | Единственный create получил HTTP `201`, но response/ownership shape не подтвердился: `mutation_outcome_unknown`, `manual_review_required=true`; lifecycle runner остановился, blind create retry отсутствовал. UI/individual GET нашли одну synthetic-запись, после чего manual DELETE завершил cleanup и GET подтвердил `deleted=true`, active exact `0`. Это зафиксировано как process safety incident: исходное cleanup-разрешение не должно было расширяться на DELETE после unknown без нового отдельного разрешения. |
| Second new sandbox lifecycle | один новый отдельно разрешённый runner command с встроенным bounded cleanup | 2026-08-04 21:50 | `sandbox` | 1 | create/get confirmed; reschedule definite provider failure; runner выполнил один exact cleanup cancel, reconciliation `matches=1 active_matches=0`; `manual_review_required=false`, unknown отсутствует. Read-only permissions доказал missing `records_edit_date_and_master_access`; следующий mutation запрещён до исправления права, успешного GET-only gate и нового отдельного разрешения |

## Post-booking scheduler gate

Scheduler остаётся выключенным в staging до успешного external lifecycle и отдельного разрешения. Локальный gate проходит через Telegram mock workflow, реальный PostgreSQL `scheduler_jobs`, `LocalBookingPort` и `NotificationOutbox`; внешние Telegram/YCLIENTS вызовы отсутствуют.

| Проверка | Команда / тесты | Время (UTC+3) | Среда | Exit | Санитизированный результат |
|---|---|---:|---|---:|---|
| Telegram booking → reminders E2E | `pytest -q tests/e2e/notifications/test_booking_flow_reminders.py` через isolated Compose project `moroz-ea5c-task13` | 2026-08-04 15:53 | `local-mock` | 0 | `4 passed in 13.68s`; confirmed create создал exact future jobs, reschedule перевёл старые jobs в `skipped` и создал один новый план, replay не дал duplicate keys, cancel оставил pending `0`, foreign/outcome-unknown создали jobs `0`, immediate reminder replay дал один outbound тому же owner |
| Full notification regression | `pytest -q tests/unit/notifications tests/integration/notifications tests/e2e/notifications` в том же isolated Compose project | 2026-08-04 15:55 | `local-mock` | 0 | `49 passed in 39.65s`; scheduler repository/concurrency, retry/DLQ, lifecycle, feedback, ownership и новый Telegram gate зелёные |

## Сквозная матрица доказательств

| Сценарий | Минимальное доказательство | Статус |
|---|---|---|
| create → get → reschedule → get → cancel | mock E2E, затем permission-gated sandbox lifecycle и reconciliation | mock готов; sandbox create/get/cleanup доказаны, полный lifecycle blocked на отдельном праве `records_edit_date_and_master_access=false` |
| duplicate/replay | одинаковый update/callback, одна mutation, terminal replay | local mock готов |
| race двух пользователей | один confirmed, второй slot unavailable | local mock готов |
| чужая запись/action | owner binding, protected GET, отсутствие раскрытия деталей | local mock готов |
| timeout / 429 / 5xx / malformed | fail-closed, нет текста об успехе и confirmed snapshot | local fake готов |
| outcome unknown | durable escalation/human mode и GET-only reconciliation | local fake готов |
| Telegram UI | opaque callback, exact confirmation, create/change/cancel | local mock готов |
| PostgreSQL | inbox/outbox/actions/events/escalations/audit replay и atomicity | local integration готов |
| scheduler/reminders | только confirmed owned snapshot; replace/skip после reschedule/cancel | local Telegram/PostgreSQL gate готов; staging enable заблокирован до успешного external reschedule/full lifecycle и отдельного разрешения |

## Task 14: индекс финальных доказательств

Общая команда целевого прогона (2026-08-04 16:05 UTC+3):

```bash
cd project && docker compose -p moroz-ea5c-task13 --env-file ../tmp/task7.env --profile test run -T --rm test pytest -q tests/unit/booking tests/contract/booking tests/integration/booking tests/e2e/booking tests/unit/notifications tests/integration/notifications tests/e2e/notifications
```

Результат: exit `0`, `645 passed in 486.03s`. Полный прогон с read-only mounts `/repo/docs` и `/docs` завершён 2026-08-04 16:31 UTC+3: exit `0`, `1347 passed in 709.54s`. Ни один тест не использовал реальные Telegram/YCLIENTS mutations или реальные ПД.

| Сценарий | Репрезентативные node IDs из зелёного Docker-прогона | Санитизированные проверки состояния |
|---|---|---|
| create/get/reschedule/get/cancel | `tests/e2e/booking/test_telegram_change_flow.py::test_mock_telegram_create_list_reschedule_list_cancel_lifecycle` | Один owned confirmed snapshot после create; после reschedule сохранён новый слот; после cancel активная запись отсутствует. Внешний sandbox lifecycle: `NOT RUN` после fail-closed preflight `403`, mutations `0`. |
| duplicate/replay | `tests/e2e/booking/test_telegram_reliability.py::test_duplicate_confirmation_replay_has_one_terminal_provider_booking`; `tests/e2e/booking/test_telegram_create_flow.py::test_duplicate_task_and_callback_do_not_duplicate_outbox_or_booking` | Один terminal booking/event/outbox effect; повтор update/callback возвращает сохранённый результат без новой mutation. |
| slot race двух пользователей | `tests/e2e/booking/test_telegram_reliability.py::test_two_owners_racing_one_slot_get_one_truthful_winner`; `tests/e2e/booking/test_telegram_create_flow.py::test_two_users_racing_same_mock_slot_get_one_confirmed_booking` | Ровно один confirmed owner; второму возвращён slot unavailable; двойной confirmed snapshot отсутствует. |
| чужая запись/action | `tests/integration/booking/test_workflow_repository.py::test_complete_action_rejects_foreign_owner_and_conflicting_replay`; `tests/e2e/notifications/test_booking_flow_reminders.py::test_foreign_confirmation_callback_schedules_nothing` | Owner mismatch отклонён до provider effect; детали записи не раскрыты; scheduler jobs для чужого callback: `0`. |
| timeout/429/5xx/malformed | `tests/contract/booking/test_yclients_adapter.py::test_create_definite_429_is_temporary_without_retry`; `tests/contract/booking/test_yclients_adapter.py::test_book_check_5xx_wins_over_embedded_conflict_code`; `tests/contract/booking/test_yclients_adapter.py::test_malformed_create_success_is_outcome_unknown`; `tests/contract/booking/test_yclients_adapter.py::test_create_connection_drop_is_outcome_unknown_and_not_retried` | Нет ложного confirmed; definite/temporary и unknown исходы разделены; mutation вслепую не повторяется. |
| outcome unknown/reconciliation | `tests/e2e/booking/test_change_booking.py::test_outcome_unknown_is_durable_and_terminal_repeat_never_retries`; `tests/integration/booking/test_reconciliation.py::test_one_exact_match_atomically_confirms_and_closes_linked_escalation`; `tests/integration/booking/test_reconciliation.py::test_replay_and_concurrency_resolve_once` | Unknown сохранён как terminal fail-closed state с human mode/escalation; только один exact owned match закрывает escalation атомарно; replay/concurrency дают одно resolution. |
| Telegram UI | `tests/e2e/booking/test_telegram_create_flow.py::test_complete_mock_create_flow_persists_safe_history_and_keyboard`; `tests/e2e/booking/test_telegram_ingress.py::test_contact_must_belong_to_sender`; lifecycle node выше | Opaque callback, явное summary/confirmation, безопасная история и keyboard; чужой contact отклонён; create/change/cancel проходят через durable inbox path. |
| PostgreSQL inbox/outbox/events/escalations/audit | `tests/e2e/booking/test_telegram_ingress.py::test_duplicate_booking_callback_keeps_single_inbox_row_and_task`; `tests/integration/booking/test_booking_repository.py::test_confirm_atomically_persists_terminal_scenario_and_booking`; `tests/integration/booking/test_booking_escalation.py::test_booking_escalation_replay_and_concurrency_do_not_duplicate`; `tests/integration/test_admin_audit.py::test_record_audit_persists_jsonb_before_after` | Один inbox/task для дубля; booking/scenario/event фиксируются атомарно; одна escalation при replay/race; audit append-only содержит санитизированные before/after JSONB. |
| scheduler/reminders | Все четыре nodes `tests/e2e/notifications/test_booking_flow_reminders.py` | Confirmed create создаёт уникальные future jobs; reschedule помечает старые `skipped` и создаёт новый набор; cancel оставляет pending `0`; foreign/unknown дают jobs `0`; повтор delivery создаёт один owner-bound outbound. |

Runtime gates 2026-08-04 16:32 UTC+3: Compose build `worker scheduler` — exit `0/0`; `compileall /app` в обоих образах — exit `0/0`; Alembic — `0010_telegram_booking_flow (head)`. Scheduler в staging намеренно не включён: для этого сначала нужен успешный external sandbox lifecycle и отдельное разрешение на включение.
