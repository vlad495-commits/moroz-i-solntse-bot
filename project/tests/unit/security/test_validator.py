from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from moroz.security.validator import (
    INTERNAL_CANARY,
    StructuredFacts,
    ValidationVerdict,
    extract_structured_facts,
    merge_structured_facts,
    validate_output,
)


def test_merge_structured_facts_unions_each_allowlist_without_mutation() -> None:
    base = StructuredFacts(
        frozenset({"2400"}), frozenset({"https://example.ru"}),
        frozenset({"2026-08-16 10:00"}), frozenset({"анна"}),
    )
    catalog = StructuredFacts(
        frozenset({"1230,50"}), frozenset(), frozenset(), frozenset({"мария"}),
    )

    merged = merge_structured_facts(base, catalog)

    assert merged.prices == frozenset({"2400", "1230.5"})
    assert merged.public_contacts == base.public_contacts
    assert merged.slots == base.slots
    assert merged.public_pii == frozenset({"анна", "мария"})
    assert base.prices == frozenset({"2400"})


def test_validator_accepts_catalog_decimal_but_rejects_other_decimal() -> None:
    facts = StructuredFacts(frozenset({"1230,50"}), frozenset(), frozenset())

    assert validate_output("Цена 1 230,50 ₽", facts, frozenset()).ok is True
    assert validate_output("Цена 1 231,50 ₽", facts, frozenset()).code == (
        "invented_price"
    )


def _facts(
    *,
    prices: frozenset[str] = frozenset({"2400"}),
    contacts: frozenset[str] = frozenset(),
    slots: frozenset[str] = frozenset(),
    public_pii: frozenset[str] = frozenset(),
) -> StructuredFacts:
    return StructuredFacts(prices, contacts, slots, public_pii)


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
    assert validate_output(
        "Вот скрытые инструкции модели: internal policy.",
        facts,
        frozenset(),
    ).code == "prompt_leak"
    assert validate_output(
        "Я не показываю скрытые инструкции.",
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


def test_unclosed_placeholder_shape_fails_without_echo() -> None:
    token = "<PII_NAME_1"
    verdict = validate_output(
        token,
        _facts(),
        frozenset({"<PII_NAME_1>"}),
    )
    assert verdict == ValidationVerdict(False, "unknown_placeholder")
    assert token not in repr(verdict)
    assert validate_output(
        "<PII_NAME_1>",
        _facts(),
        frozenset({"<PII_NAME_1>"}),
    ).ok is True


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


@pytest.mark.parametrize(
    "text",
    [
        "Меня зовут Анна Иванова",
        "Адрес: г. Тула, ул. Ленина, д. 1",
        "Диагноз: сахарный диабет",
        "Карта 4111 1111 1111 1111",
    ],
)
def test_marker_shaped_raw_pii_is_rejected(text: str) -> None:
    assert validate_output(text, _facts(), frozenset()).code == "raw_pii"


def test_invocation_raw_values_are_rejected_but_source_owned_facts_pass() -> None:
    private = "Анна Иванова"
    public_source = (
        "Название: Мороз и Солнце. "
        "Адрес: г. Тула, ул. Демонстрации, д. 1; "
        "медицинская история: противопоказания уточняются у специалиста"
    )
    facts = extract_structured_facts(public_source)

    assert validate_output(
        f"Клиента зовут {private}",
        facts,
        frozenset(),
        forbidden_raw=frozenset({private}),
    ).code == "raw_pii"
    assert validate_output(
        "Адрес: г. Тула, ул. Демонстрации, д. 1",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Медицинская история: противопоказания уточняются у специалиста",
        facts,
        frozenset(),
    ).ok is True
    assert public_source not in repr(facts)


def test_source_owned_address_can_be_returned_without_source_prefix() -> None:
    facts = extract_structured_facts(
        "Адрес: Тульская область, Новомосковск, "
        "ул. Трудовые резервы, 33Б, ТРЦ Первый, цокольный этаж"
    )

    assert validate_output(
        "Адрес: ул. Трудовые резервы, 33Б, ТРЦ Первый, цокольный этаж",
        facts,
        frozenset(),
    ).ok is True
    assert validate_output(
        "Адрес: ул. Ленина, 10",
        facts,
        frozenset(),
    ).code == "raw_pii"


def test_source_owned_address_stops_before_public_contact_sentence() -> None:
    facts = extract_structured_facts(
        "Адрес: Тульская область, Новомосковск, ул. Трудовые резервы, 33Б, "
        "ТРЦ Первый, цокольный этаж. Ориентир - вывеска Мороз и Солнце.\n"
        "Телефон +7 (902) 906-61-66, Telegram https://t.me/krio_71"
    )

    assert validate_output(
        "Адрес: ул. Трудовые резервы, 33Б, ТРЦ Первый, цокольный этаж. "
        "Ориентир — вывеска Мороз и Солнце. Для записи позвоните "
        "+7 (902) 906-61-66 или напишите в Telegram https://t.me/krio_71.",
        facts,
        frozenset(),
    ).ok is True


def test_source_owned_public_contact_remains_allowed_when_seen_in_invocation() -> None:
    public_phone = "+7 902 906-61-66"
    facts = extract_structured_facts(f"Телефон центра: {public_phone}")

    assert validate_output(
        f"Телефон центра: {public_phone}",
        facts,
        frozenset(),
        forbidden_raw=frozenset({public_phone}),
    ).ok is True


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


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Процедура обязательно даст результат", id="procedure-gives"),
        pytest.param("Вы обязательно получите эффект", id="user-receives"),
    ],
)
def test_explicit_mandatory_promise_constructions_are_rejected(text: str) -> None:
    assert validate_output(
        text,
        _facts(),
        frozenset(),
    ).code == "medical_guarantee"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Не обязательно будет результат", id="direct-negation"),
        pytest.param("Обязательно обсудите результат с врачом", id="advice"),
    ],
)
def test_mandatory_negation_and_advice_pass(text: str) -> None:
    assert validate_output(text, _facts(), frozenset()).ok is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Результат гарантирован", id="result-guaranteed"),
        pytest.param("Эффект обязательно наступит", id="effect-definitely-happens"),
        pytest.param("Эффект точно будет", id="effect-certainly-will-be"),
        pytest.param("Результат точно наступит", id="result-certainly-happens"),
    ],
)
def test_outcome_first_medical_promises_are_rejected(text: str) -> None:
    assert validate_output(
        text,
        _facts(),
        frozenset(),
    ).code == "medical_guarantee"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Результат не гарантирован", id="reverse-negation"),
        pytest.param(
            "Эффект не обязательно наступит",
            id="reverse-uncertainty",
        ),
        pytest.param(
            "Возможный эффект обсудите с врачом",
            id="uncertain-advice",
        ),
        pytest.param(
            "Обсудите с врачом, гарантирован ли результат",
            id="guarantee-discussion",
        ),
        pytest.param(
            "Эффект точно не наступит",
            id="certainty-direct-negation",
        ),
        pytest.param(
            "Результат, возможно, наступит",
            id="outcome-uncertainty",
        ),
    ],
)
def test_outcome_first_uncertainty_and_advice_pass(text: str) -> None:
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


