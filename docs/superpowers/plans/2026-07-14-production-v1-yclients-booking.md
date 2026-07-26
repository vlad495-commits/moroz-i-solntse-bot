# YCLIENTS Booking Local Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать локально проверяемые state machines создания, переноса и отмены записи через `BookingPort` и mock adapter, с durable PostgreSQL-checkpoints и fail-closed обработкой неопределённого результата.

**Architecture:** Домен не знает HTTP; `BookingPort` скрывает провайдера. PostgreSQL хранит checkpoint до каждого изменяющего действия, локальный snapshot результата и append-only booking events. Real YCLIENTS adapter откладывается до официально подтверждённого контракта и sandbox evidence.

**Tech Stack:** Python 3.12 dataclasses/Protocol, asyncpg, Alembic, PostgreSQL 16, pytest/pytest-asyncio, Docker Compose.

## Global Constraints

- YCLIENTS остаётся источником правды, но локальная часть использует только mock; live gate остаётся открытым.
- Не создавать `project/src/moroz/booking/yclients.py` и не добавлять HTTP/auth/provider-specific mapping без подтверждённого контракта.
- Не запрашивать YCLIENTS credentials, пока Tasks 1–5 не исчерпаны локально.
- Не обещать слот до успешного внешнего результата.
- Любой mutating call требует явного подтверждения и повторной проверки слота.
- Отмена и перенос менее чем за 3 часа всегда эскалируются без внешнего изменения.
- Локальный `idempotency_key` не выдаётся за provider-side exactly-once.
- Scenario в `executing` после восстановления не повторяет mutating call и получает `booking_outcome_unknown`.
- Миграция `0005_booking_state` имеет `down_revision = "0004_pipeline_order_claim"` и добавляет только новые таблицы/индексы; live rollback не выполняет DB downgrade.
- Durable эскалация — `booking_events.event_type = 'admin_attention_required'`; общий admin-task framework не добавлять.
- Не добавлять новые runtime dependencies.
- Docker-only; каждый test run использует отдельный Compose namespace и одноразовые PostgreSQL/Redis/RabbitMQ credentials.
- Временные файлы — только корневой `tmp/`; секреты и ПД не выводить.
- Каждый task: RED → GREEN → Docker-check → changelog/roadmap → отдельный commit → task review.

---

### Task 1: Booking domain и mock adapter

**Files:**
- Create: `project/src/moroz/booking/models.py`
- Create: `project/src/moroz/booking/ports.py`
- Create: `project/src/moroz/booking/mock_yclients.py`
- Create: `project/tests/unit/booking/test_mock_adapter.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Produces immutable `SlotQuery`, `Slot`, `CreateBooking`, `RescheduleBooking`, `CancelBooking`, `ExternalBooking`, `BookingIdentity`, `BookingScenario`, `BookingEvent`.
- Produces `BookingPort` with `list_slots`, `create_booking`, `reschedule_booking`, `cancel_booking`, `get_booking`.
- Produces errors `SlotUnavailable`, `BookingNotFound`, `BookingTemporaryError`, `BookingOutcomeUnknown`.
- Produces `MockYclientsAdapter(BookingPort)`.

- [ ] **Step 1: Write the failing unit tests**

Create tests that use timezone-aware datetimes and prove:

```python
async def test_list_slots_returns_only_matching_future_slots():
    query = SlotQuery(
        service_ids=("service-1",),
        starts_after=datetime(2026, 7, 22, 9, tzinfo=UTC),
        starts_before=datetime(2026, 7, 23, tzinfo=UTC),
        staff_id="staff-1",
    )
    assert [slot.id for slot in await adapter.list_slots(query)] == ["slot-ok"]


async def test_create_is_idempotent_for_same_key():
    first = await adapter.create_booking(command)
    repeated = await adapter.create_booking(command)
    assert repeated == first
    assert await adapter.get_booking(first.external_id) == first
