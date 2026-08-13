from datetime import UTC, datetime

from customer_events import normalize_customer_event


NOW = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)


def test_normalizes_known_customer_events():
    message = normalize_customer_event(
        {
            "source": "message",
            "source_id": "12",
            "occurred_at": NOW,
            "kind": "message.user",
            "description": "Хочу записаться",
            "status": None,
        }
    )
    booking = normalize_customer_event(
        {
            "source": "booking",
            "source_id": "event-1",
            "occurred_at": NOW,
            "kind": "booking.booking_confirmed",
            "description": None,
            "status": "confirmed",
        }
    )

    assert message == {
        "event_id": "message:12",
        "occurred_at": NOW,
        "category": "message",
        "kind": "message.user",
        "title": "Сообщение клиента",
        "description": "Хочу записаться",
        "status": None,
    }
    assert booking["category"] == "booking"
    assert booking["title"] == "Запись подтверждена"


def test_unknown_kind_is_neutral_and_does_not_copy_extra_fields():
    event = normalize_customer_event(
        {
            "source": "scheduler",
            "source_id": "job-1",
            "occurred_at": NOW,
            "kind": "secret_internal_kind",
            "description": None,
            "status": "failed",
            "payload": {"phone": "+79990000000"},
        }
    )

    assert event == {
        "event_id": "scheduler:job-1",
        "occurred_at": NOW,
        "category": "notification",
        "kind": "unknown",
        "title": "Системное событие",
        "description": None,
        "status": "failed",
    }


def test_maps_handoff_and_admin_categories_without_raw_details():
    handoff = normalize_customer_event(
        {
            "source": "escalation",
            "source_id": "esc-1",
            "occurred_at": NOW,
            "kind": "handoff.opened",
            "description": "low_feedback_rating",
            "status": "open",
        }
    )
    admin = normalize_customer_event(
        {
            "source": "admin",
            "source_id": "9",
            "occurred_at": NOW,
            "kind": "admin.customer.note",
            "description": None,
            "status": None,
            "ip_address": "127.0.0.1",
        }
    )

    assert handoff["category"] == "handoff"
    assert handoff["title"] == "Передано администратору"
    assert admin["category"] == "admin"
    assert "ip_address" not in admin
