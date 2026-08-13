# Admin Escalation Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить owner/admin очередь открытых human handoff обращений с безопасным просмотром и атомарным закрытием последней эскалации клиента.

**Architecture:** Существующие `escalations` и `human_mode` остаются источником правды. Один bounded SELECT строит read-модель без payload; одна PostgreSQL-транзакция блокирует эскалацию и строку human mode, закрывает обращение, выключает ручной режим только после последней открытой эскалации и пишет безопасный аудит. FastAPI-роутер использует текущие auth/RBAC/CSRF механизмы и серверный Jinja HTML.

**Tech Stack:** Python 3.12, FastAPI, asyncpg через существующий `Database`, PostgreSQL 16, Jinja2, pytest, Docker Compose.

## Global Constraints

- Доступ к GET и POST имеют только существующие роли `owner` и `admin`.
- Новых таблиц, миграций, зависимостей, фоновых процессов и JavaScript нет.
- Raw `payload`, неизвестные `reason_code`/`source`, customer ID и текст диалога не попадают в новый audit event.
- Закрытие одной из нескольких открытых эскалаций не выключает human mode.
- Все тесты запускаются только через `docker compose`.
- Telegram, YCLIENTS, staging и production не вызываются и не изменяются.

---

### Task 1: Безопасная read-модель очереди

**Files:**
- Modify: `project/admin/customer_events.py`
- Modify: `project/admin/database.py`
- Create: `project/tests/unit/admin/test_escalation_queue.py`
- Create: `project/tests/integration/admin/test_escalation_queue_postgres.py`

**Interfaces:**
- Consumes: `customer_events.SAFE_REASON_LABELS`, global `database._pool`.
- Produces: `safe_handoff_reason(reason_code: object) -> str`; `safe_handoff_source(source: object) -> str`; `database.get_open_escalations(limit: int = 100) -> list[dict[str, Any]]`.

- [ ] **Step 1: Write failing presentation tests**

```python
from customer_events import safe_handoff_reason, safe_handoff_source


def test_handoff_labels_allow_only_known_values():
    assert safe_handoff_reason("low_feedback_rating") == "Низкая оценка после визита"
    assert safe_handoff_reason("internal-secret") == "Требуется помощь администратора"
    assert safe_handoff_source("feedback") == "Обратная связь"
    assert safe_handoff_source("private-provider-name") == "Система"
```

- [ ] **Step 2: Run unit RED**

Run:

```powershell
docker compose -f project/docker-compose.yml --env-file .env run --build --rm test pytest -q tests/unit/admin/test_escalation_queue.py
```

Expected: collection/import failure because the two public helpers do not exist.

- [ ] **Step 3: Add the minimal allowlist helpers**

Add to `customer_events.py`:

```python
SAFE_HANDOFF_SOURCES = {"feedback": "Обратная связь", "booking": "Запись"}
DEFAULT_HANDOFF_REASON = "Требуется помощь администратора"


def safe_handoff_reason(reason_code: object) -> str:
    return SAFE_REASON_LABELS.get(str(reason_code), DEFAULT_HANDOFF_REASON)


def safe_handoff_source(source: object) -> str:
    return SAFE_HANDOFF_SOURCES.get(str(source), "Система")
```

Change `_safe_description` to call `safe_handoff_reason(value)` only for handoff sources, preserving message content behavior.

- [ ] **Step 4: Write integration RED for the bounded query**

Seed two open rows and one resolved control row. Call `get_open_escalations(limit=100)` and assert:

```python
assert [row["customer_id"] for row in result] == ["42", "84"]
assert all("payload" not in row for row in result)
assert all("reason_code" not in row for row in result)
assert result[0]["reason"] == "Требуется помощь администратора"
assert result[0]["source"] == "Система"
assert result[0]["human_mode_enabled"] is True
```

Also assert `limit=0` and `limit=101` raise `ValueError("open escalations limit")`.

- [ ] **Step 5: Implement the bounded read query**

Add to `database.py`:

