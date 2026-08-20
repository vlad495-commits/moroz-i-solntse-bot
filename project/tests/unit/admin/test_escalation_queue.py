import pytest
from uuid import uuid4

from customer_events import safe_handoff_reason, safe_handoff_source
from moroz.escalation.service import admin_reply_key, parse_admin_reply_key


def test_handoff_labels_allow_only_known_values():
    assert safe_handoff_reason("low_feedback_rating") == "Низкая оценка после визита"
    assert safe_handoff_reason("internal-secret") == "Требуется помощь администратора"
    assert safe_handoff_source("feedback") == "Обратная связь"
    assert safe_handoff_source("private-provider-name") == "Система"


@pytest.mark.parametrize("value", [None, "", object()])
def test_handoff_labels_hide_untrusted_values(value):
    assert safe_handoff_reason(value) == "Требуется помощь администратора"
    assert safe_handoff_source(value) == "Система"


def test_admin_reply_key_round_trips_exact_uuids():
    escalation_id = uuid4()
    reply_token = uuid4()

    key = admin_reply_key(escalation_id, reply_token)

    assert key == f"admin_handoff_reply:{escalation_id}:{reply_token}"
    assert parse_admin_reply_key(key) == (escalation_id, reply_token)


@pytest.mark.parametrize(
    "value",
    [
        "reply:other",
        "admin_handoff_reply:not-a-uuid:not-a-uuid",
        f"admin_handoff_reply:{uuid4()}",
        f"admin_handoff_reply:{uuid4()}:{uuid4()}:extra",
    ],
)
def test_admin_reply_key_parser_fails_closed(value):
    assert parse_admin_reply_key(value) is None
