from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID, uuid4

from moroz.booking.models import BookingScenario, SlotQuery
from moroz.booking.projection import PROJECTION_SYNC_KIND
from moroz.booking.service import BookingService
from moroz.notifications.models import JobResult, PlannedSchedulerJob, SchedulerJob


ADMIN_BOOKING_CREATE_KIND = "admin_booking_create"
ADMIN_BOOKING_STATUS_KIND = "admin_booking_status"
ADMIN_BOOKING_COMMAND_KINDS = {
    ADMIN_BOOKING_CREATE_KIND,
    ADMIN_BOOKING_STATUS_KIND,
}
_VISIT_STATUSES = {"completed", "no_show", "cancelled"}


class AdminBookingCommandRepository:
    def __init__(self, database) -> None:
        self._database = database

    async def record_status(self, external_id: str, status: str) -> None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT last_scenario_id, status
                    FROM bookings
                    WHERE external_id = $1
                    FOR UPDATE
                    """,
                    external_id,
                )
                if row is None:
                    return
                await connection.execute(
                    """
                    UPDATE bookings
                    SET status = $2,
                        snapshot = jsonb_set(snapshot, '{status}', to_jsonb($2::text)),
                        updated_at = now()
                    WHERE external_id = $1
                    """,
                    external_id,
                    status,
                )
                await connection.execute(
                    """
                    INSERT INTO booking_events
                        (id, scenario_id, event_type, payload)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    uuid4(),
                    row["last_scenario_id"],
                    {
                        "completed": "booking_completed",
                        "no_show": "booking_no_show",
                        "cancelled": "booking_cancelled",
                    }[status],
                    "{}",
                )


class AdminBookingCommandService:
    def __init__(
        self,
        adapter,
        booking_repository,
        command_repository,
        scheduler_repository,
        *,
        booking_service=None,
        clock=None,
    ) -> None:
        self._adapter = adapter
        self._booking_repository = booking_repository
        self._command_repository = command_repository
        self._scheduler = scheduler_repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._booking_service = booking_service or BookingService(
            adapter,
            booking_repository,
            now=self._clock,
        )

    async def handle(self, job: SchedulerJob) -> JobResult:
        if job.kind == ADMIN_BOOKING_CREATE_KIND:
            return await self._create(job)
        if job.kind == ADMIN_BOOKING_STATUS_KIND:
            return await self._status(job)
        return JobResult.skipped("unsupported_kind")

    async def _create(self, job: SchedulerJob) -> JobResult:
        payload = _create_payload(job.payload, self._clock())
        if payload is None:
            return JobResult.skipped("invalid_admin_booking_payload")
        starts_at = payload["starts_at"]
        query = SlotQuery(
            (payload["service_id"],),
            starts_at,
            starts_at + timedelta(minutes=1),
            staff_id=payload["staff_id"],
        )
        slots = await self._adapter.list_slots(query)
        slot = next(
            (
                value
                for value in slots
                if value.starts_at == starts_at
                and value.staff_id == payload["staff_id"]
                and tuple(value.service_ids) == (payload["service_id"],)
            ),
            None,
        )
        if slot is None:
            return JobResult.skipped("slot_unavailable")
        state = {
            "slot_query": {
                "service_ids": [payload["service_id"]],
                "staff_id": payload["staff_id"],
                "starts_after": starts_at.isoformat(),
                "starts_before": (starts_at + timedelta(minutes=1)).isoformat(),
            },
            "selected_slot_id": slot.id,
            "customer_name": payload["customer_name"],
            "customer_phone": payload["customer_phone"],
            "personal_data_processing_allowed": True,
            "comment": payload["comment"],
        }
        scenario = BookingScenario(
            id=job.id,
            kind="create",
            phase="awaiting_confirmation",
            idempotency_key=job.idempotency_key,
            customer_id=f"admin:{job.id}",
            state=state,
            error_code=None,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        scenario_id = await self._booking_repository.create_scenario(scenario)
        result = await self._booking_service.handle(scenario_id, confirmed=True)
        if result.status != "ok":
            return JobResult.skipped(
                result.error_code or result.next_action or result.status
            )
        await self._schedule_projection(job.id)
        return JobResult.sent()

    async def _status(self, job: SchedulerJob) -> JobResult:
        payload = _status_payload(job.payload)
        if payload is None:
            return JobResult.skipped("invalid_admin_booking_payload")
        external_id, status = payload
        await self._adapter.set_visit_status(external_id, status)
        await self._command_repository.record_status(external_id, status)
        await self._schedule_projection(job.id)
        return JobResult.sent()

    async def _schedule_projection(self, command_id: UUID) -> None:
        now = self._clock()
        await self._scheduler.schedule(
            PlannedSchedulerJob(
                kind=PROJECTION_SYNC_KIND,
                run_at=now,
                payload=MappingProxyType({}),
                idempotency_key=f"{PROJECTION_SYNC_KIND}:admin:{command_id}",
                booking_key=None,
                booking_starts_at=None,
            )
        )


def _create_payload(value: Mapping[str, object], now: datetime):
    try:
        if set(value) != {
            "customer_name",
            "customer_phone",
            "service_id",
            "staff_id",
            "starts_at",
            "personal_data_processing_allowed",
            "comment",
        }:
            raise ValueError
        name = _bounded_text(value["customer_name"], 100)
        phone = _bounded_text(value["customer_phone"], 32)
        service_id = _provider_id(value["service_id"])
        staff_id = _provider_id(value["staff_id"])
        starts_at = datetime.fromisoformat(_bounded_text(value["starts_at"], 64))
        comment = value["comment"]
        if (
            starts_at.tzinfo is None
            or starts_at <= now
            or starts_at > now + timedelta(days=365)
            or value["personal_data_processing_allowed"] is not True
            or (comment is not None and not isinstance(comment, str))
            or isinstance(comment, str) and len(comment) > 500
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "customer_name": name,
        "customer_phone": phone,
        "service_id": service_id,
        "staff_id": staff_id,
        "starts_at": starts_at,
        "comment": comment,
    }


def _status_payload(value: Mapping[str, object]) -> tuple[str, str] | None:
    if set(value) != {"external_id", "status"}:
        return None
    try:
        external_id = _provider_id(value["external_id"])
    except ValueError:
        return None
    status = value["status"]
    return (external_id, status) if status in _VISIT_STATUSES else None


def _provider_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value[0] == "0"
        or len(value) > 64
    ):
        raise ValueError
    return value


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError
    return value
