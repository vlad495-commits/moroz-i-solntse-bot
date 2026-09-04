from datetime import UTC, datetime
from moroz.booking.telegram import TelegramBookingCoordinator
from moroz.booking.telegram import normalize_russian_phone
from moroz.booking.models import Slot
from moroz.booking.models import BookingScenario
from dataclasses import replace
from uuid import uuid4


def test_slots_from_database_render_in_moscow():
    slot = Slot('slot', ('1',), '1', datetime(2026, 9, 7, 9, tzinfo=UTC), 60)
    choice = TelegramBookingCoordinator._slot_choice(slot)
    assert choice['label'] == '12:00'
    assert choice['starts_at'].endswith('+03:00')


def test_question_containing_phone_is_not_a_contact_field():
    assert normalize_russian_phone('Мой телефон +79991234567, куда я записан?') is None


def test_callback_changes_when_same_step_has_different_choices():
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    scenario = BookingScenario(uuid4(), 'create', 'collecting', 'key', '42',
        {'step': 'slot', 'choices': [{'slot_id': 'first', 'label': '12:00'}]}, None, now, now)
    changed = replace(scenario, state={'step': 'slot', 'choices': [{'slot_id': 'second', 'label': '13:00'}]})
    first = TelegramBookingCoordinator._callback(scenario, 'slot', 0)
    second = TelegramBookingCoordinator._callback(changed, 'slot', 0)
    assert first != second
    assert len(first.encode()) <= 64
