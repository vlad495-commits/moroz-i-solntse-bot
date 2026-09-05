import pytest
from aiogram.types import ReplyKeyboardMarkup

from moroz.booking.telegram import persistent_menu_command
from moroz.messaging.booking_stop import STOPPED_ACTION_REPLY
from moroz.messaging.router import RouteDecision, deterministic_route
from moroz.messaging.telegram import main_menu_options


@pytest.mark.parametrize("label, canonical, route, action", [
    ("🗓 Записаться", "📅 Записаться", "booking", "none"),
    ("📅 Записаться", "📅 Записаться", "booking", "none"),
    ("✨ Подобрать", "🧭 Подобрать процедуру", "consultation", "none"),
    ("🧭 Подобрать процедуру", "🧭 Подобрать процедуру", "consultation", "none"),
    ("🏷 Услуги и цены", "✨ Услуги и цены", "consultation", "none"),
    ("✨ Услуги и цены", "✨ Услуги и цены", "consultation", "none"),
    ("📋 Мои записи", "📋 Мои записи", "booking_management", "view"),
    ("📍 Адрес и режим", "📍 Адрес и режим", "consultation", "none"),
    ("💬 Администратор", "👩‍💼 Связаться с администратором", "escalation", "none"),
    ("👩‍💼 Связаться с администратором", "👩‍💼 Связаться с администратором", "escalation", "none"),
    ("👩‍💼 Позвать администратора", "👩‍💼 Позвать администратора", "escalation", "none"),
])
def test_new_and_legacy_menu_labels_share_worker_commands_and_routes(label, canonical, route, action):
    assert persistent_menu_command(f"  {label}\n") == canonical
    assert deterministic_route(f"  {label}\n") == RouteDecision(route, 1.0, action)


@pytest.mark.parametrize("text", ["Подобрать", "Хочу записаться", "✨ Подобрать услугу", ""])
def test_menu_aliases_do_not_capture_free_text(text):
    assert persistent_menu_command(text) is None
    assert deterministic_route(text) is None


def test_menu_serializes_three_rows_and_only_booking_is_green():
    keyboard = ReplyKeyboardMarkup.model_validate(main_menu_options()["reply_markup"])
    payload = keyboard.model_dump(exclude_none=True)
    assert payload["keyboard"] == [
        [{"text": "🗓 Записаться", "style": "success"}, {"text": "✨ Подобрать"}],
        [{"text": "🏷 Услуги и цены"}, {"text": "📋 Мои записи"}],
        [{"text": "📍 Адрес и режим"}, {"text": "💬 Администратор"}],
    ]
    assert payload["resize_keyboard"] is True
    assert payload["is_persistent"] is True
    assert "🗓 Записаться" in STOPPED_ACTION_REPLY
