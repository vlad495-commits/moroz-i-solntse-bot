# YCLIENTS Booking Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать owner/admin единый read-only список всех записей YCLIENTS за окно -30/+90 дней с происхождением «Бот / другой канал», безопасными расхождениями и freshness без provider-вызовов из admin.

**Architecture:** Один allowlisted снимок `yclients_booking_projection` атомарно заменяется существующим worker по постраничному GET-контракту YCLIENTS. Worker сам идемпотентно планирует текущий и следующий 10-минутный `scheduler_job`; неизменённые scheduler/RabbitMQ доставляют его. Admin объединяет снимок с локальными `bookings` по `external_id` и `moroz_booking_key` и вычисляет provenance/reconciliation без mutations.

**Tech Stack:** Python 3.12, PostgreSQL 16, Alembic, asyncpg через существующий `Database`, текущие `YclientsHttpClient`/rate limiter/scheduler/worker/RabbitMQ, FastAPI/Jinja2, pytest, Docker Compose.

## Global Constraints

- Рабочая ветка: `codex/admin-bookings-reconciliation`, base `5f19dac1046cbe538628a64c1e4bced5cd266814`.
- Проект и тесты запускаются только через Docker; напрямую `python`/`pytest` не запускать.
- Новая схема ограничена одной таблицей `yclients_booking_projection`; вторая sync-state таблица запрещена.
- Не добавлять сервис, очередь, dependency, Redis-кэш, WebSocket, frontend framework или generic sync abstraction.
- Admin routes никогда не импортируют и не вызывают YCLIENTS transport.
- Provider scope строго read-only: `GET /api/v1/records/{company_id}`; create/reschedule/cancel/delete запрещены.
- Окно снимка: последние 30 дней и следующие 90 дней в `YCLIENTS_TIMEZONE`; interval 10 минут; stale threshold 20 минут.
- Сохранять только provider ID, UUID marker state/key, время, нормализованный status/deleted, имя, сотрудника и названия услуг.
- Телефон, email, комментарии, agreements, остальные custom fields, provider body и raw JSON не сохранять и не логировать.
- Имя клиента показывается только как escaped display text; YCLIENTS-only запись не получает Telegram chat/detail link.
- Сопоставление только по `external_id` и `moroz_booking_key`; имя, телефон и время не используются как identity.
- Ошибка любой страницы/валидации/DB оставляет предыдущий снимок неизменным.
- Никаких staging/production/provider вызовов, push или deploy.
- `Дорожная карта.md` и `changelog.md` обновляются на каждом логическом шаге.

---

### Task 1: Migration `0010` for the bounded projection

