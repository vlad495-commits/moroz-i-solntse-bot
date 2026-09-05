from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from zoneinfo import ZoneInfo

from moroz.booking.models import Slot
from moroz.messaging.router import RouteDecision


MOSCOW = ZoneInfo("Europe/Moscow")
Requirement = Literal["service", "date", "time", "slot", "contact", "name", "confirm"]

_SLOT_FIELDS = (
    "available_slots",
    "selected_slot_id",
    "selected_date",
    "starts_at",
    "new_starts_at",
)


def _normalise(value: object) -> str:
    return str(value or "").strip().casefold().replace("ё", "е")


def _clear_slot(state: dict[str, object]) -> None:
    for key in _SLOT_FIELDS:
        state.pop(key, None)


def merge_draft(
    state: Mapping[str, object],
    decision: RouteDecision,
) -> dict[str, object]:
    merged = dict(state)

    if len(decision.services) > 1:
        merged["service_candidates"] = list(decision.services)
        for key in (
            "service_query", "service_id", "service_name",
            "staff_query", "staff_id", "staff_name",
        ):
            merged.pop(key, None)
        _clear_slot(merged)
    elif len(decision.services) == 1:
        query = decision.services[0].strip()
        previous = merged.get("service_name", merged.get("service_query"))
        if _normalise(query) != _normalise(previous):
            for key in (
                "service_id", "service_name", "staff_query", "staff_id", "staff_name",
            ):
                merged.pop(key, None)
            _clear_slot(merged)
        merged["service_query"] = query
        merged.pop("service_candidates", None)

    if decision.date is not None and decision.date != merged.get("date"):
        merged["date"] = decision.date
        _clear_slot(merged)

    if decision.time_from is not None or decision.time_to is not None:
        requested = (decision.time_from, decision.time_to)
        previous = (merged.get("time_from"), merged.get("time_to"))
        if requested != previous:
            merged["time_from"], merged["time_to"] = requested
            _clear_slot(merged)
        merged.pop("time_needs_clarification", None)

    if decision.staff is not None:
        query = decision.staff.strip()
        previous = merged.get("staff_name", merged.get("staff_query"))
        if _normalise(query) != _normalise(previous):
            merged.pop("staff_id", None)
            merged.pop("staff_name", None)
            _clear_slot(merged)
        merged["staff_query"] = query

    return merged


def next_requirement(state: Mapping[str, object]) -> Requirement:
    if state.get("service_candidates") or not state.get("service_id"):
        return "service"
    if not state.get("date"):
        return "date"
    if state.get("time_needs_clarification") is True:
        return "time"
    if not state.get("selected_slot_id"):
        return "slot"
    if not state.get("customer_phone") or state.get("processing_consent") is not True:
        return "contact"
    if not state.get("customer_name"):
        return "name"
    return "confirm"


def filter_slots(
    slots: Sequence[Slot],
    time_from: str | None,
    time_to: str | None,
) -> list[Slot]:
    return [
        slot
        for slot in slots
        if (time_from is None or slot.starts_at.astimezone(MOSCOW).strftime("%H:%M") >= time_from)
        and (time_to is None or slot.starts_at.astimezone(MOSCOW).strftime("%H:%M") <= time_to)
    ]
