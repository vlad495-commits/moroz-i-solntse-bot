# Admin Bookings Read Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить owner/admin read-only центр локально известных записей с представлениями upcoming/attention/history, безопасной карточкой, timeline и audit без live YCLIENTS-вызовов.

**Architecture:** Новый `booking_views.py` отвечает только за allowlist-представление и opaque cursor, а `bookings_database.py` читает существующие booking-таблицы через переданный `Database`. FastAPI router и два Jinja-шаблона используют эти интерфейсы; detail view фиксирует безопасный audit в той же PostgreSQL-транзакции, но никакой admin-код не импортирует и не вызывает YCLIENTS adapter.

**Tech Stack:** Python 3.12, FastAPI/Jinja2, asyncpg через существующий `Database`, PostgreSQL 16, текущая admin session/RBAC/audit инфраструктура, pytest, Docker Compose.

## Global Constraints

- Рабочая ветка: `codex/admin-bookings-read-model` от `codex/admin-ops-rc` HEAD `e7c41e7`.
- Текущая фаза только read-only относительно booking/YCLIENTS state.
- Не добавлять таблицы, Alembic migration, очередь, dependency, live/read-through YCLIENTS-вызов или provider credential.
- Не выбирать и не отдавать в шаблон raw `snapshot`, scenario `state` или event `payload`.
- YCLIENTS остаётся источником правды; UI явно называет данные локальной проекцией записей бота.
- Owner и admin используют существующие auth/RBAC helpers; booking mutations и CSRF-формы отсутствуют.
- Все тесты запускаются только через Docker Compose из `project/` с каноническим `--env-file ../.env`.
- Не выполнять push, deploy и изменения staging/production.

---

### Task 1: Safe booking presentation and cursor

**Files:**
- Create: `project/admin/booking_views.py`
- Create: `project/tests/unit/admin/test_booking_views.py`

**Interfaces:**
- Produces: `validate_booking_filters(view: str, status: str | None) -> tuple[str, str | None]`
- Produces: `encode_booking_cursor(sort_at: datetime, booking_id: UUID) -> str`
- Produces: `decode_booking_cursor(value: str | None) -> tuple[datetime, UUID] | None`
- Produces: `normalize_booking_row(row: Mapping[str, object], *, detail: bool = False) -> dict[str, object]`
- Produces: `normalize_booking_event(row: Mapping[str, object]) -> dict[str, object]`

- [ ] **Step 1: Write unit RED for allowlists and cursor**

Create tests covering:

```python
from datetime import UTC, datetime
from uuid import UUID

import pytest

from booking_views import (
    decode_booking_cursor,
    encode_booking_cursor,
    normalize_booking_event,
    normalize_booking_row,
    validate_booking_filters,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
BOOKING_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_filter_allowlist():
    assert validate_booking_filters("attention", "unknown") == (
        "attention", "unknown"
    )
    with pytest.raises(ValueError, match="booking view"):
        validate_booking_filters("private", None)
    with pytest.raises(ValueError, match="booking status"):
        validate_booking_filters("upcoming", "private")


def test_cursor_round_trip_and_rejects_malformed_values():
    encoded = encode_booking_cursor(NOW, BOOKING_ID)
    assert decode_booking_cursor(encoded) == (NOW, BOOKING_ID)
    for value in ("", "not-base64", "e30="):
        with pytest.raises(ValueError, match="booking cursor"):
            decode_booking_cursor(value)


def test_booking_and_event_normalization_hide_unknown_raw_values():
    booking = normalize_booking_row({
        "id": BOOKING_ID,
        "customer_id": "42",
        "starts_at": NOW,
        "scheduled_end_at": None,
        "status": "private-status",
        "updated_at": NOW,
        "kind": "private-kind",
        "phase": "private-phase",
        "error_code": "private-error",
        "external_id": "provider-secret",
    })
    event = normalize_booking_event({
        "id": BOOKING_ID,
        "event_type": "private-event",
        "created_at": NOW,
    })
    assert booking["status_label"] == "Неизвестный статус"
    assert booking["scenario_label"] == "Системный сценарий"
    assert booking["phase_label"] == "Неизвестное состояние"
    assert booking["error_label"] == "Требуется проверка"
    assert "provider-secret" not in repr(booking)
    assert event["title"] == "Системное событие"
    assert "private-event" not in repr(event)
```

