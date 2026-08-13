"""Safe presentation model for a customer's read-only event timeline."""

from collections.abc import Mapping


SOURCE_CATEGORIES = {
    "message": "message",
    "booking": "booking",
    "scheduler": "notification",
    "escalation": "handoff",
    "human_mode": "handoff",
    "admin": "admin",
}

EVENT_TITLES = {
    "message.user": "Сообщение клиента",
    "message.assistant": "Ответ бота",
    "booking.booking_scenario_created": "Начат сценарий записи",
    "booking.booking_execution_started": "Начато изменение записи",
    "booking.booking_confirmed": "Запись подтверждена",
    "booking.booking_cancelled": "Запись отменена",
    "booking.booking_rescheduled": "Запись перенесена",
    "booking.slot_unavailable": "Выбранное время недоступно",
    "booking.admin_attention_required": "Требуется помощь администратора",
    "scheduler.scheduled": "Уведомление запланировано",
    "scheduler.finished": "Уведомление отправлено",
    "scheduler.skipped": "Уведомление пропущено",
    "scheduler.failed": "Ошибка уведомления",
    "handoff.opened": "Передано администратору",
    "handoff.resolved": "Обращение администратора закрыто",
    "handoff.enabled": "Включён ручной режим",
    "admin.customer.note": "Заметка администратора",
}

SAFE_REASON_LABELS = {
    "low_feedback_rating": "Низкая оценка после визита",
}
SAFE_HANDOFF_SOURCES = {
    "feedback": "Обратная связь",
    "booking": "Запись",
}
DEFAULT_HANDOFF_REASON = "Требуется помощь администратора"


def safe_handoff_reason(reason_code: object) -> str:
    return SAFE_REASON_LABELS.get(str(reason_code), DEFAULT_HANDOFF_REASON)


def safe_handoff_source(source: object) -> str:
    return SAFE_HANDOFF_SOURCES.get(str(source), "Система")


def _safe_description(source: str, value: object) -> object:
    if source == "message":
        return value
    if source in {"escalation", "human_mode"}:
        return SAFE_REASON_LABELS.get(str(value))
    return None


def normalize_customer_event(row: Mapping[str, object]) -> dict[str, object]:
    source = str(row["source"])
    raw_kind = str(row["kind"])
    known = raw_kind in EVENT_TITLES
    return {
        "event_id": f"{source}:{row['source_id']}",
        "occurred_at": row["occurred_at"],
        "category": SOURCE_CATEGORIES.get(source, "admin"),
        "kind": raw_kind if known else "unknown",
        "title": EVENT_TITLES.get(raw_kind, "Системное событие"),
        "description": _safe_description(source, row.get("description")),
        "status": row.get("status"),
    }
