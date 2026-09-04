from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.models import ExternalBooking
from moroz.notifications.ports import NotificationOutbox, _reminder_text


def test_client_reminders_show_moscow_time_without_iso_offsets() -> None:
    booking = ExternalBooking(
        external_id="booking-1",
        customer_id="customer-1",
        booking_key=uuid4(),
        slot_id="slot-1",
        starts_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
        status="confirmed",
    )

    assert _reminder_text(booking, "booking_created") == (
        "Запись подтверждена на 10.09.2026 в 12:00."
    )
    assert _reminder_text(booking, "day_before") == (
        "Напоминаем: завтра у Вас запись на 10.09.2026 в 12:00."
    )
    assert _reminder_text(booking, "hour_before") == (
        "Напоминаем о записи сегодня: 10.09.2026 в 12:00."
    )


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
