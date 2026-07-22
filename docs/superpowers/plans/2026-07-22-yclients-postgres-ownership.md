# YCLIENTS PostgreSQL Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PostgreSQL the sole customer-ownership authority and use the YCLIENTS additional field `moroz_booking_key` only as an opaque, restart-safe correlation key.

**Architecture:** The UUID of the original create scenario is persisted as immutable `bookings.booking_key` and sent in `custom_fields.moroz_booking_key`. Every protected read/change receives trusted `customer_id` and `booking_key` from the locked local booking; provider customer identity is never trusted. Mutation uncertainty remains fail-closed and is never retried blindly.

**Tech Stack:** Python 3.12, dataclasses, asyncpg, Alembic/SQLAlchemy, stdlib `urllib`, PostgreSQL 16, pytest/pytest-asyncio, Docker Compose.

## Global Constraints

- Docker-only; never run project Python or pytest directly on the host.
- Before each task's first Docker command, create an empty ignored `../tmp/compose-empty.env` with `apply_patch`; use a unique task-specific Compose project name and process-only generated PostgreSQL, Redis and RabbitMQ credentials/URLs. Never print their values, never pass the project `.env` to local tests, and remove the empty file after each task.
- Temporary artifacts belong only in root `tmp/` and must be removed after use.
- Do not read or print `.env`, Authorization headers, tokens, DSNs, personal data, provider IDs or secret-shaped values.
- Do not retry POST, PUT or DELETE; an indeterminate mutation result becomes `BookingOutcomeUnknown` and a durable escalation.
- Keep GET one-shot as in the current adapter; do not add retry behavior in this phase.
- Preserve every provider `custom_fields` member not owned by this integration.
- No schema downgrade in verification and no changes to shared/prototype/production/staging containers.
- No LLM guardrails, scheduler/notifications, production-admin or final production release work.
- Log each action/error in `changelog.md`, update `Дорожная карта.md` and `План реализации.md`, and commit each completed task.

## File Map

- `project/migrations/versions/0006_yclients_booking_key.py`: additive booking-key schema, deterministic backfill, NOT NULL and UNIQUE.
- `project/src/moroz/booking/models.py`: trusted-context commands and `ExternalBooking.booking_key`.
- `project/src/moroz/booking/ports.py`: command-based protected GET contract.
- `project/src/moroz/booking/repository.py`: immutable ownership persistence and conflict rejection.
- `project/src/moroz/booking/service.py`: passes scenario/local trusted identity to the port.
- `project/src/moroz/booking/mock_yclients.py`: contract-compatible deterministic fake.
- `project/src/moroz/booking/yclients.py`: exact additional-field serialization/validation and mutation preflight checks.
- `project/src/moroz/booking/yclients_sandbox_smoke.py`: one-key smoke flow and exact-key duplicate evidence.
- Existing migration, integration, contract, unit and e2e booking tests: RED/GREEN evidence without creating parallel test frameworks.

---

### Task 1: Add the booking-key database schema

**Files:**
- Create: `project/migrations/versions/0006_yclients_booking_key.py`
- Modify: `project/tests/integration/test_migrations.py`

**Interfaces:**
- Produces: database column `bookings.booking_key UUID NOT NULL UNIQUE`.
- Produces: deterministic backfill `booking_key = bookings.id` for every row present at revision `0005_booking_state`.

- [ ] **Step 1: Write failing migration tests**

Add assertions equivalent to:

```python
run_alembic(disposable_database_url, "upgrade", "0005_booking_state")
connection = await asyncpg.connect(disposable_database_url)
legacy_booking_id = uuid4()
scenario_id = uuid4()
await connection.execute(
    "INSERT INTO booking_scenarios (id, kind, phase, idempotency_key, customer_id, state) VALUES ($1, 'create', 'confirmed', $2, 'owner-a', '{}'::jsonb)",
    scenario_id,
    f"legacy-{scenario_id}",
)
await connection.execute(
    "INSERT INTO bookings (id, last_scenario_id, external_id, customer_id, slot_id, starts_at, status, snapshot) VALUES ($1, $2, 'legacy-external', 'owner-a', 'legacy-slot', now(), 'confirmed', '{}'::jsonb)",
    legacy_booking_id,
    scenario_id,
)
await connection.close()
run_alembic(disposable_database_url, "upgrade", "head")
connection = await asyncpg.connect(disposable_database_url)
columns = await connection.fetch("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'bookings'")
assert {row["column_name"] for row in columns} >= {"booking_key"}
assert await connection.fetchval("SELECT count(*) FROM bookings WHERE booking_key IS NULL") == 0
assert await connection.fetchval("SELECT booking_key FROM bookings WHERE id = $1", legacy_booking_id) == legacy_booking_id
```

