import json
from datetime import datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from moroz.common.db import Database
from moroz.escalation.service import EscalationService


MOSCOW = ZoneInfo("Europe/Moscow")
QUIET_AFTER = time(21, 0)
NEXT_DAY_SEND_AT = time(10, 30)


class FeedbackService:
    def __init__(self, database: Database):
        self._database = database
        self._escalations = EscalationService(database)

    async def schedule_after_visit(
        self,
        *,
        customer_id: str,
        booking_key: UUID,
        completed_at: datetime,
    ) -> UUID | None:
        run_at = _feedback_run_at(completed_at)
        request_id = uuid4()
        job_id = uuid4()
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO notification_feedback_requests
                        (id, customer_id, booking_key, requested_at)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (customer_id) DO NOTHING
                    RETURNING id
                    """,
                    request_id,
                    customer_id,
                    booking_key,
                )
                if row is None:
                    return None
                await connection.execute(
                    """
                    INSERT INTO scheduler_jobs
                        (id, kind, run_at, payload, idempotency_key, status,
                         attempts, booking_key, created_at, updated_at)
                    VALUES ($1, 'feedback_request', $2, $3::jsonb, $4,
                            'pending', 0, $5, now(), now())
                    """,
                    job_id,
                    run_at,
                    json.dumps(
                        {
                            "customer_id": customer_id,
                            "booking_key": str(booking_key),
                        },
                        ensure_ascii=False,
                    ),
                    f"feedback:{customer_id}",
                    booking_key,
                )
        return job_id

    async def record_rating(
        self,
        *,
        customer_id: str,
        booking_key: UUID,
        rating: int,
    ) -> UUID | None:
        if rating > 3:
            return None
        return await self._escalations.create_low_rating(
            customer_id=customer_id,
            booking_key=booking_key,
            rating=rating,
        )


def _feedback_run_at(completed_at: datetime) -> datetime:
    candidate = completed_at.astimezone(MOSCOW) + timedelta(hours=2)
    if candidate.timetz().replace(tzinfo=None) >= QUIET_AFTER:
        return datetime.combine(
            candidate.date() + timedelta(days=1),
            NEXT_DAY_SEND_AT,
            tzinfo=MOSCOW,
        )
    return candidate

