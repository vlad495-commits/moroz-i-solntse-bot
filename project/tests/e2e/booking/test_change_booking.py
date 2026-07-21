import json
import os
import subprocess
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import (
    BookingIdentity,
    BookingOutcomeUnknown,
    BookingScenario,
    BookingTemporaryError,
    CancelBooking,
    CreateBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.common.config import Settings
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 7, 25, 10, tzinfo=UTC)
OLD_START = datetime(2026, 7, 25, 14, tzinfo=UTC)


class RedactedDatabaseURL(str):
    def __repr__(self):
        return "'<redacted-database-url>'"


class CountingChangeAdapter(MockYclientsAdapter):
    def __init__(self, slots: list[Slot]) -> None:
        super().__init__(slots)
        self.list_calls = 0
        self.reschedule_calls = 0
        self.cancel_calls = 0
        self.get_calls = 0

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        self.list_calls += 1
        return await super().list_slots(query)

    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        return await super().reschedule_booking(command)

    async def cancel_booking(self, command: CancelBooking) -> None:
        self.cancel_calls += 1
        await super().cancel_booking(command)

    async def get_booking(self, external_id: str):
        self.get_calls += 1
        return await super().get_booking(external_id)

    def reset_counts(self) -> None:
        self.list_calls = 0
        self.reschedule_calls = 0
        self.cancel_calls = 0
        self.get_calls = 0


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


@pytest_asyncio.fixture
async def repo():
    admin_url = Settings.from_env(os.environ).database_url
    assert admin_url
    database_name = f"test_change_booking_{uuid.uuid4().hex}"
    parts = urlsplit(admin_url)
    database_url = RedactedDatabaseURL(
        urlunsplit(parts._replace(path=f"/{database_name}"))
    )
    admin = await asyncpg.connect(admin_url)
    database = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        subprocess.run(
            ["alembic", "-c", "/workspace/alembic.ini", "upgrade", "head"],
            check=True,
            env={**os.environ, "DATABASE_URL": database_url},
        )
        database = Database(database_url, min_size=1, max_size=1)
        await database.connect()
        yield BookingRepository(database)
    finally:
        if database is not None:
            await database.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()


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
        phase="awaiting_confirmation",
        idempotency_key=f"{kind}:{uuid4()}",
        customer_id="customer-7",
        state=state,
        error_code=None,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )


async def _seed_booking(repo, port: MockYclientsAdapter):
    booking = await port.create_booking(
        CreateBooking("customer-7", "slot-old", f"seed:{uuid4()}")
    )
    seed = BookingScenario(
        id=uuid4(),
        kind="create",
        phase="confirmed",
        idempotency_key=f"seed-scenario:{uuid4()}",
        customer_id="customer-7",
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


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


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
    assert (port.cancel_calls, port.list_calls, port.get_calls) == (1, 0, 1)
    stored = await repo.get_scenario(scenario.id)
    snapshot = await repo.get_local_booking(scenario.id)
    assert stored.phase == "confirmed"
    assert snapshot.status == "cancelled"
    assert snapshot.starts_at == original.starts_at
    assert json.loads(json.dumps(_thaw(stored.state))) == _thaw(stored.state)


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_change_under_three_hours_escalates_before_any_port_call(repo, kind):
    port = CountingChangeAdapter([_slot("slot-new", 16)])
    scenario = _scenario(kind, "booking-42")
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
    assert (port.list_calls, port.reschedule_calls, port.cancel_calls, port.get_calls) == (
        0,
        0,
        0,
        0,
    )


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_temporary_failure_is_durable_without_a_new_promise(repo, kind):
    port = FailingMutationAdapter(
        [_slot("slot-old", 14), _slot("slot-new", 16)],
        BookingTemporaryError,
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