- [ ] **Step 2: Run RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/admin/test_booking_views.py
```

Expected: collection fails because `booking_views` does not exist.

- [ ] **Step 3: Implement the minimal presentation module**

Create constants for these exact allowlists:

```python
BOOKING_VIEWS = {"upcoming", "attention", "history"}
BOOKING_STATUS_LABELS = {
    "confirmed": "Подтверждена",
    "cancelled": "Отменена",
    "completed": "Завершена",
    "no_show": "Клиент не пришёл",
    "unknown": "Статус неизвестен",
}
SCENARIO_LABELS = {
    "create": "Создание записи",
    "reschedule": "Перенос записи",
    "cancel": "Отмена записи",
}
PHASE_LABELS = {
    "collecting": "Сбор данных",
    "awaiting_confirmation": "Ожидает подтверждения",
    "executing": "Выполняется",
    "confirmed": "Подтверждено",
    "failed": "Ошибка",
    "escalated": "Передано администратору",
}
EVENT_TITLES = {
    "booking_scenario_created": "Сценарий начат",
    "booking_execution_started": "Операция начата",
    "booking_confirmed": "Запись подтверждена",
    "booking_cancelled": "Запись отменена",
    "booking_rescheduled": "Запись перенесена",
    "slot_unavailable": "Слот уже недоступен",
    "admin_attention_required": "Требуется помощь администратора",
}
```

Cursor implementation must serialize only `{"at": sort_at.isoformat(), "id": str(booking_id)}` with compact JSON and URL-safe base64, require an aware datetime and exact keys on decode, and raise `ValueError("booking cursor")` on every malformed value. `normalize_booking_row(..., detail=False)` must omit `external_id`; detail mode may include it. Unknown raw enum/error/event strings must never be returned.

- [ ] **Step 4: Run GREEN**

Run the Task 1 command again.

Expected: all tests in `test_booking_views.py` pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add project/admin/booking_views.py project/tests/unit/admin/test_booking_views.py changelog.md
git commit -m "feat: добавить безопасную модель записей"
```

---

### Task 2: PostgreSQL list, detail and audit

**Files:**
- Create: `project/admin/bookings_database.py`
- Modify: `project/admin/database.py`
- Create: `project/tests/integration/admin/test_admin_bookings_postgres.py`

**Interfaces:**
- Consumes all Task 1 interfaces.
- Produces: `database.get_database() -> Database | None`
- Produces: `list_bookings(database: Database | None, *, view: str, status: str | None, cursor: str | None, limit: int = 50, now: datetime | None = None) -> dict[str, object]`
- Produces: `get_booking_detail(database: Database | None, booking_id: UUID, *, actor_id: int, ip_address: str | None, user_agent: str | None) -> dict[str, object] | None`
- Produces: `BookingDatabaseUnavailable(RuntimeError)`

- [ ] **Step 1: Write PostgreSQL RED for the three projections**

Seed these records with matching `booking_scenarios`:

```python
records = [
    # upcoming
    ("confirmed", NOW + timedelta(days=1), "confirmed", None),
    # attention by provider status
    ("unknown", NOW + timedelta(days=2), "confirmed", None),
    # attention by workflow failure
    ("confirmed", NOW + timedelta(days=3), "escalated",
     "booking_outcome_unknown"),
    # history
    ("cancelled", NOW - timedelta(days=1), "confirmed", None),
]
```

Assert:

- upcoming is ordered nearest-first;
- attention contains both unknown/outcome-unknown rows, newest update first;
- history contains cancelled/past rows, latest start first;
- status filter is applied after view selection;
- page size `limit + 1` yields a cursor and the next page has no duplicates;
- returned rows have no `snapshot`, `state`, `payload` or list-level `external_id`.

- [ ] **Step 2: Write PostgreSQL RED for detail and audit**

Assert one detail call returns allowlisted columns plus `external_id`, last scenario and normalized events; writes exactly one audit row:

```python
assert audit["action"] == "booking.view"
assert audit["object_type"] == "booking"
assert audit["object_id"] == str(booking_id)
assert audit["before"] is None
assert audit["after"] is None
assert "customer_id" not in repr(audit)
assert "external_id" not in repr(audit)
```

