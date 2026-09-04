from datetime import datetime, time, timedelta
from types import MappingProxyType
from uuid import UUID
from zoneinfo import ZoneInfo

from moroz.notifications.models import PlannedSchedulerJob


MOSCOW = ZoneInfo("Europe/Moscow")
MERGE_WINDOW = timedelta(minutes=30)


def plan_booking_notifications(
    *,
    booking_key: UUID,
    starts_at: datetime,
    now: datetime,
    include_created: bool = True,
) -> list[PlannedSchedulerJob]:
    starts_at = starts_at.astimezone(MOSCOW)
    now = now.astimezone(MOSCOW)
    reminder_times = {
        "booking_created": now,
        "day_before": starts_at - timedelta(days=1),
        "morning": datetime.combine(starts_at.date(), time(9), tzinfo=MOSCOW),
        "hour_before": starts_at - timedelta(hours=1),
        "no_show_check": starts_at,
    }
    if _should_merge(reminder_times["morning"], reminder_times["hour_before"]):
        reminder_times["morning_hour_before"] = min(
            reminder_times["morning"],
            reminder_times["hour_before"],
        )
        del reminder_times["morning"]
        del reminder_times["hour_before"]

    return [
        _planned_job(booking_key, starts_at, kind, run_at)
        for kind, run_at in sorted(reminder_times.items(), key=lambda item: item[1])
        if (kind != "booking_created" or include_created) and run_at >= now
    ]


def _should_merge(morning: datetime, hour_before: datetime) -> bool:
    return abs(morning - hour_before) <= MERGE_WINDOW


def _planned_job(
    booking_key: UUID,
    starts_at: datetime,
    kind: str,
    run_at: datetime,
) -> PlannedSchedulerJob:
    payload = MappingProxyType(
        {
            "booking_key": str(booking_key),
            "starts_at": starts_at.isoformat(),
            "notification_kind": kind,
        }
    )
    return PlannedSchedulerJob(
        kind=kind,
        run_at=run_at,
        payload=payload,
        idempotency_key=f"booking:{booking_key}:{starts_at.isoformat()}:{kind}",
        booking_key=booking_key,
        booking_starts_at=starts_at,
    )

