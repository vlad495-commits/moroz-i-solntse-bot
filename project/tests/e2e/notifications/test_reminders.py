from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

import pytest

from moroz.notifications.handlers import handle_scheduler_job
from moroz.notifications.models import SchedulerJob


pytestmark = pytest.mark.asyncio


class Booking:
    def __init__(self, *, status="confirmed"):
        self.booking_key = uuid4()
        self.customer_id = "customer-7"
        self.starts_at = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)
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
        self.calls.append(("staff_status_unknown", booking.customer_id, status))


def scheduler_job(kind, booking):
    return SchedulerJob(
        id=uuid4(),
        kind=kind,
        run_at=datetime(2026, 7, 28, 14, 0, tzinfo=UTC),
        payload=MappingProxyType({}),
        idempotency_key=f"job:{kind}",
        attempts=0,
        booking_key=booking.booking_key,
        booking_starts_at=booking.starts_at,
    )


async def test_normal_reminder_sends_one_customer_message():
    booking = Booking(status="confirmed")
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("hour_before", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
    )

    assert result.status == "sent"
    assert outbox.calls == [("reminder", "customer-7", "hour_before")]


async def test_cancelled_booking_is_skipped_without_messages():
    booking = Booking(status="cancelled")
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("hour_before", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
    )

    assert result.status == "skipped"
    assert result.reason == "stale"
    assert outbox.calls == []


async def test_no_show_sends_client_and_staff_messages():
    booking = Booking(status="no_show")
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("no_show_check", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
    )

    assert result.status == "sent"
    assert outbox.calls == [
        ("client_waiting", "customer-7"),
        ("staff_no_show", "customer-7"),
    ]


async def test_unknown_no_show_status_alerts_staff_only():
    booking = Booking(status="unknown")
    outbox = Outbox()

    result = await handle_scheduler_job(
        scheduler_job("no_show_check", booking),
        booking_port=BookingPort(booking),
        outbox=outbox,
    )

    assert result.status == "skipped"
    assert result.reason == "unknown_status"
    assert outbox.calls == [("staff_status_unknown", "customer-7", "unknown")]
