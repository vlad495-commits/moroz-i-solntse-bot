from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from moroz.notifications.models import JobResult, PlannedSchedulerJob, SchedulerJob


REACTIVATION_ACTIVITY_SYNC_KIND = "reactivation_activity_sync"
REACTIVATION_TICK_KIND = "reactivation_tick"
REACTIVATION_ACTIVITY_SYNC_INTERVAL = timedelta(minutes=10)
REACTIVATION_TICK_INTERVAL = timedelta(minutes=5)
PLANNER_LIMIT = 100
STEP_CLAIM_LIMIT = 100


def _job(kind: str, now: datetime, interval: timedelta) -> PlannedSchedulerJob:
    current = _aware(now)
    seconds = int(interval.total_seconds())
    bucket = datetime.fromtimestamp(
        int(current.timestamp()) // seconds * seconds, tz=UTC
    )
    return PlannedSchedulerJob(
        kind=kind,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{kind}:{bucket.isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


def reactivation_activity_sync_job(now: datetime) -> PlannedSchedulerJob:
    return _job(
        REACTIVATION_ACTIVITY_SYNC_KIND, now, REACTIVATION_ACTIVITY_SYNC_INTERVAL
    )


def reactivation_tick_job(now: datetime) -> PlannedSchedulerJob:
    return _job(REACTIVATION_TICK_KIND, now, REACTIVATION_TICK_INTERVAL)


class ReactivationCoordinator:
    def __init__(self, repository, scheduler, activity_sync, *, clock) -> None:
        self._repository = repository
        self._scheduler = scheduler
        self._activity_sync = activity_sync
        self._clock = clock

    async def ensure_current(self, now: datetime) -> None:
        jobs = (reactivation_activity_sync_job(now), reactivation_tick_job(now))
        await self._repository.recover_yclients_unavailable_jobs(
            tuple(job.idempotency_key for job in jobs)
        )
        for job in jobs:
            await self._scheduler.schedule(job)

    async def run_activity_sync(self, job: SchedulerJob) -> JobResult:
        await self._scheduler.schedule(
            reactivation_activity_sync_job(
                job.run_at + REACTIVATION_ACTIVITY_SYNC_INTERVAL
            )
        )
        return await self._activity_sync.sync_once()

    async def run_tick(self, job: SchedulerJob) -> JobResult:
        await self._scheduler.schedule(
            reactivation_tick_job(job.run_at + REACTIVATION_TICK_INTERVAL)
        )
        await self._repository.run_planner_cycle(
            _aware(self._clock()),
            planner_limit=PLANNER_LIMIT,
            step_claim_limit=STEP_CLAIM_LIMIT,
        )
        return JobResult.sent()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reactivation timestamp must be timezone-aware")
    return value.astimezone(UTC)
