from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import (
    BookingNotFound,
    BookingEvent,
    BookingScenario,
    CancelBooking,
    CreateBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)


def _slot(slot_id: str, hour: int, *, services: tuple[str, ...] = ("service-1",), staff_id: str = "staff-1") -> Slot:
    return Slot(
        id=slot_id,
        service_ids=services,
        staff_id=staff_id,
        starts_at=datetime(2026, 7, 22, hour, tzinfo=UTC),
        duration_minutes=60,
    )


def _adapter() -> MockYclientsAdapter:
    return MockYclientsAdapter(
        [
            _slot("slot-before", 8),
            _slot("slot-ok", 9, services=("service-1", "service-2")),
            _slot("slot-next", 10),
            _slot("slot-other-service", 11, services=("service-2",)),
            _slot("slot-other-staff", 12, staff_id="staff-2"),
            Slot(
                id="slot-upper-bound",
                service_ids=("service-1",),
                staff_id="staff-1",
                starts_at=datetime(2026, 7, 23, tzinfo=UTC),
                duration_minutes=60,
            ),
        ]
    )


def _create_command(
    customer_id: str = "customer-1",
    slot_id: str = "slot-ok",
    idempotency_key: str = "create-1",
) -> CreateBooking:
    return CreateBooking(
        customer_id=customer_id,
        slot_id=slot_id,
        idempotency_key=idempotency_key,
        customer_name="Sandbox Customer",
        customer_phone="+70000000000",
        personal_data_processing_allowed=True,
    )


@pytest.mark.asyncio
async def test_list_slots_returns_only_matching_future_slots():
    adapter = _adapter()
    query = SlotQuery(
        service_ids=("service-1",),
        starts_after=datetime(2026, 7, 22, 9, tzinfo=UTC),
        starts_before=datetime(2026, 7, 23, tzinfo=UTC),
        staff_id="staff-1",
    )

    assert [slot.id for slot in await adapter.list_slots(query)] == ["slot-ok", "slot-next"]


@pytest.mark.asyncio
async def test_list_slots_excludes_occupied_slot():
    adapter = _adapter()
    await adapter.create_booking(_create_command())

    slots = await adapter.list_slots(
        SlotQuery(("service-1",), datetime(2026, 7, 22, 9, tzinfo=UTC), staff_id="staff-1")
    )

    assert "slot-ok" not in [slot.id for slot in slots]


@pytest.mark.asyncio
async def test_create_is_idempotent_for_same_key():
    adapter = _adapter()
    command = _create_command()

    first = await adapter.create_booking(command)
    repeated = await adapter.create_booking(command)

    assert repeated == first
    assert await adapter.get_booking(first.external_id) == first


@pytest.mark.asyncio
async def test_create_with_different_key_on_occupied_slot_raises_slot_unavailable():
    adapter = _adapter()
    await adapter.create_booking(_create_command())

    with pytest.raises(SlotUnavailable):
        await adapter.create_booking(_create_command("customer-2", "slot-ok", "create-2"))


@pytest.mark.asyncio
async def test_reschedule_checks_availability_and_is_idempotent():
    adapter = _adapter()
    booking = await adapter.create_booking(_create_command())
    command = RescheduleBooking(booking.external_id, "slot-next", "reschedule-1")

    first = await adapter.reschedule_booking(command)
    repeated = await adapter.reschedule_booking(command)

    assert repeated == first
    assert first.slot_id == "slot-next"
    with pytest.raises(SlotUnavailable):
        await adapter.create_booking(_create_command("customer-2", "slot-next", "create-2"))


@pytest.mark.asyncio
async def test_reschedule_to_occupied_slot_leaves_original_booking_unchanged():
    adapter = _adapter()
    booking = await adapter.create_booking(_create_command())
    await adapter.create_booking(_create_command("customer-2", "slot-next", "create-2"))

    with pytest.raises(SlotUnavailable):
        await adapter.reschedule_booking(RescheduleBooking(booking.external_id, "slot-next", "reschedule-1"))

    assert await adapter.get_booking(booking.external_id) == booking


@pytest.mark.asyncio
async def test_cancel_with_same_key_is_a_safe_repeat():
    adapter = _adapter()
    booking = await adapter.create_booking(_create_command())
    command = CancelBooking(booking.external_id, "cancel-1")

    await adapter.cancel_booking(command)
    await adapter.cancel_booking(command)

    assert (await adapter.get_booking(booking.external_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_unknown_external_id_raises_booking_not_found():
    with pytest.raises(BookingNotFound):
        await _adapter().get_booking("unknown")


def test_slot_query_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        SlotQuery(("service-1",), datetime(2026, 7, 22, 9))


def test_slot_query_and_slot_freeze_caller_owned_service_lists():
    query_services = ["service-1"]
    slot_services = ["service-1"]
    query = SlotQuery(query_services, datetime(2026, 7, 22, 9, tzinfo=UTC))
    slot = Slot(
        "slot-1",
        slot_services,
        "staff-1",
        datetime(2026, 7, 22, 10, tzinfo=UTC),
        60,
    )

    query_services.append("service-2")
    slot_services.append("service-2")

    assert query.service_ids == ("service-1",)
    assert slot.service_ids == ("service-1",)
    assert isinstance(query.service_ids, tuple)
    assert isinstance(slot.service_ids, tuple)


def test_booking_scenario_and_event_freeze_nested_json_values():
    nested = {"preferences": {"services": ["service-1"]}}
    scenario = BookingScenario(
        id=uuid4(),
        kind="create",
        phase="collecting",
        idempotency_key="scenario-1",
        customer_id="customer-1",
        state=nested,
        error_code=None,
        created_at=datetime(2026, 7, 22, 9, tzinfo=UTC),
        updated_at=datetime(2026, 7, 22, 9, tzinfo=UTC),
    )
    event = BookingEvent(
        id=uuid4(),
        scenario_id=scenario.id,
        event_type="booking_scenario_created",
        payload=nested,
        created_at=datetime(2026, 7, 22, 9, tzinfo=UTC),
    )

    with pytest.raises(TypeError):
        scenario.state["preferences"] = {}
    with pytest.raises(TypeError):
        event.payload["preferences"]["services"] = ()
    with pytest.raises(AttributeError):
        scenario.state["preferences"]["services"].append("service-2")
    nested["preferences"]["services"].append("service-2")

    assert scenario.state["preferences"]["services"] == ("service-1",)
    assert event.payload["preferences"]["services"] == ("service-1",)