```python
async def get_open_escalations(limit: int = 100) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise ValueError("open escalations limit")
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.customer_id, e.source, e.reason_code, e.created_at,
                   COALESCE(h.enabled, false) AS human_mode_enabled
            FROM escalations AS e
            LEFT JOIN human_mode AS h ON h.customer_id = e.customer_id
            WHERE e.status = 'open'
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {
            "id": row["id"],
            "customer_id": row["customer_id"],
            "source": safe_handoff_source(row["source"]),
            "reason": safe_handoff_reason(row["reason_code"]),
            "created_at": row["created_at"],
            "human_mode_enabled": row["human_mode_enabled"],
        }
        for row in rows
    ]
```

Import only the two helpers from `customer_events`.

- [ ] **Step 6: Run GREEN and commit**

Run the two new test files through Docker. Expected: all PASS.

```powershell
git add project/admin/customer_events.py project/admin/database.py project/tests/unit/admin/test_escalation_queue.py project/tests/integration/admin/test_escalation_queue_postgres.py changelog.md
git commit -m "feat: добавить read-модель очереди эскалаций"
```

---

### Task 2: Атомарное закрытие и безопасный аудит

**Files:**
- Modify: `project/admin/database.py`
- Modify: `project/tests/integration/admin/test_escalation_queue_postgres.py`

**Interfaces:**
- Consumes: existing `database._pool`, tables `escalations`, `human_mode`, `admin_audit_events`.
- Produces: `database.resolve_escalation(escalation_id: UUID, *, actor_id: int, ip_address: str | None, user_agent: str | None) -> str`, returning only `resolved`, `already_resolved`, or `not_found`.

- [ ] **Step 1: Write transaction RED cases**

Use a disposable migrated PostgreSQL database and assert:

```python
result = await database.resolve_escalation(
    first_id, actor_id=7, ip_address="127.0.0.1", user_agent="test"
)
assert result == "resolved"
assert await status(first_id) == "resolved"
assert await human_enabled("42") is True  # second open escalation remains

result = await database.resolve_escalation(
    second_id, actor_id=7, ip_address=None, user_agent=None
)
assert result == "resolved"
assert await human_enabled("42") is False

assert await database.resolve_escalation(
    second_id, actor_id=7, ip_address=None, user_agent=None
) == "already_resolved"
assert await database.resolve_escalation(
    uuid4(), actor_id=7, ip_address=None, user_agent=None
) == "not_found"
```

Assert exactly two audit rows exist and each has `action='escalation.resolve'`, `object_type='escalation'`, `object_id` equal only to the escalation UUID, `before={"status":"open"}`, `after={"status":"resolved"}`; serialized audit rows must not contain `customer_id`, payload values or message text.

- [ ] **Step 2: Run integration RED**

Expected: `AttributeError` because `resolve_escalation` does not exist.

- [ ] **Step 3: Implement one transaction**

Implementation sequence inside one acquired connection and `connection.transaction()`:

```python
row = await conn.fetchrow(
    "SELECT id, customer_id, status FROM escalations WHERE id=$1 FOR UPDATE",
    escalation_id,
)
if row is None:
    return "not_found"
if row["status"] == "resolved":
    return "already_resolved"

await conn.fetchrow(
    "SELECT customer_id FROM human_mode WHERE customer_id=$1 FOR UPDATE",
    row["customer_id"],
)
await conn.execute(
    "UPDATE escalations SET status='resolved', resolved_at=now() WHERE id=$1",
    escalation_id,
)
has_open = await conn.fetchval(
    "SELECT EXISTS(SELECT 1 FROM escalations WHERE customer_id=$1 AND status='open')",
    row["customer_id"],
)
if not has_open:
    await conn.execute(
        "UPDATE human_mode SET enabled=false, expires_at=now() WHERE customer_id=$1",
        row["customer_id"],
    )
```

Insert the safe audit row on the same `conn` before transaction exit. Encode only the two fixed status dictionaries with `json.dumps`; never pass `customer_id`, source, reason or payload.

- [ ] **Step 4: Run integration GREEN and affected regressions**

Run escalation integration plus customer event journal, customer deletion and system metrics integration files. Expected: all PASS.

- [ ] **Step 5: Commit**

```powershell
git add project/admin/database.py project/tests/integration/admin/test_escalation_queue_postgres.py changelog.md
git commit -m "feat: атомарно закрывать эскалации"
```

---

### Task 3: Owner/admin экран и POST-действие