```

Also cover: occupied slot excluded, different create key on occupied slot raises `SlotUnavailable`, reschedule checks availability and is idempotent, cancel with the same key is a safe repeat, unknown external ID raises `BookingNotFound`.

- [ ] **Step 2: Run RED in isolated Docker Compose**

Run the exact target in a task-specific namespace with generated process-environment credentials:

```powershell
docker compose --env-file ../.env -p codex-yclients-task1 --profile test run --rm test pytest tests/unit/booking/test_mock_adapter.py -q
```

Expected: collection/import failure because `moroz.booking` does not exist. Record RED evidence in `changelog.md`.

- [ ] **Step 3: Implement the minimum domain and adapter**

Use these exact protocol signatures:

```python
class BookingPort(Protocol):
    async def list_slots(self, query: SlotQuery) -> list[Slot]: ...
    async def create_booking(self, command: CreateBooking) -> ExternalBooking: ...
    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking: ...
    async def cancel_booking(self, command: CancelBooking) -> None: ...
    async def get_booking(self, external_id: str) -> ExternalBooking: ...
```

`SlotQuery` filters by inclusive lower bound, exclusive optional upper bound, requested service subset and optional staff. Reject naive datetimes in dataclass validation. Mock-generated external IDs use `uuid4()`; no provider fields or HTTP behavior.

- [ ] **Step 4: Run GREEN and the unit regression set**

Run the focused file, then `pytest tests/unit -q` in the same disposable namespace. Expected: both exit 0 with no warnings/errors. Remove only `codex-yclients-task1` containers/volumes/network.

- [ ] **Step 5: Update docs and commit**

Mark Task 1 in roadmap, append RED/GREEN evidence to changelog, run `git diff --check`, then commit:

```text
feat: добавлен контракт и mock YCLIENTS
```

---

### Task 2: Expand migration и durable repository

**Files:**
- Create: `project/migrations/versions/0005_booking_state.py`
- Create: `project/src/moroz/booking/repository.py`
- Create: `project/tests/integration/booking/test_repository.py`
- Modify: `project/tests/integration/test_migrations.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes all Task 1 domain types.
- Produces:

```python
async def create_scenario(self, scenario: BookingScenario) -> UUID: ...
async def get_scenario(self, scenario_id: UUID) -> BookingScenario | None: ...
async def checkpoint(
    self,
    scenario: BookingScenario,
    event_type: str,
    payload: Mapping[str, object] | None = None,
) -> None: ...
async def confirm(self, scenario: BookingScenario, booking: ExternalBooking) -> None: ...
async def escalate(
    self,
    scenario: BookingScenario,
    error_code: str,
    payload: Mapping[str, object] | None = None,
) -> None: ...
async def complete_cancellation(
    self,
    scenario: BookingScenario,
    booking: ExternalBooking,
) -> None: ...
async def get_local_booking(self, scenario_id: UUID) -> ExternalBooking | None: ...
async def list_events(self, scenario_id: UUID) -> list[BookingEvent]: ...
```

- Every transition and its event insert is one PostgreSQL transaction.

- [ ] **Step 1: Write failing migration and repository tests**

Migration assertions:

```python
assert current_revision == "0005_booking_state"
assert {"booking_scenarios", "bookings", "booking_events"}.issubset(tables)
assert previous_tables_and_columns == catalog_after_downgrade_to_0004
```

Repository assertions:

```python
scenario_id = await repo.create_scenario(scenario)
assert await repo.create_scenario(scenario) == scenario_id
await repo.checkpoint(executing, "booking_execution_started")
assert (await repo.get_scenario(scenario.id)).phase == "executing"
assert [event.event_type for event in await repo.list_events(scenario.id)] == [
    "booking_scenario_created",
    "booking_execution_started",
]
```

Also prove unique local idempotency key, atomic `confirm` writes a local booking plus terminal event, and `escalate` appends `admin_attention_required` with an error code.

- [ ] **Step 2: Run RED**

Run focused migration/repository tests in namespace `codex-yclients-task2`. Expected: missing revision/repository failures. Record exact failure reason without DSN or credentials.

- [ ] **Step 3: Add the additive schema**

Create only:

```text
booking_scenarios: id UUID PK, kind, phase, idempotency_key UNIQUE,
                   customer_id, state JSONB, error_code, created_at, updated_at
bookings:          id UUID PK, last_scenario_id FK, external_id UNIQUE,
                   customer_id, slot_id, starts_at, status, snapshot JSONB,
                   created_at, updated_at
booking_events:    id UUID PK, scenario_id FK, event_type, payload JSONB,
                   created_at
```

