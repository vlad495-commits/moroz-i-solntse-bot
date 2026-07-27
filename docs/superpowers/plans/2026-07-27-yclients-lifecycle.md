# YCLIENTS Lifecycle Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect read-only YCLIENTS visit outcomes to the existing scheduler so completed visits create feedback once, no-shows notify safely, and unresolved outcomes receive bounded checks and staff alerts.

**Architecture:** Keep normal reminders PostgreSQL-only. Lifecycle jobs call a small durable service that reads one protected YCLIENTS record, conditionally persists its status and scheduled end, and inserts the next fixed follow-up job. Existing notification outbox, feedback service, worker retry/DLQ, and scheduler tables provide delivery and idempotency.

**Tech Stack:** Python 3.12, aiogram 3, asyncpg, aiohttp-based YCLIENTS adapter, Alembic 1.18, PostgreSQL 16, RabbitMQ, pytest, Docker Compose.

## Global Constraints

- Work locally through Docker only; never run project Python directly.
- Start from committed `main` design checkpoint `e9841da`.
- Create Alembic revision `0008_yclients_lifecycle` with `down_revision = "0007_scheduler_notifications"`.
- Do not mutate staging, production, Telegram, or YCLIENTS.
- Do not execute live YCLIENTS requests; all HTTP verification uses the existing fake server.
- Do not add a polling service, table, queue, dependency, or container.
- Persist `bookings.scheduled_end_at` so retries recover after process crashes.
- Use TDD for every production behavior: RED, observed failure, minimal GREEN, regression.
- Use a dedicated Compose project name and remove its containers, volumes, network, and local images after verification.

---

### Task 1: Domain, Migration, and Provider Mapping

**Files:**
- Create: `project/migrations/versions/0008_yclients_lifecycle.py`
- Modify: `project/src/moroz/booking/models.py`
- Modify: `project/src/moroz/booking/yclients.py`
- Modify: `project/src/moroz/booking/mock_yclients.py`
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/tests/contract/booking/test_yclients_adapter.py`
- Modify: `project/tests/integration/test_migrations.py`
- Modify: `project/tests/integration/booking/test_booking_repository.py`

**Interfaces:**
- Produces: `BookingStatus = Literal["confirmed", "cancelled", "completed", "no_show", "unknown"]`.
- Produces: `ExternalBooking.scheduled_end_at: datetime | None`.
- Produces: `_visit_status(record: Mapping[str, object]) -> BookingStatus`.
- Persists: `bookings.scheduled_end_at TIMESTAMPTZ NULL`.

- [x] **Step 1: Write failing adapter mapping tests**

Add parameterized fake-HTTP contract cases that call `YclientsAdapter.get_booking`
with exact owned records:

```python
@pytest.mark.parametrize(
    ("attendance", "deleted", "expected"),
    [
        (-1, False, "no_show"),
        (0, False, "confirmed"),
        (1, False, "completed"),
        (2, False, "confirmed"),
        (77, False, "unknown"),
        (1, True, "cancelled"),
    ],
)
async def test_get_booking_maps_visit_lifecycle(
    yclients_config, server, attendance, deleted, expected
):
    server.enqueue_json(
        200,
        {
            "success": True,
            "data": {
                **owned_record(),
                "attendance": attendance,
                "deleted": deleted,
                "datetime": "2026-07-29T12:00:00+03:00",
                "seance_length": 3600,
            },
        },
    )
    booking = await adapter(yclients_config, server).get_booking(get_command())
    assert booking.status == expected
    assert booking.scheduled_end_at == datetime(
        2026, 7, 29, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")
    )
    assert [request[0] for request in server.requests] == ["GET"]


async def test_get_booking_rejects_non_integer_attendance(yclients_config, server):
    server.enqueue_json(
        200,
        {"success": True, "data": {**owned_record(), "attendance": "1"}},
    )
    with pytest.raises(BookingTemporaryError):
        await adapter(yclients_config, server).get_booking(get_command())
