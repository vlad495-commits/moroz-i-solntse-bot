import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import (
    BookingIdentity,
    BookingNotFound,
    BookingOutcomeUnknown,
    BookingScenario,
    BookingTemporaryError,
    CancelBooking,
    CreateBooking,
    GetBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.service import BookingService


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 7, 25, 10, tzinfo=UTC)
OLD_START = datetime(2026, 7, 25, 14, tzinfo=UTC)


class CountingChangeAdapter(MockYclientsAdapter):
    def __init__(self, slots: list[Slot]) -> None:
        super().__init__(slots)
        self.list_calls = 0
        self.reschedule_calls = 0
        self.cancel_calls = 0
        self.get_calls = 0
        self.last_reschedule: RescheduleBooking | None = None
        self.last_cancel: CancelBooking | None = None

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        self.list_calls += 1
        return await super().list_slots(query)

    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        self.last_reschedule = command
        return await super().reschedule_booking(command)

    async def cancel_booking(self, command: CancelBooking) -> None:
        self.cancel_calls += 1
        self.last_cancel = command
        await super().cancel_booking(command)

    async def get_booking(self, command: GetBooking):
        self.get_calls += 1
        return await super().get_booking(command)

    def reset_counts(self) -> None:
        self.list_calls = 0
        self.reschedule_calls = 0
        self.cancel_calls = 0
        self.get_calls = 0
        self.last_reschedule = None
        self.last_cancel = None


class FailingMutationAdapter(CountingChangeAdapter):
    def __init__(self, slots: list[Slot], error: type[Exception]) -> None:
        super().__init__(slots)
        self._error = error

    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        raise self._error("reschedule failed")

    async def cancel_booking(self, command: CancelBooking) -> None:
        self.cancel_calls += 1
        raise self._error("cancel failed")


class SlotDisappearsOnRescheduleAdapter(CountingChangeAdapter):
    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        self._slots.pop(command.slot_id, None)
        raise SlotUnavailable(command.slot_id)


class BarrierRescheduleAdapter(CountingChangeAdapter):
    def __init__(self, slots: list[Slot]) -> None:
        super().__init__(slots)
        self.reschedule_entered = asyncio.Event()
        self.release_reschedule = asyncio.Event()

    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        self.reschedule_entered.set()
        await self.release_reschedule.wait()
        return await MockYclientsAdapter.reschedule_booking(self, command)


class BarrierOutcomeUnknownAdapter(CountingChangeAdapter):
    def __init__(self, slots: list[Slot]) -> None:
        super().__init__(slots)
        self.reschedule_entered = asyncio.Event()
        self.release_reschedule = asyncio.Event()

    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        self.reschedule_entered.set()
        await self.release_reschedule.wait()
        raise BookingOutcomeUnknown("reschedule result is unknown")


def _slot(slot_id: str, hour: int) -> Slot:
    return Slot(
        id=slot_id,
        service_ids=("service-1",),
        staff_id="staff-1",
        starts_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        duration_minutes=60,
    )


def _scenario(
    kind: str,
    external_id: str,
    *,
    starts_at: datetime = OLD_START,
    selected_slot_id: str = "slot-new",
    customer_id: str = "customer-7",
    phase: str = "awaiting_confirmation",
    error_code: str | None = None,
) -> BookingScenario:
    state: dict[str, object] = {
        "external_id": external_id,
        "starts_at": starts_at.isoformat(),
    }
    if kind == "reschedule":
        state.update(
            {
                "slot_query": {
                    "service_ids": ["service-1"],
                    "starts_after": "2026-07-25T15:00:00+00:00",
                    "starts_before": "2026-07-25T20:00:00+00:00",
                    "staff_id": "staff-1",
                },
                "selected_slot_id": selected_slot_id,
            }
        )
    return BookingScenario(
        id=uuid4(),
        kind=kind,
        phase=phase,
        idempotency_key=f"{kind}:{uuid4()}",
        customer_id=customer_id,
        state=state,
        error_code=error_code,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )


async def _seed_booking(repo, port: MockYclientsAdapter, customer_id="customer-7"):
    booking = await port.create_booking(
        _create_command(customer_id, "slot-old", f"seed:{uuid4()}")
    )
    seed = BookingScenario(
        id=uuid4(),
        kind="create",
        phase="confirmed",
        idempotency_key=f"seed-scenario:{uuid4()}",
        customer_id=customer_id,
        state={"external_id": booking.external_id},
        error_code=None,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(days=2),
    )
    await repo.create_scenario(seed)
    await repo.confirm(seed, booking)
    if isinstance(port, CountingChangeAdapter):
        port.reset_counts()
    return booking


def _create_command(
    customer_id: str = "customer-1",
    slot_id: str = "slot-ok",
    idempotency_key: str = "create-1",
) -> CreateBooking:
    return CreateBooking(
        customer_id=customer_id,
        booking_key=uuid4(),
        slot_id=slot_id,
        idempotency_key=idempotency_key,
        customer_name="Sandbox Customer",
        customer_phone="+70000000000",
        personal_data_processing_allowed=True,
    )


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


async def _wait_for_aggregate_lock(database_url, task) -> bool:
    watcher = await asyncpg.connect(database_url)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    try:
        while not task.done() and loop.time() < deadline:
            waiting = await watcher.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                      AND query LIKE '%pg_advisory_lock%'
                )
                """
            )
            if waiting:
                return True
            await asyncio.sleep(0.01)
        return False
    finally:
        await watcher.close()


@pytest.mark.parametrize(
    "identity",
    [
        None,
        BookingIdentity(customer_id="customer-7", confirmed=False),
        BookingIdentity(customer_id="other", confirmed=True),
    ],
    ids=["missing", "unconfirmed", "wrong-customer"],
)
async def test_change_requires_confirmed_matching_identity_before_port(repo, identity):
    port = CountingChangeAdapter([_slot("slot-new", 16)])
    scenario = _scenario("reschedule", "booking-42")
    await repo.create_scenario(scenario)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        scenario.id,
        confirmed=True,
        identity=identity,
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls, port.get_calls) == (
        0,
        0,
        0,
        0,
    )
    stored = await repo.get_scenario(scenario.id)
    assert (stored.phase, stored.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )


async def test_reschedule_persists_old_and_new_snapshot_and_repeat_is_stable(repo):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    original = await _seed_booking(repo, port)
    scenario = _scenario("reschedule", original.external_id)
    await repo.create_scenario(scenario)
    service = BookingService(port, repo, now=lambda: NOW)
    identity = BookingIdentity("customer-7", confirmed=True)

    result = await service.handle(scenario.id, confirmed=True, identity=identity)
    repeated = await service.handle(scenario.id, confirmed=True, identity=identity)

    assert result == repeated
    assert result.status == "ok"
    assert OLD_START.isoformat() in result.message
    assert _slot("slot-new", 16).starts_at.isoformat() in result.message
    assert (port.list_calls, port.reschedule_calls) == (1, 1)
    assert port.last_reschedule is not None
    assert port.last_reschedule.customer_id == original.customer_id
    assert port.last_reschedule.booking_key == original.booking_key
    stored = await repo.get_scenario(scenario.id)
    snapshot = await repo.get_local_booking(scenario.id)
    assert stored.phase == "confirmed"
    assert stored.state["previous_starts_at"] == OLD_START.isoformat()
    assert stored.state["starts_at"] == snapshot.starts_at.isoformat()
    assert (snapshot.external_id, snapshot.slot_id, snapshot.status) == (
        original.external_id,
        "slot-new",
        "confirmed",
    )
    assert json.loads(json.dumps(_thaw(stored.state))) == _thaw(stored.state)


async def test_partial_service_escalation_is_durable_without_provider_mutation(repo):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    original = await _seed_booking(repo, port)
    scenario = _scenario(
        "reschedule",
        original.external_id,
        phase="collecting",
    )
    await repo.create_scenario(scenario)

    result = await BookingService(port, repo, now=lambda: NOW).escalate(
        scenario.id,
        identity=BookingIdentity("customer-7", confirmed=True),
        error_code="partial_service_change_unsupported",
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "partial_service_change_unsupported",
    )
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls) == (0, 0, 0)
    stored = await repo.get_scenario(scenario.id)
    assert (stored.phase, stored.error_code) == (
        "escalated",
        "partial_service_change_unsupported",
    )


async def test_cancel_at_exactly_three_hours_uses_local_snapshot_and_mutates_once(repo):
    port = CountingChangeAdapter([_slot("slot-old", 14)])
    original = await _seed_booking(repo, port)
    scenario = _scenario("cancel", original.external_id)
    await repo.create_scenario(scenario)
    service = BookingService(port, repo, now=lambda: OLD_START - timedelta(hours=3))
    identity = BookingIdentity("customer-7", confirmed=True)

    result = await service.handle(scenario.id, confirmed=True, identity=identity)
    repeated = await service.handle(scenario.id, confirmed=True, identity=identity)

    assert result == repeated
    assert result.status == "ok"
    assert OLD_START.isoformat() in result.message
    assert (port.cancel_calls, port.list_calls, port.get_calls) == (1, 0, 2)
    assert port.last_cancel is not None
    assert port.last_cancel.customer_id == original.customer_id
    assert port.last_cancel.booking_key == original.booking_key
    stored = await repo.get_scenario(scenario.id)
    snapshot = await repo.get_local_booking(scenario.id)
    assert stored.phase == "confirmed"
    assert snapshot.status == "cancelled"
    assert snapshot.starts_at == original.starts_at
    assert json.loads(json.dumps(_thaw(stored.state))) == _thaw(stored.state)


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_change_under_three_hours_protects_get_before_late_rule(repo, kind):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    original = await _seed_booking(repo, port)
    scenario = _scenario(kind, original.external_id)
    await repo.create_scenario(scenario)

    result = await BookingService(
        port,
        repo,
        now=lambda: OLD_START - timedelta(hours=2, minutes=59),
    ).handle(
        scenario.id,
        confirmed=True,
        identity=BookingIdentity("customer-7", confirmed=True),
    )

    assert (result.status, result.error_code) == ("escalated", "late_booking_change")
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls) == (0, 0, 0)
    assert port.get_calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_id", "foreign-record"),
        ("customer_id", "other-customer"),
        ("booking_key", uuid4()),
        ("slot_id", "different-slot"),
        ("service_ids", ("service-1", "service-2")),
        ("staff_id", "different-staff"),
        ("starts_at", OLD_START + timedelta(minutes=1)),
        ("status", "cancelled"),
        ("scheduled_end_at", OLD_START + timedelta(hours=2)),
    ],
)
async def test_change_repeats_exact_protected_get_before_mutation(
    repo,
    field,
    value,
):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    original = await _seed_booking(repo, port)
    provider = replace(original, **{field: value})

    async def mismatched_get(_command):
        port.get_calls += 1
        return provider

    port.get_booking = mismatched_get
    scenario = _scenario("reschedule", original.external_id)
    await repo.create_scenario(scenario)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        scenario.id,
        confirmed=True,
        identity=BookingIdentity("customer-7", confirmed=True),
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "booking_temporarily_unavailable",
    )
    assert result.message == "Статус записи проверит администратор."
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls) == (0, 0, 0)
    assert port.get_calls == 1


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
@pytest.mark.parametrize("error", [BookingTemporaryError, BookingNotFound])
async def test_definite_failure_is_durable_without_a_new_promise(repo, kind, error):
    port = FailingMutationAdapter(
        [_slot("slot-old", 14), _slot("slot-new", 16)],
        error,
    )
    original = await _seed_booking(repo, port)
    scenario = _scenario(kind, original.external_id)
    await repo.create_scenario(scenario)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        scenario.id,
        confirmed=True,
        identity=BookingIdentity("customer-7", confirmed=True),
    )

    stored = await repo.get_scenario(scenario.id)
    snapshot = await repo.get_local_booking(scenario.id)
    events = await repo.list_events(scenario.id)
    assert (result.status, result.error_code) == (
        "escalated",
        "booking_temporarily_unavailable",
    )
    assert (stored.phase, stored.error_code) == (
        "escalated",
        "booking_temporarily_unavailable",
    )
    assert snapshot == original
    assert events[-1].event_type == "admin_attention_required"
    assert events[-1].payload["error_code"] == "booking_temporarily_unavailable"


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_outcome_unknown_is_durable_and_terminal_repeat_never_retries(repo, kind):
    port = FailingMutationAdapter(
        [_slot("slot-old", 14), _slot("slot-new", 16)],
        BookingOutcomeUnknown,
    )
    original = await _seed_booking(repo, port)
    scenario = _scenario(kind, original.external_id)
    await repo.create_scenario(scenario)
    service = BookingService(port, repo, now=lambda: NOW)
    identity = BookingIdentity("customer-7", confirmed=True)

    result = await service.handle(scenario.id, confirmed=True, identity=identity)
    calls = (port.list_calls, port.reschedule_calls, port.cancel_calls, port.get_calls)
    repeated = await service.handle(scenario.id, confirmed=True, identity=identity)

    assert result == repeated
    assert (result.status, result.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls, port.get_calls) == calls
    assert await repo.get_local_booking(scenario.id) == original
    events = await repo.list_events(scenario.id)
    assert [event.event_type for event in events].count("admin_attention_required") == 1


async def test_earlier_reschedule_repeat_uses_its_own_scenario_snapshot(repo):
    port = CountingChangeAdapter(
        [_slot("slot-old", 14), _slot("slot-new", 16), _slot("slot-next", 18)]
    )
    original = await _seed_booking(repo, port)
    first = _scenario("reschedule", original.external_id)
    await repo.create_scenario(first)
    service = BookingService(port, repo, now=lambda: NOW)
    identity = BookingIdentity("customer-7", confirmed=True)

    first_result = await service.handle(first.id, confirmed=True, identity=identity)
    second = _scenario(
        "reschedule",
        original.external_id,
        starts_at=_slot("slot-new", 16).starts_at,
        selected_slot_id="slot-next",
    )
    await repo.create_scenario(second)
    second_result = await service.handle(second.id, confirmed=True, identity=identity)
    calls_after_mutations = (port.list_calls, port.reschedule_calls, port.get_calls)

    repeated_first = await service.handle(first.id, confirmed=True, identity=identity)

    assert first_result == repeated_first
    assert first_result != second_result
    assert _slot("slot-new", 16).starts_at.isoformat() in first_result.message
    assert _slot("slot-next", 18).starts_at.isoformat() not in first_result.message
    assert (port.list_calls, port.reschedule_calls, port.get_calls) == calls_after_mutations


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
@pytest.mark.parametrize(
    "identity",
    [
        None,
        BookingIdentity("customer-7", confirmed=False),
        BookingIdentity("other", confirmed=True),
    ],
    ids=["missing", "unconfirmed", "wrong-customer"],
)
async def test_confirmed_change_repeat_rejects_unowned_identity_without_leaking(
    repo,
    kind,
    identity,
):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    original = await _seed_booking(repo, port)
    scenario = _scenario(kind, original.external_id)
    await repo.create_scenario(scenario)
    service = BookingService(port, repo, now=lambda: NOW)
    correct_identity = BookingIdentity("customer-7", confirmed=True)
    initial = await service.handle(
        scenario.id,
        confirmed=True,
        identity=correct_identity,
    )
    calls = (
        port.list_calls,
        port.reschedule_calls,
        port.cancel_calls,
        port.get_calls,
    )
    events_before = await repo.list_events(scenario.id)

    rejected = await service.handle(scenario.id, confirmed=True, identity=identity)

    assert (rejected.status, rejected.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )
    assert rejected.message == "Статус записи проверит администратор."
    assert OLD_START.isoformat() not in rejected.message
    assert _slot("slot-new", 16).starts_at.isoformat() not in rejected.message
    assert (
        port.list_calls,
        port.reschedule_calls,
        port.cancel_calls,
        port.get_calls,
    ) == calls
    stored = await repo.get_scenario(scenario.id)
    assert (stored.phase, stored.error_code) == ("confirmed", None)
    assert await repo.list_events(scenario.id) == events_before
    assert "admin_attention_required" not in [
        event.event_type for event in events_before
    ]
    assert await service.handle(
        scenario.id,
        confirmed=True,
        identity=correct_identity,
    ) == initial


async def test_forged_scenario_owner_cannot_mutate_another_customers_booking(repo):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    booking = await _seed_booking(repo, port, customer_id="customer-B")
    forged = _scenario(
        "reschedule",
        booking.external_id,
        customer_id="customer-A",
    )
    await repo.create_scenario(forged)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        forged.id,
        confirmed=True,
        identity=BookingIdentity("customer-A", confirmed=True),
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls) == (0, 0, 0)
    stored = await repo.get_scenario(forged.id)
    assert (stored.phase, stored.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )
    assert (await repo.list_events(forged.id))[-1].event_type == "admin_attention_required"


@pytest.mark.parametrize("phase", ["confirmed", "escalated", "executing"])
async def test_forged_owner_cannot_observe_or_mutate_terminal_or_recovery(repo, phase):
    port = CountingChangeAdapter([_slot("slot-old", 14)])
    booking = await _seed_booking(repo, port, customer_id="customer-B")
    forged = _scenario(
        "cancel",
        booking.external_id,
        customer_id="customer-A",
        phase=phase,
        error_code=("booking_temporarily_unavailable" if phase == "escalated" else None),
    )
    await repo.create_scenario(forged)
    events_before = await repo.list_events(forged.id)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        forged.id,
        confirmed=True,
        identity=BookingIdentity("customer-A", confirmed=True),
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )
    assert result.message == "Статус записи проверит администратор."
    assert OLD_START.isoformat() not in result.message
    assert await repo.get_scenario(forged.id) == forged
    assert await repo.list_events(forged.id) == events_before
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls, port.get_calls) == (
        0,
        0,
        0,
        0,
    )


async def test_distinct_change_scenarios_serialize_one_external_booking(
    migrated_database_url,
    repo_pair,
):
    first_repo, second_repo = repo_pair
    port = BarrierRescheduleAdapter(
        [_slot("slot-old", 14), _slot("slot-new", 16)]
    )
    booking = await _seed_booking(first_repo, port)
    reschedule = _scenario("reschedule", booking.external_id)
    cancel = _scenario("cancel", booking.external_id)
    await first_repo.create_scenario(reschedule)
    await first_repo.create_scenario(cancel)
    identity = BookingIdentity("customer-7", confirmed=True)

    first = asyncio.create_task(
        BookingService(port, first_repo, now=lambda: NOW).handle(
            reschedule.id, confirmed=True, identity=identity
        )
    )
    await asyncio.wait_for(port.reschedule_entered.wait(), timeout=2)
    second = asyncio.create_task(
        BookingService(port, second_repo, now=lambda: NOW).handle(
            cancel.id, confirmed=True, identity=identity
        )
    )
    lock_waiting = await _wait_for_aggregate_lock(migrated_database_url, second)
    port.release_reschedule.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert lock_waiting
    assert first_result.status == "ok"
    assert (second_result.status, second_result.error_code) == (
        "escalated",
        "booking_temporarily_unavailable",
    )
    assert (port.reschedule_calls, port.cancel_calls) == (1, 0)


async def test_outcome_unknown_blocks_later_aggregate_mutation(
    migrated_database_url,
    repo_pair,
):
    first_repo, second_repo = repo_pair
    port = BarrierOutcomeUnknownAdapter(
        [_slot("slot-old", 14), _slot("slot-new", 16)]
    )
    booking = await _seed_booking(first_repo, port)
    reschedule = _scenario("reschedule", booking.external_id)
    cancel = _scenario("cancel", booking.external_id)
    await first_repo.create_scenario(reschedule)
    await first_repo.create_scenario(cancel)
    identity = BookingIdentity("customer-7", confirmed=True)

    first = asyncio.create_task(
        BookingService(port, first_repo, now=lambda: NOW).handle(
            reschedule.id, confirmed=True, identity=identity
        )
    )
    await asyncio.wait_for(port.reschedule_entered.wait(), timeout=2)
    second = asyncio.create_task(
        BookingService(port, second_repo, now=lambda: NOW).handle(
            cancel.id, confirmed=True, identity=identity
        )
    )
    lock_waiting = await _wait_for_aggregate_lock(migrated_database_url, second)
    port.release_reschedule.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert lock_waiting
    assert (first_result.status, first_result.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )
    assert (second_result.status, second_result.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )
    assert (port.reschedule_calls, port.cancel_calls) == (1, 0)
    assert await first_repo.get_local_booking(reschedule.id) == booking
    stored_second = await second_repo.get_scenario(cancel.id)
    assert (stored_second.phase, stored_second.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )


async def test_abandoned_executing_sibling_blocks_aggregate_mutation(repo):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    booking = await _seed_booking(repo, port)
    abandoned = _scenario(
        "reschedule",
        booking.external_id,
        phase="executing",
    )
    current = _scenario("cancel", booking.external_id)
    await repo.create_scenario(abandoned)
    await repo.create_scenario(current)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        current.id,
        confirmed=True,
        identity=BookingIdentity("customer-7", confirmed=True),
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )
    assert (port.reschedule_calls, port.cancel_calls, port.list_calls) == (0, 0, 0)
    assert await repo.get_local_booking(current.id) == booking
    assert await repo.get_scenario(abandoned.id) == abandoned
    stored_current = await repo.get_scenario(current.id)
    assert (stored_current.phase, stored_current.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )


@pytest.mark.parametrize("first_kind", ["reschedule", "cancel"])
async def test_sequential_stale_change_fails_before_second_port_mutation(repo, first_kind):
    port = CountingChangeAdapter([_slot("slot-old", 14), _slot("slot-new", 16)])
    booking = await _seed_booking(repo, port)
    identity = BookingIdentity("customer-7", confirmed=True)
    first = _scenario(first_kind, booking.external_id)
    await repo.create_scenario(first)
    first_result = await BookingService(port, repo, now=lambda: NOW).handle(
        first.id, confirmed=True, identity=identity
    )
    stale_kind = "cancel" if first_kind == "reschedule" else "reschedule"
    stale = _scenario(stale_kind, booking.external_id)
    await repo.create_scenario(stale)
    calls = (port.reschedule_calls, port.cancel_calls, port.list_calls)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        stale.id, confirmed=True, identity=identity
    )

    assert first_result.status == "ok"
    assert (result.status, result.error_code) == (
        "escalated",
        "booking_temporarily_unavailable",
    )
    assert (port.reschedule_calls, port.cancel_calls, port.list_calls) == calls


@pytest.mark.parametrize("phase", ["escalated", "executing"])
@pytest.mark.parametrize(
    "identity",
    [
        None,
        BookingIdentity("customer-7", confirmed=False),
        BookingIdentity("other", confirmed=True),
    ],
)
async def test_invalid_identity_cannot_observe_or_mutate_change_recovery(
    repo,
    phase,
    identity,
):
    port = CountingChangeAdapter([_slot("slot-new", 16)])
    error_code = "booking_temporarily_unavailable" if phase == "escalated" else None
    scenario = _scenario(
        "reschedule",
        "booking-42",
        phase=phase,
        error_code=error_code,
    )
    await repo.create_scenario(scenario)
    events_before = await repo.list_events(scenario.id)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        scenario.id,
        confirmed=True,
        identity=identity,
    )

    assert (result.status, result.error_code) == (
        "escalated",
        "booking_identity_unconfirmed",
    )
    assert result.message == "Статус записи проверит администратор."
    assert await repo.get_scenario(scenario.id) == scenario
    assert await repo.list_events(scenario.id) == events_before
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls, port.get_calls) == (
        0,
        0,
        0,
        0,
    )


async def test_reschedule_slot_disappearing_during_mutation_uses_fresh_alternatives(repo):
    port = SlotDisappearsOnRescheduleAdapter(
        [
            _slot("slot-old", 14),
            _slot("slot-new", 16),
            _slot("slot-17", 17),
            _slot("slot-18", 18),
            _slot("slot-19", 19),
        ]
    )
    original = await _seed_booking(repo, port)
    scenario = _scenario("reschedule", original.external_id)
    await repo.create_scenario(scenario)

    result = await BookingService(port, repo, now=lambda: NOW).handle(
        scenario.id,
        confirmed=True,
        identity=BookingIdentity("customer-7", confirmed=True),
    )

    alternatives = result.events[0]["alternatives"]
    stored = await repo.get_scenario(scenario.id)
    events = await repo.list_events(scenario.id)
    assert (result.status, result.next_action) == ("needs_input", "choose_slot")
    assert [item["id"] for item in alternatives] == ["slot-17", "slot-18", "slot-19"]
    assert (port.list_calls, port.reschedule_calls) == (2, 1)
    assert stored.phase == "collecting"
    assert stored.state["starts_at"] == OLD_START.isoformat()
    assert await repo.get_local_booking(scenario.id) == original
    assert events[-1].event_type == "slot_unavailable"
    assert _thaw(events[-1].payload["alternatives"]) == alternatives