**Files:**
- Create: `project/migrations/versions/0010_yclients_booking_projection.py`
- Create: `project/tests/unit/admin/test_migration_0010.py`
- Modify: `project/tests/integration/test_migrations.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Alembic head `0009_production_admin`.
- Produces: table `yclients_booking_projection` with the exact columns, checks and indexes from the design.

- [x] **Step 1: Write migration contract RED**

Create `test_migration_0010.py` using the existing importlib/source contract pattern:

```python
def load_migration():
    path = Path("/workspace/migrations/versions/0010_yclients_booking_projection.py")
    spec = importlib.util.spec_from_file_location("migration_0010", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_projection_migration_contract():
    migration = load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert migration.revision == "0010_yclients_projection"
    assert migration.down_revision == "0009_production_admin"
    assert source.count('op.create_table(') == 1
    assert '"yclients_booking_projection"' in source
    assert '"ix_yclients_projection_starts_external"' in source
    assert '"ix_yclients_projection_booking_key"' in source
```

Extend `test_migrations.py` to upgrade a fresh namespace and assert exact column names, `external_id` primary key, marker/status checks and both indexes. Assert no column name contains `phone`, `email`, `comment`, `payload`, `snapshot`, `raw` or `json`.

- [x] **Step 2: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/admin/test_migration_0010.py tests/integration/test_migrations.py
```

Expected: collection fails because migration `0010_yclients_booking_projection.py` does not exist.

- [x] **Step 3: Implement the minimal migration**

Create one table with these columns:

```python
op.create_table(
    "yclients_booking_projection",
    sa.Column("external_id", sa.Text(), primary_key=True),
    sa.Column("booking_key", postgresql.UUID(as_uuid=True)),
    sa.Column("bot_marker_state", sa.Text(), nullable=False),
    sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("scheduled_end_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("deleted", sa.Boolean(), nullable=False),
    sa.Column("client_name", sa.Text()),
    sa.Column("staff_name", sa.Text()),
    sa.Column("service_names", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "bot_marker_state IN ('absent','valid','invalid')",
        name="ck_yclients_projection_marker",
    ),
    sa.CheckConstraint(
        "status IN ('confirmed','cancelled','completed','no_show','unknown')",
        name="ck_yclients_projection_status",
    ),
)
op.create_index(
    "ix_yclients_projection_starts_external",
    "yclients_booking_projection",
    ["starts_at", "external_id"],
)
op.create_index(
    "ix_yclients_projection_booking_key",
    "yclients_booking_projection",
    ["booking_key"],
    postgresql_where=sa.text("booking_key IS NOT NULL"),
)
```

Downgrade drops the booking-key index, starts index and table in that order.

- [x] **Step 4: Run GREEN and Alembic head proof**

Run the Step 2 command. Expected: all selected tests pass and integration current revision is `0010_yclients_projection` (use the exact `revision` string declared by the migration).

- [x] **Step 5: Commit Task 1**

```powershell
git add project/migrations/versions/0010_yclients_booking_projection.py project/tests/unit/admin/test_migration_0010.py project/tests/integration/test_migrations.py changelog.md
git commit -m "feat: добавить проекцию записей YCLIENTS"
```

---

### Task 2: Safe paginated YCLIENTS records reader

**Files:**
- Create: `project/src/moroz/booking/yclients_records.py`
- Create: `project/tests/contract/booking/test_yclients_records.py`
- Modify: `project/src/moroz/booking/yclients.py`
- Modify: `project/tests/contract/booking/test_yclients_adapter.py`
- Modify: `changelog.md`

**Interfaces:**
- Produces: `ProjectionRecord`, `ProjectionSnapshot`, `YclientsProjectionError`, `YclientsRecordsReader.read_window(now: datetime) -> ProjectionSnapshot`.
- Produces: `normalize_visit_status(record: Mapping[str, object]) -> BookingStatus` reused by `YclientsAdapter`.

- [x] **Step 1: Write parser and pagination RED**

Cover:

```python
snapshot = await YclientsRecordsReader(config, http=fake).read_window(NOW)
assert fake.requests == [
    ("GET", "/api/v1/records/17", (
        ("page", 1), ("count", 100),
        ("start_date", "2026-07-15"),
        ("end_date", "2026-11-12"),
        ("with_deleted", 1),
    ), True),
    ("GET", "/api/v1/records/17", (
        ("page", 2), ("count", 100),
        ("start_date", "2026-07-15"),
        ("end_date", "2026-11-12"),
        ("with_deleted", 1),
    ), True),
]
assert snapshot.records[0].booking_key == BOOKING_KEY
assert snapshot.records[0].bot_marker_state == "valid"
assert not hasattr(snapshot.records[0], "phone")
```

Fixtures include `client.name`, `staff.name`, `services[].title`, `datetime`, `seance_length`, `attendance`, `deleted` and `custom_fields.moroz_booking_key`. Add cases for absent/invalid UUID marker, deleted/no-show/completed/unknown attendance, duplicate provider ID, page 100 still full, malformed envelope/item and HTTP/transport failure. Assert exception exposes only one code from:

```python
{
    "yclients_transport",
    "yclients_http_status",
    "yclients_response_shape",
    "yclients_page_bound",
}
```

Assert display controls are removed, text is trimmed/capped at 200, and more than 50 services fails closed.

- [x] **Step 2: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/contract/booking/test_yclients_records.py tests/contract/booking/test_yclients_adapter.py
```

Expected: collection fails because `moroz.booking.yclients_records` does not exist.

- [x] **Step 3: Implement the bounded data types and safe exception**

```python
@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    external_id: str
    booking_key: UUID | None
    bot_marker_state: Literal["absent", "valid", "invalid"]
    starts_at: datetime
    scheduled_end_at: datetime | None
    status: BookingStatus
    deleted: bool
    client_name: str | None
    staff_name: str | None
    service_names: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    records: tuple[ProjectionRecord, ...]
    synced_at: datetime

class YclientsProjectionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
```

Implement `_safe_display`, strict envelope/item parsing, timezone-aware datetime/end calculation and `normalize_visit_status`. Modify `yclients.py::_external_booking` to call the shared status normalizer; preserve all adapter behavior.

- [x] **Step 4: Implement exact bounded pagination**

Use `_PAGE_SIZE = 100`, `_MAX_PAGES = 100`, `YclientsConfig.timezone_name` and `YclientsHttpClient.request(..., user_auth=True)`. Reject duplicates. Stop only when `len(data) < 100`; a full page 100 raises `yclients_page_bound`.

- [x] **Step 5: Run GREEN**

Run Step 2. Expected: all selected tests pass; no provider call leaves the fake transport.

- [x] **Step 6: Commit Task 2**

```powershell
git add project/src/moroz/booking/yclients_records.py project/src/moroz/booking/yclients.py project/tests/contract/booking/test_yclients_records.py project/tests/contract/booking/test_yclients_adapter.py changelog.md
git commit -m "feat: читать безопасный снимок записей YCLIENTS"
```

---

### Task 3: Atomic repository, lock and recurring job coordinator

**Files:**
- Create: `project/src/moroz/booking/projection.py`
- Create: `project/tests/integration/booking/test_yclients_projection.py`
- Create: `project/tests/unit/booking/test_projection_sync.py`
- Modify: `project/src/moroz/notifications/models.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 1 table; Task 2 `ProjectionSnapshot` and reader.
- Produces: `ProjectionRepository.serialized()`, `ProjectionRepository.replace(connection, snapshot)`, `ProjectionSyncCoordinator.ensure_current(now)`, `ProjectionSyncCoordinator.run(job)` and `PROJECTION_SYNC_KIND`.

- [x] **Step 1: Write PostgreSQL RED for atomic replacement**

Assert:

```python
async with repository.serialized() as connection:
    assert connection is not None
    await repository.replace(connection, first_snapshot)
assert await projection_rows(conn) == expected_first

install_rejecting_trigger(conn)
with pytest.raises(YclientsProjectionError, match="yclients_projection_write"):
    async with repository.serialized() as connection:
        assert connection is not None
        await repository.replace(connection, second_snapshot)
assert await projection_rows(conn) == expected_first
```

Also assert a second repository cannot enter `serialized()` while the first holds the session advisory lock, and can enter after release. Check DB rows contain no source fixture phone/comment/raw JSON.

- [x] **Step 2: Write coordinator RED**

```python
current = projection_job(NOW)
next_job = projection_job(NOW + timedelta(minutes=10))
await coordinator.ensure_current(NOW)
await coordinator.ensure_current(NOW)
assert scheduled_keys == [current.idempotency_key, current.idempotency_key]

result = await coordinator.run(current)
assert result == JobResult.sent()
assert next_job.idempotency_key in scheduled_keys
```

The fake repository yields `None` from `serialized()` to assert overlapping run returns `JobResult.skipped("projection_busy")` without reader call. Reader failure still schedules the next bucket before propagating the safe exception.

- [x] **Step 3: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/integration/booking/test_yclients_projection.py tests/unit/booking/test_projection_sync.py
```

Expected: collection fails because `moroz.booking.projection` does not exist.

- [x] **Step 4: Implement repository and session lock**

```python
PROJECTION_LOCK = "yclients_booking_projection:v1"

@asynccontextmanager
async def serialized(self):
    async with self._database.acquire() as connection:
        locked = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
            PROJECTION_LOCK,
        )
        try:
            yield connection if locked else None
        finally:
            if locked:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    PROJECTION_LOCK,
                )
