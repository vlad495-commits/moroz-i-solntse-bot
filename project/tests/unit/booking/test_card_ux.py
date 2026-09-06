from datetime import UTC, datetime
from uuid import uuid4

import pytest

from moroz.booking.display import service_display_name
from moroz.booking.models import BookingScenario
from moroz.booking.telegram import BookingReply, TelegramBookingCoordinator


@pytest.mark.parametrize("raw, expected", [
    ("КОЛЛАГЕНАРИЙ", "Коллагенарий"),
    ("коллариум", "Коллариум"),
    ("фреш дня", "Фреш день"),
    ("Массаж  спины 30 минут", "Массаж спины 30 минут"),
])
def test_display_name_copy(raw, expected):
    assert service_display_name(raw) == expected


def test_mixed_reply_is_not_an_editable_card():
    reply = BookingReply("Выберите время", {"booking_card": "draft", "reply_markup": {"inline_keyboard": []}})
    assert "booking_card" not in reply.outbound_options("Цена 1000. Выберите время")
    assert reply.outbound_options(reply.text)["booking_card"] == "draft"
    assert not reply.outbound_options(reply.text).get("edit_booking_card")
    assert reply.outbound_options(reply.text, callback=True)["edit_booking_card"] is True


def test_display_normalizes_only_labels_and_headings():
    from types import SimpleNamespace
    coordinator = TelegramBookingCoordinator(None, None, None, None)
    raw = '  комплекс   " fresh дня "  '
    assert coordinator._service_choice(SimpleNamespace(service_id="123", service_name=raw)) == {
        "service_id": "123", "label": "комплекс «Фреш день»"
    }
    now = datetime.now(UTC)
    scenario = BookingScenario(uuid4(), "create", "collecting", "key", "42", {
        "service_name": raw, "date": "2026-09-07", "step": "slot", "choices": []
    }, None, now, now)
    assert "комплекс «Фреш день»" in coordinator._slot_header(scenario)
    options = coordinator._inline_options([[('Выбрать', coordinator._callback(scenario, "slot", 0))]])
    assert options["booking_card"] == str(scenario.id)
    assert scenario.state["service_name"] == raw