```

- [x] **Step 2: Run adapter tests and observe RED**

Run:

```powershell
$env:COMPOSE_PROJECT_NAME='moroz_lifecycle_0008'
docker compose --env-file ../.env run --rm --build test pytest -q tests/contract/booking/test_yclients_adapter.py -k lifecycle
```

Expected: failures show `confirmed` for lifecycle outcomes and missing
`scheduled_end_at`.

- [x] **Step 3: Implement minimal immutable domain and mapping**

Use one status alias and one optional durable field:

```python
BookingStatus = Literal[
    "confirmed", "cancelled", "completed", "no_show", "unknown"
]


@dataclass(frozen=True, slots=True)
class ExternalBooking:
    external_id: str
    customer_id: str
    booking_key: UUID
    slot_id: str
    starts_at: datetime
    status: BookingStatus
    scheduled_end_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.starts_at)
        if self.scheduled_end_at is not None:
            _require_aware(self.scheduled_end_at)
            if self.scheduled_end_at <= self.starts_at:
                raise ValueError("scheduled_end_at must be after starts_at")
```

Map the exact provider contract:

```python
def _visit_status(record: Mapping[str, object]) -> BookingStatus:
    deleted = record.get("deleted", False)
    if type(deleted) is not bool:
        raise BookingTemporaryError()
    if deleted:
        return "cancelled"
    attendance = record.get("attendance")
    if attendance is None:
        return "unknown"
    if type(attendance) is not int:
        raise BookingTemporaryError()
    return {
        -1: "no_show",
        0: "confirmed",
        1: "completed",
        2: "confirmed",
    }.get(attendance, "unknown")
```

In `_external_booking`, calculate:

```python
scheduled_end_at = starts_at + timedelta(seconds=duration)
```

Pass `_visit_status(record)` and `scheduled_end_at` to `ExternalBooking`.
Populate the same field from mock slot duration and preserve it in mock
reschedule/cancel/get operations.

- [x] **Step 4: Run adapter and booking regressions GREEN**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/contract/booking/test_yclients_adapter.py tests/unit/booking tests/integration/booking
```

Expected: all selected tests pass and fake HTTP request methods remain GET-only
for lifecycle reads.

- [x] **Step 5: Write failing migration and repository tests**

Add a migration test that upgrades from `0007_scheduler_notifications`, checks
the new column and constraint, inserts all five statuses, downgrades to `0007`,
and verifies normalization:

```python
assert current_revision == "0008_yclients_lifecycle"
assert columns["scheduled_end_at"] == ("timestamp with time zone", "YES")
for status in ("confirmed", "cancelled", "completed", "no_show", "unknown"):
    await insert_booking(conn, status=status)
run_alembic(database_url, "downgrade", "0007_scheduler_notifications")
assert await conn.fetchval(
    "SELECT count(*) FROM bookings WHERE status <> 'confirmed'"
) == 0
```

Add a repository round-trip assertion:

```python
booking = confirmed_booking()
booking = replace(
    booking,
    scheduled_end_at=booking.starts_at + timedelta(hours=1),
)
await repo.complete(scenario, booking, "booking_confirmed")
stored = await repo.get_booking_for_scenario(scenario.id)
assert stored.scheduled_end_at == booking.scheduled_end_at
```

- [x] **Step 6: Run migration/repository tests and observe RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/test_migrations.py -k lifecycle
docker compose --env-file ../.env run --rm test pytest -q tests/integration/booking/test_booking_repository.py -k scheduled_end
```

Expected: first command cannot reach revision `0008`; second command loses the
scheduled end.

- [x] **Step 7: Implement migration and persistence**

Create:

```python
"""Add durable YCLIENTS visit lifecycle state."""

from alembic import op
import sqlalchemy as sa

revision = "0008_yclients_lifecycle"
down_revision = "0007_scheduler_notifications"
branch_labels = None
depends_on = None

