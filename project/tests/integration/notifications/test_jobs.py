import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.notifications.repository import SchedulerJobRepository


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=2, max_size=2)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def test_concurrent_claimers_do_not_claim_the_same_due_job(database):
    due_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    due_ids = [uuid4(), uuid4(), uuid4()]
    future_id = uuid4()
    async with database.acquire() as connection:
        for index, job_id in enumerate(due_ids):
            await connection.execute(
                """
                INSERT INTO scheduler_jobs
                    (id, kind, run_at, payload, idempotency_key, status,
                     attempts, created_at, updated_at)
                VALUES ($1, 'booking_created', $2, '{}'::jsonb, $3,
                        'pending', 0, $2, $2)
                """,
                job_id,
                due_at,
                f"due:{index}",
            )
        await connection.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status,
                 attempts, created_at, updated_at)
            VALUES ($1, 'booking_created', $2, '{}'::jsonb, 'future',
                    'pending', 0, $3, $3)
            """,
            future_id,
            due_at + timedelta(hours=1),
            due_at,
        )

    repository = SchedulerJobRepository(database)
    first, second = await asyncio.gather(
        repository.claim_due(limit=2, now=due_at + timedelta(minutes=1)),
        repository.claim_due(limit=2, now=due_at + timedelta(minutes=1)),
    )
    claimed_ids = [job.id for job in [*first, *second]]

    assert sorted(claimed_ids) == sorted(due_ids)
    assert len(claimed_ids) == len(set(claimed_ids))
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM scheduler_jobs WHERE id = $1",
            future_id,
        ) == "pending"


async def test_claimed_job_can_be_released_and_completed(database):
    due_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    job_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status,
                 attempts, created_at, updated_at)
            VALUES ($1, 'booking_created', $2, '{}'::jsonb, 'release:1',
                    'pending', 0, $2, $2)
            """,
            job_id,
            due_at,
        )

    repository = SchedulerJobRepository(database)
    claimed = await repository.claim_due(limit=1, now=due_at + timedelta(minutes=1))
    assert [job.id for job in claimed] == [job_id]
    await repository.release_claim(job_id)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM scheduler_jobs WHERE id = $1", job_id
        ) == "pending"

    claimed = await repository.claim_due(limit=1, now=due_at + timedelta(minutes=1))
    job = await repository.get_claimed(job_id)
    await repository.complete(job, result_status="finished")

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status, finished_at IS NOT NULL AS finished FROM scheduler_jobs WHERE id = $1",
            job_id,
        )
    assert [item.id for item in claimed] == [job_id]
    assert row["status"] == "finished"
    assert row["finished"] is True


async def test_failed_attempt_stays_retryable_until_terminal_dlq(database):
    due_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    job_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status,
                 attempts, created_at, updated_at)
            VALUES ($1, 'booking_created', $2, '{}'::jsonb, 'failure:1',
                    'pending', 0, $2, $2)
            """,
            job_id,
            due_at,
        )

    repository = SchedulerJobRepository(database)
    [job] = await repository.claim_due(
        limit=1,
        now=due_at + timedelta(minutes=1),
    )
    await repository.record_failure(
        job,
        error_code="RuntimeError",
        terminal=False,
    )

    retry = await repository.get_claimed(job_id)
    assert retry.attempts == 1

    await repository.record_failure(
        retry,
        error_code="RuntimeError",
        terminal=True,
    )

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT status, attempts, last_error_code,
                   finished_at IS NOT NULL AS finished
            FROM scheduler_jobs
            WHERE id = $1
            """,
            job_id,
        )
    assert tuple(row.values()) == ("failed", 2, "RuntimeError", True)
