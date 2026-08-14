from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from bookings_database import (
    BookingDatabaseUnavailable,
    get_booking_detail,
    list_bookings,
)
from moroz.common.db import Database


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    pool = Database(migrated_database_url, min_size=1, max_size=1)
    await pool.connect()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_booking(
    connection,
    *,
    status: str,
    starts_at: datetime,
    phase: str,
    error_code: str | None,
    updated_at: datetime,
) -> UUID:
    scenario_id = uuid4()
    booking_id = uuid4()
    await connection.execute(
        """
        INSERT INTO booking_scenarios
            (id, kind, phase, idempotency_key, customer_id, state,
             error_code, created_at, updated_at)
        VALUES ($1, 'create', $2, $3, '42',
                '{"private":"scenario-state"}'::jsonb, $4, $5, $5)
        """,
        scenario_id,
        phase,
        f"booking-view:{scenario_id}",
        error_code,
        updated_at,
    )
    await connection.execute(
        """
        INSERT INTO bookings
            (id, last_scenario_id, external_id, customer_id, slot_id,
             starts_at, status, snapshot, booking_key, updated_at)
        VALUES ($1, $2, $3, '42', 'slot-1', $4, $5,
                '{"private":"booking-snapshot"}'::jsonb, $6, $7)
        """,
        booking_id,
        scenario_id,
        f"external-{booking_id}",
        starts_at,
        status,
        uuid4(),
        updated_at,
    )
    await connection.execute(
        """
        INSERT INTO booking_events
            (id, scenario_id, event_type, payload, created_at)
        VALUES ($1, $2, 'booking_confirmed',
                '{"private":"event-payload"}'::jsonb, $3)
        """,
        uuid4(),
        scenario_id,
        updated_at,
    )
    return booking_id


async def test_list_bookings_projects_views_filters_and_keyset_pages(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        upcoming_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=1),
            phase="confirmed",
            error_code=None,
            updated_at=NOW + timedelta(minutes=1),
        )
        unknown_id = await _seed_booking(
            connection,
            status="unknown",
            starts_at=NOW + timedelta(days=2),
            phase="confirmed",
            error_code=None,
            updated_at=NOW + timedelta(minutes=2),
        )
        failed_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=3),
            phase="escalated",
            error_code="booking_outcome_unknown",
            updated_at=NOW + timedelta(minutes=3),
        )
        later_upcoming_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=4),
            phase="confirmed",
            error_code=None,
            updated_at=NOW + timedelta(minutes=4),
        )
        history_id = await _seed_booking(
            connection,
            status="cancelled",
            starts_at=NOW - timedelta(days=1),
            phase="confirmed",
            error_code=None,
            updated_at=NOW + timedelta(minutes=5),
        )

        upcoming = await list_bookings(
            database, view="upcoming", status=None, cursor=None, limit=1, now=NOW
        )
        attention = await list_bookings(
            database, view="attention", status=None, cursor=None, now=NOW
        )
        history = await list_bookings(
            database, view="history", status=None, cursor=None, now=NOW
        )
        filtered_upcoming = await list_bookings(
            database, view="upcoming", status="confirmed", cursor=None, now=NOW
        )
        filtered_attention = await list_bookings(
            database, view="attention", status="confirmed", cursor=None, now=NOW
        )
        next_upcoming = await list_bookings(
            database,
            view="upcoming",
            status=None,
            cursor=upcoming["next_cursor"],
            limit=1,
            now=NOW,
        )

        assert [item["id"] for item in upcoming["items"]] == [upcoming_id]
        assert upcoming["has_more"] is True
        assert upcoming["next_cursor"]
        assert [item["id"] for item in next_upcoming["items"]] == [
            unknown_id
        ]
        assert {item["id"] for item in upcoming["items"]}.isdisjoint(
            item["id"] for item in next_upcoming["items"]
        )
        assert [item["id"] for item in attention["items"]] == [
            failed_id,
            unknown_id,
        ]
        assert [item["id"] for item in history["items"]] == [history_id]
        assert [item["id"] for item in filtered_upcoming["items"]] == [
            upcoming_id,
            failed_id,
            later_upcoming_id,
        ]
        assert [item["id"] for item in filtered_attention["items"]] == [failed_id]
        for item in (
            upcoming["items"]
            + next_upcoming["items"]
            + attention["items"]
            + history["items"]
        ):
            assert {"snapshot", "state", "payload", "external_id"}.isdisjoint(item)
    finally:
        await connection.close()


