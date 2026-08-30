from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import booking_views
from jinja2 import Environment, FileSystemLoader

from booking_views import (
    calendar_layout,
    decode_booking_cursor,
    encode_booking_cursor,
    normalize_booking_event,
    normalize_booking_row,
    validate_booking_status_action,
    validate_booking_filters,
    validate_manual_booking,
    week_bounds,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
BOOKING_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_bookings_template_compiles():
    templates = Path(__file__).parents[3] / "admin" / "templates"
    Environment(loader=FileSystemLoader(templates)).get_template("bookings.html")


def test_week_bounds_use_moscow_monday_and_reject_bad_date():
    start, end = week_bounds("2026-08-14", now=NOW)

    assert start == datetime(2026, 8, 9, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 16, 21, 0, tzinfo=UTC)
    assert week_bounds(None, now=NOW) == (start, end)
    with pytest.raises(ValueError, match="booking week"):
        week_bounds("14.08.2026", now=NOW)


def test_calendar_layout_groups_cards_and_positions_them_by_moscow_time():
    week_start = date(2026, 8, 10)
    items = [
        {
            "starts_at": datetime(2026, 8, 10, 7, 30, tzinfo=UTC),
            "scheduled_end_at": datetime(2026, 8, 10, 8, 15, tzinfo=UTC),
            "client_name": "Анна",
        },
        {
            "starts_at": datetime(2026, 8, 16, 18, 0, tzinfo=UTC),
            "scheduled_end_at": None,
            "client_name": "Ирина",
        },
    ]

    layout = calendar_layout(items, week_start)

    assert [day["date"] for day in layout] == [
        week_start + timedelta(days=offset) for offset in range(7)
    ]
    assert layout[0]["items"][0]["time_label"] == "10:30–11:15"
    assert layout[0]["items"][0]["top"] == 630
    assert layout[0]["items"][0]["height"] == 45
    assert layout[6]["items"][0]["time_label"] == "21:00"
    assert layout[6]["items"][0]["height"] == 60


def test_calendar_layout_separates_overlapping_cards_and_keeps_full_day_visible():
    week_start = date(2026, 8, 10)
    items = [
        {
            "starts_at": datetime(2026, 8, 10, hour, 0, tzinfo=UTC),
            "scheduled_end_at": datetime(2026, 8, 10, hour + 1, 0, tzinfo=UTC),
        }
        for hour in (0, 0, 20)
    ]

    cards = calendar_layout(items, week_start)[0]["items"]

    assert cards[0]["left_percent"] == 0
    assert cards[1]["left_percent"] == 50
    assert cards[0]["width_percent"] == cards[1]["width_percent"] == 50
    assert cards[0]["top"] == 180
    assert cards[2]["top"] == 1380


def test_manual_booking_validation_returns_bounded_worker_payload():
    payload = validate_manual_booking(
        customer_name="  Анна  ",
        customer_phone=" +79990000000 ",
        service_staff="331:6544",
        starts_at="2026-09-01T12:30",
        consent="yes",
        comment="  Позвонить заранее  ",
        now=NOW,
    )

    assert payload == {
        "customer_name": "Анна",
        "customer_phone": "+79990000000",
        "service_id": "331",
        "staff_id": "6544",
        "starts_at": "2026-09-01T12:30:00+03:00",
        "personal_data_processing_allowed": True,
        "comment": "Позвонить заранее",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"customer_name": ""},
        {"customer_phone": "private"},
        {"service_staff": "331"},
        {"starts_at": "2026-08-01T12:00"},
        {"consent": ""},
        {"comment": "x" * 501},
    ],
)
def test_manual_booking_validation_rejects_invalid_input(changes):
    values = {
        "customer_name": "Анна",
        "customer_phone": "+79990000000",
        "service_staff": "331:6544",
        "starts_at": "2026-09-01T12:30",
        "consent": "yes",
        "comment": "",
        "now": NOW,
    }
    with pytest.raises(ValueError, match="manual booking"):
        validate_manual_booking(**{**values, **changes})


def test_status_action_allowlist_accepts_only_provider_ids_and_terminal_statuses():
    assert validate_booking_status_action("9001", "completed") == (
        "9001",
        "completed",
    )
    assert validate_booking_status_action("9001", "no_show") == (
        "9001",
        "no_show",
    )
    assert validate_booking_status_action("9001", "cancelled") == (
        "9001",
        "cancelled",
    )
    for external_id, status in (("0", "completed"), ("abc", "completed"), ("1", "confirmed")):
        with pytest.raises(ValueError, match="booking action"):
            validate_booking_status_action(external_id, status)


def test_filter_allowlist():
    assert booking_views.BOOKING_SOURCES == {"all", "bot", "other"}
    assert booking_views.BOOKING_RECONCILIATION_FILTERS == {"all", "mismatch"}
    assert validate_booking_filters("attention", "unknown", "bot", "mismatch") == (
        "attention",
        "unknown",
        "bot",
        "mismatch",
    )
    with pytest.raises(ValueError, match="booking view"):
        validate_booking_filters("private", None)
    with pytest.raises(ValueError, match="booking status"):
        validate_booking_filters("upcoming", "private")
    with pytest.raises(ValueError, match="booking source"):
        validate_booking_filters("upcoming", None, "private", "all")
    with pytest.raises(ValueError, match="booking reconciliation"):
        validate_booking_filters("upcoming", None, "all", "private")
    with pytest.raises(ValueError, match="booking source"):
        validate_booking_filters("upcoming", None, [], "all")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("yclients_transport", "Сервис сверки временно недоступен"),
        ("yclients_http_status", "Сервис сверки вернул ошибку"),
        ("yclients_response_shape", "Ответ сервиса сверки не удалось обработать"),
        ("yclients_page_bound", "Сверка превысила безопасный объём данных"),
        ("yclients_projection_write", "Результат сверки не удалось сохранить"),
        ("private-provider-body", "Сверку не удалось выполнить"),
    ],
)
def test_projection_failure_label_uses_only_local_allowlist(code, expected):
    label = booking_views.projection_failure_label(code)

    assert set(booking_views.PROJECTION_FAILURE_LABELS) == {
        "yclients_transport",
        "yclients_http_status",
        "yclients_response_shape",
        "yclients_page_bound",
        "yclients_projection_write",
    }
    assert label == expected
    assert code not in label


