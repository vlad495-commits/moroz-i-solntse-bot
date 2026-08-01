import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import CreateBooking, Slot, SlotUnavailable


pytestmark = pytest.mark.asyncio


def command(*, owner: str, booking_key, idempotency_key: str) -> CreateBooking:
    return CreateBooking(
        customer_id=owner,
        booking_key=booking_key,
        slot_id="slot-race",
        idempotency_key=idempotency_key,
        customer_name="Test Customer",
        customer_phone="+70000000000",
        personal_data_processing_allowed=True,
    )


def adapter() -> MockYclientsAdapter:
    return MockYclientsAdapter(
        [
            Slot(
                "slot-race",
                ("service-1",),
                "staff-1",
                datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
                60,
            )
        ]
    )


async def test_duplicate_confirmation_replay_has_one_terminal_provider_booking():
    port = adapter()
    booking_key = uuid4()
    create = command(
        owner="owner-1",
        booking_key=booking_key,
        idempotency_key="callback:update-1",
    )

    first, replay = await asyncio.gather(
        port.create_booking(create),
        port.create_booking(create),
    )

    assert first == replay
    assert await port.find_by_booking_key(booking_key) == [first]


async def test_two_owners_racing_one_slot_get_one_truthful_winner():
    port = adapter()

    async def attempt(owner: str):
        try:
            return await port.create_booking(
                command(
                    owner=owner,
                    booking_key=uuid4(),
                    idempotency_key=f"callback:{owner}",
                )
            )
        except SlotUnavailable:
            return "slot_unavailable"

    results = await asyncio.gather(attempt("owner-1"), attempt("owner-2"))

    winners = [item for item in results if item != "slot_unavailable"]
    assert len(winners) == 1
    assert results.count("slot_unavailable") == 1
    assert winners[0].customer_id in {"owner-1", "owner-2"}
