from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from moroz.booking.models import ExternalBooking


STARTS_AT = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "scheduled_end_at",
    [
        datetime(2026, 7, 28, 16, 0),
        STARTS_AT,
        STARTS_AT - timedelta(minutes=1),
    ],
    ids=["naive", "equal_to_start", "before_start"],
)
def test_external_booking_rejects_invalid_scheduled_end_at(scheduled_end_at):
    with pytest.raises(ValueError):
        ExternalBooking(
            external_id="booking-1",
            customer_id="customer-1",
            booking_key=uuid4(),
            slot_id="slot-1",
            starts_at=STARTS_AT,
            status="confirmed",
            scheduled_end_at=scheduled_end_at,
        )


def test_external_booking_accepts_aware_scheduled_end_after_start():
    booking = ExternalBooking(
        external_id="booking-1",
        customer_id="customer-1",
        booking_key=uuid4(),
        slot_id="slot-1",
        starts_at=STARTS_AT,
        status="confirmed",
        scheduled_end_at=STARTS_AT + timedelta(minutes=1),
    )

    assert booking.scheduled_end_at == STARTS_AT + timedelta(minutes=1)
