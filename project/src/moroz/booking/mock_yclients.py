from datetime import timedelta
from uuid import uuid4

from moroz.booking.models import (
    BookingNotFound,
    CancelBooking,
    CreateBooking,
    ExternalBooking,
    GetBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.ports import BookingPort


class MockYclientsAdapter(BookingPort):
    def __init__(self, slots: list[Slot]) -> None:
        self._slots = {slot.id: slot for slot in slots}
        self._bookings: dict[str, ExternalBooking] = {}
        self._create_results: dict[str, ExternalBooking] = {}
        self._reschedule_results: dict[str, ExternalBooking] = {}
        self._cancel_keys: set[str] = set()

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        return [
            slot
            for slot in self._slots.values()
            if slot.starts_at >= query.starts_after
            and (query.starts_before is None or slot.starts_at < query.starts_before)
            and set(query.service_ids).issubset(slot.service_ids)
            and (query.staff_id is None or slot.staff_id == query.staff_id)
            and not self._is_occupied(slot.id)
        ]

    async def create_booking(self, command: CreateBooking) -> ExternalBooking:
        if command.idempotency_key in self._create_results:
            return self._create_results[command.idempotency_key]
        slot = self._available_slot(command.slot_id)
        booking = ExternalBooking(
            external_id=str(uuid4()),
            customer_id=command.customer_id,
            booking_key=command.booking_key,
            slot_id=slot.id,
            service_ids=slot.service_ids,
            staff_id=slot.staff_id,
            starts_at=slot.starts_at,
            status="confirmed",
            scheduled_end_at=slot.starts_at + timedelta(minutes=slot.duration_minutes),
        )
        self._bookings[booking.external_id] = booking
        self._create_results[command.idempotency_key] = booking
        return booking

    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking:
        booking = await self.get_booking(
            GetBooking(
                command.external_id,
                command.customer_id,
                command.booking_key,
            )
        )
        if command.idempotency_key in self._reschedule_results:
            return self._reschedule_results[command.idempotency_key]
        slot = self._available_slot(command.slot_id, excluding_external_id=booking.external_id)
        updated = ExternalBooking(
            external_id=booking.external_id,
            customer_id=command.customer_id,
            booking_key=command.booking_key,
            slot_id=slot.id,
            service_ids=slot.service_ids,
            staff_id=slot.staff_id,
            starts_at=slot.starts_at,
            status="confirmed",
            scheduled_end_at=slot.starts_at + timedelta(minutes=slot.duration_minutes),
        )
        self._bookings[updated.external_id] = updated
        self._reschedule_results[command.idempotency_key] = updated
        return updated

    async def cancel_booking(self, command: CancelBooking) -> None:
        booking = await self.get_booking(
            GetBooking(
                command.external_id,
                command.customer_id,
                command.booking_key,
            )
        )
        if command.idempotency_key in self._cancel_keys:
            return
        self._bookings[booking.external_id] = ExternalBooking(
            external_id=booking.external_id,
            customer_id=command.customer_id,
            booking_key=command.booking_key,
            slot_id=booking.slot_id,
            service_ids=booking.service_ids,
            staff_id=booking.staff_id,
            starts_at=booking.starts_at,
            status="cancelled",
            scheduled_end_at=booking.scheduled_end_at,
        )
        self._cancel_keys.add(command.idempotency_key)

    async def get_booking(self, command: GetBooking) -> ExternalBooking:
        try:
            booking = self._bookings[command.external_id]
        except KeyError as error:
            raise BookingNotFound(command.external_id) from error
        if (
            booking.customer_id != command.customer_id
            or booking.booking_key != command.booking_key
        ):
            raise BookingNotFound(command.external_id)
        return ExternalBooking(
            external_id=booking.external_id,
            customer_id=command.customer_id,
            booking_key=command.booking_key,
            slot_id=booking.slot_id,
            service_ids=booking.service_ids,
            staff_id=booking.staff_id,
            starts_at=booking.starts_at,
            status=booking.status,
            scheduled_end_at=booking.scheduled_end_at,
        )

    def _available_slot(self, slot_id: str, *, excluding_external_id: str | None = None) -> Slot:
        try:
            slot = self._slots[slot_id]
        except KeyError as error:
            raise SlotUnavailable(slot_id) from error
        if self._is_occupied(slot_id, excluding_external_id=excluding_external_id):
            raise SlotUnavailable(slot_id)
        return slot

    def _is_occupied(self, slot_id: str, *, excluding_external_id: str | None = None) -> bool:
        return any(
            booking.external_id != excluding_external_id and booking.slot_id == slot_id and booking.status == "confirmed"
            for booking in self._bookings.values()
        )
