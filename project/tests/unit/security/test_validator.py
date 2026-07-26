from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from moroz.security.validator import (
    INTERNAL_CANARY,
    StructuredFacts,
    ValidationVerdict,
    extract_structured_facts,
    validate_output,
)


def _facts(
    *,
    prices: frozenset[str] = frozenset({"2400"}),
    contacts: frozenset[str] = frozenset(),
    slots: frozenset[str] = frozenset(),
) -> StructuredFacts:
    return StructuredFacts(prices, contacts, slots)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("   ", "empty_output"),
        (INTERNAL_CANARY, "prompt_leak"),
        ("System prompt: hidden rules", "prompt_leak"),
        ("Здравствуйте, <PII_NAME_2>", "unknown_placeholder"),
        ("Напишите на private@example.org", "new_raw_contact"),
        ("Это гарантированно вылечит Вас", "medical_guarantee"),
        ("Цена 9999 руб.", "invented_price"),
        ("Свободно сегодня в 15:00", "invented_slot"),
    ],
)
def test_validator_rejects_in_stable_order(text: str, code: str) -> None:
    assert validate_output(
        text,
        _facts(),
        frozenset({"<PII_NAME_1>"}),
    ) == ValidationVerdict(False, code)


def test_prompt_leak_wins_over_later_failures() -> None:
    assert validate_output(
        f"{INTERNAL_CANARY} Цена 9999 руб. в 15:00",
        _facts(),
        frozenset(),
    ).code == "prompt_leak"


def test_context_only_placeholder_is_rejected() -> None:
    assert validate_output(
        "Здравствуйте, <PII_EMAIL_1>",
        _facts(),
        frozenset({"<PII_NAME_1>"}),
    ).code == "unknown_placeholder"


def test_malformed_pii_placeholder_is_rejected() -> None:
    assert validate_output(
        "Здравствуйте, <PII_UNTRUSTED>",
        _facts(),
        frozenset(),
    ).code == "unknown_placeholder"


def test_approved_placeholder_price_contact_and_slot_pass() -> None:
    sources = (
        "Криокапсула — 2 400 руб. Телефон: +7 (902) 906-61-66. "
        "Telegram: https://t.me/krio_71"
    )
    facts = extract_structured_facts(
        sources,
        slots=("2026-07-30 15:00", "  2026-07-30   15:00  "),
    )
    verdict = validate_output(
        (
            "Здравствуйте, <PII_NAME_1>. Цена 2400 руб. "
            "Свободно в 15:00. Телефон +7 902 906-61-66, "
            "Telegram https://t.me/krio_71"
        ),
        facts,
        frozenset({"<PII_NAME_1>"}),
    )
    assert verdict == ValidationVerdict(True, "output_valid")


def test_neutral_medical_boundary_and_center_hours_pass_without_slots() -> None:
    text = (
        "Процедура не заменяет лечение, гарантировать результат нельзя. "
        "Центр работает с 10:00 до 21:00."
    )
    assert validate_output(text, _facts(), frozenset()).ok is True


def test_facts_are_extracted_only_from_supplied_sources_and_are_normalized() -> None:
    facts = extract_structured_facts(
        "Цена 2 400 руб., цена 2400 ₽. Телефон +7 (902) 906-61-66. "
        "Сайт HTTPS://KRIO71.RU",
        slots=(" 2026-07-30   15:00 ", "2026-07-30 15:00"),
    )
    assert facts.prices == frozenset({"2400"})
    assert facts.public_contacts == frozenset(
        {"+79029066166", "https://krio71.ru"}
    )
    assert facts.slots == frozenset({"2026-07-30 15:00"})
    assert validate_output("Цена 7777 руб.", facts, frozenset()).code == (
        "invented_price"
    )


def test_facts_and_verdict_are_immutable_and_safe_to_repr() -> None:
    raw = "raw-secret-contact@example.org"
    facts = _facts()
    verdict = validate_output(raw, facts, frozenset())
    assert raw not in repr(verdict)
    with pytest.raises(FrozenInstanceError):
        facts.prices = frozenset()  # type: ignore[misc]