Also assert the column is `NOT NULL`, the named unique constraint exists, and a duplicate key is rejected by PostgreSQL.

- [ ] **Step 2: Run RED in an isolated Docker namespace**

Create the ignored empty file `../tmp/compose-empty.env` with `apply_patch`, export newly generated required credentials/URLs into the current PowerShell process without echoing them, then run from `project/` with project name `moroz-ownership-task1-red`:

```powershell
docker compose --env-file ../tmp/compose-empty.env -p moroz-ownership-task1-red --profile test run --rm test pytest tests/integration/test_migrations.py -q
```

Expected: FAIL because revision `0006_yclients_booking_key` and its schema do not exist. Tear down only this namespace with `down -v --remove-orphans --rmi local` and verify its containers/volumes/networks/images are zero.

- [ ] **Step 3: Implement the minimal additive migration**

Use this migration sequence:

```python
revision = "0006_yclients_booking_key"
down_revision = "0005_booking_state"

def upgrade() -> None:
    op.add_column("bookings", sa.Column("booking_key", postgresql.UUID(as_uuid=True)))
    op.execute("UPDATE bookings SET booking_key = id WHERE booking_key IS NULL")
    op.alter_column("bookings", "booking_key", nullable=False)
    op.create_unique_constraint("uq_bookings_booking_key", "bookings", ["booking_key"])
```

- [ ] **Step 4: Run GREEN and migration idempotence checks**

Run the same focused files in namespace `moroz-ownership-task1-green`; expect all pass. Apply Alembic upgrade twice against the same disposable PostgreSQL service; expect both invocations to succeed with current head and all legacy rows backfilled. Clean and verify exact zero resources.

- [ ] **Step 5: Update logs/docs and commit**

Record RED cause, GREEN counts and namespace cleanup in `changelog.md`; update the YCLIENTS subsection in `Дорожная карта.md` and `План реализации.md`. Remove `../tmp/compose-empty.env`. Commit exact task files:

```bash
git commit -m "feat: добавлена схема booking key в PostgreSQL"
```

---

### Task 2: Carry trusted ownership through domain, service and fake port

**Files:**
- Modify: `project/src/moroz/booking/models.py`
- Modify: `project/src/moroz/booking/ports.py`
- Modify: `project/src/moroz/booking/service.py`
- Modify: `project/src/moroz/booking/mock_yclients.py`
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/tests/unit/booking/test_mock_adapter.py`
- Modify: `project/tests/e2e/booking/test_create_booking.py`
- Modify: `project/tests/e2e/booking/test_change_booking.py`
- Modify: `project/tests/e2e/booking/conftest.py`
- Modify: `project/tests/integration/booking/test_booking_repository.py`

**Interfaces:**
- Produces: `CreateBooking(..., booking_key: UUID)`.
- Produces: `GetBooking(external_id: str, customer_id: str, booking_key: UUID)`.
- Produces: `RescheduleBooking(..., customer_id: str, booking_key: UUID)` and `CancelBooking(..., customer_id: str, booking_key: UUID)`.
- Produces: `BookingPort.get_booking(command: GetBooking) -> ExternalBooking`.
- Produces: `ExternalBooking(..., booking_key: UUID)` and immutable repository upsert.

- [ ] **Step 1: Write failing service and fake tests**

Capture sent commands and assert exact trusted values:

```python
assert create_command.booking_key == create_scenario.id
assert change_command.customer_id == local_booking.customer_id
assert change_command.booking_key == local_booking.booking_key