Allowed scenario phases are `collecting`, `awaiting_confirmation`, `executing`, `confirmed`, `failed`, `escalated`; local booking statuses are `confirmed`, `cancelled`. Use check constraints and indexes on `booking_events(scenario_id, created_at)` and `bookings(customer_id, starts_at)`. One `bookings` row represents the latest snapshot of one external ID; `confirm`/`complete_cancellation` upsert it and set `last_scenario_id`. The terminal scenario state stores `external_id`, so `get_local_booking(scenario_id)` can resolve the snapshot even after a later scenario updates `last_scenario_id`. Downgrade may remove only these three newly added tables for disposable migration testing; staging rollback never invokes it.

- [ ] **Step 4: Implement repository transactions**

Use the existing `Database.acquire()` pattern and `SELECT ... FOR UPDATE` for state transitions. JSON serialization must thaw Task 1 `MappingProxyType`/tuple values to ordinary JSON objects/arrays, then restore immutable mappings/tuples through the dataclass constructors on read. `create_scenario` uses `ON CONFLICT (idempotency_key)` and returns the existing ID without overwriting state.

- [ ] **Step 5: Run GREEN and migration regression tests**

Run focused files, then all `tests/integration/test_migrations.py` and `tests/integration/booking/test_repository.py` in Docker. Expected: exit 0. Clean the task namespace.

- [ ] **Step 6: Update docs and commit**

Append RED/GREEN and schema evidence, mark Task 2, run `git diff --check`, commit:

```text
feat: добавлены durable checkpoints записи
```

---

### Task 3: Create-booking state machine

**Files:**
- Create: `project/src/moroz/booking/service.py`
- Create: `project/tests/e2e/booking/test_create_booking.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes `BookingPort`, `BookingRepository`, Task 1 models.
- Produces `BookingService.handle(scenario_id: UUID, *, confirmed: bool, identity: BookingIdentity | None = None) -> ScenarioResult`.
- Uses `ScenarioResult` from `moroz.messaging.models`.
- Construct with `BookingService(port, repository, now=...)`; `now` defaults to a timezone-aware UTC clock and is reused by Task 4.

The durable create state uses only JSON-safe provider-neutral fields:

```python
{
    "slot_query": {
        "service_ids": ["service-1"],
        "starts_after": "2026-07-25T00:00:00+00:00",
        "starts_before": "2026-07-26T00:00:00+00:00",  # optional
        "staff_id": "staff-1",  # optional
    },
    "selected_slot_id": "slot-9",
    # any already-collected customer/service fields remain untouched
}
```

`needs_input` before confirmation uses `next_action="confirm_booking"`. A lost slot uses `next_action="choose_slot"` and both the returned event and durable `slot_unavailable` event carry the same JSON-safe `alternatives` list (maximum three). Terminal success is reconstructed from the stored booking with a stable `ScenarioResult(status="ok", next_action=None, events=())`, so a repeat is value-equal without another port call.

- [ ] **Step 1: Write failing create-flow E2E tests**

Cover these observable behaviors against real disposable PostgreSQL and `MockYclientsAdapter`:

```python
result = await service.handle(scenario.id, confirmed=False)
assert result.status == "needs_input"
assert await repo.get_local_booking(scenario.id) is None

confirmed = await service.handle(scenario.id, confirmed=True)
repeated = await service.handle(scenario.id, confirmed=True)
assert confirmed == repeated
assert (await repo.get_scenario(scenario.id)).phase == "confirmed"
assert await repo.get_local_booking(scenario.id) is not None
```

Add a counting test double around the real mock adapter to prove the repeated terminal call performs exactly one external create. Add a lost-slot case that returns `needs_input`, `next_action == "choose_slot"`, at most three matching alternatives in events, preserves collected customer/service data and stores phase `collecting`.

- [ ] **Step 2: Run RED**

Run `tests/e2e/booking/test_create_booking.py` in namespace `codex-yclients-task3`. Expected: missing `BookingService`.

- [ ] **Step 3: Implement the create transitions**

Exact transition order:

```text
awaiting_confirmation + confirmed=False -> needs_input, no port mutation
awaiting_confirmation + confirmed=True  -> checkpoint executing
executing -> re-list matching slots
slot missing -> checkpoint collecting + slot_unavailable event + <=3 alternatives
slot present -> create_booking -> repository.confirm -> ok
confirmed repeat -> return stored ok result, no port mutation
```

If a scenario is already `executing` when loaded at method entry, call `repository.escalate(..., "booking_outcome_unknown")`; do not call the port.

- [ ] **Step 4: Run GREEN and booking regression tests**

Run the focused E2E file and all existing booking tests in Docker. Expected: exit 0. Clean task namespace.

- [ ] **Step 5: Update docs and commit**

Append transition evidence, mark Task 3, run `git diff --check`, commit:

```text
feat: реализована локальная state machine записи
```

---

### Task 4: Reschedule, cancel, ownership и fail-closed errors

**Files:**
- Modify: `project/src/moroz/booking/service.py`
- Create: `project/tests/e2e/booking/test_change_booking.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Extends Task 3 `BookingService.handle` without a second service/factory.
- Uses injectable `now: Callable[[], datetime]` with a timezone-aware default.
- Durable error codes: `booking_identity_unconfirmed`, `late_booking_change`, `booking_temporarily_unavailable`, `booking_outcome_unknown`.

