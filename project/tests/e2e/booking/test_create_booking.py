import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import (
    BookingIdentity,
    BookingScenario,
    CreateBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.service import BookingService


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class CountingMockYclientsAdapter(MockYclientsAdapter):
    def __init__(self, slots: list[Slot]) -> None:
        super().__init__(slots)
        self.list_calls = 0
        self.create_calls = 0

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        self.list_calls += 1
        return await super().list_slots(query)

    async def create_booking(self, command: CreateBooking):
        self.create_calls += 1
        return await super().create_booking(command)


class BarrierCreateAdapter(CountingMockYclientsAdapter):
    def __init__(self, slots: list[Slot]) -> None:
        super().__init__(slots)
        self.create_entered = asyncio.Event()
        self.release_create = asyncio.Event()

    async def create_booking(self, command: CreateBooking):
        self.create_calls += 1
        self.create_entered.set()
        await self.release_create.wait()
        return await MockYclientsAdapter.create_booking(self, command)


class SlotDisappearsOnCreateAdapter(CountingMockYclientsAdapter):
    async def create_booking(self, command: CreateBooking):
        self.create_calls += 1
        self._slots.pop(command.slot_id, None)
        raise SlotUnavailable(command.slot_id)


def _slot(slot_id: str, hour: int) -> Slot:
    return Slot(
        id=slot_id,
        service_ids=("service-1",),
        staff_id="staff-1",
        starts_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        duration_minutes=60,
    )


def _scenario(*, selected_slot_id: str = "slot-9", phase: str = "awaiting_confirmation") -> BookingScenario:
    return BookingScenario(
        id=uuid4(),
        kind="create",
        phase=phase,
        idempotency_key=f"create:{uuid4()}",
        customer_id="customer-7",
        state={
            "slot_query": {
                "service_ids": ["service-1"],
                "starts_after": "2026-07-25T00:00:00+00:00",
                "starts_before": "2026-07-26T00:00:00+00:00",
                "staff_id": "staff-1",
            },
            "selected_slot_id": selected_slot_id,
            "customer": {"phone": "+70000000007"},
            "service": {"id": "service-1", "name": "Криотерапия"},
        },
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


async def _wait_for_scenario_lock_or_completion(database_url, task) -> bool:
    watcher = await asyncpg.connect(database_url)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    try:
        while not task.done() and loop.time() < deadline:
            waiting = await watcher.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                      AND (
                          query LIKE '%booking_scenarios%FOR UPDATE%'
                          OR query LIKE '%pg_advisory_lock%'
                      )
                )
                """
            )
            if waiting:
                return True
            await asyncio.sleep(0.01)
        return False
    finally:
        await watcher.close()


async def test_create_requires_confirmation_then_is_stable_and_idempotent(repo):
    scenario = _scenario()
    await repo.create_scenario(scenario)
    port = CountingMockYclientsAdapter([_slot("slot-9", 14)])
    service = BookingService(port, repo, now=lambda: NOW + timedelta(minutes=1))

    result = await service.handle(scenario.id, confirmed=False)

    assert result.status == "needs_input"
    assert result.next_action == "confirm_booking"
    assert await repo.get_local_booking(scenario.id) is None
    assert (port.list_calls, port.create_calls) == (0, 0)

    confirmed = await service.handle(scenario.id, confirmed=True)
    repeated = await service.handle(scenario.id, confirmed=True)

    assert confirmed == repeated
    assert confirmed.status == "ok"
    assert (await repo.get_scenario(scenario.id)).phase == "confirmed"
    assert await repo.get_local_booking(scenario.id) is not None
    assert (port.list_calls, port.create_calls) == (1, 1)


async def test_lost_slot_returns_and_persists_three_json_safe_alternatives(repo):
    scenario = _scenario(selected_slot_id="slot-gone")
    await repo.create_scenario(scenario)
    port = CountingMockYclientsAdapter([_slot(f"slot-{hour}", hour) for hour in range(10, 14)])
    service = BookingService(port, repo, now=lambda: NOW + timedelta(minutes=2))

    result = await service.handle(scenario.id, confirmed=True)

    assert result.status == "needs_input"
    assert result.next_action == "choose_slot"
    assert len(result.events) == 1
    alternatives = result.events[0]["alternatives"]
    assert len(alternatives) == 3
    assert json.loads(json.dumps(alternatives)) == alternatives
    assert [item["id"] for item in alternatives] == ["slot-10", "slot-11", "slot-12"]
    assert port.create_calls == 0

    stored = await repo.get_scenario(scenario.id)
    events = await repo.list_events(scenario.id)
    assert stored.phase == "collecting"
    assert stored.state["customer"] == scenario.state["customer"]
    assert stored.state["service"] == scenario.state["service"]
    assert stored.state["slot_query"] == scenario.state["slot_query"]
    assert stored.state["selected_slot_id"] == "slot-gone"
    assert events[-1].event_type == "slot_unavailable"
    assert _thaw(events[-1].payload["alternatives"]) == alternatives


async def test_recovered_executing_scenario_escalates_without_port_call(repo):
    scenario = _scenario(phase="executing")
    await repo.create_scenario(scenario)
    port = CountingMockYclientsAdapter([_slot("slot-9", 14)])
    service = BookingService(port, repo, now=lambda: NOW + timedelta(minutes=3))

    result = await service.handle(scenario.id, confirmed=True)

    stored = await repo.get_scenario(scenario.id)
    assert result.status == "escalated"
    assert result.error_code == "booking_outcome_unknown"
    assert stored.phase == "escalated"
    assert stored.error_code == "booking_outcome_unknown"
    assert (port.list_calls, port.create_calls) == (0, 0)
    assert (await repo.list_events(scenario.id))[-1].event_type == "admin_attention_required"


async def test_concurrent_confirmation_is_serialized_across_repository_instances(
    migrated_database_url,
    repo_pair,
):
    first_repo, second_repo = repo_pair
    scenario = _scenario()
    await first_repo.create_scenario(scenario)
    port = BarrierCreateAdapter([_slot("slot-9", 14)])
    services = (
        BookingService(port, first_repo, now=lambda: NOW + timedelta(minutes=4)),
        BookingService(port, second_repo, now=lambda: NOW + timedelta(minutes=5)),
    )

    first = asyncio.create_task(services[0].handle(scenario.id, confirmed=True))
    await asyncio.wait_for(port.create_entered.wait(), timeout=2)
    second = asyncio.create_task(services[1].handle(scenario.id, confirmed=True))
    lock_waiting = await _wait_for_scenario_lock_or_completion(
        migrated_database_url,
        second,
    )
    port.release_create.set()
    results = await asyncio.gather(first, second)

    assert lock_waiting
    assert results[0] == results[1]
    assert results[0].status == "ok"
    assert port.create_calls == 1
    assert "admin_attention_required" not in [
        event.event_type for event in await first_repo.list_events(scenario.id)
    ]


async def test_slot_disappearing_during_create_uses_fresh_alternatives(repo):
    scenario = _scenario()
    await repo.create_scenario(scenario)
    port = SlotDisappearsOnCreateAdapter(
        [_slot("slot-9", 9), *[_slot(f"slot-{hour}", hour) for hour in range(10, 14)]]
    )
    service = BookingService(port, repo, now=lambda: NOW + timedelta(minutes=6))

    result = await service.handle(scenario.id, confirmed=True)

    stored = await repo.get_scenario(scenario.id)
    events = await repo.list_events(scenario.id)
    alternatives = result.events[0]["alternatives"]
    assert result.status == "needs_input"
    assert result.next_action == "choose_slot"
    assert [item["id"] for item in alternatives] == ["slot-10", "slot-11", "slot-12"]
    assert len(alternatives) <= 3
    assert (port.list_calls, port.create_calls) == (2, 1)
    assert stored.phase == "collecting"
    assert events[-1].event_type == "slot_unavailable"
    assert _thaw(events[-1].payload["alternatives"]) == alternatives
    assert "admin_attention_required" not in [event.event_type for event in events]


async def test_create_repeat_keeps_original_terminal_after_reschedule_and_cancel(repo):
    port = CountingMockYclientsAdapter(
        [_slot("slot-9", 14), _slot("slot-new", 16)]
    )
    create = _scenario()
    await repo.create_scenario(create)
    service = BookingService(port, repo, now=lambda: NOW)
    initial = await service.handle(create.id, confirmed=True)
    booking = await repo.get_local_booking(create.id)
    identity = BookingIdentity("customer-7", confirmed=True)
    reschedule = BookingScenario(
        id=uuid4(),
        kind="reschedule",
        phase="awaiting_confirmation",
        idempotency_key=f"reschedule:{uuid4()}",
        customer_id="customer-7",
        state={
            "external_id": booking.external_id,
            "starts_at": booking.starts_at.isoformat(),
            "slot_query": {
                "service_ids": ["service-1"],
                "starts_after": "2026-07-25T15:00:00+00:00",
                "starts_before": "2026-07-25T17:00:00+00:00",
                "staff_id": "staff-1",
            },
            "selected_slot_id": "slot-new",
        },
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.create_scenario(reschedule)
    assert (
        await service.handle(reschedule.id, confirmed=True, identity=identity)
    ).status == "ok"
    cancel = BookingScenario(
        id=uuid4(),
        kind="cancel",
        phase="awaiting_confirmation",
        idempotency_key=f"cancel:{uuid4()}",
        customer_id="customer-7",
        state={
            "external_id": booking.external_id,
            "starts_at": _slot("slot-new", 16).starts_at.isoformat(),
        },
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.create_scenario(cancel)
    assert (await service.handle(cancel.id, confirmed=True, identity=identity)).status == "ok"

    repeated = await service.handle(create.id, confirmed=True)
    stored = await repo.get_scenario(create.id)

    assert repeated == initial
    assert _slot("slot-9", 14).starts_at.isoformat() in repeated.message
    assert _slot("slot-new", 16).starts_at.isoformat() not in repeated.message
    assert stored.state["starts_at"] == _slot("slot-9", 14).starts_at.isoformat()
    assert stored.state["status"] == "confirmed"
    assert port.create_calls == 1