with pytest.raises(BookingNotFound):
    await fake.get_booking(GetBooking(external_id, "other-owner", booking_key))
with pytest.raises(BookingNotFound):
    await fake.cancel_booking(CancelBooking(
        external_id=external_id,
        customer_id="owner",
        booking_key=other_key,
        idempotency_key=key,
    ))
```

The fake must return `ExternalBooking.customer_id` only from the trusted command and preserve the same `booking_key` through create/get/reschedule/cancel.

Add two independent repository RED cases: same `external_id` with a different `booking_key`, and same `external_id`/`booking_key` with a different `customer_id`. Each must raise `RuntimeError("booking ownership conflict")`; assert the stored owner/key and the scenario phase/state/event set are unchanged, proving the transaction rolled back.

- [ ] **Step 2: Run RED**

Recreate the ignored empty file `../tmp/compose-empty.env` with `apply_patch`, export fresh process-only credentials/URLs without echoing them, then run in `moroz-ownership-task2-red`:

```powershell
docker compose --env-file ../tmp/compose-empty.env -p moroz-ownership-task2-red --profile test run --rm test pytest tests/unit/booking/test_mock_adapter.py tests/e2e/booking/test_create_booking.py tests/e2e/booking/test_change_booking.py tests/integration/booking/test_booking_repository.py -q
```

Expected: collection/type failures for the new command fields or assertions showing trusted context is absent. Clean exact namespace resources.

- [ ] **Step 3: Implement the minimal command contract**

Define the commands explicitly:

```python
@dataclass(frozen=True, slots=True)
class CreateBooking:
    customer_id: str
    booking_key: UUID
    slot_id: str
    idempotency_key: str
    customer_name: str
    customer_phone: str
    personal_data_processing_allowed: bool
    comment: str | None = None

@dataclass(frozen=True, slots=True)
class GetBooking:
    external_id: str
    customer_id: str
    booking_key: UUID

@dataclass(frozen=True, slots=True)
class RescheduleBooking:
    external_id: str
    customer_id: str
    booking_key: UUID
    slot_id: str
    idempotency_key: str

@dataclass(frozen=True, slots=True)
class CancelBooking:
    external_id: str
    customer_id: str
    booking_key: UUID
    idempotency_key: str
```

Add `customer_id` and `booking_key` to both change commands, `booking_key` to create, and `booking_key` to `ExternalBooking`. In `BookingService`, use `scenario.id` for create and the locked local `booking.customer_id` / `booking.booking_key` for changes. Update the mock's internal record and ownership checks; a mismatch raises `BookingNotFound` before mutation.

Include the key in repository SELECT, INSERT and snapshot. Use a conditional upsert:

```sql
ON CONFLICT (external_id) DO UPDATE SET
    last_scenario_id = EXCLUDED.last_scenario_id,
    slot_id = EXCLUDED.slot_id,
    starts_at = EXCLUDED.starts_at,
    status = EXCLUDED.status,
    snapshot = EXCLUDED.snapshot,
    updated_at = now()
WHERE bookings.customer_id = EXCLUDED.customer_id
  AND bookings.booking_key = EXCLUDED.booking_key