**Files:**
- Create: `project/admin/escalation_routes.py`
- Create: `project/admin/templates/escalations.html`
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/static/styles.css`
- Create: `project/tests/e2e/admin/test_escalation_queue.py`

**Interfaces:**
- Consumes: `database.get_open_escalations(limit=100)`, `database.resolve_escalation(...)`, `get_current_user`, `require_role`, `validate_csrf`, `admin_url`, request audit metadata helpers.
- Produces: `GET /escalations/`; `POST /escalations/{escalation_id}/resolve`.

- [ ] **Step 1: Write route/UI RED tests**

For both `owner` and `admin`, monkeypatch authentication and database functions, then assert GET returns 200, escaped values, `/chats/42`, a CSRF hidden input and the resolve action. Assert anonymous uses the established 302 login handler.

For POST assert:

```python
response = await client.post(
    f"/escalations/{escalation_id}/resolve",
    data={"csrf_token": "known-csrf"},
)
assert response.status_code == 302
assert response.headers["location"] == "/escalations/?resolved=resolved"
```

Missing/wrong CSRF must return 403 before calling the database. A `not_found` result must return 404. Both roles must be accepted.

- [ ] **Step 2: Run E2E RED**

Expected: import or 404 failures because the router and template do not exist.

- [ ] **Step 3: Implement the router**

Create an `APIRouter(prefix="/escalations", tags=["escalations"])`. GET authenticates, calls `require_role(user, {"owner", "admin"})`, loads at most 100 rows and renders `escalations.html` with `user`, `escalations`, `csrf_token`, and query status.

POST accepts `UUID` path input and `csrf_token: str = Form("")`; authenticate, validate CSRF, require either role, call `resolve_escalation` with actor/request metadata, raise `HTTPException(404)` for `not_found`, otherwise redirect to `admin_url(request, f"/escalations/?resolved={result}")`.

- [ ] **Step 4: Add server-rendered HTML and navigation**

Template requirements:

```html
{% extends "base.html" %}
{% block title %}Эскалации — Admin{% endblock %}
{% block content %}
<h1>Эскалации</h1>
{% if not escalations %}<p class="empty-state">Открытых обращений нет.</p>{% endif %}
{% for item in escalations %}
<article class="escalation-card">
  <a href="{{ request.scope.get('root_path', '') }}/chats/{{ item.customer_id }}">Клиент #{{ item.customer_id }}</a>
  <p>{{ item.reason }} · {{ item.source }}</p>
  <form method="post" action="{{ request.scope.get('root_path', '') }}/escalations/{{ item.id }}/resolve">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit" class="btn btn-primary">Закрыть и вернуть бота</button>
  </form>
</article>
{% endfor %}
{% endblock %}
```

Add one sidebar link visible to both authenticated roles and minimal CSS using existing card/button variables. Include the router in `app.py` beside the other modular routers.

- [ ] **Step 5: Run admin GREEN and regression**

Run the new E2E file plus existing admin auth/CSRF, chat detail, customer deletion and event journal tests. Expected: all PASS.

- [ ] **Step 6: Commit**

```powershell
git add project/admin/escalation_routes.py project/admin/templates/escalations.html project/admin/app.py project/admin/templates/base.html project/admin/static/styles.css project/tests/e2e/admin/test_escalation_queue.py changelog.md
git commit -m "feat: добавить экран эскалаций"
```

---

### Task 4: Финальный gate и документация

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: completed Tasks 1–3.
- Produces: verified branch ready for local integration; no push or deploy.

- [ ] **Step 1: Run focused Docker suite**

Run all new unit/integration/E2E files plus affected privacy, event journal, deletion and admin RBAC tests. Expected: all PASS.

- [ ] **Step 2: Run independent correctness/security review**

Review transaction races, tenant isolation, RBAC/CSRF, audit content, HTML escaping and idempotency. Fix every Critical/Important finding with a focused RED/GREEN loop.

- [ ] **Step 3: Run full Docker suite**

```powershell
docker compose -f project/docker-compose.yml --env-file .env run --build --rm test pytest -q
```

Expected: exit 0 and no failed tests.

- [ ] **Step 4: Close documentation and commit**

Mark only the escalation queue roadmap item complete and record exact focused/full test counts plus review verdict.

```powershell
git diff --check
git add 'Дорожная карта.md' changelog.md
git commit -m "docs: завершить очередь эскалаций"
git status --short --branch
```

Expected: clean `codex/admin-escalation-queue`; no push/deploy performed.
