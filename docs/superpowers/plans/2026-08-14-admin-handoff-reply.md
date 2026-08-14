# Admin Handoff Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Отправлять ответ owner/admin из открытой эскалации через текущий ordered outbox и возвращать бота только после подтверждённой доставки с синхронизированной историей/LLM-контекстом.

**Architecture:** PostgreSQL `human_mode` блокирует LLM под существующим customer advisory lock. Admin только атомарно ставит обычный outbound/task в очередь. Namespaced idempotency key связывает outbound с эскалацией, а delivery completion одной транзакцией пишет history/audit, resolve и пяти минутный cooldown; Redis остаётся best-effort кэшем и инвалидируется после durable completion.

**Tech Stack:** Python 3.12, FastAPI/Jinja2, asyncpg через существующий `Database`, PostgreSQL 16, Redis 7, текущий RabbitMQ/outbox/Telegram worker, pytest, Docker Compose.

## Global Constraints

- Рабочая ветка: `codex/admin-handoff-reply` от exact base `codex/admin-ops-rc` (`9cd5b46`).
- Новых таблиц, миграций, очередей, зависимостей и прямых Telegram/provider HTTP-вызовов из admin нет.
- Доступ только существующим ролям `owner` и `admin`; CSRF проверяется до mutation.
- Raw reply/customer/payload/provider data не попадают в audit и логи.
- Human mode выключается только после `outbound_messages.status='sent'` и только при отсутствии других открытых эскалаций.
- Все тесты и runtime gates запускаются только через Docker Compose с локальным external `.env`; staging, production и YCLIENTS не вызываются.
- Push не выполняется.

---

### Task 1: PostgreSQL human-mode fence и cooldown

**Files:**
- Modify: `project/worker/main.py`
- Modify: `project/src/moroz/escalation/service.py`
- Modify: `project/tests/e2e/test_message_delivery.py`
- Modify: `project/tests/e2e/notifications/test_feedback.py`

**Interfaces:**
- Consumes: `customer_lock_subject(customer_id)`, `human_mode`, `messages`, `message_inbox`.
- Produces: worker materialization без LLM при active human mode; `EscalationService.create_low_rating(...) -> UUID | None` с cooldown.

- [ ] **Step 1: Write worker RED**

Добавить E2E-кейс: включить `human_mode` для chat `42`, принять inbox `100`, вызвать `MessageTaskHandler.handle`, затем проверить ровно одно `messages(role='user')`, inbox `processed`, `llm.calls == []`, отсутствие `token_usage`, `outbound_messages` и `task_outbox`. Повтор handler не создаёт дубль.

- [ ] **Step 2: Run worker RED**

```powershell
Set-Location project
$localEnv = Join-Path (Split-Path -Parent (git rev-parse --git-common-dir)) '.env'
docker compose --env-file $localEnv run --build --rm test pytest -q tests/e2e/test_message_delivery.py -k human_mode
```

Expected: FAIL, потому что текущий worker вызывает LLM и создаёт outbound.

- [ ] **Step 3: Implement minimal worker fence**

Внутри уже существующей customer-lock транзакции, после валидации persisted payload и до чтения context/LLM:

```python
human_mode = await connection.fetchval(
    "SELECT enabled FROM human_mode WHERE customer_id = $1",
    chat_id,
)
if human_mode:
    await connection.execute(
        "INSERT INTO messages (chat_id, user_id, role, content) "
        "VALUES ($1, $2, 'user', $3)",
        numeric_chat_id, user_id, persisted_text,
    )
    await connection.execute(
        "UPDATE message_inbox SET status='processed' "
        "WHERE channel='telegram' AND external_message_id=ANY($1::text[])",
        accepted_ids,
    )
    return
```

Переиспользовать один `accepted_ids` для обеих веток, не выносить новую abstraction.

- [ ] **Step 4: Write cooldown RED**

В feedback E2E сначала создать disabled `human_mode` с `expires_at=now()+5 minutes`, вызвать `record_rating(rating=2)` и проверить `None`, отсутствие новой escalation и неизменность строки. Отдельно expired cooldown создаёт escalation и снова включает human mode.

- [ ] **Step 5: Run cooldown RED**

Expected: FAIL, потому что `create_low_rating` всегда создаёт эскалацию.

- [ ] **Step 6: Implement customer-locked cooldown**

В `create_low_rating` до INSERT взять `pg_advisory_xact_lock(customer_lock_subject(customer_id))`, прочитать `enabled, expires_at`; вернуть `None`, только если `enabled=false AND expires_at > now()`. Аннотацию изменить на `UUID | None`; существующий upsert сохраняется.