Also assert missing UUID returns `None` without audit, `database=None` raises `BookingDatabaseUnavailable`, and a trigger rejecting `booking.view` makes detail raise while leaving no partial audit.

- [ ] **Step 3: Run PostgreSQL RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/integration/admin/test_admin_bookings_postgres.py
```

Expected: collection fails because `bookings_database` does not exist.

- [ ] **Step 4: Add the shared pool accessor**

Add to `admin/database.py`:

```python
def get_database() -> Database | None:
    return _pool
```

No caller may mutate `_pool` through this accessor.

- [ ] **Step 5: Implement fixed SQL projections**

In `bookings_database.py`:

- validate view/status/cursor before acquiring the database;
- use three fixed SQL statements, never interpolate user input;
- select explicit columns from `bookings AS booking` and `booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id`;
- use `(starts_at, id)` ascending cursor for upcoming;
- use `(updated_at, id)` descending cursor for attention;
- use `(starts_at, id)` descending cursor for history;
- pass `status`, decoded cursor values, `now` and `limit + 1` as asyncpg parameters;
- normalize rows through Task 1 helpers.

Use these exact predicates:

```sql
-- upcoming
booking.status IN ('confirmed', 'unknown') AND booking.starts_at >= $4

-- attention
booking.status = 'unknown'
OR scenario.phase IN ('executing', 'failed', 'escalated')

-- history: everything outside the first two sets
NOT (booking.status IN ('confirmed', 'unknown') AND booking.starts_at >= $4)
AND NOT (
    booking.status = 'unknown'
    OR scenario.phase IN ('executing', 'failed', 'escalated')
)
```

For detail, start one transaction, fetch the explicit booking/scenario projection `FOR SHARE`, return `None` if missing, fetch only `id`, `event_type`, `created_at` for `last_scenario_id`, then insert:

```sql
INSERT INTO admin_audit_events (
    actor_id, action, object_type, object_id,
    before, after, ip_address, user_agent
)
VALUES ($1, 'booking.view', 'booking', $2, NULL, NULL, $3, $4)
```

Only after the transaction commits may the function return normalized detail. Audit failure must roll back and propagate.

- [ ] **Step 6: Run GREEN and booking ownership regressions**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/integration/admin/test_admin_bookings_postgres.py tests/integration/booking/test_booking_repository.py tests/integration/admin/test_customer_data_deletion_postgres.py
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add project/admin/bookings_database.py project/admin/database.py project/tests/integration/admin/test_admin_bookings_postgres.py changelog.md
git commit -m "feat: читать локальную проекцию записей"
```

---

### Task 3: Owner/admin HTTP and Jinja UI

**Files:**
- Create: `project/admin/booking_routes.py`
- Create: `project/admin/templates/bookings.html`
- Create: `project/admin/templates/booking_detail.html`
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/static/styles.css`
- Create: `project/tests/e2e/admin/test_admin_bookings.py`

**Interfaces:**
- Consumes Task 2 list/detail functions and `database.get_database()`.
- Produces GET `/bookings/` and GET `/bookings/{booking_id}`.

- [ ] **Step 1: Write HTTP RED**

Build an isolated FastAPI test app with the booking router and existing session/RBAC helpers. Cover:

- anonymous GET redirects to login;
- owner and admin receive 200;
- any non-staff role receives 403 before database access;
- malformed `view`, `status` and cursor return 422 before database access;
- missing UUID returns 404;
- unavailable database/detail-audit failure returns 503;
- `root_path=/admin` is preserved in menu, pagination and detail/chat links;
- raw external ID appears only escaped on detail;
- raw `snapshot`, `state`, `payload`, unknown enum/error/event values never appear;
- route module has no YCLIENTS adapter/client import and mocked database calls are the only data source.

- [ ] **Step 2: Run HTTP RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/e2e/admin/test_admin_bookings.py
```

Expected: collection fails because `booking_routes` does not exist.

- [ ] **Step 3: Implement routes**

Create router with `prefix="/bookings"`, `STAFF_ROLES = {"owner", "admin"}` and existing `get_current_user`, `require_role`, `request_ip_address`, `request_user_agent`, `admin_url` helpers.

List route:

