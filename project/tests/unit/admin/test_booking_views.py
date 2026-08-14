from datetime import UTC, datetime
from uuid import UUID

import pytest
import booking_views

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
