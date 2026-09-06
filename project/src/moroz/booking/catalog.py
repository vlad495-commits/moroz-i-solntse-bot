from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import asyncpg

from moroz.booking.yclients_catalog import (
    CatalogRecord,
    CatalogSnapshot,
    YclientsCatalogError,
)
from moroz.common.db import Database
from moroz.notifications.models import JobResult, PlannedSchedulerJob
from moroz.notifications.repository import SchedulerJobRepository


CATALOG_LOCK = "yclients_service_catalog:v1"
CATALOG_SYNC_KIND = "yclients_service_catalog_sync"
CATALOG_MAX_AGE = timedelta(hours=24)
@dataclass(frozen=True, slots=True)
class CatalogVariant:
    staff_id: str
    staff_name: str
    price_min: Decimal
    price_max: Decimal
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class CatalogService:
    service_id: str
    service_name: str
    category_name: str | None
    variants: tuple[CatalogVariant, ...]


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

    async def list_services(
        self, connection: asyncpg.Connection, now: datetime
    ) -> tuple[CatalogService, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        rows = await connection.fetch(
            """
            SELECT service_id, staff_id, service_name, category_name,
                   staff_name, price_min, price_max, duration_minutes, synced_at
            FROM yclients_service_catalog
            ORDER BY service_id, staff_id
            """
        )
        if not rows or now - max(row["synced_at"] for row in rows) > CATALOG_MAX_AGE:
            return ()
        return _group_records(_record_from_row(row) for row in rows)

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


def _group_records(records) -> tuple[CatalogService, ...]:
    grouped: dict[str, tuple[str, str | None, list[CatalogVariant]]] = {}
    for record in records:
        current = grouped.setdefault(
            record.service_id,
            (record.service_name, record.category_name, []),
        )
        current[2].append(
            CatalogVariant(
                record.staff_id,
                record.staff_name,
                record.price_min,
                record.price_max,
                record.duration_minutes,
            )
        )
    services = [
        CatalogService(
            service_id,
            name,
            category,
            tuple(
                sorted(
                    variants,
                    key=lambda item: (_normalize(item.staff_name), int(item.staff_id)),
                )
            ),
        )
        for service_id, (name, category, variants) in grouped.items()
    ]
    return tuple(
        sorted(
            services,
            key=lambda item: (_normalize(item.service_name), int(item.service_id)),
        )
    )


def _record_from_row(row):
    return CatalogRecord(
        service_id=row["service_id"],
        staff_id=row["staff_id"],
        service_name=row["service_name"],
        category_name=row["category_name"],
        staff_name=row["staff_name"],
        price_min=row["price_min"],
        price_max=row["price_max"],
        duration_minutes=row["duration_minutes"],
    )


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")
