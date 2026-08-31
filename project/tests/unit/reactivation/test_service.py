from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from moroz.notifications.models import JobResult, SchedulerJob
from moroz.reactivation.service import (
    REACTIVATION_ACTIVITY_SYNC_KIND,
    REACTIVATION_TICK_KIND,
    ReactivationCoordinator,
)


NOW = datetime(2026, 8, 31, 7, 0, tzinfo=UTC)


def _job(kind: str, run_at: datetime = NOW) -> SchedulerJob:
    return SchedulerJob(
        id=uuid4(), kind=kind, run_at=run_at, payload=MappingProxyType({}),
        idempotency_key=f"{kind}:{run_at.isoformat()}", attempts=0,
        booking_key=None, booking_starts_at=None,
    )


@pytest.mark.asyncio
async def test_ensure_current_seeds_both_idempotent_jobs():
    scheduler = AsyncMock()
    coordinator = ReactivationCoordinator(
        AsyncMock(), scheduler, AsyncMock(), clock=lambda: NOW
    )

    await coordinator.ensure_current(NOW)

    jobs = [call.args[0] for call in scheduler.schedule.await_args_list]
    assert [job.kind for job in jobs] == [
        REACTIVATION_ACTIVITY_SYNC_KIND,
        REACTIVATION_TICK_KIND,
    ]
    assert all(job.run_at == NOW for job in jobs)


@pytest.mark.asyncio
async def test_activity_sync_delegates_once_and_schedules_next_job():
    scheduler = AsyncMock()
    activity = AsyncMock()
    activity.sync_once.return_value = JobResult.sent()
    coordinator = ReactivationCoordinator(
        AsyncMock(), scheduler, activity, clock=lambda: NOW
    )

    result = await coordinator.run_activity_sync(
        _job(REACTIVATION_ACTIVITY_SYNC_KIND)
    )

    activity.sync_once.assert_awaited_once()
    next_job = scheduler.schedule.await_args.args[0]
    assert next_job.kind == REACTIVATION_ACTIVITY_SYNC_KIND
    assert next_job.run_at == NOW + timedelta(minutes=10)
    assert result == JobResult.sent()


@pytest.mark.asyncio
async def test_tick_always_schedules_next_job_and_runs_one_bounded_cycle():
    scheduler = AsyncMock()
    repository = AsyncMock()
    repository.run_planner_cycle.return_value = 7
    coordinator = ReactivationCoordinator(
        repository, scheduler, AsyncMock(), clock=lambda: NOW
    )

    result = await coordinator.run_tick(_job(REACTIVATION_TICK_KIND))

    repository.run_planner_cycle.assert_awaited_once_with(
        NOW, planner_limit=100, step_claim_limit=100
    )
    next_job = scheduler.schedule.await_args.args[0]
    assert next_job.kind == REACTIVATION_TICK_KIND
    assert next_job.run_at == NOW + timedelta(minutes=5)
    assert result == JobResult.sent()


@pytest.mark.asyncio
async def test_tick_schedules_successor_before_failed_cycle():
    scheduler = AsyncMock()
    repository = AsyncMock()
    repository.run_planner_cycle.side_effect = RuntimeError("database unavailable")
    coordinator = ReactivationCoordinator(
        repository, scheduler, AsyncMock(), clock=lambda: NOW
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await coordinator.run_tick(_job(REACTIVATION_TICK_KIND))

    assert scheduler.schedule.await_count == 1