_OLD = "status IN ('confirmed', 'cancelled')"
_NEW = (
    "status IN ('confirmed', 'cancelled', 'completed', 'no_show', 'unknown')"
)


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", _NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE bookings SET status = 'confirmed' "
        "WHERE status IN ('completed', 'no_show', 'unknown')"
    )
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", _OLD)
    op.drop_column("bookings", "scheduled_end_at")
```

Add `scheduled_end_at` to booking INSERT/UPDATE, snapshots, and all repository
SELECT-to-`ExternalBooking` mappings.

- [x] **Step 8: Run focused Task 1 GREEN and commit**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/contract/booking/test_yclients_adapter.py tests/integration/test_migrations.py tests/integration/booking
```

Expected: all selected tests pass; Alembic reports
`0008_yclients_lifecycle (head)`.

Commit:

```powershell
git add project/migrations/versions/0008_yclients_lifecycle.py project/src/moroz/booking project/tests/contract/booking/test_yclients_adapter.py project/tests/integration/test_migrations.py project/tests/integration/booking
git commit -m "feat: добавлены lifecycle статусы YCLIENTS"
```

---

### Task 2: Durable Lifecycle Service and Bounded Follow-ups

**Files:**
- Create: `project/src/moroz/notifications/lifecycle.py`
- Create: `project/tests/unit/notifications/test_lifecycle.py`
- Create: `project/tests/integration/notifications/test_lifecycle.py`
- Modify: `project/src/moroz/notifications/repository.py`

**Interfaces:**
- Produces: `OUTCOME_OFFSETS = (timedelta(minutes=15), timedelta(hours=2), timedelta(hours=24))`.
- Produces: `LifecycleService.refresh(local: ExternalBooking) -> ExternalBooking | None`.
- Produces: `LifecycleService.schedule_next(booking: ExternalBooking, current_index: int) -> bool`.
- Produces: `LifecycleService.schedule_feedback(booking: ExternalBooking) -> UUID | None`.

- [x] **Step 1: Write failing service unit tests**

Use a fake provider and fake storage boundary to prove:

```python
async def test_refresh_uses_exact_owned_get_command():
    refreshed = await service.refresh(local_booking())
    assert provider.commands == [
        GetBooking(
            external_id="9001",
            customer_id="customer-7",
            booking_key=BOOKING_KEY,
        )
    ]
    assert refreshed.status == "completed"


async def test_terminal_local_status_skips_provider():
    refreshed = await service.refresh(local_booking(status="no_show"))
    assert refreshed.status == "no_show"
    assert provider.commands == []


async def test_schedule_next_uses_fixed_end_relative_offsets():
    assert await service.schedule_next(completed_booking(), current_index=-1)
    assert scheduled.run_at == END_AT + timedelta(minutes=15)
    assert scheduled.payload["outcome_check_index"] == 0
```

Also test `current_index` 0/1 schedules the next offset, index 2 returns `False`,
and missing `scheduled_end_at` raises a safe `RuntimeError`.

- [x] **Step 2: Run unit tests and observe RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/notifications/test_lifecycle.py
```

Expected: import failure because `moroz.notifications.lifecycle` does not exist.

- [x] **Step 3: Implement the minimal lifecycle service**

Create a focused class:

```python
OUTCOME_OFFSETS = (
    timedelta(minutes=15),
    timedelta(hours=2),
    timedelta(hours=24),
)


