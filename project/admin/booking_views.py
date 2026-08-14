"""Safe presentation helpers for the read-only booking centre."""

import base64
import binascii
import json
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID


BOOKING_VIEWS = {"upcoming", "attention", "history"}
BOOKING_STATUS_LABELS = {
    "confirmed": "Подтверждена",
    "cancelled": "Отменена",
    "completed": "Завершена",
    "no_show": "Клиент не пришёл",
    "unknown": "Статус неизвестен",
}
SCENARIO_LABELS = {
    "create": "Создание записи",
    "reschedule": "Перенос записи",
    "cancel": "Отмена записи",
}
PHASE_LABELS = {
    "collecting": "Сбор данных",
    "awaiting_confirmation": "Ожидает подтверждения",
    "executing": "Выполняется",
    "confirmed": "Подтверждено",
    "failed": "Ошибка",
    "escalated": "Передано администратору",
}
EVENT_TITLES = {
    "booking_scenario_created": "Сценарий начат",
    "booking_execution_started": "Операция начата",
    "booking_confirmed": "Запись подтверждена",
    "booking_cancelled": "Запись отменена",
    "booking_rescheduled": "Запись перенесена",
    "slot_unavailable": "Слот уже недоступен",
    "admin_attention_required": "Требуется помощь администратора",
}


def validate_booking_filters(view: str, status: str | None) -> tuple[str, str | None]:
    if view not in BOOKING_VIEWS:
        raise ValueError("booking view")
    if status is not None and status not in BOOKING_STATUS_LABELS:
        raise ValueError("booking status")
    return view, status


def encode_booking_cursor(sort_at: datetime, booking_id: UUID) -> str:
    if not isinstance(sort_at, datetime) or sort_at.tzinfo is None or sort_at.utcoffset() is None:
        raise ValueError("booking cursor")
    value = json.dumps(
        {"at": sort_at.isoformat(), "id": str(booking_id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(value).decode()


def decode_booking_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value:
            raise ValueError
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.b64decode(value + padding, altchars=b"-_", validate=True)
        )
        if not isinstance(payload, dict) or set(payload) != {"at", "id"}:
            raise ValueError
        if not isinstance(payload["at"], str) or not isinstance(payload["id"], str):
            raise ValueError
        sort_at = datetime.fromisoformat(payload["at"])
        if sort_at.tzinfo is None or sort_at.utcoffset() is None:
            raise ValueError
        return sort_at, UUID(payload["id"])
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("booking cursor") from error


def normalize_booking_row(
    row: Mapping[str, object], *, detail: bool = False
) -> dict[str, object]:
    status = str(row["status"])
    kind = str(row["kind"])
    phase = str(row["phase"])
    customer_chat_id = _canonical_customer_chat_id(row["customer_id"])
    result = {
        "id": row["id"],
        "customer_chat_id": customer_chat_id,
        "customer_label": (
            f"Клиент #{customer_chat_id}"
            if customer_chat_id is not None
            else "Клиент"
        ),
        "starts_at": row["starts_at"],
        "scheduled_end_at": row.get("scheduled_end_at"),
        "status": status if status in BOOKING_STATUS_LABELS else "unknown",
        "status_label": BOOKING_STATUS_LABELS.get(status, "Неизвестный статус"),
        "updated_at": row["updated_at"],
        "scenario_label": SCENARIO_LABELS.get(kind, "Системный сценарий"),
        "phase_label": PHASE_LABELS.get(phase, "Неизвестное состояние"),
        "error_label": None if row.get("error_code") is None else "Требуется проверка",
    }
    if detail:
        result["external_id"] = row.get("external_id")
    return result


def _canonical_customer_chat_id(value: object) -> int | None:
    """Return an ID only when it is the canonical value accepted by /chats/{int}."""
    if not isinstance(value, str):
        return None
    try:
        chat_id = int(value)
    except ValueError:
        return None
    return chat_id if str(chat_id) == value else None


def normalize_booking_event(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": EVENT_TITLES.get(str(row["event_type"]), "Системное событие"),
    }
