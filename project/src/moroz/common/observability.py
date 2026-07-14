import json
import logging
from uuid import UUID, uuid4


def new_correlation_id() -> UUID:
    return uuid4()


def event_payload(event: str, correlation_id: UUID, **fields: object) -> str:
    return json.dumps(
        {"event": event, "correlation_id": str(correlation_id), **fields},
        ensure_ascii=False,
        sort_keys=True,
    )


def log_event(
    logger: logging.Logger,
    event: str,
    correlation_id: UUID,
    **fields: object,
) -> None:
    logger.info(event_payload(event, correlation_id, **fields))
