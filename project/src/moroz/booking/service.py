from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from moroz.booking.models import (
    BookingIdentity,
    BookingOutcomeUnknown,
    BookingScenario,
    BookingTemporaryError,
    CancelBooking,
    CreateBooking,
    ExternalBooking,
    RescheduleBooking,
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
            return await self._handle_serialized(
                session,
                confirmed=confirmed,
                identity=identity,
            )

    async def _handle_serialized(
        self,
        session: BookingScenarioSession,
        *,
        confirmed: bool,
        identity: BookingIdentity | None,
    ) -> ScenarioResult:
        scenario = session.scenario
        if scenario.phase == "confirmed":
            if scenario.kind != "create":
                if not self._owns_scenario(identity, scenario):
                    return self._escalated_result("booking_identity_unconfirmed")
                return self._change_terminal_result(scenario)
            booking = await session.get_local_booking()
            if booking is None:
                raise RuntimeError("confirmed scenario has no local booking")
            return self._confirmed_result(booking)
        if scenario.phase == "escalated":
            if scenario.error_code is None:
                raise RuntimeError("escalated scenario has no error code")
            return self._escalated_result(scenario.error_code)
        if scenario.phase == "executing":
            return await self._escalate_unknown(session, scenario)
        try:
            if scenario.kind == "create":
                return await self._handle_create(session, scenario, confirmed)
            return await self._handle_change(
                session,
                scenario,
                confirmed,
                identity,
            )
        except BookingTemporaryError:
            return await self._escalate(
                session,
                session.scenario,
                "booking_temporarily_unavailable",
            )
        except BookingOutcomeUnknown:
            return await self._escalate(
                session,
                session.scenario,
                "booking_outcome_unknown",
            )

    async def _handle_create(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
        confirmed: bool,
    ) -> ScenarioResult:
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

    async def _handle_change(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
        confirmed: bool,
        identity: BookingIdentity | None,
    ) -> ScenarioResult:
        if scenario.phase != "awaiting_confirmation":
            raise ValueError(f"unsupported {scenario.kind} phase: {scenario.phase}")
        if not self._owns_scenario(identity, scenario):
            return await self._escalate(
                session,
                scenario,
                "booking_identity_unconfirmed",
            )
        starts_at = datetime.fromisoformat(str(scenario.state["starts_at"]))
        if starts_at - self._now() < timedelta(hours=3):
            return await self._escalate(session, scenario, "late_booking_change")
        if not confirmed:
            return ScenarioResult(
                status="needs_input",
                message="Подтвердите изменение записи.",
                next_action="confirm_booking",
                events=(),
            )

        executing = replace(scenario, phase="executing", updated_at=self._now())
        await session.checkpoint(executing, f"booking_{scenario.kind}_started")
        if scenario.kind == "reschedule":
            return await self._reschedule(session, executing)
        if scenario.kind == "cancel":
            return await self._cancel(session, executing)
        raise ValueError(f"unsupported booking kind: {scenario.kind}")

    async def _reschedule(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
    ) -> ScenarioResult:
        query = self._slot_query(scenario.state)
        selected_slot_id = str(scenario.state["selected_slot_id"])
        slots = await self._port.list_slots(query)
        if not any(slot.id == selected_slot_id for slot in slots):
            return await self._slot_unavailable(
                session, scenario, selected_slot_id, slots
            )
        try:
            booking = await self._port.reschedule_booking(
                RescheduleBooking(
                    external_id=str(scenario.state["external_id"]),
                    slot_id=selected_slot_id,
                    idempotency_key=scenario.idempotency_key,
                )
            )
        except SlotUnavailable:
            fresh_slots = await self._port.list_slots(query)
            return await self._slot_unavailable(
                session, scenario, selected_slot_id, fresh_slots
            )
        state = dict(scenario.state)
        state["previous_starts_at"] = str(scenario.state["starts_at"])
        state["starts_at"] = booking.starts_at.isoformat()
        terminal = replace(
            scenario,
            phase="confirmed",
            state=state,
            updated_at=self._now(),
        )
        await session.confirm(terminal, booking)
        return self._change_terminal_result(terminal)

    async def _cancel(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
    ) -> ScenarioResult:
        booking = await session.get_local_booking()
        if booking is None:
            raise RuntimeError("cancel scenario has no local booking")
        await self._port.cancel_booking(
            CancelBooking(
                external_id=str(scenario.state["external_id"]),
                idempotency_key=scenario.idempotency_key,
            )
        )
        cancelled = replace(booking, status="cancelled")
        terminal = replace(scenario, phase="confirmed", updated_at=self._now())
        await session.complete_cancellation(terminal, cancelled)
        return self._change_terminal_result(terminal)

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
        return await self._escalate(session, scenario, "booking_outcome_unknown")

    async def _escalate(
        self,
        session: BookingScenarioSession,
        scenario: BookingScenario,
        error_code: str,
    ) -> ScenarioResult:
        escalated = replace(
            scenario,
            phase="escalated",
            error_code=error_code,
            updated_at=self._now(),
        )
        await session.escalate(escalated, error_code)
        return self._escalated_result(error_code)

    @staticmethod
    def _escalated_result(error_code: str) -> ScenarioResult:
        return ScenarioResult(
            status="escalated",
            message="Статус записи проверит администратор.",
            next_action=None,
            events=(),
            error_code=error_code,
        )

    @staticmethod
    def _owns_scenario(
        identity: BookingIdentity | None,
        scenario: BookingScenario,
    ) -> bool:
        return (
            identity is not None
            and identity.confirmed
            and identity.customer_id == scenario.customer_id
        )

    @staticmethod
    def _change_terminal_result(scenario: BookingScenario) -> ScenarioResult:
        if scenario.kind == "reschedule":
            previous = scenario.state.get("previous_starts_at")
            if previous is None:
                raise RuntimeError("confirmed reschedule has no previous start")
            return ScenarioResult(
                status="ok",
                message=(
                    f"Запись перенесена с {previous} "
                    f"на {scenario.state['starts_at']}."
                ),
                next_action=None,
                events=(),
            )
        if scenario.kind == "cancel":
            return ScenarioResult(
                status="ok",
                message=f"Запись на {scenario.state['starts_at']} отменена.",
                next_action=None,
                events=(),
            )
        raise ValueError(f"unsupported change kind: {scenario.kind}")

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
