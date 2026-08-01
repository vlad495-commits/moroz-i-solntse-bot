import json

import pytest

from moroz.booking.interaction import WorkflowReply
from moroz.booking.presenter import BookingPresenter, PresentedAction
from moroz.messaging.models import ScenarioResult


def test_inline_buttons_are_json_safe_and_contain_only_opaque_action_ids():
    reply = BookingPresenter().choice(
        "Выберите услугу",
        (
            PresentedAction("Крио", "opaque-a"),
            PresentedAction("Массаж", "opaque-b"),
        ),
    )

    encoded = json.dumps(reply.to_result(), ensure_ascii=False)
    callbacks = [
        button["callback_data"]
        for row in reply.delivery_options["reply_markup"]["inline_keyboard"]
        for button in row
    ]

    assert callbacks == ["booking:opaque-a", "booking:opaque-b"]
    assert "service-331" not in encoded
    assert "+70000000000" not in encoded


def test_contact_request_is_serializable_and_never_embeds_phone_data():
    reply = BookingPresenter().request_contact("Как вас зовут?")

    encoded = json.dumps(reply.to_result(), ensure_ascii=False)
    button = reply.delivery_options["reply_markup"]["keyboard"][0][0]

    assert button == {"text": "Отправить свой контакт", "request_contact": True}
    assert "+70000000000" not in encoded


def test_contact_request_keeps_collecting_back_and_cancel_navigation_visible():
    reply = BookingPresenter().request_contact("Отправьте контакт")

    keyboard = reply.delivery_options["reply_markup"]["keyboard"]

    assert keyboard[1] == [{"text": "Назад"}, {"text": "Отмена"}]


def test_workflow_reply_round_trips_as_a_durable_plain_result():
    original = BookingPresenter().plain("Готово")

    restored = WorkflowReply.from_result(
        json.loads(json.dumps(original.to_result(), ensure_ascii=False))
    )

    assert restored == original


@pytest.mark.parametrize(
    "action_id",
    ("", "booking:nested", "provider/42", "contains whitespace"),
)
def test_presenter_rejects_non_opaque_action_ids(action_id: str):
    with pytest.raises(ValueError):
        BookingPresenter().choice(
            "Выберите",
            (PresentedAction("Кнопка", action_id),),
        )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ScenarioResult("ok", "provider text", None, ()),
            "Запись подтверждена.",
        ),
        (
            ScenarioResult(
                "needs_input",
                "provider text",
                "choose_slot",
                (),
            ),
            "Выбранное время уже недоступно. Выберите время заново.",
        ),
        (
            ScenarioResult(
                "escalated",
                "provider text",
                None,
                (),
                "booking_outcome_unknown",
        ),
            (
                "Статус записи проверит администратор. Мы не обещаем слот, "
                "пока не получим однозначное подтверждение."
            ),
        ),
        (
            ScenarioResult("failed", "provider text", None, ()),
            "Не удалось выполнить запись. Попробуйте позже.",
        ),
    ],
)
def test_scenario_results_are_presented_with_deterministic_safe_text(
    result: ScenarioResult,
    expected: str,
):
    reply = BookingPresenter().scenario_result(result)

    assert reply.text == expected
    assert "provider text" not in reply.text
    if result.status != "ok":
        assert "подтверждена" not in reply.text.casefold()
