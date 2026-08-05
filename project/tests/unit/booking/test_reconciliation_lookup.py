from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import CreateBooking, Slot


pytestmark = pytest.mark.asyncio


async def test_mock_lookup_is_read_only_and_finds_only_the_opaque_booking_key():
    starts_at = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
    adapter = MockYclientsAdapter(
        [Slot("slot-1", ("service-1",), "staff-1", starts_at, 60)]
    )
    booking_key = uuid4()
    booking = await adapter.create_booking(
        CreateBooking(
            customer_id="700001",
            booking_key=booking_key,
            slot_id="slot-1",
            idempotency_key="create-1",
            customer_name="Test",
            customer_phone="+70000000000",
            personal_data_processing_allowed=True,
        )
    )

    assert await adapter.find_by_booking_key(booking_key) == [booking]
    assert await adapter.find_by_booking_key(uuid4()) == []