The provider-neutral change states are JSON-safe:

```python
# reschedule
{
    "external_id": "booking-42",
    "starts_at": "2026-07-25T14:00:00+00:00",
    "slot_query": {...},
    "selected_slot_id": "slot-new",
}

# cancel
{
    "external_id": "booking-42",
    "starts_at": "2026-07-25T14:00:00+00:00",
}
```

On reschedule success, preserve the old value as `previous_starts_at` and replace `starts_at` with the new snapshot time before `repository.confirm`; the stable terminal message must contain both values. Cancellation uses the existing local snapshot, calls `cancel_booking` once, changes that snapshot status to `cancelled`, and persists it with `complete_cancellation` without a second provider read. All three kinds use terminal scenario phase `confirmed`; the booking snapshot distinguishes `confirmed` from `cancelled`. A repeated `escalated` scenario reconstructs its stored error result without any port call.

- [ ] **Step 1: Write failing change-flow E2E tests**

Cover:

```python
wrong_identity = BookingIdentity(customer_id="other", confirmed=True)
result = await service.handle(scenario.id, confirmed=True, identity=wrong_identity)
assert (result.status, result.error_code) == (
    "escalated",
    "booking_identity_unconfirmed",
)
```

Also prove: reschedule success returns an explicit old/new summary and persists the new snapshot; cancellation at exactly 3 hours is allowed; any change under 3 hours escalates before the port; temporary port failure preserves no promised slot and creates `admin_attention_required`; outcome-unknown does the same and is never retried; repeated terminal reschedule/cancel does not repeat the mutation.

- [ ] **Step 2: Run RED**

Run `tests/e2e/booking/test_change_booking.py` in namespace `codex-yclients-task4`. Expected: missing branches or failing assertions.

- [ ] **Step 3: Implement the minimum branches**

Before reschedule/cancel:

```python
if identity is None or not identity.confirmed or identity.customer_id != scenario.customer_id:
    return await self._escalate(scenario, "booking_identity_unconfirmed")
if datetime.fromisoformat(str(scenario.state["starts_at"])) - self._now() < timedelta(hours=3):
    return await self._escalate(scenario, "late_booking_change")
```

Reschedule repeats slot availability after the `executing` checkpoint and handles a mutation-time `SlotUnavailable` through the same fresh-list lost-slot transition as create. Cancellation performs no slot query. Map `BookingTemporaryError` to `booking_temporarily_unavailable` and `BookingOutcomeUnknown` to `booking_outcome_unknown`; both use repository escalation and never claim success. The existing PostgreSQL serialized scenario session covers each full change flow, so concurrent repeats wait and reconstruct the terminal result without another mutation.

- [ ] **Step 4: Run GREEN and all booking tests**

Run unit, integration and E2E booking paths in Docker. Expected: exit 0 with no warnings. Clean task namespace.

- [ ] **Step 5: Update docs and commit**

Append failure/recovery evidence, mark Task 4, run `git diff --check`, commit:

```text
feat: добавлены перенос отмена и fallback записи
```

---

### Task 5: Local booking checkpoint

