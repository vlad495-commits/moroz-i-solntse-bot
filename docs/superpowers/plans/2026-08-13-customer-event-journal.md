# Customer Event Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать в карточке диалога безопасную read-only ленту последних событий клиента из уже существующих PostgreSQL-таблиц.

**Architecture:** `admin/database.py` выполняет один параметризованный `UNION ALL` запрос и возвращает страницу событий. Небольшой модуль `admin/customer_events.py` переводит внутренние source/kind/status в безопасные русские подписи, а существующий route `/chats/{chat_id}` передаёт результат в `chat_detail.html`.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, PostgreSQL, Jinja2, pytest, Docker Compose.

## Global Constraints

- Новая event-таблица, миграция и зависимость не добавляются.
- Write-paths Telegram, worker, scheduler и YCLIENTS не меняются.
- Максимум 50 событий на страницу; `events_offset >= 0`.
- В шаблон не передаются JSON payload, snapshot, idempotency key, IP, user-agent, токены и служебные ошибки.
- Реализация следует TDD: каждый production-шаг начинается с подтверждённого RED.

---

### Task 1: Нормализация безопасного события

**Files:**
- Create: `project/admin/customer_events.py`
- Create: `project/tests/unit/admin/test_customer_events.py`

**Interfaces:**
- Consumes: строку SQL как `Mapping[str, object]` с `source`, `source_id`, `occurred_at`, `kind`, `description`, `status`.
- Produces: `normalize_customer_event(row) -> dict[str, object]` с `event_id`, `occurred_at`, `category`, `kind`, `title`, `description`, `status`.

- [ ] **Step 1: Write the failing normalization tests**

Проверить user/assistant message, известные booking/scheduler/handoff kinds, неизвестный kind и отсутствие raw payload-shaped полей:

```python
def test_unknown_kind_is_neutral_and_does_not_copy_extra_fields():
    event = normalize_customer_event({
        "source": "scheduler", "source_id": "job-1",
        "occurred_at": NOW, "kind": "secret_internal_kind",
        "description": None, "status": "failed",
        "payload": {"phone": "+79990000000"},
    })
    assert event == {
        "event_id": "scheduler:job-1",
        "occurred_at": NOW,
        "category": "notification",
        "kind": "unknown",
        "title": "Системное событие",
        "description": None,
        "status": "failed",
    }
```

- [ ] **Step 2: Run RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_events.py
```

Expected: FAIL because `customer_events` does not exist.

- [ ] **Step 3: Implement the minimal allowlist mapper**

Создать `SOURCE_CATEGORIES`, `EVENT_TITLES` и функцию, которая формирует новый словарь только из шести разрешённых входных полей. Unknown kind получает `unknown` и `Системное событие`; raw kind не передаётся в шаблон.

- [ ] **Step 4: Run GREEN**

Run the Task 1 command. Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add project/admin/customer_events.py project/tests/unit/admin/test_customer_events.py
git commit -m "feat: добавить модель событий клиента"
```

### Task 2: Read-only объединение и пагинация PostgreSQL

**Files:**
- Modify: `project/admin/database.py`
- Create: `project/tests/integration/admin/test_customer_events_postgres.py`

**Interfaces:**
- Consumes: `normalize_customer_event(row)` из Task 1.
- Produces: `get_customer_events(chat_id: int, limit: int = 50, cursor: str | None = None) -> dict[str, object]`, где результат содержит `items`, `next_cursor`, `has_more`.

- [ ] **Step 1: Write the failing PostgreSQL integration tests**

В одной мигрированной БД создать target/control данные в `messages`, `booking_scenarios`, `booking_events`, `bookings`, `scheduler_jobs`, `escalations`, `human_mode` и адресный `admin_audit_events`. Проверить:

```python
page = await admin_database.get_customer_events(42, limit=3, offset=0)
assert len(page["items"]) == 3
assert page["has_more"] is True
assert page["next_offset"] == 3
assert all(event["event_id"] != control_id for event in page["items"])
assert "payload" not in page["items"][0]
```

Отдельно проверить `limit=0`, `limit=51` и `offset=-1` → `ValueError`, стабильную вторую страницу без пересечений и отсутствие обезличенного `customer_data.delete` с `object_id IS NULL`.

