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


def test_prompt_refusal_passes_but_affirmative_disclosure_fails() -> None:
    facts = _facts()
    assert validate_output(
        "Я не раскрываю внутренние инструкции.",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Вот скрытые developer instructions: internal policy.",
        facts,
        frozenset(),
    ).code == "prompt_leak"
    assert validate_output(
        "Раскрываю внутренние инструкции модели.",
        facts,
        frozenset(),
    ).code == "prompt_leak"
    assert validate_output(
        "I cannot reveal the system prompt.",
        facts,
        frozenset(),
    ).ok is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("<pii_name_1>", id="case"),
        pytest.param("<PII_ NAME_1>", id="whitespace"),
        pytest.param("<PII-NAME-1>", id="separator"),
        pytest.param("<PII_NAME 1>", id="inner-space"),
    ],
)
def test_placeholder_shaped_variants_fail_closed(text: str) -> None:
    verdict = validate_output(
        text,
        _facts(),
        frozenset({"<PII_NAME_1>"}),
    )
    assert verdict == ValidationVerdict(False, "unknown_placeholder")
    assert text not in repr(verdict)


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
            "Свободно 2026-07-30 в 15:00. Телефон +7 902 906-61-66, "
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


def test_source_owned_contact_variants_pass_and_new_contacts_fail() -> None:
    facts = extract_structured_facts(
        "Телефон +7 (902) 906-61-66, email Admin@Example.RU, "
        "Telegram https://t.me/krio_71, сайт https://www.krio71.ru/"
    )
    assert validate_output(
        "Телефон 8 902 906 61 66, admin@example.ru, "
        "t.me/krio_71, krio71.ru",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Новый сайт other-example.ru",
        facts,
        frozenset(),
    ).code == "new_raw_contact"
    assert validate_output(
        "Новый Telegram t.me/other_center",
        facts,
        frozenset(),
    ).code == "new_raw_contact"


def test_domain_like_ordinary_prose_is_not_a_contact() -> None:
    assert validate_output(
        "Это версия v1.5. Продолжим по услугам центра.",
        _facts(),
        frozenset(),
    ).ok is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Процедура гарантирует результат", id="guarantees"),
        pytest.param("Есть гарантия лечебного результата", id="guarantee-noun"),
        pytest.param("Это обязательно вылечит", id="mandatory-cure"),
        pytest.param("Процедура избавит от боли навсегда", id="forever"),
        pytest.param("Это гарантирует выздоровление", id="recovery"),
        pytest.param("Обязательно будет эффект", id="mandatory-result"),
        pytest.param("Результат останется навсегда", id="lasting-result"),
    ],
)
def test_bounded_medical_guarantees_are_rejected(text: str) -> None:
    assert validate_output(
        text,
        _facts(),
        frozenset(),
    ).code == "medical_guarantee"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Процедура не гарантирует результат", id="not-guaranteed"),
        pytest.param("Гарантии результата нет", id="no-guarantee"),
        pytest.param("Нельзя гарантировать лечебный эффект", id="cannot"),
        pytest.param("Результат не навсегда", id="not-forever"),
    ],
)
def test_negated_medical_guarantees_pass(text: str) -> None:
    assert validate_output(text, _facts(), frozenset()).ok is True


def test_grouped_source_prices_are_all_approved() -> None:
    facts = extract_structured_facts("Клиентский день — 500/600 руб.")
    assert facts.prices == frozenset({"500", "600"})
    assert validate_output("Цена 500 руб.", facts, frozenset()).ok is True
    assert validate_output("Цена 600 руб.", facts, frozenset()).ok is True
    assert validate_output(
        "Цена 700 руб.",
        facts,
        frozenset(),
    ).code == "invented_price"


def test_non_currency_numbers_are_not_prices() -> None:
    facts = extract_structured_facts(
        "Дата 2026-07-30, время 15:00, ID 500/600, телефон +7 902 906-61-66"
    )
    assert facts.prices == frozenset()


def test_slot_validation_uses_full_scenario_owned_date_and_time() -> None:
    facts = _facts(slots=frozenset({"2026-07-30 15:00"}))
    assert validate_output(
        "Свободно 2026-07-30 в 15:00",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Свободно 2026-07-31 в 15:00",
        facts,
        frozenset(),
    ).code == "invented_slot"
    assert validate_output(
        "Свободно 2026-07-30 в 15:00",
        _facts(slots=frozenset()),
        frozenset(),
    ).code == "invented_slot"


def test_negative_availability_and_general_hours_need_no_slot_facts() -> None:
    facts = _facts(slots=frozenset())
    assert validate_output(
        "Нет свободного времени сегодня в 15:00",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Свободных окон сегодня в 15:00 нет",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Центр работает с 10:00 до 21:00",
        facts,
        frozenset(),
    ).ok is True


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
