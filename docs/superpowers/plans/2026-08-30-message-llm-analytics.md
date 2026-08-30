# Пометочная LLM-аналитика сообщений — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Точно связывать всю LLM-цепочку с входящим сообщением пользователя и показывать в деталях диалога общий расход вместе с разбивкой по purpose/model.

**Architecture:** Additive migration помечает новые наблюдаемые user messages и добавляет nullable FK из `token_usage`. Worker записывает связь в одной транзакции, admin одним дополнительным grouped query собирает usage без N+1, существующий pricing helper считает каждую model-group отдельно, а Jinja отображает три согласованных состояния.

**Tech Stack:** Python 3.12, asyncpg, Alembic/SQLAlchemy, FastAPI, Jinja2, pytest, Docker Compose.

## Global Constraints

- Проект и все тесты запускаются только через Docker Compose с `--env-file ../.env`.
- Старые `messages` и `token_usage` не backfill-ятся и не связываются по времени.
- Верхние chat/list/global aggregates продолжают учитывать все старые usage-строки.
- Аналитика показывается только под user message; assistant message её не дублирует.
- Отображаются общий итог и группы `(purpose, model)` только для фактически состоявшихся вызовов.
- Новых зависимостей, внешних LLM-вызовов, staging/production действий и push нет.

---

### Task 1: Additive schema for exact message linkage

**Files:**
- Create: `project/migrations/versions/0020_message_llm_analytics.py`
- Create: `project/tests/unit/admin/test_migration_0020.py`
- Verify: `project/tests/integration/test_migrations.py`

**Interfaces:**
- Produces: nullable `messages.llm_usage_tracked`.
- Produces: nullable `token_usage.source_message_id -> messages.id ON DELETE CASCADE`.
- Produces: `idx_token_usage_source_message_id`.

- [ ] **Step 1: Write the failing migration contract test**

```python
from pathlib import Path


MIGRATION = Path("/workspace/migrations/versions/0020_message_llm_analytics.py")


def test_migration_adds_nullable_tracking_and_exact_usage_link():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0020_message_llm_analytics"' in text
    assert 'down_revision = "0019_router_v2"' in text
    assert 'op.add_column("messages"' in text
    assert '"llm_usage_tracked"' in text
    assert 'op.add_column("token_usage"' in text
    assert '"source_message_id"' in text
    assert 'ondelete="CASCADE"' in text
    assert '"idx_token_usage_source_message_id"' in text
    assert "server_default" not in text
    assert "UPDATE messages" not in text


def test_migration_downgrade_removes_owned_objects_in_dependency_order():
    text = MIGRATION.read_text(encoding="utf-8")
    downgrade = text.split("def downgrade", 1)[1]

    index = downgrade.index('op.drop_index("idx_token_usage_source_message_id"')
    constraint = downgrade.index("op.drop_constraint")
    usage_column = downgrade.index('op.drop_column("token_usage", "source_message_id")')
    message_column = downgrade.index('op.drop_column("messages", "llm_usage_tracked")')
    assert index < constraint < usage_column < message_column
```

- [ ] **Step 2: Run RED and confirm the missing migration is the reason**

Run from `project/`:

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/admin/test_migration_0020.py
```

Expected: FAIL with `FileNotFoundError` for `0020_message_llm_analytics.py`.

- [ ] **Step 3: Add the minimal additive migration**

```python
"""Link token usage to an observed user message."""

from alembic import op
import sqlalchemy as sa


