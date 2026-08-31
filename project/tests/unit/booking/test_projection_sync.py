from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest

from moroz.booking.projection import (
    PROJECTION_SYNC_KIND,
    ProjectionSyncCoordinator,
    projection_job,
)
from moroz.booking.yclients_records import (
    ProjectionRecord,
    ProjectionSnapshot,
    YclientsProjectionError,
)
from moroz.notifications.models import JobResult


NOW = datetime(2026, 8, 14, 12, 7, tzinfo=UTC)


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    async def schedule(self, job):
        self.jobs.append(job)
        return True


class FakeRepository:
    def __init__(self, *, busy=False):
        self.busy = busy
        self.replaced = []

    @asynccontextmanager
    async def serialized(self):
        yield None if self.busy else object()

    async def replace(self, connection, snapshot):
        self.replaced.append((connection, snapshot))


class FakeReader:
    def __init__(self, result):
        self.result = result
        self.read_at = []

    async def read_window(self, now):
        self.read_at.append(now)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def projection_snapshot():
    return ProjectionSnapshot(
        records=(
            ProjectionRecord(
                external_id="record-1",
                booking_key=uuid4(),
                bot_marker_state="valid",
                starts_at=NOW,
                scheduled_end_at=None,
                status="confirmed",
                deleted=False,
                client_name="Иван",
                staff_name="Мария",
                service_names=("Солярий",),
                client_id="55",
                record_created_at=NOW - timedelta(days=1),
            ),
        ),
        synced_at=NOW,
    )


@pytest.mark.asyncio
async def test_ensure_current_is_idempotently_bucketed_and_run_schedules_next():
    scheduler = FakeScheduler()
    repository = FakeRepository()
    snapshot = projection_snapshot()
    reader = FakeReader(snapshot)
    coordinator = ProjectionSyncCoordinator(
        repository, reader, scheduler, clock=lambda: NOW
    )
    current = projection_job(NOW)
    next_job = projection_job(NOW + timedelta(minutes=10))

    await coordinator.ensure_current(NOW)
    await coordinator.ensure_current(NOW)
    result = await coordinator.run(current)

    assert result == JobResult.sent()
    assert [job.idempotency_key for job in scheduler.jobs] == [
        current.idempotency_key,
        current.idempotency_key,
        next_job.idempotency_key,
    ]
    assert reader.read_at == [NOW]
    assert [stored for _, stored in repository.replaced] == [snapshot]
    assert repository.replaced[0][1].records[0].client_id == "55"


@pytest.mark.asyncio
async def test_run_skips_when_projection_lock_is_busy_without_reading_provider():
    scheduler = FakeScheduler()
    repository = FakeRepository(busy=True)
    reader = FakeReader(projection_snapshot())
    coordinator = ProjectionSyncCoordinator(
        repository, reader, scheduler, clock=lambda: NOW
    )
    job = projection_job(NOW)

    assert await coordinator.run(job) == JobResult.skipped("projection_busy")
    assert reader.read_at == []
    assert repository.replaced == []
    assert [scheduled.idempotency_key for scheduled in scheduler.jobs] == [
        projection_job(NOW + timedelta(minutes=10)).idempotency_key
    ]


@pytest.mark.asyncio
async def test_reader_failure_schedules_next_bucket_before_propagating_safe_error():
    scheduler = FakeScheduler()
    reader = FakeReader(YclientsProjectionError("yclients_http_status"))
    coordinator = ProjectionSyncCoordinator(
        FakeRepository(), reader, scheduler, clock=lambda: NOW
    )
    job = projection_job(NOW)

    with pytest.raises(YclientsProjectionError, match="^yclients_http_status$"):
        await coordinator.run(job)

    assert [scheduled.idempotency_key for scheduled in scheduler.jobs] == [
        projection_job(NOW + timedelta(minutes=10)).idempotency_key
    ]


def test_projection_job_uses_utc_ten_minute_bucket_and_no_booking_fields():
    job = projection_job(NOW)

    assert job.kind == PROJECTION_SYNC_KIND
    assert job.run_at == datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    assert job.payload == MappingProxyType({})
    assert job.booking_key is None
    assert job.booking_starts_at is None