```

`replace(connection, snapshot)` uses one `connection.transaction()`, `DELETE FROM yclients_booking_projection`, then `executemany` of explicit columns. Wrap DB failures as `YclientsProjectionError("yclients_projection_write")` without preserving provider/PII text in `str(error)`.

- [x] **Step 5: Implement job bucketing and coordinator**

Change `PlannedSchedulerJob.booking_key` and `.booking_starts_at` to optional types; DB columns are already nullable.

```python
PROJECTION_SYNC_KIND = "yclients_booking_projection_sync"

def projection_job(now: datetime) -> PlannedSchedulerJob:
    utc = now.astimezone(UTC)
    bucket = utc.replace(minute=(utc.minute // 10) * 10, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=PROJECTION_SYNC_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{PROJECTION_SYNC_KIND}:{bucket.isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )
```

`ProjectionSyncCoordinator` receives an injectable aware-UTC clock. `ensure_current` calls existing `SchedulerJobRepository.schedule`. `run(job)` schedules `projection_job(job.run_at + 10 minutes)` before trying the lock/reader, calls `reader.read_window(clock())`, then atomically replaces the snapshot if lock was acquired.

- [x] **Step 6: Run GREEN and booking repository regressions**

```powershell
docker compose --env-file ../.env run --build --rm test pytest -q tests/integration/booking/test_yclients_projection.py tests/unit/booking/test_projection_sync.py tests/integration/booking/test_booking_repository.py tests/integration/notifications/test_jobs.py
```

Expected: all selected tests pass.

- [x] **Step 7: Commit Task 3**

```powershell
git add project/src/moroz/booking/projection.py project/src/moroz/notifications/models.py project/tests/integration/booking/test_yclients_projection.py project/tests/unit/booking/test_projection_sync.py changelog.md
git commit -m "feat: атомарно обновлять проекцию YCLIENTS"
```

---

### Task 4: Worker runtime wiring and safe scheduler failures

**Files:**
- Modify: `project/src/moroz/notifications/handlers.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `project/tests/e2e/notifications/test_reminders.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 3 `ProjectionSyncCoordinator` and `PROJECTION_SYNC_KIND`.
- Produces: optional worker runtime coordinator when YCLIENTS config is complete; scheduler container remains unchanged.

- [x] **Step 1: Write worker RED**

Add tests that a projection `SchedulerJob`:

- calls `projection_sync.run(job)` before any `booking_port.get_booking`;
- completes via existing repository on `JobResult.sent/skipped`;
- records only `YclientsProjectionError.code`, never private exception text;
- follows existing retry/terminal threshold;
- fails closed when sync job arrives without configured coordinator;
- calls `ensure_current` once during configured worker startup.

Also assert `MessageTaskHandler` requires `SchedulerJobRepository` before loading any job, but requires `booking_port`/notification outbox only for non-projection jobs. A configured projection job must not be blocked by missing reminder dependencies.

Assert scheduler unit tests and scheduler Compose environment are unchanged.

- [x] **Step 2: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_worker.py tests/unit/test_scheduler.py tests/e2e/notifications/test_reminders.py
```

Expected: new projection-job assertions fail because worker has no coordinator wiring.

- [x] **Step 3: Add the handler branch**

At the top of `handle_scheduler_job`:

```python
if job.kind == PROJECTION_SYNC_KIND:
    if projection_sync is None:
        raise RuntimeError("projection sync is not configured")
    return await projection_sync.run(job)
```

Pass `projection_sync` through `MessageTaskHandler`; existing reminder/lifecycle behavior remains unchanged. Map a `YclientsProjectionError` to its allowlisted `.code` before `record_failure`; all other errors retain the existing type-only code.

- [x] **Step 4: Build one runtime graph**

Replace the lifecycle-only builder with this exact runtime seam so the same `YclientsConfig` is parsed once:

```python
def _build_yclients_services(
    database: Database,
) -> tuple[LifecycleService | None, ProjectionSyncCoordinator | None]:
    required = (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    )
    present = tuple(bool(os.environ.get(name, "").strip()) for name in required)
    if not any(present):
        return None, None
    if not all(present):
        raise ValueError("YCLIENTS lifecycle configuration is incomplete")
    config = YclientsConfig.from_env(os.environ)
    lifecycle = LifecycleService(
        database,
        YclientsAdapter(config),
        FeedbackService(database),
    )
    coordinator = ProjectionSyncCoordinator(
        ProjectionRepository(database),
        YclientsRecordsReader(config),
        SchedulerJobRepository(database),
    )
    return lifecycle, coordinator
```

The all-empty case returns `(None, None)` and partial required config raises the existing incomplete-config error. The complete case creates:

```python
reader = YclientsRecordsReader(config)
projection_sync = ProjectionSyncCoordinator(
    ProjectionRepository(database),
    reader,
    SchedulerJobRepository(database),
)
```

After DB connect and before queue consume, call `await projection_sync.ensure_current(datetime.now(UTC))` when configured. Do not pass YCLIENTS variables to scheduler, do not edit Compose, and do not add a service/queue.

- [x] **Step 5: Run GREEN and architecture assertions**

Run Step 2 plus:

```powershell
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" -e ARCHITECTURE_HTML_PATH=/docs/production-v1-architecture.html test pytest -q tests/unit/test_architecture_visual.py tests/unit/test_migration_profile.py
```

Use the canonical read-only docs mount/path override required by `test_architecture_visual.py`. Expected: all selected tests pass and Compose service count is unchanged.

- [x] **Step 6: Commit Task 4**

```powershell
git add project/src/moroz/notifications/handlers.py project/worker/main.py project/tests/unit/test_worker.py project/tests/e2e/notifications/test_reminders.py changelog.md
git commit -m "feat: запускать сверку через существующий worker"
```

---

### Task 5: Unified PostgreSQL read model, provenance and versioned cursor

**Files:**
- Modify: `project/admin/booking_views.py`
- Modify: `project/admin/bookings_database.py`
- Modify: `project/tests/unit/admin/test_booking_views.py`
- Modify: `project/tests/integration/admin/test_admin_bookings_postgres.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 1 table and Task 3 job kind.
- Produces: `validate_booking_filters(view, status, source="all", reconciliation="all")`, unified `list_bookings(..., source="all", reconciliation="all")`, versioned text-key cursor and `page["freshness"]`.

- [x] **Step 1: Write unit RED for filters, source labels and cursor**

Require only:

```python
BOOKING_SOURCES = {"all", "bot", "other"}
BOOKING_RECONCILIATION_FILTERS = {"all", "mismatch"}

cursor = encode_booking_cursor(NOW, "y:123")
assert decode_booking_cursor(cursor) == (NOW, "y:123")
for key in ("x:123", "y:", "l:not-a-uuid", "y:" + "1" * 65):
    with pytest.raises(ValueError, match="booking cursor"):
        encode_booking_cursor(NOW, key)
```

Legacy unversioned UUID cursor and unknown source/reconciliation return `ValueError` before DB access. Safe labels never expose invalid marker/raw statuses.

- [x] **Step 2: Write PostgreSQL RED for the reconciliation matrix**

Seed:

1. matching local/projection ID+key → `bot/in_sync`;
2. matching identity but changed start/status → `bot/changed_in_yclients`;
3. projection without marker/local → `other/yclients_only`;
4. projection valid marker without local → `bot/local_missing`;
5. local row absent from a successful snapshot → `bot/provider_missing`;
6. local ID with missing/different/invalid marker → `bot/identity_conflict`;
7. no successful sync yet → local row `freshness_unknown`, never `provider_missing`.

Assert source/mismatch/status/view filters are SQL-parameterized and applied before limit. Assert provider truth wins for displayed time/status, local scenario remains only on bot rows, and YCLIENTS-only rows have `customer_chat_id=None`, no local detail UUID, phone/raw/custom fields.

Assert keyset insertion-before-cursor stability for `y:` and `l:` row keys in upcoming/history/attention. Freshness uses projection `MAX(synced_at)` when nonempty and successful job `finished_at` for a valid empty snapshot; stale is true only after 20 minutes.

Extend customer deletion integration: deleting local customer-owned data must leave the provider projection row unchanged, remove every chat/detail link, and make a valid bot marker without local ownership appear as `local_missing`. No provider mutation is enqueued.

- [x] **Step 3: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/admin/test_booking_views.py tests/integration/admin/test_admin_bookings_postgres.py
```

Expected: failures on new filter/cursor signatures and missing unified rows.

- [x] **Step 4: Implement versioned bounded cursor**

Use compact URL-safe base64 JSON:

```python
{"v": 2, "at": sort_at.isoformat(), "key": row_key}
```

Accept exact keys only. `y:` requires canonical positive integer text of at most 64 digits; `l:` requires canonical UUID. Aware datetime is mandatory.

- [x] **Step 5: Implement one fixed unified SQL CTE per view**

Build `provider_rows` from projection with `LEFT JOIN bookings` on external ID, then `UNION ALL` local rows in the same -30/+90 window with `NOT EXISTS` projection external ID. Compute `row_key`, `source`, `reconciliation_state`, `attention_at`, safe display columns and local detail UUID with SQL `CASE`; never select booking `snapshot`, scenario `state`, event `payload` or provider fields not in the projection.

Use fixed allowlisted SQL for upcoming/history/attention; user input appears only as asyncpg parameters. Preserve `limit + 1`, source/status/reconciliation filters and versioned keyset comparison.

View membership is exact:

```sql
-- upcoming
status IN ('confirmed', 'unknown') AND starts_at >= $now

-- attention
reconciliation_state IN (
    'changed_in_yclients', 'local_missing',
    'provider_missing', 'identity_conflict'
)
OR local_phase IN ('executing', 'failed', 'escalated')
OR local_status = 'unknown'

-- history
NOT (upcoming predicate) AND NOT (attention predicate)
```

`yclients_only` by itself is expected for another channel and is not a mismatch/attention condition. A deleted provider row normalizes to `cancelled` and therefore enters history unless a bot-owned identity/status discrepancy makes it attention.

- [x] **Step 6: Run GREEN and ownership regressions**

```powershell
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/admin/test_booking_views.py tests/integration/admin/test_admin_bookings_postgres.py tests/integration/booking/test_booking_repository.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/integration/admin/test_customer_events_postgres.py
```

Expected: all selected tests pass.

- [x] **Step 7: Commit Task 5**

```powershell
git add project/admin/booking_views.py project/admin/bookings_database.py project/tests/unit/admin/test_booking_views.py project/tests/integration/admin/test_admin_bookings_postgres.py project/tests/integration/admin/test_customer_data_deletion_postgres.py changelog.md
git commit -m "feat: объединить локальные записи с YCLIENTS"
```

---

### Task 6: Owner/admin unified UI and safe filters

**Files:**
- Modify: `project/admin/booking_routes.py`
- Modify: `project/admin/templates/bookings.html`
- Modify: `project/admin/static/styles.css`
- Modify: `project/tests/e2e/admin/test_admin_bookings.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 5 unified `list_bookings` page.
- Produces: existing GET `/bookings/` with `source` and `reconciliation` query filters; local detail route remains unchanged.

- [x] **Step 1: Write HTTP RED**

Cover owner/admin 200, anonymous redirect, non-staff 403 before DB, malformed `source/reconciliation/cursor` 422 before DB, DB unavailable 503 and `root_path=/admin` preservation.

Rendered assertions:

```python
assert "Создано ботом" in html
assert "Другой канал" in html
assert "Есть расхождение" in html
assert "Последняя синхронизация" in html
assert "Данные YCLIENTS могут быть устаревшими" in stale_html
assert "/admin/chats/" not in yclients_only_row
assert "/admin/bookings/" not in yclients_only_row
assert private_phone not in html
assert raw_custom_field not in html
```

Assert pagination preserves view/status/source/reconciliation/root path. Unknown provider text is escaped and never rendered as HTML or an internal error/status code.

- [x] **Step 2: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/e2e/admin/test_admin_bookings.py
```

Expected: new filter/freshness/source assertions fail.

- [x] **Step 3: Extend GET route only**

Add query arguments with defaults `source="all"`, `reconciliation="all"`; pass them to Task 5. Preserve auth/RBAC before validation/DB and existing safe 422/503 handling. Do not import YCLIENTS modules into `booking_routes.py`.

- [x] **Step 4: Extend the existing template and CSS**

Add two native `<select>` filters, source/reconciliation badges, client/staff/services cells and freshness banner. Links exist only when `detail_id` or `customer_chat_id` is non-null. No JavaScript, POST form, refresh button or frontend dependency.

- [x] **Step 5: Run GREEN and admin security regressions**

```powershell
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" -e ARCHITECTURE_HTML_PATH=/docs/production-v1-architecture.html test pytest -q tests/e2e/admin/test_admin_bookings.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_admin_escalation_queue.py tests/unit/test_architecture_visual.py
```

Mount root `docs/` read-only and set `ARCHITECTURE_HTML_PATH` for the selected visual test. Expected: all selected tests pass.

- [x] **Step 6: Commit Task 6**

```powershell
git add project/admin/booking_routes.py project/admin/templates/bookings.html project/admin/static/styles.css project/tests/e2e/admin/test_admin_bookings.py changelog.md
git commit -m "feat: показать происхождение записей в админке"
```

---

### Task 7: Migration proof, independent review and full closure

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-08-14-admin-bookings-reconciliation.md`

**Interfaces:**
- Consumes: complete Tasks 1–6.
- Produces: clean local merge-ready branch with exact evidence; no push/deploy/provider call.

- [x] **Step 1: Run affected Docker gate**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" -e ARCHITECTURE_HTML_PATH=/docs/production-v1-architecture.html test pytest -q tests/unit/admin/test_migration_0010.py tests/contract/booking/test_yclients_records.py tests/integration/booking/test_yclients_projection.py tests/unit/booking/test_projection_sync.py tests/unit/test_worker.py tests/unit/test_scheduler.py tests/unit/admin/test_booking_views.py tests/integration/admin/test_admin_bookings_postgres.py tests/e2e/admin/test_admin_bookings.py tests/integration/booking/test_booking_repository.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/integration/admin/test_customer_events_postgres.py tests/e2e/admin/test_csrf_rbac_audit.py tests/unit/test_architecture_visual.py
```

Expected: exit 0, zero failures.

- [x] **Step 2: Prove migration head**

Use an isolated test namespace and run:

```powershell
docker compose --env-file ../.env run --build --rm migrate
docker compose --env-file ../.env run --rm migrate alembic -c /app/alembic.ini current
```

Expected current output contains the exact Task 1 revision with `(head)`. Drop only the verified isolated test namespace after evidence.

- [x] **Step 3: Request independent review**

Review exact range `5f19dac..HEAD` against the approved spec. Require separate findings for Critical/Important/Minor and Ready Yes/No, focusing on PII/raw data, source misattribution, partial snapshots, job loss/overlap, cursor stability, RBAC order, provider calls from admin and accidental mutations. Fix every Critical/Important with a reproducing RED test and request re-review; evaluate Minor findings technically and close those that improve correctness without expanding scope.

- [x] **Step 4: Run fresh full Docker gate**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" test pytest -q
```

Expected: exit 0, zero failures.

- [x] **Step 5: Run static and document gates**

```powershell
git diff --check
docker compose --env-file ../.env run --build --rm test python -m compileall -q admin src worker
docker compose --env-file ../.env run --build --rm --volume "${PWD}/../docs:/docs:ro" test pytest -q tests/unit/test_documented_compose_commands.py
```

Expected: all commands exit 0.

- [x] **Step 6: Close roadmap and changelog**

Mark only phase 2 read-only reconciliation complete. Record exact affected/full counts, migration head, review verdict, final HEAD, no push/deploy/provider calls, and defer granular source attribution, manual refresh and every YCLIENTS mutation.

- [x] **Step 7: Commit closure and verify branch**

```powershell
git add "Дорожная карта.md" changelog.md docs/superpowers/plans/2026-08-14-admin-bookings-reconciliation.md
git commit -m "docs: завершить read-only сверку YCLIENTS"
git status --short --branch
git log --oneline --reverse 5f19dac..HEAD
git merge-base --is-ancestor 5f19dac HEAD
git branch -r --contains HEAD
```

Expected: clean `codex/admin-bookings-reconciliation`, ancestry exit 0, and no remote branch contains final HEAD.

**Completion evidence (2026-08-15):** exact affected gate `178 passed`; isolated migration proof `0010_yclients_projection (head)`; fresh full Docker suite on implementation HEAD `ca74221` — `1150 passed in 569.79s`; Docker compileall, document gate and `git diff --check` exited `0`; final whole-branch review — `0 Critical / 0 Important / 0 Minor`, Ready Yes. No push, deploy, provider, staging or production calls were made. Granular source attribution, manual refresh, real sandbox permissions/preflight and every YCLIENTS mutation remain deferred.