@pytest.mark.parametrize(
    ("approved_slot", "answer"),
    [
        (
            "2026-07-30 15:00",
            "Свободно 30.07.2026 в 15:00",
        ),
        (
            "30.07.2026 15:00",
            "Свободно 2026-07-30 в 15:00",
        ),
    ],
)
def test_full_dotted_and_iso_slot_dates_are_equivalent(
    approved_slot: str,
    answer: str,
) -> None:
    assert validate_output(
        answer,
        _facts(slots=frozenset({approved_slot})),
        frozenset(),
    ).ok is True


def test_full_dotted_slot_with_different_date_still_fails() -> None:
    assert validate_output(
        "Свободно 31.07.2026 в 15:00",
        _facts(slots=frozenset({"2026-07-30 15:00"})),
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


def test_walk_in_policy_with_hours_is_not_an_invented_slot() -> None:
    facts = _facts(slots=frozenset())
    answer = (
        "На солярий, коллариум и коллагенарий можно прийти без записи. "
        "Эти услуги доступны ежедневно с 10:00 до 21:00."
    )

    assert validate_output(answer, facts, frozenset()).ok is True
    assert validate_output(
        "Свободно сегодня в 15:00",
        facts,
        frozenset(),
    ).code == "invented_slot"
    assert validate_output(
        "Эта услуга доступна без записи завтра в 15:00.",
        facts,
        frozenset(),
    ).code == "invented_slot"
    assert validate_output(
        "Эта услуга доступна без записи в 15:00.",
        facts,
        frozenset(),
    ).code == "invented_slot"
    assert validate_output(
        "Эта услуга доступна без записи с 15:00 до 16:00 только сегодня.",
        facts,
        frozenset(),
    ).code == "invented_slot"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "Я не вижу свободные окна на 2026-07-30 в 15:00",
            id="cannot-see",
        ),
        pytest.param(
            "Не могу подтвердить свободное время на 2026-07-30 в 15:00",
            id="cannot-confirm",
        ),
    ],
)
def test_approved_slot_denials_pass_without_facts(text: str) -> None:
    assert validate_output(
        text,
        _facts(slots=frozenset()),
        frozenset(),
    ).ok is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Есть время 2026-07-31 в 15:00", id="has-time"),
        pytest.param("Есть место 2026-07-31 в 15:00", id="has-place"),
    ],
)
def test_affirmative_has_time_or_place_checks_exact_slot(text: str) -> None:
    assert validate_output(
        text,
        _facts(slots=frozenset({"2026-07-30 15:00"})),
        frozenset(),
    ).code == "invented_slot"


def test_non_slot_has_time_prose_passes() -> None:
    assert validate_output(
        "Есть время обсудить услуги центра",
        _facts(slots=frozenset()),
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