revision = "0020_message_llm_analytics"
down_revision = "0019_router_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("llm_usage_tracked", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "token_usage",
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_token_usage_source_message_id_messages",
        "token_usage",
        "messages",
        ["source_message_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_token_usage_source_message_id",
        "token_usage",
        ["source_message_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_token_usage_source_message_id", table_name="token_usage")
    op.drop_constraint(
        "fk_token_usage_source_message_id_messages",
        "token_usage",
        type_="foreignkey",
    )
    op.drop_column("token_usage", "source_message_id")
    op.drop_column("messages", "llm_usage_tracked")
```

- [ ] **Step 4: Run GREEN plus real migration cycle**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/admin/test_migration_0020.py tests/integration/test_migrations.py
```

Expected: PASS; Alembic reports one head `0020_message_llm_analytics` and upgrade/downgrade cycles remain green.

- [ ] **Step 5: Log and commit the schema step**

Update `changelog.md`, then:

```powershell
git add project/migrations/versions/0020_message_llm_analytics.py project/tests/unit/admin/test_migration_0020.py changelog.md
git commit -m "feat: добавлена схема аналитики сообщений"
```

---

### Task 2: Persist one source message id for the whole LLM chain

**Files:**
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `project/tests/integration/test_worker_usage_postgres.py`

**Interfaces:**
- Consumes: `messages.llm_usage_tracked`, `token_usage.source_message_id` from Task 1.
- Produces: `_persist_token_usage(connection, chat_id, user_id, source_message_id, result) -> None`.
- Guarantees: every physical `LLMUsage` row from one processing result references the same saved user message.

- [ ] **Step 1: Change the unit test first to require the exact source id**

Update the existing persistence call and assertion:

```python
await worker_main._persist_token_usage(connection, 81, 82, 901, result)

persisted_usage = [args[2:] for _query, args in connection.executions]
assert persisted_usage == [
    (901, "router", 3, 1, 0, 4, "router-model"),
    (901, "answer", 9, 4, 1, 13, "answer-model"),
]
assert all("source_message_id" in query for query, _args in connection.executions)
```

Pass `901` to both legacy fallback calls and expect it before the purpose in `test_worker_persistence_falls_back_only_for_non_zero_legacy_answer`.

- [ ] **Step 2: Extend the PostgreSQL test before production changes**

After processing, fetch the observed message and linked usage:

```python
message = await connection.fetchrow(
    "SELECT id, llm_usage_tracked FROM messages "
    "WHERE chat_id = 81 AND role = 'user'"
)
rows = await connection.fetch(
    "SELECT source_message_id, purpose, prompt_tokens, completion_tokens, "
    "cached_tokens, total_tokens, model FROM token_usage ORDER BY id"
)

assert message["llm_usage_tracked"] is True
assert {row["source_message_id"] for row in rows} == {message["id"]}
```

Add a no-LLM human-mode case:

```python
async def test_human_mode_marks_message_observed_without_usage(database):
    async with database.acquire() as connection:
        await connection.execute(
            "INSERT INTO human_mode (customer_id, enabled) VALUES ('93', true)"
        )
    await MessageRepository(database).accept(
        IncomingMessage(
            update_id="human-usage-1",
            message_id="human-message-1",
            channel="telegram",
            chat_id="93",
            user_id="94",
            text="Сообщение оператору",
            received_at=datetime(2026, 8, 30, tzinfo=UTC),
            correlation_id=uuid4(),
        )
    )

    async def forbidden_llm(*_args, **_kwargs):
        raise AssertionError("human mode must not call LLM")

    await MessageTaskHandler(database, forbidden_llm, telegram=None).handle(
        QueueTask(
            kind="process_message",
            payload={"update_ids": ["human-usage-1"]},
            idempotency_key="process_message:human-usage-1",
        )
    )
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT llm_usage_tracked FROM messages WHERE chat_id = 93"
        )
        usage_count = await connection.fetchval(
            "SELECT count(*) FROM token_usage WHERE chat_id = 93"
        )
    assert row["llm_usage_tracked"] is True
    assert usage_count == 0
```

- [ ] **Step 3: Run RED and verify both missing behaviors**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/test_worker.py::test_worker_persists_each_consumed_usage_as_its_own_row tests/unit/test_worker.py::test_worker_persistence_falls_back_only_for_non_zero_legacy_answer tests/integration/test_worker_usage_postgres.py
```

Expected: FAIL because `_persist_token_usage` has the old signature and new messages/source rows are untracked.

- [ ] **Step 4: Implement the minimal transactional linkage**

Change the persistence signature and insert:

```python
async def _persist_token_usage(
    connection, chat_id: int, user_id: int, source_message_id: int, result
) -> None:
    usages = getattr(result, "usage", ())
    if not usages and result.total_tokens > 0:
        usages = (
            LLMUsage(
                "answer",
                result.prompt_tokens,
                result.completion_tokens,
                result.cached_tokens,
                result.total_tokens,
                result.model,
            ),
        )
    for usage in usages:
        if usage.total_tokens <= 0:
            continue
        await connection.execute(
            """
            INSERT INTO token_usage
                (chat_id, user_id, source_message_id, purpose, prompt_tokens,
                 completion_tokens, cached_tokens, total_tokens, model)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            chat_id,
            user_id,
            source_message_id,
            usage.purpose,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cached_tokens,
            usage.total_tokens,
            usage.model,
        )
```

In human mode, add the explicit observation marker:

```sql
INSERT INTO messages
    (chat_id, user_id, role, content, llm_usage_tracked)
VALUES ($1, $2, 'user', $3, TRUE)
```

In the normal path, replace the two-row insert with two simple statements in the same existing transaction:

```python
source_message_id = await connection.fetchval(
    """
    INSERT INTO messages
        (chat_id, user_id, role, content, llm_usage_tracked)
    VALUES ($1, $2, 'user', $3, TRUE)
    RETURNING id
    """,
    numeric_chat_id,
    user_id,
    persisted_text,
)
await connection.execute(
    """
    INSERT INTO messages (chat_id, user_id, role, content)
    VALUES ($1, $2, 'assistant', $3)
    """,
    numeric_chat_id,
    user_id,
    result.text,
)
await _persist_token_usage(
    connection,
    numeric_chat_id,
    user_id,
    source_message_id,
    result,
)
```

- [ ] **Step 5: Run GREEN and the neighboring message-delivery tests**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/test_worker.py tests/integration/test_worker_usage_postgres.py tests/e2e/test_message_delivery.py
```

Expected: PASS with one user row, one assistant row and all usage rows linked exactly once.

- [ ] **Step 6: Log and commit the worker step**

```powershell
git add project/worker/main.py project/tests/unit/test_worker.py project/tests/integration/test_worker_usage_postgres.py changelog.md
git commit -m "feat: usage привязан к сообщению пользователя"
```

---

### Task 3: Read, group and price per-message usage

**Files:**
- Modify: `project/admin/database.py`
- Modify: `project/admin/pricing.py`
- Modify: `project/admin/app.py`
- Create: `project/tests/integration/admin/test_message_llm_analytics_postgres.py`
- Create: `project/tests/unit/admin/test_message_llm_analytics.py`

**Interfaces:**
- Produces: each message dict has `llm_usage_state` in `unavailable | none | used` and `usage_groups`.
- Produces: `summarize_usage_groups(groups: list[dict]) -> dict` with totals plus priced groups.

- [ ] **Step 1: Write a failing pure pricing-summary test**

```python
import pytest

from pricing import summarize_usage_groups


def test_usage_summary_sums_calls_tokens_and_each_models_price():
    result = summarize_usage_groups(
        [
            {
                "purpose": "router",
                "model": "gpt-4o-mini",
                "llm_calls": 2,
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "cached_tokens": 200,
                "total_tokens": 1100,
            },
            {
                "purpose": "answer",
                "model": "gpt-4.1",
                "llm_calls": 1,
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "cached_tokens": 100,
                "total_tokens": 550,
            },
        ]
    )

    assert result["llm_calls"] == 3
    assert result["prompt_tokens"] == 1500
    assert result["completion_tokens"] == 150
    assert result["cached_tokens"] == 300
    assert result["total_tokens"] == 1650
    assert result["cost_usd"] == pytest.approx(0.001445)
    assert result["savings_usd"] == pytest.approx(0.000165)
    assert [group["purpose"] for group in result["groups"]] == [
        "router",
        "answer",
    ]
```

- [ ] **Step 2: Write the PostgreSQL three-state and grouping test**

Seed in one chat:

```python
old_id = await connection.fetchval(
    "INSERT INTO messages (chat_id, user_id, role, content) "
    "VALUES (42, 7, 'user', 'old') RETURNING id"
)
none_id = await connection.fetchval(
    "INSERT INTO messages "
    "(chat_id, user_id, role, content, llm_usage_tracked) "
    "VALUES (42, 7, 'user', 'without-llm', true) RETURNING id"
)
used_id = await connection.fetchval(
    "INSERT INTO messages "
    "(chat_id, user_id, role, content, llm_usage_tracked) "
    "VALUES (42, 7, 'user', 'with-llm', true) RETURNING id"
)
await connection.execute(
    "INSERT INTO messages (chat_id, user_id, role, content) "
    "VALUES (42, 7, 'assistant', 'answer')"
)
await connection.executemany(
    "INSERT INTO token_usage "
    "(chat_id, user_id, source_message_id, purpose, prompt_tokens, "
    "completion_tokens, cached_tokens, total_tokens, model) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
    [
        (42, 7, None, "legacy", 1, 1, 0, 2, "old-model"),
        (42, 7, used_id, "router", 3, 1, 0, 4, "router-model"),
        (42, 7, used_id, "router", 4, 1, 1, 5, "router-model"),
        (42, 7, used_id, "answer", 9, 4, 1, 13, "answer-model"),
    ],
)
```

Assert:

```python
detail = await admin_database.get_chat_detail(42)
users = [message for message in detail["messages"] if message["role"] == "user"]
assert [message["llm_usage_state"] for message in users] == [
    "unavailable",
    "none",
    "used",
]
assert users[0]["usage_groups"] == []
assert users[1]["usage_groups"] == []
assert users[2]["usage_groups"] == [
    {
        "purpose": "answer",
        "model": "answer-model",
        "llm_calls": 1,
        "prompt_tokens": 9,
        "completion_tokens": 4,
        "cached_tokens": 1,
        "total_tokens": 13,
    },
    {
        "purpose": "router",
        "model": "router-model",
        "llm_calls": 2,
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "cached_tokens": 1,
        "total_tokens": 9,
    },
]
assert detail["stats"]["llm_calls"] == 4
```

- [ ] **Step 3: Run RED**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/admin/test_message_llm_analytics.py tests/integration/admin/test_message_llm_analytics_postgres.py
```

Expected: FAIL because the summary helper and per-message query do not exist.

- [ ] **Step 4: Add the minimal grouped query and three-state mapping**

Include `llm_usage_tracked` in the message query. Fetch usage once:

```sql
SELECT
    source_message_id,
    purpose,
    model,
    COUNT(*) AS llm_calls,
    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
    COALESCE(SUM(total_tokens), 0) AS total_tokens
FROM token_usage
WHERE chat_id = $1 AND source_message_id IS NOT NULL
GROUP BY source_message_id, purpose, model
ORDER BY source_message_id, purpose, model
```

Map without N+1:

```python
usage_by_message: dict[int, list[dict[str, Any]]] = {}
for row in usage_rows:
    source_message_id = row["source_message_id"]
    usage_by_message.setdefault(source_message_id, []).append(
        {key: value for key, value in dict(row).items() if key != "source_message_id"}
    )

messages = []
for row in msg_rows:
    message = dict(row)
    groups = usage_by_message.get(message["id"], [])
    message["usage_groups"] = groups
    if message["role"] != "user" or message["llm_usage_tracked"] is None:
        message["llm_usage_state"] = "unavailable"
    elif groups:
        message["llm_usage_state"] = "used"
    else:
        message["llm_usage_state"] = "none"
    messages.append(message)
```

- [ ] **Step 5: Add the existing-pricing based summary helper**

```python
def summarize_usage_groups(groups: list[dict]) -> dict:
    totals = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "savings_usd": 0.0,
        "groups": [],
    }
    for group in groups:
        item = dict(group)
        cost, savings = calculate_cost(
            item["prompt_tokens"],
            item["completion_tokens"],
            item["cached_tokens"],
            item["model"],
        )
        item["cost_usd"] = cost
        item["savings_usd"] = savings
        totals["groups"].append(item)
        for key in (
            "llm_calls",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "total_tokens",
        ):
            totals[key] += item[key]
        totals["cost_usd"] += cost
        totals["savings_usd"] += savings
    return totals
```

In `chat_detail`, enrich only used messages:

```python
# Replace the existing app.py import with:
from pricing import calculate_cost, summarize_usage_groups

for message in detail["messages"]:
    if message["role"] == "user" and message["llm_usage_state"] == "used":
        message["llm_usage"] = summarize_usage_groups(message["usage_groups"])
```

- [ ] **Step 6: Run GREEN plus existing chat/event regressions**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/admin/test_message_llm_analytics.py tests/integration/admin/test_message_llm_analytics_postgres.py tests/e2e/admin/test_customer_event_journal.py
```

Expected: PASS; the old unlinked usage remains in `detail["stats"]` but not in a message group.

- [ ] **Step 7: Log and commit data preparation**

```powershell
git add project/admin/database.py project/admin/pricing.py project/admin/app.py project/tests/integration/admin/test_message_llm_analytics_postgres.py project/tests/unit/admin/test_message_llm_analytics.py changelog.md
git commit -m "feat: рассчитана LLM-аналитика сообщения"
```

---

### Task 4: Render compact per-message analytics in the dialog UI

**Files:**
- Modify: `project/admin/templates/chat_detail.html`
- Modify: `project/admin/static/styles.css`
- Create: `project/tests/e2e/admin/test_message_llm_analytics.py`

**Interfaces:**
- Consumes: `llm_usage_state`, `llm_usage` and priced `groups` from Task 3.
- Produces: compact HTML block only under user messages.

- [ ] **Step 1: Write failing route/template tests for all three states**

Mock `get_chat_detail` with an old user message, a tracked no-LLM user message, a used user message and one assistant reply. Make `get_customer_events` return an empty page and stub `record_audit`.

Core assertions:

```python
assert response.status_code == 200
assert response.text.count("Нет точной привязки") == 1
assert response.text.count("LLM не вызывалась") == 1
assert "Итого: 3 LLM-вызова" in response.text
assert "1 650 токенов" in response.text
assert "Prompt: 1 500" in response.text
assert "Completion: 150" in response.text
assert "Кэш: 300" in response.text
assert "Router" in response.text
assert "Answer" in response.text
assert "Compact" not in response.text
assert "router-model" in response.text
assert "answer-model" in response.text
assert "P 1 000 · C 100 · кэш 200" in response.text
assert "сэкономлено $0.0000" in response.text
assert response.text.count('class="message-llm message-llm-') == 3
assert '<img src=x onerror=alert(1)>' not in response.text
assert '&lt;img src=x onerror=alert(1)&gt;' in response.text
```

Also assert unknown purpose text is escaped by Jinja and does not become HTML.

- [ ] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/e2e/admin/test_message_llm_analytics.py
```

Expected: FAIL because the message analytics markup is absent.

- [ ] **Step 3: Add minimal Jinja markup**

Immediately after `.msg-body`, inside the message loop:

```jinja2
{% if msg.role == 'user' %}
<div class="message-llm message-llm-{{ msg.llm_usage_state }}">
    {% if msg.llm_usage_state == 'used' %}
    <div class="message-llm-total">
        Итого: {{ msg.llm_usage.llm_calls }} LLM-вызова ·
        {{ msg.llm_usage.total_tokens|int }} токенов ·
        {{ msg.llm_usage.cost_usd|money }} ·
        сэкономлено {{ msg.llm_usage.savings_usd|money }}
    </div>
    <div class="message-llm-tokens">
        Prompt: {{ msg.llm_usage.prompt_tokens|int }} ·
        Completion: {{ msg.llm_usage.completion_tokens|int }} ·
        Кэш: {{ msg.llm_usage.cached_tokens|int }}
    </div>
    <div class="message-llm-groups">
        {% for usage in msg.llm_usage.groups %}
        <div class="message-llm-group">
            <strong>{{ {'security': 'Security', 'router': 'Router', 'compact': 'Compact', 'answer': 'Answer', 'validator': 'Validator', 'legacy': 'Legacy'}.get(usage.purpose, usage.purpose) }}</strong>
            <span>
                {{ usage.llm_calls }} выз. · {{ usage.total_tokens|int }} токенов ·
                P {{ usage.prompt_tokens|int }} · C {{ usage.completion_tokens|int }} ·
                кэш {{ usage.cached_tokens|int }} · {{ usage.cost_usd|money }} ·
                сэкономлено {{ usage.savings_usd|money }} · {{ usage.model }}
            </span>
        </div>
        {% endfor %}
    </div>
    {% elif msg.llm_usage_state == 'none' %}
    <span>LLM не вызывалась</span>
    {% else %}
    <span>Нет точной привязки</span>
    {% endif %}
</div>
{% endif %}
```

- [ ] **Step 4: Add restrained responsive CSS**

```css
.message-llm {
    margin-top: 10px;
    padding-top: 9px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 12px;
}

.message-llm-total { color: var(--text); font-weight: 700; }
.message-llm-tokens { margin-top: 3px; }
.message-llm-groups { display: grid; gap: 3px; margin-top: 7px; }
.message-llm-group { display: flex; gap: 8px; flex-wrap: wrap; }
.message-llm-none { color: var(--ok); }
```

- [ ] **Step 5: Run GREEN and neighboring admin security/UI tests**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/e2e/admin/test_message_llm_analytics.py tests/e2e/admin/test_customer_event_journal.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_public_prefix.py
```

Expected: PASS; no analytics block appears under assistant messages and all dynamic text remains escaped.

- [ ] **Step 6: Log and commit the UI step**

```powershell
git add project/admin/templates/chat_detail.html project/admin/static/styles.css project/tests/e2e/admin/test_message_llm_analytics.py changelog.md
git commit -m "feat: показана LLM-аналитика в диалоге"
```

---

### Task 5: Privacy, regression and project-state closure

**Files:**
- Modify: `project/tests/integration/test_retention_postgres.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Verifies: cascade linkage does not weaken customer deletion or retention.
- Produces: completed roadmap entry with exact Docker evidence.

- [ ] **Step 1: Add or extend the deletion/retention assertions before any fix**

Seed a tracked message with linked usage in each relevant integration fixture. After deletion/retention, assert both are absent:

```python
assert await connection.fetchval(
    "SELECT count(*) FROM messages WHERE id = $1", source_message_id
) == 0
assert await connection.fetchval(
    "SELECT count(*) FROM token_usage WHERE source_message_id = $1",
    source_message_id,
) == 0
```

No production change is expected: current customer deletion removes `token_usage` before `messages`, while retention executes both deletes in one transaction. A failure here is treated as a regression and diagnosed before proceeding; the test is not weakened.

- [ ] **Step 2: Run the focused privacy and retention gate**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/integration/test_retention_postgres.py tests/integration/admin/test_customer_data_deletion_postgres.py
```

Expected: PASS with no orphan usage and no control-customer deletion.

- [ ] **Step 3: Run the complete feature regression**

```powershell
docker compose --env-file ../.env --profile test run --rm test pytest -q tests/unit/admin/test_migration_0020.py tests/unit/admin/test_message_llm_analytics.py tests/unit/test_worker.py tests/integration/test_migrations.py tests/integration/test_worker_usage_postgres.py tests/integration/admin/test_message_llm_analytics_postgres.py tests/integration/test_retention_postgres.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/e2e/admin/test_message_llm_analytics.py tests/e2e/admin/test_customer_event_journal.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_public_prefix.py tests/e2e/test_message_delivery.py
```

Expected: all selected tests PASS with no warnings/errors caused by the feature.

- [ ] **Step 4: Verify syntax, Compose and migration head**

```powershell
docker compose --env-file ../.env --profile test run --rm test python -m compileall -q /workspace/admin /workspace/worker /workspace/src /workspace/migrations
docker compose --env-file ../.env config --quiet
docker compose --env-file ../.env --profile test run --rm test alembic -c /workspace/alembic.ini heads
```

Expected: exit `0`; exactly one head `0020_message_llm_analytics`.

- [ ] **Step 5: Review the final diff for scope and secrets**

```powershell
git diff --check
git diff --stat 4c670a5..HEAD
git status --short
```

Confirm: no timestamp matching, no backfill, no prompt/context/provider payload in UI, no secret material, no unrelated refactor.

- [ ] **Step 6: Update project state and commit closure**

Mark the roadmap item complete with exact test counts and migration head. Append the final verification evidence to `changelog.md`.

```powershell
git add 'Дорожная карта.md' changelog.md project/tests/integration/test_retention_postgres.py project/tests/integration/admin/test_customer_data_deletion_postgres.py
git commit -m "test: закрыта аналитика LLM по сообщениям"
```

- [ ] **Step 7: Report local completion only**

Report changed behavior, exact Docker evidence and local commits. Explicitly state that GitHub push, staging, production and external LLM calls were not performed.
