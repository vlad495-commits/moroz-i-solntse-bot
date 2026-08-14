from datetime import UTC, datetime
from uuid import UUID

import pytest

from booking_views import (
    decode_booking_cursor,
    encode_booking_cursor,
    normalize_booking_event,
    normalize_booking_row,
    validate_booking_filters,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
BOOKING_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_filter_allowlist():
    assert validate_booking_filters("attention", "unknown") == (
        "attention",
        "unknown",
    )
    with pytest.raises(ValueError, match="booking view"):
        validate_booking_filters("private", None)
    with pytest.raises(ValueError, match="booking status"):
        validate_booking_filters("upcoming", "private")


def test_cursor_round_trip_and_rejects_malformed_values():
    encoded = encode_booking_cursor(NOW, BOOKING_ID)
    assert decode_booking_cursor(encoded) == (NOW, BOOKING_ID)
    for value in ("", "not-base64", "e30="):
        with pytest.raises(ValueError, match="booking cursor"):
            decode_booking_cursor(value)


def test_cursor_requires_aware_datetime_and_exact_payload_shape():
    with pytest.raises(ValueError, match="booking cursor"):
        encode_booking_cursor(NOW.replace(tzinfo=None), BOOKING_ID)
    with pytest.raises(ValueError, match="booking cursor"):
        decode_booking_cursor("eyJhdCI6ICIyMDI2LTA4LTE0VDEyOjAwOjAwKzAwOjAwIiwgImlkIjogImFhYWFhYWFhLWFhYWEtYWFhYS1hYWFhLWFhYWFhYWFhYWFhYWEiLCAiZXh0cmEiOiB0cnVlfQ==")
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
    assert "provider-secret" not in repr(booking)
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