RETURNING external_id
```

If `RETURNING` yields no row, raise `RuntimeError("booking ownership conflict")` inside the transaction so scenario and booking/event updates roll back together. Never overwrite `customer_id` or `booking_key` on conflict.

- [ ] **Step 4: Run GREEN plus booking regression**

Run the RED command; expect all pass. Clean `moroz-ownership-task2-green` to exact zero resources and remove `../tmp/compose-empty.env`.

- [ ] **Step 5: Update changelog and commit**

Commit:

```bash
git commit -m "refactor: booking port получает trusted ownership"
```

---

### Task 3: Replace YCLIENTS `api_id` ownership with exact custom-field validation

**Files:**
- Modify: `project/src/moroz/booking/yclients.py`
- Modify: `project/tests/contract/booking/test_yclients_adapter.py`
- Modify: `project/tests/contract/booking/test_yclients_http.py`
- Modify: `project/tests/e2e/booking/test_yclients_fail_closed.py`

**Interfaces:**
- Consumes: trusted command types and `ExternalBooking.booking_key` from Task 2.
- Produces: provider payload member `custom_fields.moroz_booking_key = str(command.booking_key)`.
- Produces: fail-closed exact validation of the same field for create response, GET, reschedule preflight/response and cancel preflight.

- [ ] **Step 1: Write failing HTTP contract tests**

Assert create does not use `api_id` for ownership and sends exactly the key:

```python
assert request_json["custom_fields"]["moroz_booking_key"] == str(booking_key)
assert "api_id" not in request_json
```

Cover these response cases for create and protected GET: missing key, non-string key, malformed UUID, different UUID, uppercase UUID and braced UUID fail closed as `BookingNotFound`; missing/non-object `custom_fields` is malformed provider structure and raises `BookingTemporaryError`. For reschedule, seed provider data with `{"foreign": "keep", "moroz_booking_key": str(booking_key)}` and assert the PUT preserves `foreign`. For cancel, assert a protected GET occurs first and mismatched/missing key causes zero DELETE calls. Keep the existing tests proving POST/PUT/DELETE are never retried after send/transport/shape uncertainty.

- [ ] **Step 2: Run RED**

Recreate the ignored empty file `../tmp/compose-empty.env` with `apply_patch`, export fresh process-only credentials/URLs without echoing them, then run in `moroz-ownership-task3-red`:

```powershell
docker compose --env-file ../tmp/compose-empty.env -p moroz-ownership-task3-red --profile test run --rm test pytest tests/contract/booking/test_yclients_adapter.py tests/contract/booking/test_yclients_http.py tests/e2e/booking/test_yclients_fail_closed.py -q
```

Expected: request and ownership assertions fail because the adapter still treats `api_id` as the marker. Clean exact namespace resources.

- [ ] **Step 3: Implement one strict parser and reuse it**

Keep one module constant and one parser:

```python
_BOOKING_KEY_FIELD = "moroz_booking_key"

