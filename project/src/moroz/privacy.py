"""Shared contracts for active customer-data deletion."""

DELETION_MARKER_TTL_SECONDS = 300


def deletion_marker_key(channel: str, chat_id: str) -> str:
    return f"privacy:deleting:{channel}:{chat_id}"