- [ ] **Step 7: Run GREEN and affected regressions**

Запустить оба изменённых файла. Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add project/worker/main.py project/src/moroz/escalation/service.py project/tests/e2e/test_message_delivery.py project/tests/e2e/notifications/test_feedback.py changelog.md
git commit -m "fix: соблюдать human mode в worker"
```

---

### Task 2: Идемпотентный admin enqueue и форма ответа

**Files:**
- Modify: `project/src/moroz/escalation/service.py`
- Modify: `project/admin/database.py`
- Modify: `project/admin/escalation_routes.py`
- Modify: `project/admin/templates/escalations.html`
- Modify: `project/tests/integration/admin/test_escalation_queue_postgres.py`
- Modify: `project/tests/e2e/admin/test_admin_escalation_queue.py`

**Interfaces:**
- Produces: `admin_reply_key(escalation_id: UUID, reply_token: UUID) -> str`; `parse_admin_reply_key(value: str) -> tuple[UUID, UUID] | None`; `database.enqueue_escalation_reply(...) -> tuple[str, UUID | None]`; `POST /escalations/{id}/reply`.
- Consumes: `MessageRepository.enqueue_outbound_in_transaction`, existing auth/RBAC/CSRF/request metadata.

- [ ] **Step 1: Write key/validation RED**

Проверить exact round-trip `admin_handoff_reply:{escalation_uuid}:{reply_uuid}` и fail-closed `None` для другого prefix, лишних частей и malformed UUID. Route-кейсы: whitespace/409, >4096/422, bad token/422, bad CSRF/403 до DB.

- [ ] **Step 2: Run RED**

Expected: import/404 failures — helpers и route отсутствуют.

- [ ] **Step 3: Implement key helpers and route validation**

Helpers разместить в существующем `moroz.escalation.service`. Route принимает `reply_text: str = Form("")`, `reply_token: UUID = Form(...)`, применяет `text = reply_text.strip()` и exact bounds `1..4096`, затем вызывает DB. `not_found -> 404`, `inactive -> 409`, иначе redirect `?reply=queued|already_queued`.

- [ ] **Step 4: Write transactional enqueue RED**

Seed open escalation + enabled mode + real admin actor. Первый вызов должен создать по одной строке outbound/task/audit; outbound pending и text exact. Повтор с тем же token возвращает `already_queued` и тот же ID без дублей. Проверить audit action `escalation.reply_queued`, object escalation ID, fixed after с outbound ID/status, отсутствие customer/reply/payload. Unknown/inactive ничего не создают. Trigger failure на audit откатывает outbound и task.

- [ ] **Step 5: Implement enqueue transaction**

Последовательность: non-lock lookup customer → common advisory lock → `SELECT escalation ... FOR UPDATE` → `SELECT human_mode ... FOR UPDATE` → pre-check exact idempotency key → `MessageRepository(database._pool).enqueue_outbound_in_transaction(...)` → audit INSERT. Возвращать только fixed statuses и outbound UUID.

- [ ] **Step 6: Replace immediate resolve UI**

Для каждой строки route создаёт новый `reply_token`; template показывает textarea `required maxlength=4096`, CSRF/token hidden и кнопку «Ответить клиенту». Старую форму «Закрыть и вернуть бота» и POST resolve удалить из router; DB `resolve_escalation` удалить как обход delivery gate. Success notices не выводят текст.

- [ ] **Step 7: Run GREEN and regressions**

Запустить admin integration/E2E, CSRF/RBAC и customer events tests. Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add project/src/moroz/escalation/service.py project/admin/database.py project/admin/escalation_routes.py project/admin/templates/escalations.html project/tests/integration/admin/test_escalation_queue_postgres.py project/tests/e2e/admin/test_admin_escalation_queue.py changelog.md
git commit -m "feat: ставить ответ администратора в outbox"
```

---

### Task 3: Delivery-confirmed history, audit, resolve и context sync

**Files:**
- Modify: `project/src/moroz/escalation/service.py`
- Modify: `project/src/moroz/messaging/repository.py`
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: `project/worker/main.py`
- Modify: `project/admin/customer_events.py`
- Modify: `project/admin/database.py`
- Modify: `project/tests/e2e/test_message_delivery.py`
- Modify: `project/tests/unit/admin/test_customer_events.py`
- Modify: `project/tests/integration/admin/test_customer_events_postgres.py`

**Interfaces:**
- Produces: `complete_admin_reply_delivery(connection, *, outbound_id, escalation_id, chat_id, text) -> bool`; `MessageRepository.mark_outbound_sent(...) -> str | None`; optional `TelegramSender(..., context_cache=None)`.
- Consumes: queued audit actor/request metadata, existing outbound claim/order and Redis client.

