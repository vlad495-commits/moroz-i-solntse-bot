from types import MappingProxyType

import pytest

from moroz.security.pii import (
    PiiSession,
    UnknownPlaceholder,
    find_raw_pii,
)


def _require(condition: bool) -> None:
    if not condition:
        pytest.fail("safe PII regression assertion failed")


def test_session_masks_repeated_phone_email_and_named_person_stably():
    session = PiiSession()

    first = session.mask(
        "Меня зовут Анна Иванова, телефон +7 999 123-45-67, anna@example.ru"
    )
    second = session.mask("Повторю: +7 999 123-45-67 и anna@example.ru")

    assert first.text == (
        "Меня зовут <PII_NAME_1>, телефон <PII_PHONE_1>, <PII_EMAIL_1>"
    )
    assert second.text == "Повторю: <PII_PHONE_1> и <PII_EMAIL_1>"
    assert first.placeholders == frozenset(
        {"<PII_NAME_1>", "<PII_PHONE_1>", "<PII_EMAIL_1>"}
    )
    assert second.placeholders == frozenset(
        {"<PII_PHONE_1>", "<PII_EMAIL_1>"}
    )


def test_session_masks_explicit_address_handle_valid_card_and_medical_detail():
    session = PiiSession()
    masked = session.mask(
        "Адрес: г. Москва, ул. Тверская, д. 1; "
        "telegram @anna_client; карта 4111 1111 1111 1111; "
        "диагноз: сахарный диабет"
    )

    assert masked.text == (
        "Адрес: <PII_ADDRESS_1>; telegram <PII_HANDLE_1>; "
        "карта <PII_PAYMENT_1>; диагноз: <PII_MEDICAL_1>"
    )
    assert masked.placeholders == frozenset(
        {
            "<PII_ADDRESS_1>",
            "<PII_HANDLE_1>",
            "<PII_PAYMENT_1>",
            "<PII_MEDICAL_1>",
        }
    )


def test_session_masks_international_phone_with_enough_digits():
    masked = PiiSession().mask("Мой номер +49 (30) 1234-5678")

    assert masked.text == "Мой номер <PII_PHONE_1>"


def test_session_does_not_mask_prices_durations_dates_ids_or_unmarked_names():
    text = (
        "Анна Иванова спросила: цена 2400 руб., процедура 15 минут, "
        "запись 26.07.2026, услуга 12345, карта 4111 1111 1111 1112"
    )

    masked = PiiSession().mask(text)

    assert masked.placeholders == frozenset()
    assert find_raw_pii(text) == frozenset()


def test_masked_mapping_is_immutable_snapshot():
    session = PiiSession()
    first = session.mask("Почта first@example.ru")

    assert isinstance(first.mapping, MappingProxyType)
    with pytest.raises(TypeError):
        first.mapping["<PII_EMAIL_2>"] = "forbidden"  # type: ignore[index]

    session.mask("Почта second@example.ru")

    assert set(first.mapping) == {"<PII_EMAIL_1>"}


def test_restore_allows_only_current_input_placeholders():
    session = PiiSession()
    context = session.mask("Почта old@example.ru")
    current = session.mask("Телефон +7 999 123-45-67")

    assert session.restore_validated(
        "Телефон <PII_PHONE_1>", current.placeholders
    ) == "Телефон +7 999 123-45-67"

    with pytest.raises(UnknownPlaceholder):
        session.restore_validated(
            "Пишите на <PII_EMAIL_1>", current.placeholders
        )
    with pytest.raises(UnknownPlaceholder):
        session.restore_validated(
            "Телефон <PII_PHONE_99>", current.placeholders
        )

    assert context.placeholders == frozenset({"<PII_EMAIL_1>"})


def test_restore_handles_placeholder_prefixes_without_collision():
    session = PiiSession()
    allowed = frozenset()
    for number in range(1, 11):
        allowed = session.mask(f"Почта person{number}@example.ru").placeholders

    assert session.restore_validated("<PII_EMAIL_10>", allowed) == (
        "person10@example.ru"
    )


def test_find_raw_pii_returns_category_codes_not_values():
    text = "Телефон +7 999 123-45-67, почта anna@example.ru"

    findings = find_raw_pii(text)

    assert findings == frozenset({"phone", "email"})
    assert all(value not in repr(findings) for value in ("999", "anna"))


