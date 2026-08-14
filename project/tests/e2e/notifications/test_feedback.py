from datetime import datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.notifications.feedback import FeedbackService


pytest_plugins = ("tests.integration.conftest",)
pytestmark = pytest.mark.asyncio
MOSCOW = ZoneInfo("Europe/Moscow")


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=2)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def service(database):
    return FeedbackService(database)


async def test_completed_visit_schedules_feedback_two_hours_later(database, service):
    booking_key = uuid4()
    completed_at = datetime(2026, 7, 28, 13, 0, tzinfo=MOSCOW)

    job_id = await service.schedule_after_visit(
        customer_id="customer-7",
        booking_key=booking_key,
        completed_at=completed_at,
    )

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT job.kind, job.run_at, job.booking_key, feedback.customer_id
            FROM scheduler_jobs AS job
            JOIN notification_feedback_requests AS feedback
              ON feedback.booking_key = job.booking_key
            WHERE job.id = $1
            """,
            job_id,
        )
    assert tuple(row.values()) == (
        "feedback_request",
        completed_at + timedelta(hours=2),
        booking_key,
        "customer-7",
    )


async def test_feedback_after_21_moves_to_next_day_morning(database, service):
    completed_at = datetime(2026, 7, 28, 20, 30, tzinfo=MOSCOW)

    job_id = await service.schedule_after_visit(
        customer_id="customer-7",
        booking_key=uuid4(),
        completed_at=completed_at,
    )

    async with database.acquire() as connection:
        run_at = await connection.fetchval(
            "SELECT run_at FROM scheduler_jobs WHERE id = $1",
            job_id,
        )
    assert run_at == datetime.combine(
        completed_at.date() + timedelta(days=1),
        time(10, 30),
        tzinfo=MOSCOW,
    )


async def test_feedback_is_scheduled_once_per_customer(database, service):
    first = await service.schedule_after_visit(
        customer_id="customer-7",
        booking_key=uuid4(),
        completed_at=datetime(2026, 7, 28, 13, 0, tzinfo=MOSCOW),
    )
    second = await service.schedule_after_visit(
        customer_id="customer-7",
        booking_key=uuid4(),
        completed_at=datetime(2026, 7, 29, 13, 0, tzinfo=MOSCOW),
    )

    async with database.acquire() as connection:
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM notification_feedback_requests) AS requests,
                (SELECT count(*) FROM scheduler_jobs
                 WHERE kind = 'feedback_request') AS jobs
            """
        )
    assert first is not None
    assert second is None
    assert tuple(counts.values()) == (1, 1)


async def test_low_rating_creates_escalation_and_human_mode(database, service):
    booking_key = uuid4()

    escalation_id = await service.record_rating(
        customer_id="customer-7",
        booking_key=booking_key,
        rating=2,
    )

    async with database.acquire() as connection:
        escalation = await connection.fetchrow(
            "SELECT status, reason_code, booking_key FROM escalations WHERE id = $1",
            escalation_id,
        )
        human_mode = await connection.fetchrow(
            "SELECT enabled, reason_code, escalation_id FROM human_mode WHERE customer_id = $1",
            "customer-7",
        )
    assert tuple(escalation.values()) == ("open", "low_feedback_rating", booking_key)
    assert tuple(human_mode.values()) == (True, "low_feedback_rating", escalation_id)


async def test_recent_handoff_cooldown_suppresses_repeat_escalation(
    database,
    service,
):
    previous_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id,
                 enabled_at, expires_at)
            VALUES (
                'customer-7', false, 'low_feedback_rating', $1,
                now() - interval '1 minute', now() + interval '5 minutes'
            )
            """,
            previous_id,
        )

    result = await service.record_rating(
        customer_id="customer-7",
        booking_key=uuid4(),
        rating=2,
    )

    async with database.acquire() as connection:
        count = await connection.fetchval("SELECT count(*) FROM escalations")
        mode = await connection.fetchrow(
            "SELECT enabled, escalation_id FROM human_mode "
            "WHERE customer_id='customer-7'"
        )
    assert result is None
    assert count == 0
    assert tuple(mode.values()) == (False, previous_id)


async def test_expired_handoff_cooldown_allows_escalation(database, service):
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id,
                 enabled_at, expires_at)
            VALUES (
                'customer-7', false, 'low_feedback_rating', $1,
                now() - interval '10 minutes', now() - interval '5 minutes'
            )
            """,
            uuid4(),
        )

    escalation_id = await service.record_rating(
        customer_id="customer-7",
        booking_key=uuid4(),
        rating=2,
    )

    async with database.acquire() as connection:
        mode = await connection.fetchrow(
            "SELECT enabled, escalation_id, expires_at FROM human_mode "
            "WHERE customer_id='customer-7'"
        )
    assert escalation_id is not None
    assert tuple(mode.values()) == (True, escalation_id, None)