- [ ] **Step 2: Run RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/admin/test_customer_events_postgres.py
```

Expected: FAIL because `get_customer_events` does not exist.

- [ ] **Step 3: Implement one bounded UNION ALL query**

Запрос должен возвращать только эти колонки:

```sql
source, source_id, occurred_at, kind, description, status
```

Ветки:

- `messages`: `message.user` / `message.assistant`, description = `content`;
- `booking_events JOIN booking_scenarios`: `booking.` + `event_type`, без `payload`;
- `scheduler_jobs`: событие `scheduler.scheduled` на `created_at` и `scheduler.` + `status` на `finished_at`; строковый `payload.customer_id` имеет приоритет, booking fallback разрешён только при его отсутствии;
- `escalations`: `handoff.opened` на `created_at` и `handoff.resolved` на `resolved_at`, description = `reason_code`;
- `human_mode`: `handoff.enabled` на `enabled_at`, description = `reason_code`;
- `admin_audit_events`: только `object_type = 'customer' AND object_id = $1`, kind = `admin.` + `action`, без `before/after`.

Внешний запрос сортирует `occurred_at DESC, source DESC, source_id DESC`, применяет LIMIT как `limit + 1` и односторонний keyset predicate по bounded opaque cursor. Python отбрасывает лишнюю строку и формирует следующий cursor для кнопки «Раньше». Обратный snapshot-navigation намеренно отсутствует: mutable источники не позволяют честно восстановить прошлую версию без отдельного event store.

- [ ] **Step 4: Run GREEN and existing database regressions**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/admin/test_customer_events_postgres.py tests/integration/admin/test_system_metrics_postgres.py tests/integration/admin/test_customer_data_deletion_postgres.py
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add project/admin/database.py project/tests/integration/admin/test_customer_events_postgres.py
git commit -m "feat: собрать события клиента из PostgreSQL"
```

### Task 3: Событийная лента в карточке диалога

**Files:**
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/chat_detail.html`
- Modify: `project/admin/static/styles.css`
- Create: `project/tests/e2e/admin/test_customer_event_journal.py`
- Modify: `project/tests/e2e/admin/test_csrf_rbac_audit.py`

**Interfaces:**
- Consumes: `database.get_customer_events(chat_id, limit=50, cursor=events_cursor)`.
- Produces: HTML block `.customer-events`, safe page links with `events_offset`, and unchanged message/danger-zone UI.

- [ ] **Step 1: Write failing route/template tests**

Подменить `get_chat_detail` и `get_customer_events`, вызвать route для owner/admin и проверить:

```python
assert response.status_code == 200
assert "События клиента" in response.text
assert "Сообщение клиента" in response.text
assert "events_offset=50" in response.text
assert "<script>" not in response.text
assert "&lt;script&gt;" in response.text
```

Проверить `events_offset=-1` → 422, unknown chat сохраняет существующий redirect, admin не видит owner-only danger zone, а owner видит её как раньше.

- [ ] **Step 2: Run RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/admin/test_customer_event_journal.py tests/e2e/admin/test_csrf_rbac_audit.py
```

Expected: FAIL because route/template do not expose the event page.

- [ ] **Step 3: Implement the minimal route and HTML**

Добавить FastAPI `Query(0, ge=0)` параметр. После успешного `get_chat_detail` запросить события и передать `events` в template context. В `chat_detail.html` вывести `event.title`, `event.description`, `event.status`, `event.occurred_at`; ссылки строить относительно `request.scope.root_path`. CSS ограничить классами `.customer-events`, `.customer-event`, `.customer-event-category-*`, `.customer-events-pagination` без JavaScript.

- [ ] **Step 4: Run GREEN and admin regression**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/admin/test_customer_event_journal.py tests/e2e/admin/test_csrf_rbac_audit.py tests/unit/admin
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add project/admin/app.py project/admin/templates/chat_detail.html project/admin/static/styles.css project/tests/e2e/admin/test_customer_event_journal.py project/tests/e2e/admin/test_csrf_rbac_audit.py
git commit -m "feat: показать событийный журнал клиента"
```

### Task 4: Документация, полный gate и review

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: завершённые Tasks 1–3.
- Produces: проверенную ветку без Critical/Important замечаний.

- [ ] **Step 1: Run focused safety checks**

```powershell
git diff --check
docker compose --env-file ../.env run --rm test python -m compileall -q admin src
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_events.py tests/integration/admin/test_customer_events_postgres.py tests/e2e/admin/test_customer_event_journal.py
```

- [ ] **Step 2: Run full Docker pytest**

```powershell
docker compose --env-file ../.env run --rm --volume "${PWD}/docs:/docs:ro" test pytest -q
```

Expected: 0 failures.

- [ ] **Step 3: Independent review**

Проверить scope, SQL tenant isolation, pagination stability, absence of raw payload/PII leakage, auth/RBAC regression and deletion compatibility. Исправить все Critical/Important через новый RED/GREEN цикл.

- [ ] **Step 4: Update project records and commit**

Отметить journal выполненным в `Дорожная карта.md`; в `changelog.md` записать focused/full test counts и review result.

```powershell
git add -- 'Дорожная карта.md' changelog.md
git commit -m "docs: завершить событийный журнал клиента"
```
