# Customer Data Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить owner-only полное удаление локальных данных Telegram-клиента из PostgreSQL и Redis без изменений в YCLIENTS.

**Architecture:** Общий модуль `moroz.privacy` задаёт короткоживущую ingress-метку; webhook прекращает приём сообщений клиента, пока метка активна. Отдельный admin-сервис под advisory lock очищает точные Redis-ключи и удаляет связанные SQL-строки в одной транзакции, после чего пишет обезличенный audit event. Существующая карточка чата вызывает сервис через CSRF-protected POST с фразой `УДАЛИТЬ`.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, redis.asyncio, PostgreSQL 16, Redis 7, Jinja2, pytest, Docker Compose.

## Global Constraints

- Базовая продуктовая ветка: актуальная `codex/admin-ops-rc`; выполнять в отдельном worktree.
- Полная спецификация: `docs/superpowers/specs/2026-08-13-customer-data-deletion-design.md`.
- Только Docker: локальный `python` и локальный `pytest` не запускать.
- Не добавлять миграции, зависимости, очередь удаления или новую клиентскую сущность.
- Не импортировать и не вызывать YCLIENTS adapter.
- Не логировать `chat_id`, `user_id`, username, телефон, имя, тексты, external booking ID или booking key.
- Удалять Redis только по точным ключам; `SCAN`, wildcard deletion и `FLUSH*` запрещены.
- Все новые POST-пути проходят auth, CSRF и owner RBAC до обращения к Redis/PostgreSQL.
- После каждого task обновлять `Дорожная карта.md` и `changelog.md`, затем делать локальный commit.

---

### Task 1: Shared privacy marker and webhook ingress gate

**Files:**
- Create: `project/src/moroz/privacy.py`
- Modify: `project/llm/webhook.py`
- Modify: `project/tests/e2e/test_privacy_gate.py`

**Interfaces:**
- Produces: `deletion_marker_key(channel: str, chat_id: str) -> str`.
- Produces: `DELETION_MARKER_TTL_SECONDS = 300`.
- Consumes later: admin deletion service uses the same key and TTL.

- [ ] **Step 1: Write the failing webhook test**

Add a test beside the existing consent-flow cases. Seed valid processing consent and the marker, send a normal text update, and assert no durable ingress:

```python
@pytest.mark.asyncio
async def test_deletion_marker_blocks_new_telegram_ingress(webhook_client, db, redis_client):
    await db.execute(
        "INSERT INTO processing_consents (channel, user_id, consent_version) "
        "VALUES ('telegram', '7', 'v1')"
    )
    await redis_client.set("privacy:deleting:telegram:42", "1", ex=300)

    response = await webhook_client.post(
        "/telegram/webhook",
        json=telegram_text_update(update_id=901, chat_id=42, user_id=7, text="Привет"),
        headers=telegram_secret_headers(),
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval("SELECT count(*) FROM outbound_messages") == 0
```

- [ ] **Step 2: Run RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py -k deletion_marker
```

Expected: FAIL because webhook does not inspect the marker.

- [ ] **Step 3: Add the shared key helper**

Create `project/src/moroz/privacy.py`:

```python
"""Small shared contracts for active customer-data deletion."""

DELETION_MARKER_TTL_SECONDS = 300


def deletion_marker_key(channel: str, chat_id: str) -> str:
    return f"privacy:deleting:{channel}:{chat_id}"
```

- [ ] **Step 4: Gate webhook before consent and message acceptance**

Import `deletion_marker_key` in `project/llm/webhook.py`. After validating a private message and before pause/non-text/command/consent branches, return HTTP 200 when the exact marker exists:

```python
from moroz.privacy import deletion_marker_key

# inside telegram_webhook, after private-chat validation
if await webhook_app.state.redis.get(
    deletion_marker_key("telegram", str(message.chat.id))
):
    return Response(status_code=200)
