from decimal import Decimal

from moroz.booking.catalog import match_catalog
from moroz.booking.yclients_catalog import CatalogRecord


def record(service_id, name, *, staff_id="10", staff="Анна", price="1000", duration=30):
    return CatalogRecord(
        service_id,
        staff_id,
        name,
        "Категория",
        staff,
        Decimal(price),
        Decimal(price),
        duration,
    )


def test_exact_price_question_groups_staff_variants_without_llm_decision():
    records = (
        record("20", "Криотерапия", staff_id="10", staff="Анна", price="1230"),
        record("20", "Криотерапия", staff_id="11", staff="Мария", price="1500"),
        record("21", "Прессотерапия"),
    )

    result = match_catalog(records, "Сколько СТОИТ криотерапия?")

    assert result.status == "fresh"
    assert result.simple_kind == "price"
    assert result.ambiguous is False
    assert [service.service_id for service in result.services] == ["20"]
    assert [variant.staff_name for variant in result.services[0].variants] == [
        "Анна", "Мария",
    ]


def test_normalizes_yo_and_punctuation_for_duration_question():
    result = match_catalog(
        (record("20", "Всё тело", duration=45),),
        "Сколько времени длится ВСЕ-ТЕЛО?",
    )

    assert result.simple_kind == "duration"
    assert result.services[0].service_name == "Всё тело"


def test_exact_multiword_phrase_beats_longer_token_overlap():
    result = match_catalog(
        (
            record("20", "Всё тело"),
            record("21", "Тело всё плюс"),
        ),
        "Сколько стоит всё тело?",
    )

    assert result.ambiguous is False
    assert [item.service_id for item in result.services] == ["20"]


def test_generic_equal_match_is_ambiguous_and_does_not_choose_price():
    result = match_catalog(
        (record("20", "Массаж лица"), record("21", "Массаж спины")),
        "Сколько стоит массаж?",
    )

    assert result.simple_kind == "price"
    assert result.ambiguous is True
    assert [item.service_name for item in result.services] == [
        "Массаж лица", "Массаж спины",
    ]


def test_comparison_disables_simple_reply_and_limits_deterministic_candidates():
    records = tuple(
        record(str(index), f"Крио процедура {index}") for index in range(1, 8)
    )

    result = match_catalog(records, "Сравни крио процедуры: что лучше выбрать?")

    assert result.simple_kind is None
    assert result.ambiguous is False
    assert len(result.services) == 5
    assert [item.service_id for item in result.services] == ["1", "2", "3", "4", "5"]


def test_unrelated_or_only_generic_catalog_word_returns_no_candidates():
    records = (record("20", "Массаж лица"),)

    assert match_catalog(records, "Как до вас добраться?").services == ()
    assert match_catalog(records, "Какие услуги есть?").services == ()


def test_exact_short_service_name_is_not_lost_by_token_filter():
    result = match_catalog(
        (record("20", "RF"),),
        "Сколько стоит RF?",
    )

    assert result.simple_kind == "price"
    assert [item.service_id for item in result.services] == ["20"]
