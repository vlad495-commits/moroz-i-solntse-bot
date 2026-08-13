import pytest

from customer_events import safe_handoff_reason, safe_handoff_source


def test_handoff_labels_allow_only_known_values():
    assert safe_handoff_reason("low_feedback_rating") == "Низкая оценка после визита"
    assert safe_handoff_reason("internal-secret") == "Требуется помощь администратора"
    assert safe_handoff_source("feedback") == "Обратная связь"
    assert safe_handoff_source("private-provider-name") == "Система"


@pytest.mark.parametrize("value", [None, "", object()])
def test_handoff_labels_hide_untrusted_values(value):
    assert safe_handoff_reason(value) == "Требуется помощь администратора"
    assert safe_handoff_source(value) == "Система"
