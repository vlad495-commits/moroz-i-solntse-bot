"""Safe PostgreSQL projections for the read-only admin booking centre."""

from datetime import UTC, datetime, timedelta
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


_UNIFIED_CTES = """
    WITH projection_identities AS (
        SELECT projection.external_id, projection.booking_key,
               projection.bot_marker_state, projection.starts_at,
               projection.scheduled_end_at, projection.status,
               projection.deleted, projection.client_name,
               projection.staff_name, projection.service_names,
               projection.synced_at,
               CASE
                   WHEN projection.bot_marker_state = 'valid'
                    AND projection.booking_key IS NOT NULL
                   THEN COUNT(*) FILTER (
                       WHERE projection.bot_marker_state = 'valid'
                   ) OVER (PARTITION BY projection.booking_key)
                   ELSE 0
               END AS booking_key_count
        FROM yclients_booking_projection AS projection
    ),
    provider_rows AS (
        SELECT
            'y:' || projection.external_id AS row_key,
            CASE WHEN booking.id IS NOT NULL THEN booking.id END AS detail_id,
            CASE WHEN booking.id IS NOT NULL THEN booking.customer_id END AS customer_id,
            projection.starts_at,
            projection.scheduled_end_at,
            CASE WHEN projection.deleted THEN 'cancelled' ELSE projection.status END AS status,
            projection.synced_at AS updated_at,
            CASE WHEN booking.id IS NOT NULL THEN scenario.kind END AS kind,
            CASE WHEN booking.id IS NOT NULL THEN scenario.phase END AS phase,
            CASE WHEN booking.id IS NOT NULL THEN scenario.error_code END AS error_code,
            CASE WHEN booking.id IS NOT NULL THEN booking.status END AS local_status,
            CASE WHEN booking.id IS NOT NULL THEN scenario.phase END AS local_phase,
            CASE
                WHEN booking.id IS NOT NULL OR projection.bot_marker_state = 'valid' THEN 'bot'
                WHEN projection.bot_marker_state = 'absent' THEN 'other'
                ELSE 'unknown'
            END AS source,
            CASE
                WHEN booking.id IS NOT NULL
                 AND projection.booking_key_count = 1
                 AND projection.bot_marker_state = 'valid'
                 AND external_identity.id IS NOT NULL
                 AND marker_identity.id = external_identity.id
                THEN CASE WHEN
                    projection.starts_at IS DISTINCT FROM booking.starts_at
                    OR projection.scheduled_end_at IS DISTINCT FROM booking.scheduled_end_at
                    OR (CASE WHEN projection.deleted THEN 'cancelled' ELSE projection.status END)
                       IS DISTINCT FROM booking.status
                    THEN 'changed_in_yclients' ELSE 'in_sync' END
                WHEN booking.id IS NOT NULL
                  OR (
                     projection.bot_marker_state = 'valid'
                     AND (
                         projection.booking_key IS NULL
                         OR projection.booking_key_count > 1
                     )
                  )
                THEN 'identity_conflict'
                WHEN projection.bot_marker_state = 'valid' THEN 'local_missing'
                WHEN projection.bot_marker_state = 'absent' THEN 'yclients_only'
                ELSE 'identity_conflict'
            END AS reconciliation_state,
            CASE
                WHEN projection.booking_key_count = 1
                 AND projection.bot_marker_state = 'valid'
                 AND external_identity.id IS NOT NULL
                 AND marker_identity.id = external_identity.id
                 AND projection.starts_at IS NOT DISTINCT FROM booking.starts_at
                 AND projection.scheduled_end_at IS NOT DISTINCT FROM booking.scheduled_end_at
                 AND (CASE WHEN projection.deleted THEN 'cancelled' ELSE projection.status END)
                     IS NOT DISTINCT FROM booking.status
                THEN booking.updated_at
                ELSE projection.synced_at
            END AS attention_at,
            projection.client_name,
            projection.staff_name,
            projection.service_names
        FROM projection_identities AS projection
        LEFT JOIN bookings AS external_identity
               ON external_identity.external_id = projection.external_id
        LEFT JOIN bookings AS marker_identity
               ON projection.bot_marker_state = 'valid'
              AND marker_identity.booking_key = projection.booking_key
        LEFT JOIN bookings AS booking
               ON booking.id = COALESCE(external_identity.id, marker_identity.id)
        LEFT JOIN booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id
        WHERE projection.starts_at >= $6::timestamptz - interval '30 days'
          AND projection.starts_at <= $6::timestamptz + interval '90 days'
    ),
    local_rows AS (
        SELECT
            'l:' || booking.id::text AS row_key,
            booking.id AS detail_id,
            booking.customer_id,
            booking.starts_at,
            booking.scheduled_end_at,
            booking.status,
            booking.updated_at,
            scenario.kind,
            scenario.phase,
            scenario.error_code,
            booking.status AS local_status,
            scenario.phase AS local_phase,
            'bot'::text AS source,
            CASE WHEN $8::boolean THEN 'provider_missing' ELSE 'freshness_unknown' END
                AS reconciliation_state,
            booking.updated_at AS attention_at,
            NULL::text AS client_name,
            NULL::text AS staff_name,
            ARRAY[]::text[] AS service_names
        FROM bookings AS booking
        JOIN booking_scenarios AS scenario ON scenario.id = booking.last_scenario_id
        WHERE booking.starts_at >= $6::timestamptz - interval '30 days'
          AND booking.starts_at <= $6::timestamptz + interval '90 days'
          AND NOT EXISTS (
              SELECT 1 FROM yclients_booking_projection AS projection
              WHERE projection.external_id = booking.external_id
                 OR (
                     projection.bot_marker_state = 'valid'
                     AND projection.booking_key = booking.booking_key
                 )
          )
    ),
    unified AS (
        SELECT * FROM provider_rows
        UNION ALL
        SELECT * FROM local_rows
    )
"""