```

Apply the same check to private callback queries before consent state is changed. Do not send a reply, create an outbox row, or log the identifiers.

- [ ] **Step 5: Run GREEN and commit**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py
```

Expected: all privacy-gate tests PASS.

Commit:

```powershell
git add project/src/moroz/privacy.py project/llm/webhook.py project/tests/e2e/test_privacy_gate.py "Дорожная карта.md" changelog.md
git commit -m "feat: блокировать входящие сообщения при удалении данных"
```

### Task 2: Transactional deletion service

**Files:**
- Create: `project/admin/customer_data_deletion.py`
- Create: `project/tests/unit/admin/test_customer_data_deletion.py`

**Interfaces:**
- Produces: `DeletionResult(status: Literal["deleted", "already_absent"], deleted_counts: dict[str, int])`.
- Produces: `delete_customer_data(*, pool, redis_client, chat_id: int, actor_id: int, ip_address: str | None, user_agent: str | None) -> DeletionResult`.
- Raises: `CustomerDataDeletionError` with no customer values in its message.

- [ ] **Step 1: Write unit tests for exact Redis scope and audit redaction**

Use fake pool/connection/Redis objects to assert:

```python
assert redis.deleted == {
    "chat:42:messages",
    "buffer:42",
    "consent:state:telegram:42:7",
}
assert redis.zremoved == [("buffer:deadlines", "42")]
assert result.deleted_counts["messages"] == 2
assert audit_args[2] == "customer_data"
assert audit_args[3] is None
assert "42" not in json.dumps(audit_args[5], ensure_ascii=False)
assert "7" not in json.dumps(audit_args[5], ensure_ascii=False)
```

Also assert that Redis failure occurs before destructive SQL and that exception/log text contains only `CustomerDataDeletionError`.

- [ ] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_data_deletion.py
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the minimal service**

Create immutable result/error types and these private stages:

```python
from dataclasses import dataclass
from typing import Literal

from moroz.privacy import DELETION_MARKER_TTL_SECONDS, deletion_marker_key


class CustomerDataDeletionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeletionResult:
    status: Literal["deleted", "already_absent"]
    deleted_counts: dict[str, int]
```

The public function must:

```python
marker = deletion_marker_key("telegram", str(chat_id))
await redis_client.set(marker, "1", ex=DELETION_MARKER_TTL_SECONDS)
try:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
    str(chat_id),
            )
finally:
    try:
        await redis_client.delete(marker)
    except Exception:
        pass  # TTL releases ingress; caller result must not be masked after commit
```

Immediately after the advisory lock, in the same transaction, collect identities with parameterized `SELECT` queries, clear the exact Redis keys, execute the following deletion manifest child-first, verify every matching count is zero, and insert the redacted audit row. Use parameterized SQL only. The manifest is exact:

```text
booking_events by scenario_id
scheduler_jobs by booking_key or payload->>'customer_id'
notification_feedback_requests by customer_id
bookings by customer_id
booking_scenarios by customer_id
escalations by customer_id
human_mode by customer_id
outbound_messages by channel/chat_id
task_outbox by exact top-level payload chat_id/user_id/customer_id
message_inbox by channel/chat_id
processing_consents by channel and collected user_ids
token_usage by numeric chat_id or collected numeric user_ids
messages by numeric chat_id
security_incidents by chat_id only when to_regclass returns non-null
```

Parse asyncpg command tags (`DELETE n`) into counts. Set `status="already_absent"` only when all PostgreSQL counts are zero; Redis cleanup still runs. Audit `after` is limited to `{"channel": "telegram", "status": status, "deleted_counts": counts}` and `object_id` is `NULL`.

- [ ] **Step 4: Run GREEN and commit**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_data_deletion.py
```

Expected: PASS.

```powershell
git add project/admin/customer_data_deletion.py project/tests/unit/admin/test_customer_data_deletion.py "Дорожная карта.md" changelog.md
git commit -m "feat: добавить транзакционное удаление данных клиента"
```

### Task 3: Real PostgreSQL and Redis integration coverage

**Files:**
- Create: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`

