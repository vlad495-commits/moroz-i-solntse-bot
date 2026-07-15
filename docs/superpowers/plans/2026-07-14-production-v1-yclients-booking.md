# YCLIENTS Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать проверяемые state machines создания, переноса и отмены записи через сменяемый mock/real YCLIENTS adapter.

**Architecture:** Домен не знает HTTP; `BookingPort` скрывает YCLIENTS. Каждое изменяющее действие имеет checkpoint, idempotency key, повторную проверку и локальную копию внешнего результата.

**Tech Stack:** Python dataclasses/enums, asyncpg, httpx, pytest, respx.

## Global Constraints

- YCLIENTS — источник правды.
- Без реального доступа использовать mock; production gate остаётся закрытым.
- Не обещать слот до успешного внешнего ответа.
- Отмена менее чем за 3 часа всегда эскалируется.

---

### Task 1: Booking domain и mock adapter

**Files:**
- Create: `project/src/moroz/booking/models.py`
- Create: `project/src/moroz/booking/ports.py`
- Create: `project/src/moroz/booking/mock_yclients.py`
- Test: `project/tests/unit/booking/test_mock_adapter.py`

**Interfaces:** `SlotQuery`, `Slot`, `CreateBooking`, `RescheduleBooking`, `CancelBooking`, `ExternalBooking`, `BookingPort` from master plan.

- [ ] **Step 1:** Write a test that `list_slots` returns only matching future slots and `create_booking` returns the same booking for the same idempotency key.
- [ ] **Step 2:** Run `docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_mock_adapter.py -q`; expect import failure.
- [ ] **Step 3:** Implement an in-memory adapter keyed by `idempotency_key`:

```python
async def create_booking(self, command: CreateBooking) -> ExternalBooking:
    if command.idempotency_key in self.created:
        return self.created[command.idempotency_key]
    if command.slot_id not in self.available_slot_ids:
        raise SlotUnavailable(command.slot_id)
    booking = ExternalBooking(external_id=str(uuid4()), status="confirmed", **command.booking_fields())
    self.created[command.idempotency_key] = booking
    return booking
```

- [ ] **Step 4:** Re-run test; expect pass.
- [ ] **Step 5:** Commit `feat: добавлен контракт и mock YCLIENTS`.

### Task 2: Real HTTP adapter contract tests

**Files:**
- Create: `project/src/moroz/booking/yclients.py`
- Test: `project/tests/contract/booking/test_yclients_adapter.py`
- Modify: `project/src/moroz/common/config.py`

**Interfaces:** Produces `YclientsAdapter(BookingPort)`; consumes API token, company ID, timeout.

- [ ] **Step 1:** Write respx tests for 200, 409 slot conflict, 429 retryable and 401 non-retryable responses.
- [ ] **Step 2:** Run contract tests; expect import failure.
- [ ] **Step 3:** Implement explicit mapping:

```python
response = await self.client.post(url, headers=self.headers, json=payload)
if response.status_code == 409:
    raise SlotUnavailable(command.slot_id)
if response.status_code == 429:
    raise YclientsTemporaryError(retry_after=parse_retry_after(response))
response.raise_for_status()
return ExternalBooking.from_yclients(response.json())
```

Do not guess undocumented endpoints: keep URL paths in adapter config and mark real contract suite `@pytest.mark.yclients_live` requiring provided sandbox credentials.

- [ ] **Step 4:** Run mocked contract tests; expect pass. Run live marker only after access.
- [ ] **Step 5:** Commit `feat: добавлен YCLIENTS HTTP adapter`.

### Task 3: Create-booking state machine

**Files:**
- Create: `project/src/moroz/booking/service.py`
- Create: `project/src/moroz/booking/repository.py`
- Create: `project/migrations/versions/0003_booking_state.py`
- Test: `project/tests/e2e/booking/test_create_booking.py`

**Interfaces:** Produces `BookingService.handle(command, state) -> ScenarioResult` and states `collecting`, `awaiting_confirmation`, `executing`, `confirmed`, `failed`.

- [ ] **Step 1:** Test that no external create occurs before confirmation and a lost slot returns three new alternatives.
- [ ] **Step 2:** Run test; expect failure.
- [ ] **Step 3:** Add `booking_scenarios`, `bookings`, `booking_events`; implement:

```python
if state.phase == "awaiting_confirmation" and command.confirmed:
    await repo.checkpoint(state.to_executing())
    slots = await port.list_slots(state.slot_query())
    if state.slot_id not in {slot.id for slot in slots}:
        return ScenarioResult("needs_input", "Это время уже занято. Вот ближайшие варианты…", "choose_slot", ())
    external = await port.create_booking(state.create_command())
    await repo.confirm(state.id, external)
```

- [ ] **Step 4:** Run E2E; expect pass and durable confirmed checkpoint.
- [ ] **Step 5:** Commit `feat: реализована новая запись через YCLIENTS`.

### Task 4: Reschedule, cancel, ownership and fallback

**Files:**
- Modify: `project/src/moroz/booking/service.py`
- Test: `project/tests/e2e/booking/test_change_booking.py`

- [ ] **Step 1:** Test ownership rejection, reschedule «было/станет», cancel ≥3h, escalation <3h and API outage fallback.
- [ ] **Step 2:** Run tests; expect failures for missing branches.
- [ ] **Step 3:** Implement explicit rules:

```python
if not identity.confirmed_for(booking.customer_id):
    return escalated("booking_identity_unconfirmed")
if command.kind == "cancel" and booking.starts_at - clock.now() < timedelta(hours=3):
    return escalated("late_cancellation")
```

Temporary YCLIENTS errors preserve checkpoint, tell the client no slot is promised and create an admin task.

- [ ] **Step 4:** Run all booking tests; expect pass.
- [ ] **Step 5:** Commit `feat: добавлены перенос отмена и fallback YCLIENTS`.

### Task 5: Booking checkpoint

- [ ] Run all unit/contract/E2E booking tests in Docker; expect pass.
- [ ] Run mock full flow twice; expect one external booking.
- [ ] If sandbox exists, run `pytest -m yclients_live`; otherwise record launch gate as open.
- [ ] Update roadmap/changelog with evidence.
- [ ] Commit `docs: зафиксирован YCLIENTS booking checkpoint`.