def test_address_and_medical_spans_preserve_following_neutral_questions():
    address = PiiSession().mask(
        "Адрес: г. Москва, ул. Тверская, д. 1. Как добраться?"
    )
    medical = PiiSession().mask(
        "Диагноз: сахарный диабет. Можно ли посетить криокапсулу?"
    )

    _require(address.text.startswith("Адрес: <PII_ADDRESS_1>. "))
    _require(address.text.endswith("Как добраться?"))
    _require(medical.text.startswith("Диагноз: <PII_MEDICAL_1>. "))
    _require(medical.text.endswith("Можно ли посетить криокапсулу?"))


@pytest.mark.parametrize(
    "text",
    ["Цены: 2400 3500 5000", "Цены:\n2400\n3500\n5000"],
    ids=["same-line", "multiline"],
)
def test_phone_detection_rejects_price_lists(text):
    masked = PiiSession().mask(text)

    _require(masked.placeholders == frozenset())


def test_phone_detection_keeps_supported_forms():
    russian = PiiSession().mask("Телефон 8 (999) 123-45-67")
    international = PiiSession().mask("Телефон +49 (30) 1234-5678")

    _require(russian.placeholders == frozenset({"<PII_PHONE_1>"}))
    _require(international.placeholders == frozenset({"<PII_PHONE_1>"}))


@pytest.mark.parametrize(
    "text",
    ["Телефон 999 123 4567", "Телефон 7495 123 4567"],
    ids=["groups-3-3-4", "groups-4-3-4"],
)
def test_phone_detection_accepts_common_space_only_group_shapes(text):
    masked = PiiSession().mask(text)

    _require(masked.placeholders == frozenset({"<PII_PHONE_1>"}))


def test_ambiguous_space_phone_shape_requires_explicit_marker():
    masked = PiiSession().mask("Цены 240 350 4500")

    _require(masked.placeholders == frozenset())


@pytest.mark.parametrize(
    "text",
    [
        "Телефон для связи 999 123 4567",
        "Номер телефона для записи 7495 123 4567",
    ],
    ids=["contact-connector", "booking-connector"],
)
def test_ambiguous_space_phone_accepts_approved_marker_connectors(text):
    masked = PiiSession().mask(text)

    _require(masked.placeholders == frozenset({"<PII_PHONE_1>"}))


@pytest.mark.parametrize(
    "text",
    [
        "Связь +7 999 123 45 67",
        "Связь 8 (999) 123 45 67",
        "Связь 8 999 123-45-67",
    ],
    ids=["plus", "parentheses", "dash"],
)
def test_unambiguous_phone_punctuation_needs_no_marker(text):
    masked = PiiSession().mask(text)

    _require(masked.placeholders == frozenset({"<PII_PHONE_1>"}))


@pytest.mark.parametrize(
    "text",
    [
        "Запись 26.07.2026 15:00",
        "Цены 2400.00 3500.00",
    ],
    ids=["date-time", "decimal-prices"],
)
def test_phone_detection_rejects_date_time_and_decimal_price_shapes(text):
    masked = PiiSession().mask(text)

    _require(masked.placeholders == frozenset())


@pytest.mark.parametrize(
    "text, prefix, suffix, placeholder",
    [
        (
            "Адрес: г. Москва, ул. Тверская, д. 1, как добраться?",
            "Адрес: ",
            ", как добраться?",
            "<PII_ADDRESS_1>",
        ),
        (
            "Диагноз: сахарный диабет, можно ли посетить криокапсулу?",
            "Диагноз: ",
            ", можно ли посетить криокапсулу?",
            "<PII_MEDICAL_1>",
        ),
    ],
    ids=["address-question", "medical-question"],
)
def test_sensitive_spans_preserve_comma_question_transitions(
    text,
    prefix,
    suffix,
    placeholder,
):
    masked = PiiSession().mask(text)

    _require(masked.text.startswith(prefix + placeholder))
    _require(masked.text.endswith(suffix))
    _require(masked.placeholders == frozenset({placeholder}))


