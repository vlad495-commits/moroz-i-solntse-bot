from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest

from moroz.booking.catalog import (
    CATALOG_SYNC_KIND,
    CatalogSyncCoordinator,
    catalog_job,
)
from moroz.booking.yclients_catalog import (
    CatalogRecord,
    CatalogSnapshot,
    YclientsCatalogError,
)
from moroz.notifications.models import JobResult


NOW = datetime(2026, 8, 15, 12, 59, tzinfo=UTC)


def snapshot():
    return CatalogSnapshot(
        (
            CatalogRecord(
                "20", "10", "Криотерапия", "Крио", "Анна",
                Decimal("1230.00"), Decimal("1500.00"), 3,
            ),
        ),
        NOW,
    )


class Scheduler:
    def __init__(self):
        self.jobs = []

    async def schedule(self, job):
        self.jobs.append(job)
        return True


class Repository:
    def __init__(self, *, busy=False):
        self.busy = busy
        self.replaced = []

    @asynccontextmanager
    async def serialized(self):
        yield None if self.busy else object()

    async def replace(self, connection, value):
        self.replaced.append((connection, value))


class Reader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def read(self, now):
        self.calls.append(now)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def coordinator(repository=None, reader=None, scheduler=None):
    return CatalogSyncCoordinator(
        repository or Repository(),
        reader or Reader(snapshot()),
        scheduler or Scheduler(),
        clock=lambda: NOW,
    )


def test_catalog_job_uses_utc_hour_bucket_and_no_booking_fields():
    job = catalog_job(NOW)

    assert job.kind == CATALOG_SYNC_KIND
    assert job.run_at == datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    assert job.payload == MappingProxyType({})
    assert job.idempotency_key == (
        "yclients_service_catalog_sync:2026-08-15T12:00:00+00:00"
    )
    assert job.booking_key is None
    assert job.booking_starts_at is None


@pytest.mark.asyncio
async def test_ensure_current_and_run_schedule_current_and_next_hour():
    scheduler = Scheduler()
    repository = Repository()
    reader = Reader(snapshot())
    sync = coordinator(repository, reader, scheduler)
    current = catalog_job(NOW)
    next_job = catalog_job(NOW + timedelta(hours=1))

    await sync.ensure_current(NOW)
    result = await sync.run(current)

    assert result == JobResult.sent()
    assert [job.idempotency_key for job in scheduler.jobs] == [
        current.idempotency_key,
        next_job.idempotency_key,
    ]
    assert reader.calls == [NOW]
    assert [value for _, value in repository.replaced] == [snapshot()]


@pytest.mark.asyncio
async def test_busy_lock_skips_without_provider_read_but_schedules_next():
    scheduler = Scheduler()
    repository = Repository(busy=True)
    reader = Reader(snapshot())
    sync = coordinator(repository, reader, scheduler)

    assert await sync.run(catalog_job(NOW)) == JobResult.skipped("catalog_busy")
    assert reader.calls == []
    assert repository.replaced == []
    assert scheduler.jobs == [catalog_job(NOW + timedelta(hours=1))]


@pytest.mark.asyncio
async def test_reader_error_propagates_after_next_hour_is_scheduled():
    scheduler = Scheduler()
    reader = Reader(YclientsCatalogError("yclients_catalog_http_status"))
    sync = coordinator(Repository(), reader, scheduler)

    with pytest.raises(YclientsCatalogError, match="^yclients_catalog_http_status$"):
        await sync.run(catalog_job(NOW))

    assert scheduler.jobs == [catalog_job(NOW + timedelta(hours=1))]