def test_cursor_round_trip_and_rejects_malformed_values():
    encoded = encode_booking_cursor(NOW, "y:123")
    assert decode_booking_cursor(encoded) == (NOW, "y:123")
    local = encode_booking_cursor(NOW, f"l:{BOOKING_ID}")
    assert decode_booking_cursor(local) == (NOW, f"l:{BOOKING_ID}")
    for value in ("", "not-base64", "e30=", "A" * 257):
        with pytest.raises(ValueError, match="booking cursor"):
            decode_booking_cursor(value)
    for key in ("x:123", "y:", "y:0", "y:01", "l:not-a-uuid", "y:" + "1" * 65):
        with pytest.raises(ValueError, match="booking cursor"):
            encode_booking_cursor(NOW, key)


def test_cursor_requires_aware_datetime_and_exact_payload_shape():
    with pytest.raises(ValueError, match="booking cursor"):
        encode_booking_cursor(NOW.replace(tzinfo=None), "y:123")
    with pytest.raises(ValueError, match="booking cursor"):
        decode_booking_cursor("eyJhdCI6ICIyMDI2LTA4LTE0VDEyOjAwOjAwKzAwOjAwIiwgImlkIjogImFhYWFhYWFhLWFhYWEtYWFhYS1hYWFhLWFhYWFhYWFhYWFhYWEiLCAiZXh0cmEiOiB0cnVlfQ==")
    legacy = "eyJhdCI6IjIwMjYtMDgtMTRUMTI6MDA6MDArMDA6MDAiLCJpZCI6ImFhYWFhYWFhLWFhYWEtYWFhYS1hYWFhLWFhYWFhYWFhYWFhYWEifQ=="
    with pytest.raises(ValueError, match="booking cursor"):
        decode_booking_cursor(legacy)
    assert decode_booking_cursor(None) is None


def test_booking_and_event_normalization_hide_unknown_raw_values():
    booking = normalize_booking_row(
        {
            "id": BOOKING_ID,
            "customer_id": "42",
            "starts_at": NOW,
            "scheduled_end_at": None,
            "status": "private-status",
            "updated_at": NOW,
            "kind": "private-kind",
            "phase": "private-phase",
            "error_code": "private-error",
            "external_id": "provider-secret",
            "source": "private-source",
            "reconciliation_state": "private-reconciliation",
        }
    )
    event = normalize_booking_event(
        {
            "id": BOOKING_ID,
            "event_type": "private-event",
            "created_at": NOW,
        }
    )
    assert booking["status_label"] == "Неизвестный статус"
    assert booking["scenario_label"] == "Системный сценарий"
    assert booking["phase_label"] == "Неизвестное состояние"
    assert booking["error_label"] == "Требуется проверка"
    assert booking["source"] == "unknown"
    assert booking["source_label"] == "Источник не подтверждён"
    assert booking["reconciliation_state"] == "identity_conflict"
    assert booking["reconciliation_label"] == "Требуется сверка"
    assert "provider-secret" not in repr(booking)
    assert "private-source" not in repr(booking)
    assert "private-reconciliation" not in repr(booking)
    assert event["title"] == "Системное событие"
    assert "private-event" not in repr(event)


def test_booking_detail_can_include_external_id():
    booking = normalize_booking_row(
        {
            "id": BOOKING_ID,
            "customer_id": "42",
            "starts_at": NOW,
            "scheduled_end_at": None,
            "status": "confirmed",
            "updated_at": NOW,
            "kind": "create",
            "phase": "confirmed",
            "error_code": None,
            "external_id": "provider-42",
        },
        detail=True,
    )
    assert booking["status_label"] == "Подтверждена"
    assert booking["scenario_label"] == "Создание записи"
    assert booking["phase_label"] == "Подтверждено"
    assert booking["error_label"] is None
    assert booking["external_id"] == "provider-42"


def test_booking_normalization_exposes_only_canonical_telegram_chat_ids():
    base_row = {
        "id": BOOKING_ID,
        "starts_at": NOW,
        "scheduled_end_at": None,
        "status": "confirmed",
        "updated_at": NOW,
        "kind": "create",
        "phase": "confirmed",
        "error_code": None,
    }

    numeric = normalize_booking_row({**base_row, "customer_id": "42"})
    incompatible = normalize_booking_row(
        {**base_row, "customer_id": "external:alice@example.test"}
    )

    assert numeric["customer_chat_id"] == 42
    assert numeric["customer_label"] == "Клиент #42"
    assert incompatible["customer_chat_id"] is None
    assert incompatible["customer_label"] == "Клиент"
    assert "customer_id" not in incompatible
    assert "external:alice@example.test" not in repr(incompatible)

    for unsafe_value in (" 42", "+42", "042", "42.0", "external:42"):
        booking = normalize_booking_row({**base_row, "customer_id": unsafe_value})
        assert booking["customer_chat_id"] is None