def _require_booking_key(record: Mapping[str, object], expected: UUID) -> UUID:
    fields = record.get("custom_fields")
    if not isinstance(fields, Mapping):
        raise BookingTemporaryError("booking custom fields are malformed")
    raw = fields.get(_BOOKING_KEY_FIELD)
    try:
        actual = UUID(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise BookingNotFound("booking ownership marker is invalid") from error
    if actual != expected or raw != str(expected):
        raise BookingNotFound("booking ownership marker does not match")
    return actual
```

Create sends the custom field and accepts 201 only when the returned record contains the exact UUID. `_external_booking` receives trusted `customer_id` and expected key, never derives owner identity from provider fields, and returns those trusted values. Reschedule performs protected GET, validates, shallow-copies all returned `custom_fields`, sets only `moroz_booking_key`, then issues one PUT. Cancel performs protected GET and validation before one DELETE. Remove only ownership use of `api_id`; do not add compatibility fallback for legacy records.

- [ ] **Step 4: Run GREEN and fail-closed regression**

Run the same focused tests plus every `project/tests/**/booking/` test in `moroz-ownership-task3-green`; expect all pass and existing mutation-attempt counters remain exactly one or zero as appropriate. Clean exact namespace resources and remove `../tmp/compose-empty.env`.

- [ ] **Step 5: Update changelog and commit**

Commit:

```bash
git commit -m "fix: YCLIENTS ownership переведён на custom field"
```

---

### Task 4: Make sandbox smoke prove exact-key lifecycle and duplicate absence

**Files:**
- Modify: `project/src/moroz/booking/yclients_sandbox_smoke.py`
- Modify: `project/tests/unit/booking/test_yclients_sandbox_smoke.py`
- Modify: `Дорожная карта.md`
- Modify: `План реализации.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 2 command contract and Task 3 strict adapter.
- Produces: one generated UUID reused as `booking_key` for create/get/reschedule/get/cancel.
- Produces: safe evidence only: stage names, counts, allowlisted unknown kind/status and boolean gates; never IDs, names, phones, comments or credentials.

- [ ] **Step 1: Write failing smoke tests**

Update the spy adapter to accept `GetBooking`. Assert every lifecycle command carries the same UUID and trusted synthetic customer id. Add a read-only reconciliation fixture containing unrelated records and two structurally similar records; only exact `custom_fields.moroz_booking_key` matches count. Assert `matches == 1` after create/reschedule and `active_matches == 0` after cancel. Assert availability failure or create uncertainty prevents every later mutation.

- [ ] **Step 2: Run RED**

Recreate the ignored empty file `../tmp/compose-empty.env` with `apply_patch`, export fresh process-only credentials/URLs without echoing them, then run in `moroz-ownership-task4-red`:

```powershell
docker compose --env-file ../tmp/compose-empty.env -p moroz-ownership-task4-red --profile test run --rm test pytest tests/unit/booking/test_yclients_sandbox_smoke.py -q
```

Expected: failures from the old external-id-only GET and `api_id` duplicate correlation. Clean exact namespace resources.

- [ ] **Step 3: Implement minimal exact-key smoke flow**

Generate `booking_key = uuid4()` once inside the consented run, pass it through all commands and inspect duplicate evidence only by exact key in provider `custom_fields`. Keep the established sequence:

```text
services -> staff -> slots -> create -> get -> reschedule -> get -> cancel -> read-only reconciliation
```

Do not add field-management API calls. If the additional field is absent/misconfigured, the create outcome remains fail-closed and the run stops without later mutations.

- [ ] **Step 4: Run GREEN and static privacy checks**

Run the focused unit test and safe-logging tests in `moroz-ownership-task4-green`; expect all pass. Search the smoke source/output schema for Authorization, tokens, raw response body, phone/name/comment and provider IDs; expect no disclosure path. Clean exact namespace resources and remove `../tmp/compose-empty.env`.

- [ ] **Step 5: Update docs and commit**

Document that local implementation is complete but live completion requires (a) branch field `moroz_booking_key`, (b) separate cleanup consent for the one pre-design active synthetic record, and (c) a newly consented lifecycle smoke. Remove `../tmp/compose-empty.env`. Commit:

```bash
git commit -m "test: sandbox smoke использует moroz booking key"
```

---

### Task 5: Independent review, fix-loop and fresh Docker verification

**Files:**
- Modify only files required by concrete review findings.
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`
- Modify: `План реализации.md`

**Interfaces:**
- Consumes: all completed tasks.
- Produces: review report with Critical/Important/Minor counts and fresh canonical Docker evidence.

- [ ] **Step 1: Run independent spec-compliance review**

Reviewer checks the committed diff against `docs/superpowers/specs/2026-07-22-yclients-postgres-ownership-design.md`, with special attention to PostgreSQL authority, immutable upsert, exact-key parsing, preservation of foreign fields, cancel preflight and no mutation retry. Fix every valid finding via a new RED/GREEN cycle and re-review until `0 Critical / 0 Important / 0 Minor`.

- [ ] **Step 2: Run fresh migration and full suite**

Build a fresh test image and run Alembic upgrade plus the entire pytest suite in a new namespace such as `moroz-ownership-final-<timestamp>`, using newly generated process-only credentials. Expected: migration head `0006_yclients_booking_key`, all tests pass, no skipped ownership tests, exit 0.

- [ ] **Step 3: Verify hygiene and cleanup**

Run `git diff --check`, a scoped secret-shaped-value scan, and inspect `git status --short`. Tear down only the final namespace using exact name and verify its containers, volumes, networks and local images are all zero. Confirm shared/prototype/production/staging resources were untouched.

- [ ] **Step 4: Commit verified local completion**

Record exact test count, review result and cleanup counts; mark local/fake HTTP work complete and leave live phase open. Commit:

```bash
git commit -m "docs: подтверждён local ownership YCLIENTS"
```

- [ ] **Step 5: Stop at the honest external gate**

Do not run another provider mutation automatically. Ask only for the minimal UI action needed to create/configure the branch additional field named exactly `moroz_booking_key`; do not ask for credentials or screenshots with values. After the user confirms that field and separately authorizes a new sandbox attempt, run one lifecycle smoke. The existing active synthetic record is not deleted without separate explicit cleanup consent.
