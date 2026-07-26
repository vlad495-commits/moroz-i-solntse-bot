from types import MappingProxyType

import pytest

from moroz.security.pii import (
    PiiSession,
    UnknownPlaceholder,
    find_raw_pii,
)


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