**Interfaces:**
- Consumes: `delete_customer_data(...)` from Task 2.
- Verifies: current Alembic head `0009_production_admin` without a new migration.

- [ ] **Step 1: Seed every customer-owned table and exact Redis keys**

The integration fixture must create one target customer (`chat_id=42`, `user_id=7`) and one control customer (`chat_id=84`, `user_id=8`). Insert target/control rows into every table in the manifest using valid UUID relationships. Seed:

```python
await redis_client.rpush("chat:42:messages", '{"role":"user","content":"secret"}')
await redis_client.rpush("buffer:42", '{"update_id":"1","text":"secret"}')
await redis_client.zadd("buffer:deadlines", {"42": 1, "84": 1})
await redis_client.set("consent:state:telegram:42:7", "pii")
await redis_client.set("bot:paused", "1")
```

- [ ] **Step 2: Assert complete target deletion and control preservation**

Call the service and query every table with exact target predicates. Assert zero target rows, unchanged control rows, absent target Redis keys, preserved `buffer:deadlines` member `84`, and preserved `bot:paused`.

Read the newest `customer_data.delete` audit row and recursively assert forbidden values are absent:

```python
serialized = json.dumps(dict(audit_row), ensure_ascii=False, default=str)
for forbidden in ("42", "7", "secret", "target-user"):
    assert forbidden not in serialized
```

- [ ] **Step 3: Assert rollback behavior**

Inject a Redis client whose `delete` raises before SQL deletion. Assert all target PostgreSQL rows remain. Then force a SQL failure after Redis cleanup and assert the transaction preserves PostgreSQL rows while cache loss is accepted.

- [ ] **Step 4: Run integration GREEN and commit**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/admin/test_customer_data_deletion_postgres.py
```

Expected: PASS.

```powershell
git add project/tests/integration/admin/test_customer_data_deletion_postgres.py "Дорожная карта.md" changelog.md
git commit -m "test: проверить полное удаление данных клиента"
```

### Task 4: Owner-only admin route and danger-zone UI

**Files:**
- Create: `project/admin/customer_data_routes.py`
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/chat_detail.html`
- Modify: `project/admin/templates/chats_list.html`
- Modify: `project/admin/static/styles.css`
- Modify: `project/tests/e2e/admin/test_csrf_rbac_audit.py`

**Interfaces:**
- Produces: `POST /customer-data/delete`; `chat_id` передаётся в теле формы и не попадает в access-log URL.
- Consumes: `delete_customer_data(...)` and existing auth/RBAC/CSRF helpers.

- [ ] **Step 1: Write route security tests**

Add tests proving wrong confirmation, missing CSRF and role `admin` return before `_redis_client` or deletion service is called:

```python
response = await client.post(
    "/customer-data/delete",
    data={"chat_id": "42", "csrf_token": "known-csrf", "confirmation": "НЕ УДАЛЯТЬ"},
)
assert response.status_code == 400

response = await admin_client.post(
    "/customer-data/delete",
    data={"chat_id": "42", "csrf_token": "known-csrf", "confirmation": "УДАЛИТЬ"},
)
assert response.status_code == 403
```

Add an owner success test that stubs the service and asserts redirect `/\?deleted=1` without putting `chat_id` in query parameters.

- [ ] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/admin/test_csrf_rbac_audit.py -k customer_data
```

Expected: FAIL because route/module/UI do not exist.

- [ ] **Step 3: Implement the route in fail-closed order**

Create `customer_data_routes.py` with this validation order:

```python
user = await get_current_user(request)
validate_csrf(user, csrf_token)
require_role(user, {"owner"})
if confirmation != "УДАЛИТЬ":
    raise HTTPException(status_code=400, detail="bad_confirmation")
