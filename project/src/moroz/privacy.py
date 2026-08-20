"""Shared contracts for active customer-data deletion."""

DELETION_MARKER_TTL_SECONDS = 300
DELETION_OPERATION_TIMEOUT_SECONDS = 240


def deletion_marker_key(channel: str, chat_id: str) -> str:
    return f"privacy:deleting:{channel}:{chat_id}"


def deletion_lock_key(channel: str, chat_id: str) -> str:
    return f"lock:privacy-delete:{channel}:{chat_id}"


def customer_lock_subject(chat_id: str) -> str:
    return str(chat_id)
