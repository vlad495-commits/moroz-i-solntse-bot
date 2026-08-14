from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

import pytest

from moroz.booking.projection import PROJECTION_SYNC_KIND
from moroz.notifications.handlers import handle_scheduler_job
from moroz.notifications.models import JobResult, SchedulerJob


pytestmark = pytest.mark.asyncio


class Booking:
    def __init__(
        self,
        *,
        status="confirmed",
        booking_key=None,
        customer_id="customer-7",
        starts_at=None,
    ):
        self.booking_key = booking_key or uuid4()
        self.customer_id = customer_id
        self.starts_at = starts_at or datetime(2026, 7, 28, 15, 0, tzinfo=UTC)
        self.status = status


class BookingPort:
    def __init__(self, booking):
        self.booking = booking
        self.requests = []

    async def get_booking(self, booking_key):
        self.requests.append(booking_key)
        return self.booking


class Outbox:
    def __init__(self):
        self.calls = []

    async def reminder(self, booking, kind):
        self.calls.append(("reminder", booking.customer_id, kind))

    async def client_waiting(self, booking):
        self.calls.append(("client_waiting", booking.customer_id))

    async def staff_no_show(self, booking):
        self.calls.append(("staff_no_show", booking.customer_id))

    async def staff_status_unknown(self, booking, status):
        self.calls.append(("staff_status_unknown", status))


class Lifecycle:
    def __init__(self, status, *, schedule_next=True):
        self.status = status
        self.schedule_next_result = schedule_next
        self.refresh_calls = []
        self.next_calls = []
        self.feedback_calls = []

    async def refresh(self, booking):
        self.refresh_calls.append(booking.booking_key)
        return Booking(
            status=self.status,
            booking_key=booking.booking_key,
            customer_id=booking.customer_id,
            starts_at=booking.starts_at,
        )

    async def schedule_next(self, booking, current_index):
        self.next_calls.append((booking.booking_key, current_index))
        return self.schedule_next_result

    async def schedule_feedback(self, booking):
        self.feedback_calls.append(booking.booking_key)


def scheduler_job(kind, booking, *, index=None):
    payload = {}
    if index is not None:
        payload["outcome_check_index"] = index
    return SchedulerJob(
        id=uuid4(),
        kind=kind,
        run_at=datetime(2026, 7, 28, 14, 0, tzinfo=UTC),
        payload=MappingProxyType(payload),
        idempotency_key=f"job:{kind}",
        attempts=0,
        booking_key=booking.booking_key,
        booking_starts_at=booking.starts_at,
    )


async def test_projection_job_runs_before_booking_lookup():
    booking = Booking()
    calls = []

    class ProjectionSync:
        async def run(self, job):
            calls.append(job)
            return JobResult.sent()

    class BookingPort:
        async def get_booking(self, _booking_key):
            raise AssertionError("projection jobs must not load bookings")

    job = scheduler_job(PROJECTION_SYNC_KIND, booking)
    result = await handle_scheduler_job(
        job,
        booking_port=BookingPort(),
        outbox=None,
        projection_sync=ProjectionSync(),
    )

    assert result == JobResult.sent()
    assert calls == [job]


async def test_projection_job_without_coordinator_fails_closed():
    with pytest.raises(RuntimeError, match="projection sync is not configured"):
        await handle_scheduler_job(
            scheduler_job(PROJECTION_SYNC_KIND, Booking()),
            booking_port=None,
            outbox=None,
        )