```python
@router.get("/", response_class=HTMLResponse)
async def booking_list(
    request: Request,
    view: str = "upcoming",
    status: str | None = None,
    cursor: str | None = None,
):
    user = await get_current_user(request)
    require_role(user, STAFF_ROLES)
    try:
        page = await list_bookings(
            database.get_database(),
            view=view,
            status=status,
            cursor=cursor,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        logger.error("booking_list_failed error_type=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="bookings unavailable") from error
    return templates.TemplateResponse(
        request, "bookings.html",
        {"user": user, "page": page, "view": view, "status": status},
    )
```

Detail route validates auth/RBAC first, calls `get_booking_detail` with safe request metadata, returns 404 for `None`, logs only error type and returns 503 for database/audit failure.

- [ ] **Step 4: Implement templates and navigation**

`bookings.html` must contain:

- visible local-projection/YCLIENTS warning;
- tabs «Ближайшие», «Требуют внимания», «История»;
- allowlisted status filter;
- responsive cards/table with time, status, customer link, scenario/phase and local `updated_at`;
- empty state that says «В локальной проекции нет записей по этому фильтру»;
- one-way «Дальше» cursor link preserving root path, view and status.

`booking_detail.html` must contain:

- back link preserving root path;
- provider ID, times, status, freshness and safe scenario fields;
- customer link only when `customer_chat_id` is not `None`;
- safe event timeline;
- no action form/button for create/reschedule/cancel.

Add a «Записи» item for authenticated owner/admin in `base.html`. Extend existing CSS tokens/classes only; do not introduce JavaScript or a frontend dependency.

- [ ] **Step 5: Register router**

Import and include `booking_router` in `admin/app.py` alongside existing customer/escalation routers.

- [ ] **Step 6: Run GREEN and admin security regressions**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/e2e/admin/test_admin_bookings.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_admin_escalation_queue.py tests/unit/test_architecture_visual.py
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 3**

```powershell
git add project/admin/booking_routes.py project/admin/templates/bookings.html project/admin/templates/booking_detail.html project/admin/templates/base.html project/admin/static/styles.css project/admin/app.py project/tests/e2e/admin/test_admin_bookings.py changelog.md
git commit -m "feat: показать записи в админке"
```

---

### Task 4: Close documentation, review and full verification

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-08-14-admin-bookings-read-model.md`

**Interfaces:**
- Consumes the complete Task 1–3 feature.
- Produces a merge-ready local branch with exact evidence.

- [ ] **Step 1: Run affected Docker gate**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/admin/test_booking_views.py tests/integration/admin/test_admin_bookings_postgres.py tests/e2e/admin/test_admin_bookings.py tests/integration/booking/test_booking_repository.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/unit/admin/test_customer_events.py tests/integration/admin/test_customer_events_postgres.py tests/e2e/admin/test_csrf_rbac_audit.py tests/unit/test_architecture_visual.py
```

Expected: exit 0, zero failures.

- [ ] **Step 2: Request independent review**

Review exact range `e7c41e7..HEAD` for Critical/Important findings, focusing on PII exposure, RBAC order, audit fail-closed behavior, cursor stability, SQL projection overlap, raw JSON leakage and accidental YCLIENTS/provider calls. Fix every Critical/Important with a reproducing RED test and request re-review.

- [ ] **Step 3: Run full fresh Docker gate**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" test pytest -q
```

Expected: exit 0, zero failures.

- [ ] **Step 4: Run static and document gates**

```powershell
git diff --check
docker compose --env-file ../.env run --build --rm test python -m compileall -q admin src worker
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" test pytest -q tests/unit/test_documented_compose_commands.py
```

Expected: all commands exit 0.

- [ ] **Step 5: Close roadmap and changelog**

Mark only the phase-1 read-model item complete. Record exact focused/full counts, review verdict, final HEAD, no push/deploy/provider calls and explicit deferral of YCLIENTS reconciliation/mutations.

- [ ] **Step 6: Commit closure and verify branch**

```powershell
git add "Дорожная карта.md" changelog.md docs/superpowers/plans/2026-08-14-admin-bookings-read-model.md
git commit -m "docs: завершить read-only центр записей"
git status --short --branch
git log --oneline --reverse e7c41e7..HEAD
git merge-base --is-ancestor e7c41e7 HEAD
```

Expected: clean `codex/admin-bookings-read-model`, ancestry exit 0, no remote branch containing final HEAD.
