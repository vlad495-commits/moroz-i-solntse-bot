from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import asyncpg

from moroz.booking.yclients_records import (
    ProjectionSnapshot,
    YclientsProjectionError,
)
from moroz.common.db import Database
from moroz.notifications.models import JobResult, PlannedSchedulerJob
from moroz.notifications.repository import SchedulerJobRepository


PROJECTION_LOCK = "yclients_booking_projection:v1"
PROJECTION_SYNC_KIND = "yclients_booking_projection_sync"


class ProjectionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def serialized(self):
        async with self._database.acquire() as connection:
            try:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    PROJECTION_LOCK,
                )
            except asyncpg.PostgresError as error:
                raise YclientsProjectionError("yclients_projection_write") from error
            try:
                yield connection if locked else None
            finally:
                if locked:
                    try:
                        await connection.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            PROJECTION_LOCK,
                        )
                    except asyncpg.PostgresError as error:
                        raise YclientsProjectionError("yclients_projection_write") from error

    async def replace(self, connection, snapshot: ProjectionSnapshot) -> None:
        rows = [
            (
                record.external_id,
                record.booking_key,
                record.bot_marker_state,
                record.starts_at,
                record.scheduled_end_at,
                record.status,
                record.deleted,
                record.client_name,
                record.staff_name,
                list(record.service_names),
                snapshot.synced_at,
                record.client_id,
                record.record_created_at,
            )
            for record in snapshot.records
        ]
        try:
            async with connection.transaction():
                suppressed_ids = {
                    row["external_id"]
                    for row in await connection.fetch(
                        "SELECT external_id "
                        "FROM yclients_projection_suppressions "
                        "WHERE external_id = ANY($1::text[])",
                        [row[0] for row in rows],
                    )
                }
                rows = [row for row in rows if row[0] not in suppressed_ids]
                await connection.execute("DELETE FROM yclients_booking_projection")
                if rows:
                    await connection.executemany(
                        """
                        INSERT INTO yclients_booking_projection
                            (external_id, booking_key, bot_marker_state, starts_at,
                             scheduled_end_at, status, deleted, client_name, staff_name,
                             service_names, synced_at, client_id, record_created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                $11, $12, $13)
                        """,
                        rows,
                    )
                await connection.execute(
                    """
                    UPDATE customer_activity_projection AS activity
                    SET next_active_booking_at = (
                            SELECT min(projection.starts_at)
                            FROM yclients_booking_projection AS projection
                            WHERE projection.client_id = activity.yclients_client_id
                              AND projection.status = 'confirmed'
                              AND NOT projection.deleted
                              AND projection.starts_at >= $1
                        ),
                        recent_bookings_synced_at = $1,
                        updated_at = $1
                    WHERE activity.identity_status = 'verified'
                      AND activity.yclients_client_id IS NOT NULL
                    """,
                    snapshot.synced_at,
                )
        except asyncpg.PostgresError as error:
            raise YclientsProjectionError("yclients_projection_write") from error


def projection_job(now: datetime) -> PlannedSchedulerJob:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    utc = now.astimezone(UTC)
    bucket = utc.replace(minute=(utc.minute // 10) * 10, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=PROJECTION_SYNC_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{PROJECTION_SYNC_KIND}:{bucket.isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


class ProjectionSyncCoordinator:
    def __init__(
        self,
        repository: ProjectionRepository,
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
        await self._scheduler.schedule(projection_job(now))

    async def run(self, job: PlannedSchedulerJob) -> JobResult:
        await self._scheduler.schedule(projection_job(job.run_at + timedelta(minutes=10)))
        async with self._repository.serialized() as connection:
            if connection is None:
                return JobResult.skipped("projection_busy")
            snapshot = await self._reader.read_window(self._clock())
            await self._repository.replace(connection, snapshot)
        return JobResult.sent()
