import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from moroz.booking.models import ExternalBooking, GetBooking
from moroz.notifications.lifecycle import LifecycleService


BOOKING_KEY = UUID("00000000-0000-0000-0000-000000000007")
STARTS_AT = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
END_AT = STARTS_AT + timedelta(hours=1)


class FakeConnection:
    def __init__(self):
        self.row = None
        self.executions = []

    async def fetchrow(self, query, *args):
        self.executions.append((query, args))
        return self.row

    async def execute(self, query, *args):
        self.executions.append((query, args))
        return "INSERT 0 1"


class FakeDatabase:
    def __init__(self):
        self.connection = FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class FakeProvider:
    def __init__(self, booking):
        self.booking = booking
        self.commands = []

    async def get_booking(self, command):
        self.commands.append(command)
        return self.booking


class FakeFeedback:
    def __init__(self):
        self.calls = []
        self.job_id = uuid4()

    async def schedule_after_visit(self, **kwargs):
        self.calls.append(kwargs)
        return self.job_id


def local_booking(*, status="confirmed", scheduled_end_at=END_AT):
    return ExternalBooking(
        external_id="9001",
        customer_id="customer-7",
        booking_key=BOOKING_KEY,
        slot_id="slot-1",
        starts_at=STARTS_AT,
        status=status,
        scheduled_end_at=scheduled_end_at,
    )


def _row(booking):
    return {
        "external_id": booking.external_id,
        "customer_id": booking.customer_id,
        "booking_key": booking.booking_key,
        "slot_id": booking.slot_id,
        "starts_at": booking.starts_at,
        "status": booking.status,
        "scheduled_end_at": booking.scheduled_end_at,
    }


@pytest.mark.asyncio
async def test_refresh_uses_exact_owned_get_command():
    database = FakeDatabase()
    provider_booking = local_booking(status="completed")
    database.connection.row = _row(provider_booking)
    provider = FakeProvider(provider_booking)
    service = LifecycleService(database, provider, FakeFeedback())

    refreshed = await service.refresh(local_booking())

    assert provider.commands == [
        GetBooking(
            external_id="9001",
            customer_id="customer-7",
            booking_key=BOOKING_KEY,
        )
    ]
    assert refreshed.status == "completed"


@pytest.mark.asyncio
async def test_terminal_local_status_skips_provider():
    provider = FakeProvider(local_booking(status="completed"))
    local = local_booking(status="no_show")
    service = LifecycleService(FakeDatabase(), provider, FakeFeedback())

    assert await service.refresh(local) == local
    assert provider.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_index", "next_index"),
    [(-1, 0), (0, 1), (1, 2)],
)
async def test_schedule_next_uses_fixed_end_relative_offsets(
    current_index, next_index
):
    database = FakeDatabase()
    service = LifecycleService(database, FakeProvider(local_booking()), FakeFeedback())

    assert await service.schedule_next(local_booking(status="completed"), current_index)

    _, args = database.connection.executions[0]
    assert args[2] == END_AT + (timedelta(minutes=15), timedelta(hours=2), timedelta(hours=24))[next_index]
    assert json.loads(args[3])["outcome_check_index"] == next_index
    assert args[4] == f"booking:{BOOKING_KEY}:{STARTS_AT.isoformat()}:outcome:{next_index}"


@pytest.mark.asyncio
async def test_schedule_next_stops_after_last_offset():
    service = LifecycleService(FakeDatabase(), FakeProvider(local_booking()), FakeFeedback())

    assert not await service.schedule_next(local_booking(status="completed"), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("current_index", [-2, 3])
async def test_schedule_next_rejects_out_of_range_current_index(current_index):
    service = LifecycleService(FakeDatabase(), FakeProvider(local_booking()), FakeFeedback())

    with pytest.raises(ValueError, match="current index"):
        await service.schedule_next(local_booking(status="completed"), current_index)


@pytest.mark.asyncio
async def test_schedule_next_requires_persisted_scheduled_end():
    service = LifecycleService(FakeDatabase(), FakeProvider(local_booking()), FakeFeedback())

    with pytest.raises(RuntimeError, match="scheduled end"):
        await service.schedule_next(
            local_booking(scheduled_end_at=None),
            -1,
        )


@pytest.mark.asyncio
async def test_schedule_feedback_uses_persisted_scheduled_end():
    feedback = FakeFeedback()
    service = LifecycleService(FakeDatabase(), FakeProvider(local_booking()), feedback)

    assert await service.schedule_feedback(local_booking(status="completed")) == feedback.job_id
    assert feedback.calls == [
        {
            "customer_id": "customer-7",
            "booking_key": BOOKING_KEY,
            "completed_at": END_AT,
        }
    ]
