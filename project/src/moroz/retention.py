from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from moroz.notifications.models import JobResult, PlannedSchedulerJob


RETENTION_CLEANUP_KIND = "retention_cleanup"
RETENTION_ERROR_CODE = "retention_cleanup_failed"


class RetentionCleanupError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(RETENTION_ERROR_CODE)
        self.code = RETENTION_ERROR_CODE


def _delete_count(command_tag: str) -> int:
    parts = command_tag.split()
    if len(parts) != 2 or parts[0] != "DELETE" or not parts[1].isdigit():
        raise RetentionCleanupError()
    return int(parts[1])


def retention_job(now: datetime) -> PlannedSchedulerJob:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    bucket = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=RETENTION_CLEANUP_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{RETENTION_CLEANUP_KIND}:{bucket.date().isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


async def delete_expired_records(connection, retention_days: int) -> dict[str, int]:
    if retention_days <= 0:
        return {}
    messages = await connection.execute(
        "DELETE FROM messages "
        "WHERE created_at < now() - make_interval(days => $1)",
        retention_days,
    )
    token_usage = await connection.execute(
        "DELETE FROM token_usage "
        "WHERE created_at < now() - make_interval(days => $1)",
        retention_days,
    )
    return {
        "messages": _delete_count(messages),
        "token_usage": _delete_count(token_usage),
    }


class RetentionCleanupCoordinator:
    def __init__(self, database, scheduler, *, retention_days: int) -> None:
        self._database = database
        self._scheduler = scheduler
        self._retention_days = retention_days

    async def ensure_current(self, now: datetime) -> None:
        if self._retention_days > 0:
            try:
                await self._scheduler.schedule(retention_job(now))
                await self._scheduler.schedule(
                    retention_job(now + timedelta(days=1))
                )
            except RetentionCleanupError:
                raise
            except Exception as error:
                raise RetentionCleanupError() from error

    async def run(self, job) -> JobResult:
        if self._retention_days <= 0:
            return JobResult.skipped("retention_disabled")
        try:
            await self._scheduler.schedule(
                retention_job(job.run_at + timedelta(days=2))
            )
            await self._scheduler.schedule(
                retention_job(job.run_at + timedelta(days=1))
            )
            async with self._database.acquire() as connection:
                async with connection.transaction():
                    await delete_expired_records(connection, self._retention_days)
        except RetentionCleanupError:
            raise
        except Exception as error:
            raise RetentionCleanupError() from error
        return JobResult.sent()
