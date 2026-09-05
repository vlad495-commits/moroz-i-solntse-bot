"""Durable booking-only STOP fence. Callers hold the customer advisory lock."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from moroz.security.consent import _trusted_telegram_sequence


STOPPED_ACTION_REPLY = (
    "Это действие было отправлено до команды «Стоп» и уже неактуально. "
    "Чтобы начать новое оформление, нажмите «📅 Записаться»."
)


def before_stop(update_id: str, payload: dict, stop) -> bool:
    """Use Telegram sequence; allow a later text after a possible week reset.

    Callback receipt time is not an event timestamp, so cannot prove a reset.
    """
    sequence = _trusted_telegram_sequence("telegram_explicit", update_id)
    stop_sequence = _trusted_telegram_sequence("telegram_explicit", stop["external_message_id"])
    occurred_at = datetime.fromisoformat(payload["received_at"])
    stop_at = datetime.fromisoformat(stop["payload"]["received_at"])
    if payload.get("kind", "text") != "callback":
        if occurred_at - stop_at >= timedelta(days=7):
            return False
        if stop_at - occurred_at >= timedelta(days=7):
            return True
    if sequence is not None and stop_sequence is not None:
        return sequence <= stop_sequence
    return occurred_at <= stop_at


async def stop_markers(connection, chat_id: str):
    rows = await connection.fetch(
        "SELECT external_message_id, payload FROM message_inbox "
        "WHERE channel='telegram' AND chat_id=$1 AND payload->>'kind'='booking_stop'",
        chat_id,
    )
    return [dict(row, payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"])
            for row in rows]


async def register_stop(connection, *, chat_id: str, update_id: str, occurred_at: datetime) -> bool:
    return await connection.fetchval(
        "INSERT INTO message_inbox "
        "(id,channel,external_message_id,chat_id,payload,status,correlation_id) "
        "VALUES ($1,'telegram',$2,$3,$4::jsonb,'processed',$5) "
        "ON CONFLICT (channel,external_message_id) DO NOTHING RETURNING id",
        uuid4(), update_id, chat_id,
        json.dumps({"kind": "booking_stop", "received_at": occurred_at.isoformat()}),
        uuid4(),
    ) is not None