**Files:**
- Rename: `project/tests/integration/booking/test_repository.py` → `project/tests/integration/booking/test_booking_repository.py`
- Modify: `План реализации.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- No new runtime code.
- Records local/fake completion separately from real YCLIENTS/live gate.

- [ ] **Step 1: Resolve the pytest collection blocker and run the complete isolated Docker verification**

The first canonical `pytest -q` run is the RED evidence: default prepend import mode collides on the two tracked `test_repository.py` modules in `tests/integration/booking` and `tests/integration/messaging`. `--import-mode=importlib` is not retained because the established E2E plugin imports then double-register `tests/integration/conftest.py`. Keep default pytest behavior and rename only the new booking test to the unique basename `test_booking_repository.py`, without changing its content. Then rerun the canonical suite without flags or config overrides.

In a new namespace with new process-environment credentials run:

```powershell
docker compose --env-file ../.env --profile test build test
docker compose --env-file ../.env --profile test run --rm test pytest -q
docker compose --env-file ../.env config --quiet
```

Also run `alembic current` through the migration image and verify `0005_booking_state (head)`. Capture counts only; never output credentials/DSN.

- [ ] **Step 2: Run the mock flow twice**

Use the E2E test that handles the same confirmed create scenario twice and prove one external booking snapshot and one local booking row.

- [ ] **Step 3: Run static gates**

Run `python -m compileall` inside the test image, `git diff --check`, and scan tracked changes for secret-shaped values and accidental real adapter files. Expected: no findings.

- [ ] **Step 4: Update phase evidence**

Tasks 1–5 local/fake и real-adapter local/fake gate завершены. Открытым остаётся внешний gate:

```text
Real YCLIENTS adapter local/fake: complete; protected CRUD smoke tooling ready.
YCLIENTS live/sandbox evidence: not run; phase is not live-complete.
```

Keep Staging rollback open until the first distinct candidate deploy completes `candidate → previous → candidate` with no DB downgrade.

- [ ] **Step 5: Commit checkpoint**

Run fresh verification for documentation changes and commit:

```text
docs: зафиксирован локальный YCLIENTS checkpoint
```

---

### Whole-phase review fix loop: external booking aggregate

**Files:**
- Modify: `project/src/moroz/booking/models.py`
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/src/moroz/booking/service.py`
- Modify: `project/tests/unit/booking/test_mock_adapter.py`
- Modify: `project/tests/e2e/booking/test_create_booking.py`
- Modify: `project/tests/e2e/booking/test_change_booking.py`
- Optional minimal fixture consolidation: `project/tests/e2e/booking/conftest.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Required corrections:**

1. Normalize `SlotQuery.service_ids` and `Slot.service_ids` to tuple in `__post_init__`; frozen models must not retain a caller-owned mutable list.
2. Keep the scenario advisory lock and add a nested namespaced session advisory lock for every change-flow `external_id`. On the same held connection, reread the current local booking before any change port call.
3. A change is authorized only when identity matches both `scenario.customer_id` and the current snapshot owner. The current snapshot must be `confirmed`, and its `starts_at` must equal the collected scenario start. The three-hour rule uses the current snapshot time. Owner mismatch returns `booking_identity_unconfirmed`; stale status/time uses the existing `booking_temporarily_unavailable`; neither path calls the port.
4. Identity protects every non-create phase. Invalid identity on an already `confirmed`, `escalated`, or recovered `executing` scenario returns a generic non-leaking result without port or database mutation. Awaiting change keeps the durable identity escalation.
5. At create success, persist original `starts_at` and status in terminal scenario state. Every create terminal repeat is reconstructed from that state, not the mutable latest `bookings` row, so a later reschedule/cancel cannot change it or produce false success.
6. Add RED→GREEN tests for a forged-owner scenario, two independent repository instances running distinct reschedule/cancel scenarios for one external ID, stale status/start, protected escalated/executing identity paths, and create → reschedule/cancel → repeat of the original create.
7. Consolidate duplicated E2E disposable-PostgreSQL setup only if the existing integration fixture can be reused without changing pytest import semantics; do not add a new framework or global pytest config.
8. Catch change-flow `BookingTemporaryError` and `BookingOutcomeUnknown` and commit the durable escalation while the external-ID advisory lock is still held. Before any awaiting change mutation, query on that same connection for every sibling scenario of the aggregate in `phase='executing'` or with `phase='escalated'` and `error_code='booking_outcome_unknown'`; if one exists, durably escalate the current scenario with `booking_outcome_unknown` and do not call the port. This fail-closed block remains until future reconciliation; reconciliation/admin tooling is outside this fix loop.

Run focused RED, all booking/migration tests, then the canonical Docker suite. Repeat independent review until `0 Critical / 0 Important / 0 Minor`.