- [ ] **Step 1: Write delivery RED matrix**

Через настоящий repository/sender проверить:

- до send: open/enabled, history без admin reply;
- confirmed send: outbound sent, assistant history exact once, selected escalation resolved, safe delivered audit, last open disables mode and sets expiry approximately +5 minutes;
- second open keeps mode enabled and no cooldown;
- duplicate callback no duplicate history/audit;
- network unknown and definite failure do not resolve/write history;
- mismatched chat/escalation and malformed namespaced key fail closed atomically;
- queued-audit trigger/delivery-audit trigger failure rolls back all PostgreSQL completion changes.

- [ ] **Step 2: Run delivery RED**

Expected: FAIL — current mark only updates outbound status.

- [ ] **Step 3: Implement domain completion**

В `complete_admin_reply_delivery` lock escalation/human row, validate open/customer match, load the unique queued audit by escalation/outbound ID, insert assistant `messages` using latest known user metadata, resolve selected escalation, test remaining open count, update human mode/cooldown, insert fixed delivered audit. Не включать reply/customer/provider values в audit.

- [ ] **Step 4: Wire atomic mark-sent**

`mark_outbound_sent` открывает transaction, делает `UPDATE ... WHERE status='sending' RETURNING channel,chat_id,text,idempotency_key`; обычный key возвращает `None`. Валидный admin key вызывает completion в той же transaction и возвращает chat ID для cache invalidation. Если conditional UPDATE ничего не вернул, side effects не выполняются.

- [ ] **Step 5: Add best-effort Redis invalidation**

`TelegramSender` после успешного durable completion удаляет `chat:{chat_id}:messages` через optional injected Redis client. Catch/log только error type. Worker runtime передаёт уже созданный `redis_client`; новых connections нет. Ошибка cache не меняет sent/durable state.

- [ ] **Step 6: Add safe event-journal projection**

Добавить titles queued/delivered. В SQL read-model присоединить только allowlisted audit actions к customer через `audit.object_type='escalation' AND audit.object_id=escalation.id::text`; description `NULL`, status fixed из action. Audit customer ID не хранит.

- [ ] **Step 7: Run GREEN and focused regressions**

Запустить delivery, escalation admin, customer journal/deletion/privacy tests. Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add project/src/moroz/escalation/service.py project/src/moroz/messaging/repository.py project/src/moroz/messaging/telegram.py project/worker/main.py project/admin/customer_events.py project/admin/database.py project/tests changelog.md
git commit -m "feat: завершать handoff после доставки"
```

---

### Task 4: Документация, независимый review и полный gate

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify if review finds a defect: only files already in Tasks 1–3 and focused tests.

- [ ] **Step 1: Run affected Docker gate**

Admin escalation/journal/RBAC, message delivery, feedback, privacy and customer deletion. Record exact count.

- [ ] **Step 2: Request independent review**

Review exact `9cd5b46..HEAD` against spec for races, outbox order/idempotency, network ambiguity, human-mode fence, history/context, RBAC/CSRF/audit/PII. Исправить каждый Critical/Important отдельным RED/GREEN; Minor только если минимален и в scope.

- [ ] **Step 3: Run full fresh Docker gate**

```powershell
Set-Location project
$localEnv = Join-Path (Split-Path -Parent (git rev-parse --git-common-dir)) '.env'
docker compose --env-file $localEnv run --build --rm --volume "${PWD}/../docs:/docs:ro" test pytest -q
```

Expected: exit 0, zero failures.

- [ ] **Step 4: Static/cleanliness gates**

```powershell
git diff --check
$localEnv = Join-Path (Split-Path -Parent (git rev-parse --git-common-dir)) '.env'
docker compose --env-file $localEnv run --build --rm test python -m compileall -q admin src worker
git status --short --branch
```

Expected: diff/compile exit 0; only intended docs before closure commit.

- [ ] **Step 5: Close roadmap/changelog and commit**

Отметить только пункт admin handoff reply выполненным, записать exact focused/full counts, review verdict, отсутствие push/deploy/external calls.

```powershell
git add 'Дорожная карта.md' changelog.md
git commit -m "docs: завершить ответ из эскалации"
```

- [ ] **Step 6: Final verification**

Проверить clean worktree, exact HEAD, ancestry от `codex/admin-ops-rc`, локальные commits и отсутствие push. Выдать merge readiness обратно в `codex/admin-ops-rc` без выполнения merge.