async def test_normal_reminder_sends_one_customer_message():
    booking = Booking(status="confirmed")
    outbox = Outbox()
    lifecycle = Lifecycle("completed")

    result = await handle_scheduler_job(
        scheduler_job("hour_before", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
        lifecycle=lifecycle,
    )

    assert result == JobResult.sent()
    assert lifecycle.refresh_calls == []
    assert outbox.calls == [("reminder", "customer-7", "hour_before")]


@pytest.mark.parametrize("status", ["completed", "no_show", "unknown"])
async def test_reminder_skips_non_confirmed_lifecycle_status(status):
    booking = Booking(status=status)
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("hour_before", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
    )

    assert result == JobResult.skipped("stale")
    assert outbox.calls == []


async def test_cancelled_booking_is_skipped_without_messages():
    booking = Booking(status="cancelled")
    outbox = Outbox()
    lifecycle = Lifecycle("completed")

    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", booking, index=0),
        booking_port=BookingPort(booking),
        outbox=outbox,
        lifecycle=lifecycle,
    )

    assert result == JobResult.skipped("stale")
    assert lifecycle.refresh_calls == []
    assert outbox.calls == []


async def test_no_show_sends_client_and_staff_messages():
    booking = Booking(status="no_show")
    outbox = Outbox()
    lifecycle = Lifecycle("no_show")

    result = await handle_scheduler_job(
        scheduler_job("no_show_check", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
        lifecycle=lifecycle,
    )

    assert result == JobResult.sent()
    assert outbox.calls == [
        ("client_waiting", "customer-7"),
        ("staff_no_show", "customer-7"),
    ]


async def test_unknown_no_show_status_alerts_staff_only():
    booking = Booking(status="unknown")
    outbox = Outbox()
    lifecycle = Lifecycle("unknown")

    result = await handle_scheduler_job(
        scheduler_job("no_show_check", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
        lifecycle=lifecycle,
    )

    assert result == JobResult.skipped("unknown_status")
    assert outbox.calls == [("staff_status_unknown", "unknown")]


async def test_confirmed_no_show_check_schedules_first_outcome_check():
    booking = Booking()
    lifecycle = Lifecycle("confirmed")

    result = await handle_scheduler_job(
        scheduler_job("no_show_check", booking),
        booking_port=BookingPort(booking),
        outbox=Outbox(),
        lifecycle=lifecycle,
    )

    assert result == JobResult.skipped("outcome_pending")
    assert lifecycle.next_calls == [(booking.booking_key, -1)]


async def test_completed_visit_schedules_feedback_without_reminder():
    booking = Booking()
    lifecycle = Lifecycle("completed")
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", booking, index=0),
        booking_port=BookingPort(booking),
        outbox=outbox,
        lifecycle=lifecycle,
    )

    assert result == JobResult.sent()
    assert lifecycle.feedback_calls == [booking.booking_key]
    assert outbox.calls == []


async def test_confirmed_visit_schedules_next_bounded_check():
    booking = Booking()
    lifecycle = Lifecycle("confirmed", schedule_next=True)

    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", booking, index=0),
        booking_port=BookingPort(booking),
        outbox=Outbox(),
        lifecycle=lifecycle,
    )

    assert result == JobResult.skipped("outcome_pending")
    assert lifecycle.next_calls == [(booking.booking_key, 0)]


async def test_final_confirmed_visit_alerts_staff_once():
    booking = Booking()
    lifecycle = Lifecycle("confirmed", schedule_next=False)
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("visit_outcome_check", booking, index=2),
        booking_port=BookingPort(booking),
        outbox=outbox,
        lifecycle=lifecycle,
    )

    assert result == JobResult.skipped("outcome_unresolved")
    assert outbox.calls == [("staff_status_unknown", "outcome_unresolved")]


@pytest.mark.parametrize("index", [None, True, -1, 3, "0"])
async def test_visit_outcome_check_rejects_invalid_index(index):
    booking = Booking()
    lifecycle = Lifecycle("confirmed")
    job = scheduler_job("visit_outcome_check", booking, index=index)

    result = await handle_scheduler_job(
        job,
        booking_port=BookingPort(booking),
        outbox=Outbox(),
        lifecycle=lifecycle,
    )

    assert result == JobResult.skipped("invalid_outcome_payload")
    assert lifecycle.next_calls == []