_COMMON_FILTERS = """
      AND ($1::text IS NULL OR status = $1)
      AND ($2::text = 'all' OR source = $2)
      AND (
          $3::text = 'all'
          OR reconciliation_state IN (
              'changed_in_yclients', 'local_missing',
              'provider_missing', 'identity_conflict'
          )
      )
"""

_ATTENTION_PREDICATE = """
    reconciliation_state IN (
        'changed_in_yclients', 'local_missing',
        'provider_missing', 'identity_conflict'
    )
    OR local_phase IN ('executing', 'failed', 'escalated')
    OR local_status = 'unknown'
"""

_UPCOMING_SQL = _UNIFIED_CTES + """
    SELECT row_key, detail_id, customer_id, starts_at, scheduled_end_at,
           status, updated_at, kind, phase, error_code, source,
           reconciliation_state, client_name, staff_name, service_names
    FROM unified
    WHERE status IN ('confirmed', 'unknown') AND starts_at >= $6::timestamptz
""" + _COMMON_FILTERS + """
      AND ($4::timestamptz IS NULL OR (starts_at, row_key) > ($4, $5::text))
    ORDER BY starts_at ASC, row_key ASC
    LIMIT $7
"""

_ATTENTION_SQL = _UNIFIED_CTES + """
    SELECT row_key, detail_id, customer_id, starts_at, scheduled_end_at,
           status, updated_at, kind, phase, error_code, source,
           reconciliation_state, client_name, staff_name, service_names,
           attention_at
    FROM unified
    WHERE (""" + _ATTENTION_PREDICATE + ")\n" + _COMMON_FILTERS + """
      AND ($4::timestamptz IS NULL OR (attention_at, row_key) < ($4, $5::text))
    ORDER BY attention_at DESC, row_key DESC
    LIMIT $7
"""

_HISTORY_SQL = _UNIFIED_CTES + """
    SELECT row_key, detail_id, customer_id, starts_at, scheduled_end_at,
           status, updated_at, kind, phase, error_code, source,
           reconciliation_state, client_name, staff_name, service_names
    FROM unified
    WHERE NOT (status IN ('confirmed', 'unknown') AND starts_at >= $6::timestamptz)
      AND NOT ((""" + _ATTENTION_PREDICATE + ") IS TRUE)\n" + _COMMON_FILTERS + """
      AND ($4::timestamptz IS NULL OR (starts_at, row_key) < ($4, $5::text))
    ORDER BY starts_at DESC, row_key DESC
    LIMIT $7
"""

_FRESHNESS_SQL = """
    SELECT
        (SELECT MAX(synced_at) FROM yclients_booking_projection)
            AS projection_synced_at,
        (SELECT MAX(finished_at) FROM scheduler_jobs
         WHERE kind = 'yclients_booking_projection_sync'
           AND status = 'finished'
           AND finished_at IS NOT NULL)
            AS empty_snapshot_at
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
    source: str = "all",
    reconciliation: str = "all",
    cursor: str | None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return one safe keyset page from the unified booking projection."""
    view, status, source, reconciliation = validate_booking_filters(
        view, status, source, reconciliation
    )
    boundary = decode_booking_cursor(cursor)
    if not 1 <= limit <= 50:
        raise ValueError("booking page bounds")
    if database is None:
        raise BookingDatabaseUnavailable("booking database unavailable")

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("booking time")
    sort_column = "attention_at" if view == "attention" else "starts_at"
    async with database.acquire() as connection:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            freshness_row = await connection.fetchrow(_FRESHNESS_SQL)
            last_success_at = (
                freshness_row["projection_synced_at"]
                or freshness_row["empty_snapshot_at"]
            )
            query = {
                "upcoming": _UPCOMING_SQL,
                "attention": _ATTENTION_SQL,
                "history": _HISTORY_SQL,
            }[view]
            rows = await connection.fetch(
                query,
                status,
                source,
                reconciliation,
                boundary[0] if boundary else None,
                boundary[1] if boundary else None,
                current_time,
                limit + 1,
                last_success_at is not None,
            )

    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    return {
        "items": [normalize_booking_row(row) for row in visible_rows],
        "next_cursor": (
            encode_booking_cursor(
                visible_rows[-1][sort_column], visible_rows[-1]["row_key"]
            )
            if has_more
            else None
        ),
        "has_more": has_more,
        "freshness": {
            "last_success_at": last_success_at,
            "stale": (
                last_success_at is not None
                and current_time - last_success_at > timedelta(minutes=20)
            ),
        },
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
