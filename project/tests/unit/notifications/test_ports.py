from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.models import ExternalBooking
from moroz.notifications.ports import NotificationOutbox


@pytest.mark.asyncio
async def test_staff_notification_fails_when_chat_is_not_configured():
    booking = ExternalBooking(
        external_id="booking-1",
        customer_id="customer-1",
        booking_key=uuid4(),
        slot_id="slot-1",
        starts_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        status="confirmed",
    )
    outbox = NotificationOutbox(object())

    with pytest.raises(
        RuntimeError,
        match="STAFF_TELEGRAM_CHAT_ID is not configured",
    ):
        await outbox.staff_no_show(booking)
