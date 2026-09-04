from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from moroz.notifications.planner import plan_booking_notifications


MOSCOW = ZoneInfo("Europe/Moscow")


def test_plans_full_booking_reminder_schedule():
    booking_key = uuid4()
    now = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW)
    starts_at = datetime(2026, 7, 28, 15, 0, tzinfo=MOSCOW)

    jobs = plan_booking_notifications(
        booking_key=booking_key,
        starts_at=starts_at,
        now=now,
    )

    assert [(job.kind, job.run_at) for job in jobs] == [
        ("booking_created", now),
        ("day_before", starts_at - timedelta(days=1)),
        ("morning", datetime(2026, 7, 28, 9, 0, tzinfo=MOSCOW)),
        ("hour_before", starts_at - timedelta(hours=1)),
        ("no_show_check", starts_at),
    ]
    assert [job.idempotency_key for job in jobs] == [
        f"booking:{booking_key}:{starts_at.isoformat()}:booking_created",
        f"booking:{booking_key}:{starts_at.isoformat()}:day_before",
        f"booking:{booking_key}:{starts_at.isoformat()}:morning",
        f"booking:{booking_key}:{starts_at.isoformat()}:hour_before",
        f"booking:{booking_key}:{starts_at.isoformat()}:no_show_check",
    ]
    assert all(job.booking_key == booking_key for job in jobs)
    assert all(job.booking_starts_at == starts_at for job in jobs)


def test_merges_morning_and_hour_before_when_they_are_close():
    booking_key = uuid4()
    now = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW)
    starts_at = datetime(2026, 7, 28, 9, 30, tzinfo=MOSCOW)

    jobs = plan_booking_notifications(
        booking_key=booking_key,
        starts_at=starts_at,
        now=now,
    )

    assert [(job.kind, job.run_at) for job in jobs] == [
        ("booking_created", now),
        ("day_before", starts_at - timedelta(days=1)),
        ("morning_hour_before", starts_at - timedelta(hours=1)),
        ("no_show_check", starts_at),
    ]


def test_skips_past_reminder_deadlines_for_late_booking():
    booking_key = uuid4()
    now = datetime(2026, 7, 28, 14, 30, tzinfo=MOSCOW)
    starts_at = datetime(2026, 7, 28, 15, 0, tzinfo=MOSCOW)

    jobs = plan_booking_notifications(
        booking_key=booking_key,
        starts_at=starts_at,
        now=now,
    )

    assert [(job.kind, job.run_at) for job in jobs] == [
        ("booking_created", now),
        ("no_show_check", starts_at),
    ]


def test_reschedule_skips_created_notification_but_keeps_future_jobs():
    booking_key = uuid4()
    now = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW)
    starts_at = datetime(2026, 7, 28, 15, 0, tzinfo=MOSCOW)

    jobs = plan_booking_notifications(
        booking_key=booking_key,
        starts_at=starts_at,
        now=now,
        include_created=False,
    )

    assert [job.kind for job in jobs] == [
        "day_before",
        "morning",
        "hour_before",
        "no_show_check",
    ]
