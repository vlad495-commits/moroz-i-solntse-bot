from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from moroz.messaging.router import RouteDecision, route_message


@pytest.mark.parametrize(
    ("text", "intents"),
    [
        ("Мне списали деньги, хочу пожаловаться", ("complaint",)),
        ("У меня обморок и сильная боль", ("medical_risk",)),
        ("Отмените мою запись", ("booking_cancel",)),
        ("Запись нужно отменить", ("booking_cancel",)),
        ("Перенесите запись на другой день", ("booking_change",)),
        ("Запись хочу перенести на другой день", ("booking_change",)),
        ("Хочу записаться завтра", ("booking",)),
        ("Сколько стоит криокапсула?", ("faq",)),
        ("Расскажи анекдот", ("unknown",)),
    ],
)
def test_router_recognizes_supported_intents(
    text: str,
    intents: tuple[str, ...],
) -> None:
    assert route_message(text) == RouteDecision(intents, False)


def test_normal_faq_and_booking_keep_approved_priority() -> None:
    assert route_message("Сколько стоит крио и хочу записаться").intents == (
        "booking",
        "faq",
    )


def test_risk_and_complaint_precede_booking_and_faq_without_duplicates() -> None:
    decision = route_message(
        "У меня сильная боль, списали деньги, хочу записаться, сколько стоит?"
    )
    assert decision.intents == (
        "complaint",
        "medical_risk",
        "booking",
        "faq",
    )
    assert len(decision.intents) == len(set(decision.intents))


def test_change_and_cancel_conflict_requires_clarification() -> None:
    assert route_message(
        "Хочу перенести или отменить запись"
    ) == RouteDecision(
        ("booking_cancel", "booking_change"),
        True,
    )


def test_router_is_pure_immutable_and_does_not_echo_input() -> None:
    text = "неизвестный секретный пользовательский маркер"
    decision = route_message(text)
    assert text not in repr(decision)
    with pytest.raises(FrozenInstanceError):
        decision.requires_clarification = False  # type: ignore[misc]
