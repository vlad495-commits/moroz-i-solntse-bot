import json
from datetime import datetime
from types import MappingProxyType

from moroz.common.db import Database
from moroz.notifications.models import SchedulerJob


def _load_payload(value: object) -> MappingProxyType:
    payload = json.loads(value) if isinstance(value, str) else value
    return MappingProxyType(dict(payload or {}))


class SchedulerJobRepository:
    def __init__(self, database: Database):
        self._database = database

    async def claim_due(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[SchedulerJob]:
        if limit <= 0:
            return []
        async with self._database.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    """
                    UPDATE scheduler_jobs AS job
                    SET status = 'claimed',
                        claimed_at = now(),
                        updated_at = now()
                    FROM (
                        SELECT id
                        FROM scheduler_jobs
                        WHERE status = 'pending'
                          AND run_at <= COALESCE($1, now())
                        ORDER BY run_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT $2
                    ) AS due
                    WHERE job.id = due.id
                    RETURNING job.id, job.kind, job.run_at, job.payload,
                              job.idempotency_key, job.attempts,
                              job.booking_key, job.booking_starts_at
                    """,
                    now,
                    limit,
                )
        return [
            SchedulerJob(
                id=row["id"],
                kind=row["kind"],
                run_at=row["run_at"],
                payload=_load_payload(row["payload"]),
                idempotency_key=row["idempotency_key"],
                attempts=row["attempts"],
                booking_key=row["booking_key"],
                booking_starts_at=row["booking_starts_at"],
            )
            for row in rows
        ]

