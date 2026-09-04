from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from moroz.notifications.models import JobResult, PlannedSchedulerJob


RETENTION_CLEANUP_KIND = "retention_cleanup"
RETENTION_ERROR_CODE = "retention_cleanup_failed"
RETENTION_BATCH_SIZE = 1000


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
        "WITH expired AS (SELECT id FROM messages "
        "WHERE created_at < now() - make_interval(days => $1) "
        f"ORDER BY created_at, id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM messages USING expired WHERE messages.id = expired.id",
        retention_days,
    )
    token_usage = await connection.execute(
        "WITH expired AS (SELECT id FROM token_usage "
        "WHERE created_at < now() - make_interval(days => $1) "
        f"ORDER BY created_at, id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM token_usage USING expired WHERE token_usage.id = expired.id",
        retention_days,
    )
    message_inbox = await connection.execute(
        "WITH expired AS (SELECT id FROM message_inbox "
        "WHERE status = 'processed' "
        "AND created_at < now() - make_interval(days => $1) "
        f"ORDER BY created_at, id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM message_inbox USING expired "
        "WHERE message_inbox.id = expired.id",
        retention_days,
    )
    outbound_messages = await connection.execute(
        "WITH expired AS (SELECT id FROM outbound_messages "
        "WHERE status IN ('sent', 'failed', 'delivery_unknown') "
        "AND created_at < now() - make_interval(days => $1) "
        f"ORDER BY created_at, id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM outbound_messages USING expired "
        "WHERE outbound_messages.id = expired.id",
        retention_days,
    )
    journeys = await connection.execute(
        "WITH expired AS (SELECT id FROM reactivation_journeys "
        "WHERE status = 'closed' "
        "AND closed_at < now() - make_interval(days => $1) "
        f"ORDER BY closed_at, id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM reactivation_journeys USING expired "
        "WHERE reactivation_journeys.id = expired.id",
        retention_days,
    )
    activity = await connection.execute(
        "WITH expired AS (SELECT activity.channel, activity.user_id "
        "FROM customer_activity_projection AS activity "
        "WHERE activity.updated_at < now() - make_interval(days => $1) "
        "AND NOT EXISTS (SELECT 1 FROM marketing_consents AS consent "
        "WHERE consent.channel = activity.channel "
        "AND consent.user_id = activity.user_id AND consent.active = true) "
        "AND NOT EXISTS (SELECT 1 FROM reactivation_journeys AS journey "
        "WHERE journey.channel = activity.channel "
        "AND journey.user_id = activity.user_id AND journey.status != 'closed') "
        f"ORDER BY activity.updated_at, activity.channel, activity.user_id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM customer_activity_projection AS activity USING expired "
        "WHERE activity.channel = expired.channel AND activity.user_id = expired.user_id",
        retention_days,
    )
    consent_events = await connection.execute(
        "WITH expired AS (SELECT event.id FROM marketing_consent_events AS event "
        "WHERE event.created_at < now() - make_interval(days => $1) "
        "AND NOT EXISTS (SELECT 1 FROM marketing_consents AS consent "
        "WHERE consent.channel = event.channel AND consent.user_id = event.user_id "
        "AND consent.active = true) "
        f"ORDER BY event.created_at, event.id LIMIT {RETENTION_BATCH_SIZE}) "
        "DELETE FROM marketing_consent_events AS event USING expired "
        "WHERE event.id = expired.id",
        retention_days,
    )
    return {
        "messages": _delete_count(messages),
        "token_usage": _delete_count(token_usage),
        "message_inbox": _delete_count(message_inbox),
        "outbound_messages": _delete_count(outbound_messages),
        "reactivation_journeys": _delete_count(journeys),
        "customer_activity_projection": _delete_count(activity),
        "marketing_consent_events": _delete_count(consent_events),
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
