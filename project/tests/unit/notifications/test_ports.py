from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.models import ExternalBooking
from moroz.notifications.ports import LocalBookingPort, NotificationOutbox


class FakeConnection:
    async def fetchrow(self, _query, _booking_key):
        return {
            "external_id": "legacy-booking-1",
            "customer_id": "customer-1",
            "booking_key": _booking_key,
            "slot_id": "legacy-slot-1",
            "service_ids": None,
            "staff_id": None,
            "starts_at": datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
            "scheduled_end_at": None,
            "status": "confirmed",
        }


class FakeDatabase:
    @asynccontextmanager
    async def acquire(self):
        yield FakeConnection()


@pytest.mark.asyncio
async def test_local_booking_port_marks_legacy_snapshot_as_unmatched():
    booking = await LocalBookingPort(FakeDatabase()).get_booking(uuid4())

    assert booking is not None
    assert booking.service_ids == ()
    assert booking.staff_id == ""


@pytest.mark.asyncio
async def test_staff_notification_fails_when_chat_is_not_configured():
    booking = ExternalBooking(
        external_id="booking-1",
        customer_id="customer-1",
        booking_key=uuid4(),
        slot_id="slot-1",
        service_ids=("service-1",),
        staff_id="staff-1",
        starts_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        status="confirmed",
    )
    outbox = NotificationOutbox(object())

    with pytest.raises(
        RuntimeError,
        match="STAFF_TELEGRAM_CHAT_ID is not configured",
    ):
        await outbox.staff_no_show(booking)