class LifecycleService:
    def __init__(self, database: Database, provider, feedback: FeedbackService):
        self._database = database
        self._provider = provider
        self._feedback = feedback

    async def refresh(self, local: ExternalBooking) -> ExternalBooking | None:
        if local.status in {"cancelled", "completed", "no_show", "unknown"}:
            return local
        provider_booking = await self._provider.get_booking(
            GetBooking(
                external_id=local.external_id,
                customer_id=local.customer_id,
                booking_key=local.booking_key,
            )
        )
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE bookings
                SET status = $3,
                    scheduled_end_at = $4,
                    snapshot = snapshot || jsonb_build_object(
                        'status', $3::text,
                        'scheduled_end_at', $4::timestamptz
                    ),
                    updated_at = now()
                WHERE booking_key = $1
                  AND starts_at = $2
                  AND status IN ('confirmed', 'unknown')
                RETURNING external_id, customer_id, booking_key, slot_id,
                          starts_at, status, scheduled_end_at
                """,
                local.booking_key,
                local.starts_at,
                provider_booking.status,
                provider_booking.scheduled_end_at,
            )
        return _booking_from_row(row) if row is not None else None
```

`schedule_next` inserts one `visit_outcome_check` with
`ON CONFLICT (idempotency_key) DO NOTHING`; return `True` when either inserted
or already present. Its key is:

```python
f"booking:{booking.booking_key}:{booking.starts_at.isoformat()}:outcome:{next_index}"
```

`schedule_feedback` requires `scheduled_end_at` and delegates to:

```python
await self._feedback.schedule_after_visit(
    customer_id=booking.customer_id,
    booking_key=booking.booking_key,
    completed_at=booking.scheduled_end_at,
)
```

- [x] **Step 4: Run unit tests GREEN**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/notifications/test_lifecycle.py
```

Expected: all lifecycle unit tests pass.

- [x] **Step 5: Write failing PostgreSQL concurrency/idempotency tests**

Cover real database behavior:

```python
async def test_refresh_does_not_overwrite_concurrent_cancel(...):
    provider.pause_after_get()
    task = asyncio.create_task(service.refresh(local))
    await provider.wait_until_read()
    await cancel_booking_in_database(database, BOOKING_KEY)
    provider.resume()
    assert await task is None
    assert await booking_status(database, BOOKING_KEY) == "cancelled"


async def test_duplicate_schedule_next_creates_one_job(...):
    await asyncio.gather(
        service.schedule_next(booking, -1),
        service.schedule_next(booking, -1),
    )
    assert await outcome_job_count(database, BOOKING_KEY, 0) == 1
```

Also prove a changed `starts_at` rejects a stale provider response and a
duplicate completed refresh can still schedule feedback from persisted
`scheduled_end_at`.

- [x] **Step 6: Run integration tests RED, implement repository helpers, rerun GREEN**

Run RED and GREEN with:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/notifications/test_lifecycle.py
```

Expected RED: missing persistence/scheduling behavior. Expected GREEN: all
tests pass with one job per key and terminal local states preserved.

- [x] **Step 7: Commit Task 2**

```powershell
git add project/src/moroz/notifications/lifecycle.py project/src/moroz/notifications/repository.py project/tests/unit/notifications/test_lifecycle.py project/tests/integration/notifications/test_lifecycle.py
git commit -m "feat: добавлен durable lifecycle ingestion"
```

---

### Task 3: Scheduler Handler and Worker Runtime Wiring

**Files:**
- Modify: `project/src/moroz/notifications/handlers.py`
- Modify: `project/src/moroz/notifications/ports.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/e2e/notifications/test_reminders.py`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `project/tests/unit/test_migration_profile.py`

**Interfaces:**
- `handle_scheduler_job(..., lifecycle=None)` keeps old reminder behavior.
- Lifecycle kinds are `no_show_check` and `visit_outcome_check`.
- Worker constructs one real `YclientsAdapter` and one `LifecycleService`.

- [ ] **Step 1: Write failing handler tests**

Add outcome scenarios:

```python
async def test_completed_visit_schedules_feedback_without_reminder():
    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", local_booking(), index=0),
        booking_port=local_port,
        outbox=outbox,
        lifecycle=lifecycle_returning(status="completed"),
    )
    assert result == JobResult.sent()
    assert lifecycle.feedback_calls == [BOOKING_KEY]
    assert outbox.calls == []


async def test_confirmed_visit_schedules_next_bounded_check():
    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", local_booking(), index=0),
        booking_port=local_port,
        outbox=outbox,
        lifecycle=lifecycle_returning(status="confirmed", schedule_next=True),
    )
    assert result == JobResult.skipped("outcome_pending")
    assert lifecycle.next_calls == [(BOOKING_KEY, 0)]


async def test_final_confirmed_visit_alerts_staff_once():
    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", local_booking(), index=2),
        booking_port=local_port,
        outbox=outbox,
        lifecycle=lifecycle_returning(status="confirmed", schedule_next=False),
    )
    assert result == JobResult.skipped("outcome_unresolved")
    assert outbox.calls == [("staff_status_unknown", "outcome_unresolved")]
```

Retain existing no-show, unknown, cancelled, and reminder tests.

- [ ] **Step 2: Run handler tests and observe RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/notifications/test_reminders.py
```

Expected: `visit_outcome_check` is unsupported and lifecycle dependency is not
accepted.

- [ ] **Step 3: Implement minimal lifecycle routing**

Preserve the local stale check first, then refresh only lifecycle kinds:

```python
LIFECYCLE_KINDS = {"no_show_check", "visit_outcome_check"}


async def handle_scheduler_job(
    job: SchedulerJob,
    *,
    booking_port,
    outbox,
    lifecycle=None,
) -> JobResult:
    # Existing feedback_request branch remains first.
    booking = await booking_port.get_booking(job.booking_key)
    if booking is None or booking.starts_at != job.booking_starts_at:
        return JobResult.skipped("stale")
    if booking.status == "cancelled":
        return JobResult.skipped("stale")
    if job.kind in LIFECYCLE_KINDS:
        if lifecycle is None:
            raise RuntimeError("lifecycle service is not configured")
        booking = await lifecycle.refresh(booking)
        if booking is None:
            return JobResult.skipped("stale")
        return await _handle_lifecycle(job, booking, outbox, lifecycle)
    # Existing reminder branch remains unchanged.
```

`_handle_lifecycle`:

- sends existing no-show messages for `no_show`;
- calls `schedule_feedback` for `completed`;
- alerts staff for `unknown`;
- calls `schedule_next` for `confirmed`;
- emits unresolved staff alert only when no next check exists;
- skips `cancelled` as stale.

Validate `outcome_check_index` with `type(value) is int` and range `0..2`;
malformed payload returns `JobResult.skipped("invalid_outcome_payload")`.

- [ ] **Step 4: Run handler tests GREEN**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/notifications/test_reminders.py tests/e2e/notifications/test_feedback.py
```

Expected: all selected notification E2E tests pass.

- [ ] **Step 5: Write failing worker wiring tests**

Assert scheduler handler receives the lifecycle dependency:

```python
assert scheduler_handler.await_args.kwargs["lifecycle"] is lifecycle
```

Assert Compose continues to expose only the already allowlisted YCLIENTS
variables to worker and adds no new service. Assert test/migrate/cutover profiles
still receive no YCLIENTS tokens.

- [ ] **Step 6: Run worker/Compose tests and observe RED**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/test_worker.py tests/unit/test_migration_profile.py
```

Expected: worker does not construct or pass a lifecycle service.

- [ ] **Step 7: Wire the existing adapter and lifecycle service**

Construct only inside worker startup when all required YCLIENTS values are
present:

```python
def _build_lifecycle_service(database: Database):
    required = (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    )
    present = tuple(bool(os.environ.get(name, "").strip()) for name in required)
    if not any(present):
        return None
    if not all(present):
        raise ValueError("YCLIENTS lifecycle configuration is incomplete")
    config = YclientsConfig.from_env(os.environ)
    return LifecycleService(
        database,
        YclientsAdapter(config),
        FeedbackService(database),
    )
```

Pass the optional service through `MessageTaskHandler` to
`handle_scheduler_job`. A completely empty provider configuration preserves the
current local worker startup; partial configuration fails at startup. A
lifecycle job with no service raises the existing safe runtime error and uses
Rabbit retry/DLQ. `YclientsHttpClient` uses bounded `urllib` calls and owns no
persistent session, so no new shutdown resource is added.

- [ ] **Step 8: Run Task 3 GREEN and commit**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/notifications tests/unit/test_worker.py tests/unit/test_migration_profile.py
```

Expected: all selected tests pass; no Compose service count change.

Commit:

```powershell
git add project/src/moroz/notifications project/worker/main.py project/tests/e2e/notifications project/tests/unit/test_worker.py project/tests/unit/test_migration_profile.py
git commit -m "feat: подключён lifecycle scheduler runtime"
```

---

### Task 4: Regression, Review, and Local Completion Gate

**Files:**
- Modify: `docs/superpowers/plans/2026-07-27-yclients-lifecycle.md`
- Modify: `Дорожная карта.md`
- Modify: `План реализации.md`
- Modify: `changelog.md`
- Modify only if review finds a tested defect: lifecycle source/test files from Tasks 1-3.

**Interfaces:**
- Produces a locally merge-ready `codex/yclients-lifecycle-0008` branch.
- Does not push or change any external environment.

- [ ] **Step 1: Run focused lifecycle gate**

```powershell
$env:COMPOSE_PROJECT_NAME='moroz_lifecycle_0008'
docker compose --env-file ../.env run --rm --build test pytest -q tests/contract/booking/test_yclients_adapter.py tests/integration/test_migrations.py tests/integration/booking tests/unit/notifications tests/integration/notifications tests/e2e/notifications tests/unit/test_worker.py
```

Expected: all selected tests pass with zero failed tests.

- [ ] **Step 2: Run migration and image gates**

```powershell
docker compose --env-file ../.env up -d postgres
docker compose --env-file ../.env run --rm migrate
docker compose --env-file ../.env run --rm migrate alembic -c /app/alembic.ini current
docker compose --env-file ../.env build worker scheduler
docker compose --env-file ../.env run --rm --no-deps worker python -m compileall -q /app
docker compose --env-file ../.env run --rm --no-deps scheduler python -m compileall -q /app
```

Expected: current revision is `0008_yclients_lifecycle (head)`; build and
compile commands exit `0`.

- [ ] **Step 3: Run complete Docker pytest suite**

```powershell
docker compose --env-file ../.env run --rm test pytest -q
```

Expected: all tests pass, no unexpected skips, and no network call reaches
YCLIENTS because the test profile has no provider credentials.

- [ ] **Step 4: Request independent code review and fix through TDD**

Review the complete diff from design checkpoint `e9841da` for:

- status transition regressions;
- stale reschedule/cancel races;
- crash windows between status, follow-up, and feedback writes;
- duplicate scheduler/Rabbit delivery;
- unbounded provider calls;
- provider mutation methods;
- token/PII logging;
- migration upgrade/downgrade correctness;
- missing worker cleanup.

For every valid finding, first add a focused failing Docker test, observe RED,
then implement the minimal fix and rerun the affected and full suites.

- [ ] **Step 5: Update project records**

Mark the lifecycle roadmap item complete only after all gates pass. Update the
Phase 6 status in `План реализации.md` with:

- migration head `0008_yclients_lifecycle`;
- focused/full test counts;
- review result;
- exact cleanup result;
- explicit statement that staging/production/provider mutations were not run.

Append each action and any encountered failure to `changelog.md` using Moscow
time.

- [ ] **Step 6: Cleanup exact Docker namespace**

```powershell
docker compose --env-file ../.env down --volumes --remove-orphans --rmi local
docker ps -a --filter "label=com.docker.compose.project=moroz_lifecycle_0008" -q
docker volume ls --filter "label=com.docker.compose.project=moroz_lifecycle_0008" -q
docker network ls --filter "label=com.docker.compose.project=moroz_lifecycle_0008" -q
docker images --filter "label=com.docker.compose.project=moroz_lifecycle_0008" -q
```

Expected leftovers: `0/0/0/0`.

- [ ] **Step 7: Commit completion checkpoint**

```powershell
git add -A
git commit -m "docs: завершён lifecycle checkpoint 0008"
git status --short --branch
```

Expected: clean worktree on `codex/yclients-lifecycle-0008`, locally ahead of
`main`; no push.
