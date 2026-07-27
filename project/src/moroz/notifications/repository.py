import json
from datetime import datetime
from types import MappingProxyType

from moroz.common.db import Database
from moroz.notifications.models import JobResult, SchedulerJob


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
            _job_from_row(row)
            for row in rows
        ]

    async def get_claimed(self, job_id) -> SchedulerJob | None:
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, kind, run_at, payload, idempotency_key, attempts,
                       booking_key, booking_starts_at
                FROM scheduler_jobs
                WHERE id = $1 AND status = 'claimed'
                """,
                job_id,
            )
        return _job_from_row(row) if row is not None else None

    async def release_claim(self, job_id) -> None:
        async with self._database.acquire() as connection:
            await connection.execute(
                """
                UPDATE scheduler_jobs
                SET status = 'pending',
                    claimed_at = NULL,
                    updated_at = now()
                WHERE id = $1 AND status = 'claimed'
                """,
                job_id,
            )

    async def complete(
        self,
        job: SchedulerJob,
        result: JobResult | None = None,
        *,
        result_status: str | None = None,
        error_code: str | None = None,
    ) -> None:
        status = result_status or _status_from_result(result)
        if status not in {"finished", "skipped", "failed"}:
            raise ValueError("unsupported scheduler job terminal status")
        last_error_code = error_code if error_code is not None else (
            result.reason if result else None
        )
        async with self._database.acquire() as connection:
            await connection.execute(
                """
                UPDATE scheduler_jobs
                SET status = $2,
                    finished_at = now(),
                    last_error_code = $3,
                    updated_at = now()
                WHERE id = $1 AND status = 'claimed'
                """,
                job.id,
                status,
                last_error_code,
            )

    async def record_failure(
        self,
        job: SchedulerJob,
        *,
        error_code: str,
        terminal: bool,
    ) -> None:
        async with self._database.acquire() as connection:
            await connection.execute(
                """
                UPDATE scheduler_jobs
                SET status = CASE WHEN $3 THEN 'failed' ELSE 'claimed' END,
                    attempts = attempts + 1,
                    finished_at = CASE WHEN $3 THEN now() ELSE NULL END,
                    last_error_code = $2,
                    updated_at = now()
                WHERE id = $1
                  AND status = 'claimed'
                  AND attempts = $4
                """,
                job.id,
                error_code,
                terminal,
                job.attempts,
            )


def _job_from_row(row) -> SchedulerJob:
    return SchedulerJob(
        id=row["id"],
        kind=row["kind"],
        run_at=row["run_at"],
        payload=_load_payload(row["payload"]),
        idempotency_key=row["idempotency_key"],
        attempts=row["attempts"],
        booking_key=row["booking_key"],
        booking_starts_at=row["booking_starts_at"],
    )


def _status_from_result(result: JobResult | None) -> str:
    if result is None:
        raise ValueError("scheduler job result is required")
    if result.status == "sent":
        return "finished"
    return result.status
