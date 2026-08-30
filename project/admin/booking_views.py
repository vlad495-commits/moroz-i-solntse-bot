"""Safe presentation helpers for the read-only booking centre."""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo


BOOKING_VIEWS = {"upcoming", "attention", "history"}
BOOKING_SOURCES = {"all", "bot", "other"}
BOOKING_RECONCILIATION_FILTERS = {"all", "mismatch"}
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
    "booking_completed": "Клиент пришёл",
    "booking_no_show": "Клиент не пришёл",
    "booking_rescheduled": "Запись перенесена",
    "slot_unavailable": "Слот уже недоступен",
    "admin_attention_required": "Требуется помощь администратора",
}
BOOKING_SOURCE_LABELS = {
    "bot": "Создано ботом",
    "other": "Другой канал",
    "unknown": "Источник не подтверждён",
}
BOOKING_RECONCILIATION_LABELS = {
    "in_sync": "Синхронизировано",
    "changed_in_yclients": "Изменено в YCLIENTS",
    "yclients_only": "Только в YCLIENTS",
    "local_missing": "Нет локальной записи",
    "provider_missing": "Нет в YCLIENTS",
    "identity_conflict": "Требуется сверка",
    "freshness_unknown": "Синхронизация ещё не выполнялась",
}
PROJECTION_FAILURE_LABELS = {
    "yclients_transport": "Сервис сверки временно недоступен",
    "yclients_http_status": "Сервис сверки вернул ошибку",
    "yclients_response_shape": "Ответ сервиса сверки не удалось обработать",
    "yclients_page_bound": "Сверка превысила безопасный объём данных",
    "yclients_projection_write": "Результат сверки не удалось сохранить",
}
MOSCOW = ZoneInfo("Europe/Moscow")
CALENDAR_START_HOUR = 7
CALENDAR_END_HOUR = 22
_DAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_PHONE_RE = re.compile(r"^\+?\d{10,16}$")
_BOOKING_ACTION_STATUSES = {"completed", "no_show", "cancelled"}


