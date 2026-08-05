import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.booking.models import ExternalBooking
from moroz.common.db import Database
from moroz.notifications.feedback import FeedbackService
from moroz.notifications.lifecycle import LifecycleService
from moroz.notifications.ports import LocalBookingPort


pytestmark = pytest.mark.asyncio
STARTS_AT = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
END_AT = STARTS_AT + timedelta(hours=1)


class PausingProvider:
    def __init__(self, booking):
        self.booking = booking
        self.commands = []
        self.read = asyncio.Event()
        self.resume = asyncio.Event()

    async def get_booking(self, command):
        self.commands.append(command)
        self.read.set()
        await self.resume.wait()
        return self.booking


class ImmediateProvider:
    def __init__(self, booking):
        self.booking = booking
        self.commands = []

    async def get_booking(self, command):
        self.commands.append(command)
        return self.booking


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=2, max_size=2)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def booking(*, booking_key, status="confirmed", starts_at=STARTS_AT, scheduled_end_at=END_AT):
    return ExternalBooking(
        external_id="9001",
        customer_id="customer-7",
        booking_key=booking_key,
        slot_id="slot-1",
        service_ids=("service-1", "service-2"),
        staff_id="staff-7",
        starts_at=starts_at,
        status=status,
        scheduled_end_at=scheduled_end_at,
    )


async def seed_booking(database, local):
    scenario_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO booking_scenarios
                (id, kind, phase, idempotency_key, customer_id, state)
            VALUES ($1, 'create', 'confirmed', $2, $3, '{}'::jsonb)
            """,
            scenario_id,
            f"lifecycle:{scenario_id}",
            local.customer_id,
        )
        await connection.execute(
            """
            INSERT INTO bookings
                (id, last_scenario_id, external_id, customer_id, booking_key,
                 slot_id, starts_at, scheduled_end_at, status, snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            """,
            uuid4(),
            scenario_id,
            local.external_id,
            local.customer_id,
            local.booking_key,
            local.slot_id,
            local.starts_at,
            local.scheduled_end_at,
            local.status,
            json.dumps({
                "service_ids": list(local.service_ids),
                "staff_id": local.staff_id,
            }),
        )


async def booking_status(database, booking_key):
    async with database.acquire() as connection:
        return await connection.fetchval(
            "SELECT status FROM bookings WHERE booking_key = $1",
            booking_key,
        )


async def test_refresh_persists_completed_status_and_scheduled_end(database):
    booking_key = uuid4()
    local = booking(booking_key=booking_key, scheduled_end_at=None)
    completed = booking(booking_key=booking_key, status="completed")
    await seed_booking(database, local)
    provider = ImmediateProvider(completed)
    service = LifecycleService(database, provider, FeedbackService(database))

    refreshed = await service.refresh(local)

    assert refreshed == completed
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status, scheduled_end_at, snapshot FROM bookings WHERE booking_key = $1",
            booking_key,
        )
    assert row["status"] == "completed"
    assert row["scheduled_end_at"] == END_AT
    snapshot = json.loads(row["snapshot"])
    assert snapshot["status"] == "completed"
    assert snapshot["service_ids"] == ["service-1", "service-2"]
    assert snapshot["staff_id"] == "staff-7"


async def test_refresh_does_not_overwrite_concurrent_cancel(database):
    booking_key = uuid4()
    local = booking(booking_key=booking_key)
    await seed_booking(database, local)
    provider = PausingProvider(booking(booking_key=booking_key, status="completed"))
    service = LifecycleService(database, provider, FeedbackService(database))

    refresh = asyncio.create_task(service.refresh(local))
    await provider.read.wait()
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE booking_key = $1",
            booking_key,
        )
    provider.resume.set()

    assert await refresh is None
    assert await booking_status(database, booking_key) == "cancelled"


async def test_refresh_does_not_overwrite_concurrent_unknown(database):
    booking_key = uuid4()
    local = booking(booking_key=booking_key)
    await seed_booking(database, local)
    provider = PausingProvider(booking(booking_key=booking_key, status="completed"))
    service = LifecycleService(database, provider, FeedbackService(database))

    refresh = asyncio.create_task(service.refresh(local))
    await provider.read.wait()
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE bookings SET status = 'unknown' WHERE booking_key = $1",
            booking_key,
        )
    provider.resume.set()

    assert await refresh is None
    assert await booking_status(database, booking_key) == "unknown"


async def test_refresh_rejects_stale_provider_response_after_reschedule(database):
    booking_key = uuid4()
    local = booking(booking_key=booking_key)
    await seed_booking(database, local)
    provider = PausingProvider(booking(booking_key=booking_key, status="completed"))
    service = LifecycleService(database, provider, FeedbackService(database))

    refresh = asyncio.create_task(service.refresh(local))
    await provider.read.wait()
    moved_starts_at = STARTS_AT + timedelta(days=1)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE bookings SET starts_at = $2 WHERE booking_key = $1",
            booking_key,
            moved_starts_at,
        )
    provider.resume.set()

    assert await refresh is None
    assert await booking_status(database, booking_key) == "confirmed"


async def test_duplicate_schedule_next_creates_one_job(database):
    local = booking(booking_key=uuid4(), status="completed")
    service = LifecycleService(
        database,
        ImmediateProvider(local),
        FeedbackService(database),
    )

    assert await asyncio.gather(
        service.schedule_next(local, -1),
        service.schedule_next(local, -1),
    ) == [True, True]

    async with database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT run_at, payload, idempotency_key
            FROM scheduler_jobs
            WHERE booking_key = $1 AND kind = 'visit_outcome_check'
            """,
            local.booking_key,
        )
    assert len(rows) == 1
    assert rows[0]["run_at"] == END_AT + timedelta(minutes=15)
    assert json.loads(rows[0]["payload"])["outcome_check_index"] == 0
    assert rows[0]["idempotency_key"] == (
        f"booking:{local.booking_key}:{STARTS_AT.isoformat()}:outcome:0"
    )


async def test_completed_duplicate_uses_persisted_end_without_provider_get(database):
    local = booking(booking_key=uuid4(), status="completed")
    await seed_booking(database, local)
    provider = ImmediateProvider(local)
    service = LifecycleService(database, provider, FeedbackService(database))

    persisted = await LocalBookingPort(database).get_booking(local.booking_key)
    assert persisted is not None
    assert await service.refresh(persisted) == persisted
    assert await service.schedule_feedback(persisted) is not None
    assert provider.commands == []
