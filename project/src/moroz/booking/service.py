from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from moroz.booking.models import (
    BookingIdentity,
    BookingScenario,
    CreateBooking,
    ExternalBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.ports import BookingPort
from moroz.booking.repository import BookingRepository, BookingScenarioSession
from moroz.messaging.models import ScenarioResult


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BookingService:
    def __init__(
        self,
        port: BookingPort,
        repository: BookingRepository,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._port = port
        self._repository = repository
        self._now = now

    async def handle(
        self,
        scenario_id: UUID,
        *,
        confirmed: bool,
        identity: BookingIdentity | None = None,
    ) -> ScenarioResult:
        async with self._repository.serialized_scenario(scenario_id) as session:
            return await self._handle_serialized(session, confirmed=confirmed)

    async def _handle_serialized(
        self,
        session: BookingScenarioSession,
        *,
        confirmed: bool,
    ) -> ScenarioResult:
        scenario = session.scenario
        if scenario.kind != "create":
            raise NotImplementedError(f"{scenario.kind} is not implemented")
        if scenario.phase == "confirmed":
            booking = await session.get_local_booking()
            if booking is None:
                raise RuntimeError("confirmed scenario has no local booking")
            return self._confirmed_result(booking)
        if scenario.phase == "executing":
            return await self._escalate_unknown(session, scenario)
        if scenario.phase != "awaiting_confirmation":
            raise ValueError(f"unsupported create phase: {scenario.phase}")
        if not confirmed:
            return ScenarioResult(
                status="needs_input",
                message="Подтвердите запись.",
                next_action="confirm_booking",
                events=(),
            )

        executing = replace(
            scenario,
            phase="executing",
            updated_at=self._now(),
        )
        await session.checkpoint(executing, "booking_execution_started")
        query = self._slot_query(executing.state)
        slots = await self._port.list_slots(query)
        selected_slot_id = str(executing.state["selected_slot_id"])
        if not any(slot.id == selected_slot_id for slot in slots):
            return await self._slot_unavailable(
                session, executing, selected_slot_id, slots
            )

        try:
            booking = await self._port.create_booking(
                CreateBooking(
                    customer_id=executing.customer_id,
                    slot_id=selected_slot_id,
                    idempotency_key=executing.idempotency_key,
                )
            )
        except SlotUnavailable:
            fresh_slots = await self._port.list_slots(query)
            return await self._slot_unavailable(
                session, executing, selected_slot_id, fresh_slots
            )
        terminal = replace(
            executing,
            phase="confirmed",
            updated_at=self._now(),
        )
        await session.confirm(terminal, booking)
        return self._confirmed_result(booking)

    async def _slot_unavailable(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
        selected_slot_id: str,
        slots: list[Slot],
    ) -> ScenarioResult:
        alternatives = [self._slot_payload(slot) for slot in slots[:3]]
        payload = {
            "selected_slot_id": selected_slot_id,
            "alternatives": alternatives,
        }
        collecting = replace(
            scenario,
            phase="collecting",
            updated_at=self._now(),
        )
        await session.checkpoint(collecting, "slot_unavailable", payload)
        return ScenarioResult(
            status="needs_input",
            message="Выбранный слот уже недоступен.",
            next_action="choose_slot",
            events=({"event_type": "slot_unavailable", **payload},),
        )

    async def _escalate_unknown(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
    ) -> ScenarioResult:
        error_code = "booking_outcome_unknown"
        escalated = replace(
            scenario,
            phase="escalated",
            updated_at=self._now(),
        )
        await session.escalate(escalated, error_code)
        return ScenarioResult(
            status="escalated",
            message="Статус записи проверит администратор.",
            next_action=None,
            events=(),
            error_code=error_code,
        )

    @staticmethod
    def _slot_query(state: Mapping[str, object]) -> SlotQuery:
        raw = state["slot_query"]
        if not isinstance(raw, Mapping):
            raise ValueError("slot_query must be an object")
        starts_before = raw.get("starts_before")
        return SlotQuery(
            service_ids=tuple(raw["service_ids"]),
            starts_after=datetime.fromisoformat(str(raw["starts_after"])),
            starts_before=(
                datetime.fromisoformat(str(starts_before))
                if starts_before is not None
                else None
            ),
            staff_id=str(raw["staff_id"]) if raw.get("staff_id") is not None else None,
        )

    @staticmethod
    def _slot_payload(slot: Slot) -> dict[str, object]:
        return {
            "id": slot.id,
            "service_ids": list(slot.service_ids),
            "staff_id": slot.staff_id,
            "starts_at": slot.starts_at.isoformat(),
            "duration_minutes": slot.duration_minutes,
        }

    @staticmethod
    def _confirmed_result(booking: ExternalBooking) -> ScenarioResult:
        return ScenarioResult(
            status="ok",
            message=f"Запись подтверждена на {booking.starts_at.isoformat()}.",
            next_action=None,
            events=(),
        )