```

Then create a decode-responses Redis client, call the service using `database._pool`, and always close Redis. Log only `customer_data_delete_failed error_type=<type>` on failure. Return `/\?deleted=1`, `/\?deleted=already_absent`, or `/chats/{chat_id}?delete_error=unavailable`; do not include identifiers in error text or query values.

Register the router in `app.py`.

- [ ] **Step 4: Add owner-only danger zone**

In `chat_detail.html`, render only for owner:

```html
{% if user.role == 'owner' %}
<section class="danger-zone">
  <h2>Удаление данных</h2>
  <p>Удалит историю, согласие, локальные записи и временную память. YCLIENTS не изменится.</p>
  <form method="post" action="{{ request.scope.get('root_path', '') }}/customer-data/delete">
    <input type="hidden" name="chat_id" value="{{ chat.chat_id }}">
    <input type="hidden" name="csrf_token" value="{{ user.csrf_token }}">
    <label>Для подтверждения введите УДАЛИТЬ</label>
    <input name="confirmation" autocomplete="off" required>
    <button class="danger-button" type="submit">Удалить локальные данные</button>
  </form>
</section>
{% endif %}
```

Add a neutral success banner to `chats_list.html` based only on `request.query_params.get('deleted')`, and minimal styles in `styles.css` reusing current spacing, border radius and typography.

- [ ] **Step 5: Run GREEN and commit**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/admin/test_csrf_rbac_audit.py
```

Expected: PASS.

```powershell
git add project/admin/customer_data_routes.py project/admin/app.py project/admin/templates/chat_detail.html project/admin/templates/chats_list.html project/admin/static/styles.css project/tests/e2e/admin/test_csrf_rbac_audit.py "Дорожная карта.md" changelog.md
git commit -m "feat: добавить удаление данных в админке"
```

### Task 5: Concurrency, consent return and release documentation

**Files:**
- Modify: `project/tests/e2e/test_privacy_gate.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `project/ops/backup-runbook.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Verifies: webhook marker + worker advisory lock + deletion transaction form one coherent boundary.

- [ ] **Step 1: Add concurrency and return-flow tests**

Use two PostgreSQL connections. Hold `pg_advisory_xact_lock(hashtextextended('42', 0))` on the first; start deletion on the second and assert it remains pending. Release the first transaction and assert deletion completes. Separately hold the real Redis `lock:buffer:42` and prove deletion waits instead of deleting another owner's lock.

After deletion, remove/expire the marker and send a new normal message. Assert no `message_inbox` row and one consent prompt outbound row. This proves the user returns as new without restoring deleted history.

- [ ] **Step 2: Document backup boundary**

Add an explicit privacy paragraph to the existing restore runbook:

```markdown
### Privacy gate after restore

A restored database remains isolated. Do not route Telegram, worker, scheduler or admin traffic to it until the backup age and customer-deletion window have been reviewed. Backup archives expire under the configured retention policy; never overwrite production directly from an older archive.
```

Record the configured backup retention value or mark it as a blocking launch input if the deployment value is not yet available.

- [ ] **Step 3: Run focused and full Docker verification**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_data_deletion.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/test_privacy_gate.py
docker compose --env-file ../.env run --rm test pytest -q
docker compose --env-file ../.env config --quiet
git diff --check
```

Expected: focused suite PASS, full suite PASS, Compose exit 0, diff check clean.

- [ ] **Step 4: Final privacy review**

Search the changed code and audit assertions:

```powershell
rg -n "customer_data_delete|customer_data\.delete|privacy:deleting" project
rg -n "logger\..*(chat_id|user_id|message|payload|booking_key)" project/admin/customer_data_deletion.py project/admin/customer_data_routes.py
```

Expected: first command shows only designed paths; second produces no matches.

- [ ] **Step 5: Close roadmap and commit**

Mark the P0 roadmap item complete with exact test evidence, add a timestamped changelog entry, then:

```powershell
git add project docs "Дорожная карта.md" changelog.md
git commit -m "feat: завершить полное удаление данных клиента"
```

Do not push, deploy, call Telegram, or mutate YCLIENTS without a separate explicit request.
