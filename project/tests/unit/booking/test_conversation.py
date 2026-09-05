from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from moroz.booking.conversation import filter_slots, merge_draft, next_requirement
from moroz.booking.models import Slot
from moroz.messaging.router import RouteDecision


MOSCOW = ZoneInfo("Europe/Moscow")


def decision(**overrides) -> RouteDecision:
    values = {
        "route": "booking",
        "confidence": 0.99,
        "action": "continue",
    }
    values.update(overrides)
    return RouteDecision(**values)


def slot(hour: int, minute: int = 0) -> Slot:
    return Slot(
        id=f"slot-{hour}-{minute}",
        service_ids=("service-7",),
        staff_id="staff-2",
        starts_at=datetime(2026, 9, 8, hour, minute, tzinfo=MOSCOW),
        duration_minutes=30,
    )


def test_date_correction_invalidates_slot_but_keeps_service():
    state = {
        "service_id": "7",
        "service_name": "Криосауна",
        "date": "2026-09-08",
        "available_slots": [{"slot_id": "old"}],
        "selected_slot_id": "old",
        "starts_at": "2026-09-08T18:00:00+03:00",
    }

    merged = merge_draft(state, decision(date="2026-09-09"))

    assert merged["service_id"] == "7"
    assert merged["date"] == "2026-09-09"
    assert "available_slots" not in merged
    assert "selected_slot_id" not in merged
    assert "starts_at" not in merged


def test_service_correction_invalidates_staff_and_slots_but_keeps_date():
    state = {
        "service_id": "7",
        "service_name": "Криосауна",
        "staff_id": "2",
        "staff_name": "Анна",
        "date": "2026-09-08",
        "selected_slot_id": "old",
    }

    merged = merge_draft(state, decision(services=("прессотерапия",)))

    assert merged["service_query"] == "прессотерапия"
    assert merged["date"] == "2026-09-08"
    assert "service_id" not in merged
    assert "staff_id" not in merged
    assert "selected_slot_id" not in merged


def test_new_time_window_replaces_old_window_and_invalidates_slot():
    state = {
        "service_id": "7",
        "date": "2026-09-08",
        "time_from": "18:00",
        "time_to": None,
        "selected_slot_id": "old",
    }

    merged = merge_draft(state, decision(time_to="15:00"))

    assert merged["time_from"] is None
    assert merged["time_to"] == "15:00"
    assert "selected_slot_id" not in merged


def test_multiple_services_require_one_service_choice():
    merged = merge_draft(
        {"date": "2026-09-08"},
        decision(services=("криосауна", "массаж")),
    )

    assert merged["service_candidates"] == ["криосауна", "массаж"]
    assert next_requirement(merged) == "service"


def test_filter_slots_honours_inclusive_local_time_window():
    slots = [slot(17, 30), slot(18), slot(19), slot(19, 30)]

    filtered = filter_slots(slots, "18:00", "19:00")

    assert [item.id for item in filtered] == ["slot-18-0", "slot-19-0"]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, "service"),
        ({"service_id": "7"}, "date"),
        ({"service_id": "7", "date": "2026-09-08", "time_needs_clarification": True}, "time"),
        ({"service_id": "7", "date": "2026-09-08"}, "slot"),
        ({"service_id": "7", "date": "2026-09-08", "selected_slot_id": "s"}, "contact"),
        ({"service_id": "7", "date": "2026-09-08", "selected_slot_id": "s", "customer_phone": "+79001112233", "processing_consent": True}, "name"),
        ({"service_id": "7", "date": "2026-09-08", "selected_slot_id": "s", "customer_phone": "+79001112233", "processing_consent": True, "customer_name": "Иван"}, "confirm"),
    ],
)
def test_next_requirement_is_derived_from_independent_fields(state, expected):
    assert next_requirement(state) == expected
