"""Safe PostgreSQL projections for the read-only admin booking centre."""

from datetime import UTC, datetime
from uuid import UUID

from booking_views import (
    decode_booking_cursor,
    encode_booking_cursor,
    normalize_booking_event,
    normalize_booking_row,
    validate_booking_filters,
)
from moroz.common.db import Database


class BookingDatabaseUnavailable(RuntimeError):
    """Raised when the booking read model cannot access PostgreSQL."""


_UPCOMING_SQL = """
    SELECT booking.id, booking.customer_id, booking.starts_at,
           booking.scheduled_end_at, booking.status, booking.updated_at,
           scenario.kind, scenario.phase, scenario.error_code
    FROM bookings AS booking
    JOIN booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id
    WHERE booking.status IN ('confirmed', 'unknown') AND booking.starts_at >= $4
      AND ($1::text IS NULL OR booking.status = $1)
      AND ($2::timestamptz IS NULL OR (booking.starts_at, booking.id)
           > ($2::timestamptz, $3::uuid))
    ORDER BY booking.starts_at ASC, booking.id ASC
    LIMIT $5
"""

_ATTENTION_SQL = """
    SELECT booking.id, booking.customer_id, booking.starts_at,
           booking.scheduled_end_at, booking.status, booking.updated_at,
           scenario.kind, scenario.phase, scenario.error_code
    FROM bookings AS booking
    JOIN booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id
    WHERE (
        booking.status = 'unknown'
        OR scenario.phase IN ('executing', 'failed', 'escalated')
    )
      AND ($1::text IS NULL OR booking.status = $1)
      AND ($2::timestamptz IS NULL OR (booking.updated_at, booking.id)
           < ($2::timestamptz, $3::uuid))
    ORDER BY booking.updated_at DESC, booking.id DESC
    LIMIT $4
"""

_HISTORY_SQL = """
    SELECT booking.id, booking.customer_id, booking.starts_at,
           booking.scheduled_end_at, booking.status, booking.updated_at,
           scenario.kind, scenario.phase, scenario.error_code
    FROM bookings AS booking
    JOIN booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id
    WHERE NOT (booking.status IN ('confirmed', 'unknown') AND booking.starts_at >= $4)
      AND NOT (
          booking.status = 'unknown'
          OR scenario.phase IN ('executing', 'failed', 'escalated')
      )
      AND ($1::text IS NULL OR booking.status = $1)
      AND ($2::timestamptz IS NULL OR (booking.starts_at, booking.id)
           < ($2::timestamptz, $3::uuid))
    ORDER BY booking.starts_at DESC, booking.id DESC
    LIMIT $5
"""

_DETAIL_SQL = """
    SELECT booking.id, booking.last_scenario_id, booking.external_id,
           booking.customer_id, booking.starts_at, booking.scheduled_end_at,
           booking.status, booking.updated_at, scenario.kind, scenario.phase,
           scenario.error_code
    FROM bookings AS booking
    JOIN booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id
    WHERE booking.id = $1
    FOR SHARE
"""

_EVENTS_SQL = """
    SELECT id, event_type, created_at
    FROM booking_events
    WHERE scenario_id = $1
    ORDER BY created_at ASC, id ASC
"""

_AUDIT_SQL = """
    INSERT INTO admin_audit_events (
        actor_id, action, object_type, object_id,
        before, after, ip_address, user_agent
    )
    VALUES ($1, 'booking.view', 'booking', $2, NULL, NULL, $3, $4)
"""


async def list_bookings(
    database: Database | None,
    *,
    view: str,
    status: str | None,
    cursor: str | None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return one safe keyset page from a fixed local booking projection."""
    view, status = validate_booking_filters(view, status)
    boundary = decode_booking_cursor(cursor)
    if not 1 <= limit <= 50:
        raise ValueError("booking page bounds")
    if database is None:
        raise BookingDatabaseUnavailable("booking database unavailable")

    current_time = now or datetime.now(UTC)
    sort_column = "updated_at" if view == "attention" else "starts_at"
    async with database.acquire() as connection:
        if view == "upcoming":
            rows = await connection.fetch(
                _UPCOMING_SQL,
                status,
                boundary[0] if boundary else None,
                boundary[1] if boundary else None,
                current_time,
                limit + 1,
            )
        elif view == "attention":
            rows = await connection.fetch(
                _ATTENTION_SQL,
                status,
                boundary[0] if boundary else None,
                boundary[1] if boundary else None,
                limit + 1,
            )
        else:
            rows = await connection.fetch(
                _HISTORY_SQL,
                status,
                boundary[0] if boundary else None,
                boundary[1] if boundary else None,
                current_time,
                limit + 1,
            )

    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    return {
        "items": [normalize_booking_row(row) for row in visible_rows],
        "next_cursor": (
            encode_booking_cursor(visible_rows[-1][sort_column], visible_rows[-1]["id"])
            if has_more
            else None
        ),
        "has_more": has_more,
    }


async def get_booking_detail(
    database: Database | None,
    booking_id: UUID,
    *,
    actor_id: int,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, object] | None:
    """Read, audit, and return a safe booking detail in one transaction."""
    if database is None:
        raise BookingDatabaseUnavailable("booking database unavailable")

    detail: dict[str, object] | None = None
    async with database.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(_DETAIL_SQL, booking_id)
            if row is None:
                return None
            event_rows = await connection.fetch(_EVENTS_SQL, row["last_scenario_id"])
            await connection.execute(
                _AUDIT_SQL,
                actor_id,
                str(booking_id),
                ip_address,
                user_agent,
            )
            detail = normalize_booking_row(row, detail=True)
            detail["events"] = [
                normalize_booking_event(event_row) for event_row in event_rows
            ]
    return detail