async def test_booking_detail_is_allowlisted_audited_and_fails_closed(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    booking_id = await _seed_booking(
        connection,
        status="confirmed",
        starts_at=NOW + timedelta(days=1),
        phase="escalated",
        error_code="booking_outcome_unknown",
        updated_at=NOW,
    )
    try:
        detail = await get_booking_detail(
            database,
            booking_id,
            actor_id=7,
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
        audit = await connection.fetchrow(
            """
            SELECT actor_id, action, object_type, object_id, before, after,
                   ip_address, user_agent
            FROM admin_audit_events
            WHERE action = 'booking.view'
            """
        )

        assert detail is not None
        assert detail["id"] == booking_id
        assert detail["external_id"] == f"external-{booking_id}"
        assert detail["scenario_label"] == "Создание записи"
        assert detail["phase_label"] == "Передано администратору"
        assert [event["title"] for event in detail["events"]] == [
            "Запись подтверждена"
        ]
        assert {"snapshot", "state", "payload"}.isdisjoint(detail)
        assert "scenario-state" not in repr(detail)
        assert "event-payload" not in repr(detail)
        assert audit["actor_id"] == 7
        assert audit["action"] == "booking.view"
        assert audit["object_type"] == "booking"
        assert audit["object_id"] == str(booking_id)
        assert audit["before"] is None
        assert audit["after"] is None
        assert "customer_id" not in repr(audit)
        assert "external_id" not in repr(audit)

        assert await get_booking_detail(
            database,
            uuid4(),
            actor_id=7,
            ip_address=None,
            user_agent=None,
        ) is None
        assert await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events WHERE action = 'booking.view'"
        ) == 1

        await connection.execute(
            """
            CREATE FUNCTION reject_booking_view() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.action = 'booking.view' THEN
                    RAISE EXCEPTION 'forced audit failure';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER reject_booking_view_insert
            BEFORE INSERT ON admin_audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_booking_view();
            """
        )

        with pytest.raises(asyncpg.PostgresError, match="forced audit failure"):
            await get_booking_detail(
                database,
                booking_id,
                actor_id=7,
                ip_address=None,
                user_agent=None,
            )
        assert await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events WHERE action = 'booking.view'"
        ) == 1
    finally:
        await connection.close()


async def test_booking_database_is_required():
    with pytest.raises(BookingDatabaseUnavailable):
        await list_bookings(None, view="upcoming", status=None, cursor=None)
    with pytest.raises(BookingDatabaseUnavailable):
        await get_booking_detail(
            None,
            uuid4(),
            actor_id=7,
            ip_address=None,
            user_agent=None,
        )


@pytest.mark.parametrize(
    ("view", "first_status", "first_starts_at", "first_phase", "first_updated_at", "insert_status", "insert_starts_at", "insert_phase", "insert_updated_at"),
    [
        (
            "upcoming",
            "confirmed",
            NOW + timedelta(days=2),
            "confirmed",
            NOW,
            "confirmed",
            NOW + timedelta(days=1),
            "confirmed",
            NOW + timedelta(minutes=1),
        ),
        (
            "attention",
            "unknown",
            NOW + timedelta(days=1),
            "confirmed",
            NOW + timedelta(minutes=2),
            "unknown",
            NOW + timedelta(days=2),
            "confirmed",
            NOW + timedelta(minutes=3),
        ),
        (
            "history",
            "cancelled",
            NOW - timedelta(days=2),
            "confirmed",
            NOW + timedelta(minutes=3),
            "cancelled",
            NOW - timedelta(days=1),
            "confirmed",
            NOW + timedelta(minutes=2),
        ),
    ],
)
async def test_keyset_page_skips_insertion_before_cursor_without_repeating_viewed_booking(
    database,
    migrated_database_url,
    view,
    first_status,
    first_starts_at,
    first_phase,
    first_updated_at,
    insert_status,
    insert_starts_at,
    insert_phase,
    insert_updated_at,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        await connection.execute(
            "TRUNCATE booking_events, bookings, booking_scenarios CASCADE"
        )
        first_id = await _seed_booking(
            connection,
            status=first_status,
            starts_at=first_starts_at,
            phase=first_phase,
            error_code=None,
            updated_at=first_updated_at,
        )
        next_id = await _seed_booking(
            connection,
            status=insert_status,
            starts_at=(
                NOW + timedelta(days=3)
                if view in {"upcoming", "attention"}
                else NOW - timedelta(days=3)
            ),
            phase=insert_phase,
            error_code=None,
            updated_at=NOW + timedelta(minutes=1),
        )
        first_page = await list_bookings(
            database, view=view, status=None, cursor=None, limit=1, now=NOW
        )
        inserted_id = await _seed_booking(
            connection,
            status=insert_status,
            starts_at=insert_starts_at,
            phase=insert_phase,
            error_code=None,
            updated_at=insert_updated_at,
        )
        second_page = await list_bookings(
            database,
            view=view,
            status=None,
            cursor=first_page["next_cursor"],
            limit=1,
            now=NOW,
        )

        assert [item["id"] for item in first_page["items"]] == [first_id]
        assert first_page["next_cursor"]
        assert [item["id"] for item in second_page["items"]] == [next_id]
        assert inserted_id not in {item["id"] for item in second_page["items"]}
        assert {item["id"] for item in first_page["items"]}.isdisjoint(
            item["id"] for item in second_page["items"]
        )
    finally:
        await connection.close()
