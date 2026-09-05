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


def test_exact_title_without_action_words_wins_before_candidate_limit():
    records = tuple(
        record(str(index), f"Солярий {index + 10} минут")
        for index in range(1, 7)
    ) + (record("99", "Солярий 10 минут"),)

    result = match_catalog(records, "СОЛЯРИЙ | 10 МИНУТ!")

    assert result.ambiguous is False
    assert [item.service_id for item in result.services] == ["99"]


def test_reversed_declined_walk_in_query_keeps_exact_requested_minutes():
    result = match_catalog(
        (
            record("10", "Солярий 10 минут", price="700", duration=10),
            record("16", "Солярий 16 минут", price="900", duration=16),
            record("20", "Солярий 20 минут", price="1100", duration=20),
        ),
        "Сколько стоят 10 минут солярия?",
    )

    assert result.simple_kind == "price"
    assert result.ambiguous is False
    assert [item.service_name for item in result.services] == ["Солярий 10 минут"]


def test_unknown_walk_in_duration_has_no_closest_substitution():
    result = match_catalog(
        (
            record("10", "Солярий 10 минут", duration=10),
            record("20", "Солярий 20 минут", duration=20),
        ),
        "Сколько стоит солярий 99 минут?",
    )

    assert result.simple_kind == "price"
    assert result.services == ()


def test_generic_walk_in_family_does_not_match_other_minute_services():
    result = match_catalog(
        (
            record("10", "Солярий 10 минут", duration=10),
            record("20", "Солярий 20 минут", duration=20),
            record("30", "Коллариум 10 минут", duration=10),
            record("40", "Вода 10 минут", duration=10),
        ),
        "Сколько стоит солярий?",
    )

    assert result.ambiguous is True
    assert [item.service_name for item in result.services] == [
        "Солярий 10 минут",
        "Солярий 20 минут",
    ]


def test_walk_in_families_and_singular_minute_are_resolved_exactly():
    records = (
        record("7", "Коллариум 7 минут", duration=7),
        record("8", "Коллариум 8 минут", duration=8),
        record("1", "Коллагенарий 1 минута", duration=1),
        record("2", "Коллагенарий 2 минуты", duration=2),
    )

    collarium = match_catalog(records, "Коллариум 7 минут")
    collagenarium = match_catalog(records, "Коллагенарий 1 минута")

    assert [item.service_id for item in collarium.services] == ["7"]
    assert [item.service_id for item in collagenarium.services] == ["1"]


def test_numeric_duration_disambiguates_non_walk_in_tariffs():
    result = match_catalog(
        (
            record("30", "Вода | 30 минут", duration=30),
            record("60", "Вода | 60 минут", duration=60),
        ),
        "Сколько стоит вода 30 минут?",
    )

    assert result.ambiguous is False
    assert [item.service_id for item in result.services] == ["30"]


def test_two_explicit_service_names_are_not_reduced_to_one_top_score():
    result = match_catalog(
        (
            record("20", "Криомассаж головы", price="1200"),
            record("21", "Прессотерапия", price="1500"),
            record("22", "Криомассаж лица", price="1300"),
        ),
        "Сколько стоят криомассаж головы и прессотерапия?",
    )

    assert result.simple_kind == "price"
    assert result.ambiguous is False
    assert result.multiple_requested is True
    assert [item.service_id for item in result.services] == ["20", "21"]
    assert result.direct_reply is None


def test_walk_in_duration_does_not_drop_second_explicit_service():
    result = match_catalog(
        (
            record("10", "Солярий 10 минут", duration=10),
            record("16", "Солярий 16 минут", duration=16),
            record("21", "Прессотерапия", duration=30),
        ),
        "Сколько стоят Солярий 10 минут и прессотерапия?",
    )

    assert result.multiple_requested is True
    assert [item.service_id for item in result.services] == ["21", "10"]


def test_declined_walk_in_duration_does_not_drop_second_explicit_service():
    result = match_catalog(
        (
            record("10", "Солярий 10 минут", duration=10),
            record("16", "Солярий 16 минут", duration=16),
            record("21", "Прессотерапия", duration=30),
        ),
        "Сколько стоят 10 минут солярия и прессотерапия?",
    )

    assert result.multiple_requested is True
    assert [item.service_id for item in result.services] == ["21", "10"]


def test_unknown_non_walk_in_duration_has_no_closest_substitution():
    result = match_catalog(
        (
            record("30", "Водородотерапия 30 минут", duration=30),
            record("60", "Водородотерапия 60 минут", duration=60),
        ),
        "Сколько стоит водородотерапия 99 минут?",
    )

    assert result.simple_kind == "price"
    assert result.services == ()


def test_direct_price_reply_is_concise_and_retains_missing_price_intent():
    found = match_catalog(
        (record("20", "Солярий 10 минут", price="700", duration=10),),
        "Сколько стоит солярий 10 минут?",
    )
    missing = match_catalog(
        (record("20", "Солярий 10 минут", price="700", duration=10),),
        "Сколько стоит коллариум 7 минут?",
    )

    assert found.direct_reply == "«Солярий 10 минут» — 700 ₽, 10 мин."
    assert missing.direct_reply == "Чтобы назвать цену, уточните услугу."


def test_catalog_data_labels_staff_neutrally_and_omits_missing_category():
    result = match_catalog(
        (
            CatalogRecord(
                "20", "10", "Криотерапия", None, "Кабинет 1",
                Decimal("1230"), Decimal("1230"), 3,
            ),
        ),
        "Криотерапия",
    )

    data = result.data_block()

    assert "Ресурс/специалист: Кабинет 1" in data
    assert "Категория:" not in data
    assert "Варианты:" not in data
