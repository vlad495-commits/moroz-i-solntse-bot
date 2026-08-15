from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import asyncpg

from moroz.booking.yclients_catalog import CatalogSnapshot, YclientsCatalogError
from moroz.common.db import Database
from moroz.notifications.models import JobResult, PlannedSchedulerJob
from moroz.notifications.repository import SchedulerJobRepository


CATALOG_LOCK = "yclients_service_catalog:v1"
CATALOG_SYNC_KIND = "yclients_service_catalog_sync"


class CatalogRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def serialized(self):
        async with self._database.acquire() as connection:
            try:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    CATALOG_LOCK,
                )
            except asyncpg.PostgresError as error:
                raise YclientsCatalogError("yclients_catalog_write") from error
            try:
                yield connection if locked else None
            finally:
                if locked:
                    try:
                        await connection.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            CATALOG_LOCK,
                        )
                    except asyncpg.PostgresError as error:
                        raise YclientsCatalogError(
                            "yclients_catalog_write"
                        ) from error

    async def replace(self, connection, snapshot: CatalogSnapshot) -> None:
        rows = [
            (
                record.service_id,
                record.staff_id,
                record.service_name,
                record.category_name,
                record.staff_name,
                record.price_min,
                record.price_max,
                record.duration_minutes,
                snapshot.synced_at,
            )
            for record in snapshot.records
        ]
        try:
            async with connection.transaction():
                await connection.execute("DELETE FROM yclients_service_catalog")
                if rows:
                    await connection.executemany(
                        """
                        INSERT INTO yclients_service_catalog
                            (service_id, staff_id, service_name, category_name,
                             staff_name, price_min, price_max,
                             duration_minutes, synced_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        rows,
                    )
        except asyncpg.PostgresError as error:
            raise YclientsCatalogError("yclients_catalog_write") from error


def catalog_job(now: datetime) -> PlannedSchedulerJob:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    bucket = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=CATALOG_SYNC_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{CATALOG_SYNC_KIND}:{bucket.isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


class CatalogSyncCoordinator:
    def __init__(
        self,
        repository: CatalogRepository,
        reader,
        scheduler: SchedulerJobRepository,
        *,
        clock,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._scheduler = scheduler
        self._clock = clock

    async def ensure_current(self, now: datetime) -> None:
        await self._scheduler.schedule(catalog_job(now))

    async def run(self, job: PlannedSchedulerJob) -> JobResult:
        await self._scheduler.schedule(catalog_job(job.run_at + timedelta(hours=1)))
        async with self._repository.serialized() as connection:
            if connection is None:
                return JobResult.skipped("catalog_busy")
            snapshot = await self._reader.read(self._clock())
            await self._repository.replace(connection, snapshot)
        return JobResult.sent()
