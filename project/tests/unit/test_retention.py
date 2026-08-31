from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from moroz.notifications.models import JobResult
from moroz.retention import (
    RETENTION_CLEANUP_KIND,
    RetentionCleanupError,
    RetentionCleanupCoordinator,
    delete_expired_records,
    retention_job,
)


NOW = datetime(2026, 8, 18, 19, 22, tzinfo=UTC)


class Scheduler:
    def __init__(self):
        self.jobs = []

    async def schedule(self, job):
        if all(
            existing.idempotency_key != job.idempotency_key
            for existing in self.jobs
        ):
            self.jobs.append(job)
        return True


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Connection:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return Transaction()

    async def execute(self, query, retention_days):
        self.queries.append((query, retention_days))
        counts = {
            "messages": 2,
            "token_usage": 3,
            "reactivation_journeys": 4,
            "customer_activity_projection": 5,
            "marketing_consent_events": 6,
        }
        table = next(name for name in counts if f"DELETE FROM {name}" in query)
        return f"DELETE {counts[table]}"


class Database:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def test_retention_job_uses_one_utc_day_bucket_without_payload():
    job = retention_job(NOW)

    assert job.kind == RETENTION_CLEANUP_KIND
    assert job.run_at == datetime(2026, 8, 18, tzinfo=UTC)
    assert job.payload == MappingProxyType({})
    assert job.idempotency_key == "retention_cleanup:2026-08-18"
    assert job.booking_key is None
    assert job.booking_starts_at is None


@pytest.mark.asyncio
async def test_positive_retention_schedules_current_and_next_day_and_cleans():
    scheduler = Scheduler()
    connection = Connection()
    coordinator = RetentionCleanupCoordinator(
        Database(connection), scheduler, retention_days=1095
    )

    await coordinator.ensure_current(NOW)
    result = await coordinator.run(retention_job(NOW))

    assert result == JobResult.sent()
    assert [job.idempotency_key for job in scheduler.jobs] == [
        "retention_cleanup:2026-08-18",
        "retention_cleanup:2026-08-19",
        "retention_cleanup:2026-08-20",
    ]
    assert [days for _, days in connection.queries] == [1095] * 5


@pytest.mark.asyncio
async def test_schedule_failure_uses_safe_retention_error_and_keeps_runway():
    class FailingScheduler(Scheduler):
        async def schedule(self, job):
            raise RuntimeError("database detail")

    coordinator = RetentionCleanupCoordinator(
        Database(Connection()), FailingScheduler(), retention_days=1095
    )

    with pytest.raises(RetentionCleanupError) as raised:
        await coordinator.run(retention_job(NOW))

    assert raised.value.code == "retention_cleanup_failed"


@pytest.mark.asyncio
async def test_disabled_retention_does_not_schedule_or_delete():
    scheduler = Scheduler()
    connection = Connection()
    coordinator = RetentionCleanupCoordinator(
        Database(connection), scheduler, retention_days=0
    )

    await coordinator.ensure_current(NOW)
    result = await coordinator.run(retention_job(NOW))

    assert result == JobResult.skipped("retention_disabled")
    assert scheduler.jobs == []
    assert connection.queries == []


@pytest.mark.asyncio
async def test_shared_delete_contract_returns_only_counts():
    connection = Connection()

    assert await delete_expired_records(connection, 1095) == {
        "messages": 2,
        "token_usage": 3,
        "reactivation_journeys": 4,
        "customer_activity_projection": 5,
        "marketing_consent_events": 6,
    }
    assert all("LIMIT 1000" in query for query, _ in connection.queries)


def test_legacy_cleanup_delegates_to_shared_retention_contract():
    source = Path("/workspace/llm/db.py").read_text(encoding="utf-8")

    assert "from moroz.retention import delete_expired_records" in source
    assert "return await delete_expired_records(conn, DATA_RETENTION_DAYS)" in source
    assert 'f"DELETE FROM {table}' not in source