def week_bounds(
    value: str | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("booking week")
    try:
        selected = date.fromisoformat(value) if value else current.astimezone(MOSCOW).date()
    except (TypeError, ValueError) as error:
        raise ValueError("booking week") from error
    monday = selected - timedelta(days=selected.weekday())
    local_start = datetime.combine(monday, time.min, tzinfo=MOSCOW)
    return local_start.astimezone(UTC), (local_start + timedelta(days=7)).astimezone(UTC)


def calendar_layout(
    items: list[dict[str, object]],
    week_start: date,
) -> list[dict[str, object]]:
    days = [
        {
            "date": week_start + timedelta(days=offset),
            "label": _DAY_NAMES[offset],
            "items": [],
        }
        for offset in range(7)
    ]
    for item in items:
        starts_at = item.get("starts_at")
        if not isinstance(starts_at, datetime) or starts_at.tzinfo is None:
            continue
        local_start = starts_at.astimezone(MOSCOW)
        day_index = (local_start.date() - week_start).days
        if day_index not in range(7):
            continue
        end_at = item.get("scheduled_end_at")
        local_end = (
            end_at.astimezone(MOSCOW)
            if isinstance(end_at, datetime) and end_at.tzinfo is not None
            else None
        )
        duration = (
            max(1, int((local_end - local_start).total_seconds() // 60))
            if local_end is not None and local_end > local_start
            else 60
        )
        card = dict(item)
        card.update(
            top=max(
                0,
                local_start.hour * 60
                + local_start.minute
                - CALENDAR_START_HOUR * 60,
            ),
            height=max(36, duration),
            time_label=(
                local_start.strftime("%H:%M")
                if local_end is None
                else f"{local_start:%H:%M}–{local_end:%H:%M}"
            ),
        )
        days[day_index]["items"].append(card)
    return days


def validate_manual_booking(
    *,
    customer_name: str,
    customer_phone: str,
    service_staff: str,
    starts_at: str,
    consent: str,
    comment: str,
    now: datetime | None = None,
) -> dict[str, object]:
    try:
        name = customer_name.strip()
        phone = customer_phone.strip().replace(" ", "").replace("-", "")
        service_id, staff_id = service_staff.split(":", 1)
        local_start = datetime.fromisoformat(starts_at).replace(tzinfo=MOSCOW)
        current = (now or datetime.now(UTC)).astimezone(MOSCOW)
        note = comment.strip()
        if (
            not 1 <= len(name) <= 100
            or not _PHONE_RE.fullmatch(phone)
            or not _canonical_provider_id(service_id)
            or not _canonical_provider_id(staff_id)
            or local_start <= current
            or local_start > current + timedelta(days=365)
            or consent != "yes"
            or len(note) > 500
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("manual booking") from error
    return {
        "customer_name": name,
        "customer_phone": phone,
        "service_id": service_id,
        "staff_id": staff_id,
        "starts_at": local_start.isoformat(),
        "personal_data_processing_allowed": True,
        "comment": note or None,
    }


def validate_booking_status_action(
    external_id: str,
    status: str,
) -> tuple[str, str]:
    if not _canonical_provider_id(external_id) or status not in _BOOKING_ACTION_STATUSES:
        raise ValueError("booking action")
    return external_id, status


def _canonical_provider_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and value[0] != "0"
        and len(value) <= 64
    )


def projection_failure_label(code: object) -> str:
    if not isinstance(code, str):
        return "Сверку не удалось выполнить"
    return PROJECTION_FAILURE_LABELS.get(code, "Сверку не удалось выполнить")


def validate_booking_filters(
    view: str,
    status: str | None,
    source: str = "all",
    reconciliation: str = "all",
) -> tuple[str, str | None, str, str]:
    if not isinstance(view, str) or view not in BOOKING_VIEWS:
        raise ValueError("booking view")
    if status is not None and (
        not isinstance(status, str) or status not in BOOKING_STATUS_LABELS
    ):
        raise ValueError("booking status")
    if not isinstance(source, str) or source not in BOOKING_SOURCES:
        raise ValueError("booking source")
    if (
        not isinstance(reconciliation, str)
        or reconciliation not in BOOKING_RECONCILIATION_FILTERS
    ):
        raise ValueError("booking reconciliation")
    return view, status, source, reconciliation


def encode_booking_cursor(sort_at: datetime, row_key: str) -> str:
    if not isinstance(sort_at, datetime) or sort_at.tzinfo is None or sort_at.utcoffset() is None:
        raise ValueError("booking cursor")
    _validate_booking_row_key(row_key)
    value = json.dumps(
        {"v": 2, "at": sort_at.isoformat(), "key": row_key},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(value).decode()


def decode_booking_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.b64decode(value + padding, altchars=b"-_", validate=True)
        )
        if not isinstance(payload, dict) or set(payload) != {"v", "at", "key"}:
            raise ValueError
        if payload["v"] != 2 or type(payload["v"]) is not int:
            raise ValueError
        if not isinstance(payload["at"], str) or not isinstance(payload["key"], str):
            raise ValueError
        sort_at = datetime.fromisoformat(payload["at"])
        if sort_at.tzinfo is None or sort_at.utcoffset() is None:
            raise ValueError
        return sort_at, _validate_booking_row_key(payload["key"])
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("booking cursor") from error


def _validate_booking_row_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("booking cursor")
    if value.startswith("y:"):
        external_id = value[2:]
        if (
            not external_id
            or len(external_id) > 64
            or not external_id.isascii()
            or not external_id.isdigit()
            or external_id[0] == "0"
        ):
            raise ValueError("booking cursor")
        return value
    if value.startswith("l:"):
        booking_id = value[2:]
        try:
            parsed = UUID(booking_id)
        except ValueError as error:
            raise ValueError("booking cursor") from error
        if booking_id != str(parsed):
            raise ValueError("booking cursor")
        return value
    raise ValueError("booking cursor")


def normalize_booking_row(
    row: Mapping[str, object], *, detail: bool = False
) -> dict[str, object]:
    status = str(row["status"])
    kind = row.get("kind")
    phase = row.get("phase")
    source_value = row.get("source", "bot")
    source = (
        source_value
        if isinstance(source_value, str) and source_value in BOOKING_SOURCE_LABELS
        else "unknown"
    )
    reconciliation_value = row.get("reconciliation_state", "in_sync")
    reconciliation = (
        reconciliation_value
        if isinstance(reconciliation_value, str)
        and reconciliation_value in BOOKING_RECONCILIATION_LABELS
        else "identity_conflict"
    )
    customer_chat_id = _canonical_customer_chat_id(row["customer_id"])
    detail_id = row.get("detail_id", row.get("id"))
    result = {
        "id": detail_id,
        "detail_id": detail_id,
        "row_key": row.get("row_key"),
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
        "scenario_label": (
            None if kind is None else SCENARIO_LABELS.get(str(kind), "Системный сценарий")
        ),
        "phase_label": (
            None if phase is None else PHASE_LABELS.get(str(phase), "Неизвестное состояние")
        ),
        "error_label": None if row.get("error_code") is None else "Требуется проверка",
        "source": source,
        "source_label": BOOKING_SOURCE_LABELS[source],
        "reconciliation_state": reconciliation,
        "reconciliation_label": BOOKING_RECONCILIATION_LABELS[reconciliation],
        "client_name": _safe_optional_text(row.get("client_name")),
        "staff_name": _safe_optional_text(row.get("staff_name")),
        "service_names": _safe_service_names(row.get("service_names")),
    }
    if detail:
        result["external_id"] = row.get("external_id")
    return result


def _safe_optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _safe_service_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


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
