import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from moroz.booking.models import BookingOutcomeUnknown, Slot
from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository
from moroz.notifications.handlers import handle_scheduler_job
from moroz.notifications.models import JobResult
from moroz.notifications.ports import LocalBookingPort, NotificationOutbox
from moroz.notifications.repository import SchedulerJobRepository
from tests.e2e.booking.test_telegram_create_flow import (
    _callback,
    _click,
    _process,
    _ready_confirmation,
    _runtime,
)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    value = Database(migrated_database_url, min_size=1, max_size=10)
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


async def _jobs(database, owner: str):
    async with database.acquire() as connection:
        return await connection.fetch(
            """
            SELECT j.kind, j.booking_starts_at, j.status,
                   j.idempotency_key
            FROM scheduler_jobs AS j
            JOIN bookings AS b ON b.booking_key = j.booking_key
            WHERE b.customer_id = $1
            ORDER BY j.booking_starts_at, j.kind
            """,
            owner,
        )


async def test_telegram_create_reschedule_cancel_replaces_jobs_without_duplicates(
    database,
):
    runtime = _runtime(database)
    runtime.port._slots["slot-b"] = Slot(
        "slot-b",
        ("1", "2"),
        "7",
        datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        60,
    )
    owner = "751"
    summary = await _ready_confirmation(runtime, database, owner, "reminders")

    created = await _click(
        runtime, owner, "reminders-confirm-create", summary, "Подтвердить"
    )

    assert created.text == "Запись подтверждена."
    created_jobs = await _jobs(database, owner)
    assert {row["kind"] for row in created_jobs} == {
        "booking_created",
        "morning_hour_before",
        "no_show_check",
    }
    assert {row["status"] for row in created_jobs} == {"pending"}
    assert len({row["idempotency_key"] for row in created_jobs}) == len(created_jobs)

    selection = await _process(
        runtime,
        owner=owner,
        update_id="reminders-reschedule",
        text="/reschedule",
    )
    reply = await _click(
        runtime, owner, "reminders-select-booking", selection, "02.08"
    )
    reply = await _click(runtime, owner, "reminders-move-master", reply, "Любой")
    reply = await _click(runtime, owner, "reminders-move-date", reply, "03.08")
    reply = await _click(runtime, owner, "reminders-move-slot", reply, "13:00")
    moved = await _click(
        runtime, owner, "reminders-confirm-move", reply, "Подтвердить"
    )
    replayed_move = await _click(
        runtime, owner, "reminders-replay-move", reply, "Подтвердить"
    )

    assert moved.text == replayed_move.text == "Запись перенесена."
    moved_jobs = await _jobs(database, owner)
    old_start = datetime(2026, 8, 2, 7, 0, tzinfo=UTC)
    new_start = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    assert {
        row["status"] for row in moved_jobs if row["booking_starts_at"] == old_start
    } == {"skipped"}
    assert {
        row["status"] for row in moved_jobs if row["booking_starts_at"] == new_start
    } == {"pending"}
    assert len({row["idempotency_key"] for row in moved_jobs}) == len(moved_jobs)

    cancel = await _process(
        runtime,
        owner=owner,
        update_id="reminders-cancel",
        text="/cancel",
    )
    cancel = await _click(
        runtime, owner, "reminders-cancel-select", cancel, "03.08"
    )
    cancelled = await _click(
        runtime,
        owner,
        "reminders-confirm-cancel",
        cancel,
        "Да, отменить запись",
    )
    replayed_cancel = await _click(
        runtime,
        owner,
        "reminders-replay-cancel",
        cancel,
        "Да, отменить запись",
    )

    assert cancelled.text == replayed_cancel.text == "Запись отменена."
    assert {row["status"] for row in await _jobs(database, owner)} == {"skipped"}


async def test_foreign_confirmation_callback_schedules_nothing(database):
    runtime = _runtime(database)
    summary = await _ready_confirmation(runtime, database, "752", "foreign-job")

    foreign = await _process(
        runtime,
        owner="753",
        update_id="foreign-job-confirm",
        kind="callback",
        data={"callback_data": _callback(summary, "Подтвердить")},
    )

    assert "истёк" in foreign.text.casefold()
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM bookings") == 0
        assert await connection.fetchval("SELECT count(*) FROM scheduler_jobs") == 0


async def test_create_outcome_unknown_schedules_nothing(database):
    runtime = _runtime(database)
    runtime.port.create_booking = AsyncMock(side_effect=BookingOutcomeUnknown())
    summary = await _ready_confirmation(runtime, database, "754", "unknown-job")

    result = await _click(
        runtime, "754", "unknown-job-confirm", summary, "Подтвердить"
    )

    assert "провер" in result.text.casefold()
    assert "подтверждена" not in result.text.casefold()
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM bookings") == 0
        assert await connection.fetchval("SELECT count(*) FROM scheduler_jobs") == 0
        escalation = await connection.fetchrow(
            "SELECT status, reason_code FROM escalations WHERE source = 'booking'"
        )
    assert dict(escalation) == {
        "status": "open",
        "reason_code": "booking_outcome_unknown",
    }


async def test_immediate_reminder_replay_delivers_once_to_booking_owner(database):
    runtime = _runtime(database)
    owner = "755"
    summary = await _ready_confirmation(runtime, database, owner, "delivery-job")
    await _click(
        runtime, owner, "delivery-job-confirm", summary, "Подтвердить"
    )
    jobs = await SchedulerJobRepository(database).claim_due(
        limit=10,
        now=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
    )

    assert [job.kind for job in jobs] == ["booking_created"]
    job = jobs[0]
    outbox = NotificationOutbox(MessageRepository(database))
    booking_port = LocalBookingPort(database)

    first = await handle_scheduler_job(
        job,
        booking_port=booking_port,
        outbox=outbox,
    )
    replay = await handle_scheduler_job(
        job,
        booking_port=booking_port,
        outbox=outbox,
    )

    assert first == replay == JobResult.sent()
    async with database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT chat_id, idempotency_key
            FROM outbound_messages
            WHERE idempotency_key LIKE 'notification:%'
            """
        )
    assert len(rows) == 1
    assert rows[0]["chat_id"] == owner
    assert rows[0]["idempotency_key"].endswith(":booking_created")