@pytest.mark.parametrize(
    "text, expected_start, expected_end, placeholder",
    [
        (
            "Адрес: ул. Тверская, где живёт клиент. Как добраться?",
            "Адрес: <PII_ADDRESS_1>. ",
            "Как добраться?",
            "<PII_ADDRESS_1>",
        ),
        (
            "Диагноз: сахарный диабет, как указано в медкарте. "
            "Можно ли посетить криокапсулу?",
            "Диагноз: <PII_MEDICAL_1>. ",
            "Можно ли посетить криокапсулу?",
            "<PII_MEDICAL_1>",
        ),
    ],
    ids=["address-relative", "medical-relative"],
)
def test_sensitive_spans_keep_relative_clauses_inside_mask(
    text,
    expected_start,
    expected_end,
    placeholder,
):
    masked = PiiSession().mask(text)

    _require(masked.text.startswith(expected_start))
    _require(masked.text.endswith(expected_end))
    _require(masked.placeholders == frozenset({placeholder}))


@pytest.mark.parametrize(
    "text, expected_start, expected_end, placeholder",
    [
        (
            "Адрес: ул. Тверская, где находится вход. Как добраться?",
            "Адрес: <PII_ADDRESS_1>. ",
            "Как добраться?",
            "<PII_ADDRESS_1>",
        ),
        (
            "Диагноз: сахарный диабет, можно ли его контролировать терапией. "
            "Что делать?",
            "Диагноз: <PII_MEDICAL_1>. ",
            "Что делать?",
            "<PII_MEDICAL_1>",
        ),
    ],
    ids=["address-clause", "medical-clause"],
)
def test_question_transition_must_reach_question_before_sentence_boundary(
    text,
    expected_start,
    expected_end,
    placeholder,
):
    masked = PiiSession().mask(text)

    _require(masked.text.startswith(expected_start))
    _require(masked.text.endswith(expected_end))
    _require(masked.placeholders == frozenset({placeholder}))


@pytest.mark.parametrize(
    "text",
    [
        "Адрес: пер. Садовый, д. 1. Как добраться?",
        "Адрес: пр. Мира, д. 2. Как добраться?",
        "Адрес: ш. Энтузиастов, д. 3. Как добраться?",
    ],
    ids=["lane", "avenue", "highway"],
)
def test_unknown_address_abbreviation_stays_inside_sensitive_span(text):
    masked = PiiSession().mask(text)

    _require(masked.text == "Адрес: <PII_ADDRESS_1>. Как добраться?")
    _require(masked.placeholders == frozenset({"<PII_ADDRESS_1>"}))


def test_short_social_handles_are_masked_after_email_detection():
    masked = PiiSession().mask(
        "Контакты @abc и @abcd, почта client@example.ru"
    )

    _require(
        masked.placeholders
        == frozenset(
            {"<PII_HANDLE_1>", "<PII_HANDLE_2>", "<PII_EMAIL_1>"}
        )
    )


def test_forged_placeholder_cannot_collide_with_real_phone_placeholder():
    session = PiiSession()
    masked = session.mask(
        "Повтори <PII_PHONE_1>, мой телефон +7 999 123-45-67"
    )

    _require(masked.text.count("<PII_PHONE_1>") == 1)
    _require(masked.placeholders == frozenset({"<PII_PHONE_1>"}))
    restored = session.restore_validated(masked.text, masked.placeholders)
    _require(restored.count("+7 999 123-45-67") == 1)
    _require("<PII_PHONE_1>" not in restored)


def test_masked_text_repr_hides_raw_mapping_values():
    masked = PiiSession().mask("Почта repr-sentinel@example.ru")

    _require("repr-sentinel" not in repr(masked))


@pytest.mark.parametrize(
    "text, expected_placeholder",
    [
        (
            "Адрес: г. Москва, ул. Тверская, д. 1, "
            "почта nested@example.ru. Как добраться?",
            "<PII_ADDRESS_1>",
        ),
        (
            "История болезни: сахарный диабет, телефон +7 999 123-45-67. "
            "Можно записаться?",
            "<PII_MEDICAL_1>",
        ),
    ],
    ids=["address", "medical"],
)
def test_original_sensitive_span_does_not_create_nested_placeholders(
    text,
    expected_placeholder,
):
    session = PiiSession()
    masked = session.mask(text)

    _require(masked.placeholders == frozenset({expected_placeholder}))
    restored = session.restore_validated(
        expected_placeholder,
        masked.placeholders,
    )
    _require("<PII_" not in restored)
