import json
import logging
from uuid import UUID

from moroz.common.observability import event_payload, log_event, new_correlation_id


def test_event_payload_contains_stable_correlation_id():
    cid = new_correlation_id()
    payload = event_payload("message.accepted", cid, chat_id="42")
    assert json.loads(payload) == {
        "event": "message.accepted",
        "correlation_id": str(cid),
        "chat_id": "42",
    }


def test_new_correlation_id_returns_unique_uuids():
    first = new_correlation_id()
    second = new_correlation_id()

    assert isinstance(first, UUID)
    assert first != second


def test_log_event_writes_json_payload(caplog):
    logger = logging.getLogger("test.observability")
    correlation_id = new_correlation_id()

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "database.connected", correlation_id, service="bot")

    assert json.loads(caplog.records[-1].message) == {
        "event": "database.connected",
        "correlation_id": str(correlation_id),
        "service": "bot",
    }
