from datetime import timedelta
from types import MappingProxyType
from uuid import UUID

from moroz.booking.models import ExternalBooking, GetBooking
from moroz.common.db import Database
from moroz.notifications.feedback import FeedbackService
from moroz.notifications.models import PlannedSchedulerJob
from moroz.notifications.repository import SchedulerJobRepository


OUTCOME_OFFSETS = (
    timedelta(minutes=15),
    timedelta(hours=2),
    timedelta(hours=24),
)
_TERMINAL_STATUSES = {"cancelled", "completed", "no_show", "unknown"}


class LifecycleService:
    def __init__(self, database: Database, provider, feedback: FeedbackService):
        self._database = database
        self._provider = provider
        self._feedback = feedback
        self._jobs = SchedulerJobRepository(database)

    async def refresh(self, local: ExternalBooking) -> ExternalBooking | None:
        if local.status in _TERMINAL_STATUSES:
            return local
        provider_booking = await self._provider.get_booking(
            GetBooking(
                external_id=local.external_id,
                customer_id=local.customer_id,
                booking_key=local.booking_key,
            )
        )
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE bookings
                SET status = $3,
                    scheduled_end_at = $4,
                    snapshot = snapshot || jsonb_build_object(
                        'status', $3::text,
                        'scheduled_end_at', $4::timestamptz
                    ),
                    updated_at = now()
                WHERE booking_key = $1
                  AND starts_at = $2
                  AND status IN ('confirmed', 'unknown')
                RETURNING external_id, customer_id, booking_key, slot_id,
                          starts_at, status, scheduled_end_at
                """,
                local.booking_key,
                local.starts_at,
                provider_booking.status,
                provider_booking.scheduled_end_at,
            )
        return _booking_from_row(row) if row is not None else None

    async def schedule_next(
        self,
        booking: ExternalBooking,
        current_index: int,
    ) -> bool:
        next_index = current_index + 1
        if next_index >= len(OUTCOME_OFFSETS):
            return False
        if booking.scheduled_end_at is None:
            raise RuntimeError("scheduled end is required for outcome checks")
        starts_at = booking.starts_at
        await self._jobs.schedule(
            PlannedSchedulerJob(
                kind="visit_outcome_check",
                run_at=booking.scheduled_end_at + OUTCOME_OFFSETS[next_index],
                payload=MappingProxyType(
                    {
                        "booking_key": str(booking.booking_key),
                        "starts_at": starts_at.isoformat(),
                        "outcome_check_index": next_index,
                    }
                ),
                idempotency_key=(
                    f"booking:{booking.booking_key}:{starts_at.isoformat()}:"
                    f"outcome:{next_index}"
                ),
                booking_key=booking.booking_key,
                booking_starts_at=starts_at,
            )
        )
        return True

    async def schedule_feedback(self, booking: ExternalBooking) -> UUID | None:
        if booking.scheduled_end_at is None:
            raise RuntimeError("scheduled end is required for feedback")
        return await self._feedback.schedule_after_visit(
            customer_id=booking.customer_id,
            booking_key=booking.booking_key,
            completed_at=booking.scheduled_end_at,
        )


def _booking_from_row(row) -> ExternalBooking:
    return ExternalBooking(
        external_id=row["external_id"],
        customer_id=row["customer_id"],
        booking_key=row["booking_key"],
        slot_id=row["slot_id"],
        starts_at=row["starts_at"],
        status=row["status"],
        scheduled_end_at=row["scheduled_end_at"],
    )
