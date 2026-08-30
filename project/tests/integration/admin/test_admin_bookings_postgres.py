from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from bookings_database import (
    BookingDatabaseUnavailable,
    get_booking_detail,
    list_calendar_bookings,
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
    external_id: str | None = None,
    booking_key: UUID | None = None,
    scheduled_end_at: datetime | None = None,
    customer_id: str = "42",
) -> UUID:
    scenario_id = uuid4()
    booking_id = uuid4()
    await connection.execute(
        """
        INSERT INTO booking_scenarios
            (id, kind, phase, idempotency_key, customer_id, state,
             error_code, created_at, updated_at)
        VALUES ($1, 'create', $2, $3, $4,
                '{"private":"scenario-state"}'::jsonb, $5, $6, $6)
        """,
        scenario_id,
        phase,
        f"booking-view:{scenario_id}",
        customer_id,
        error_code,
        updated_at,
    )
    await connection.execute(
        """
        INSERT INTO bookings
            (id, last_scenario_id, external_id, customer_id, slot_id,
             starts_at, scheduled_end_at, status, snapshot, booking_key, updated_at)
        VALUES ($1, $2, $3, $4, 'slot-1', $5, $6, $7,
                '{"private":"booking-snapshot"}'::jsonb, $8, $9)
        """,
        booking_id,
        scenario_id,
        external_id or f"external-{booking_id}",
        customer_id,
        starts_at,
        scheduled_end_at,
        status,
        booking_key or uuid4(),
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


async def _seed_projection(
    connection,
    *,
    external_id: str,
    booking_key: UUID | None,
    marker_state: str,
    starts_at: datetime,
    status: str = "confirmed",
    scheduled_end_at: datetime | None = None,
    synced_at: datetime = NOW,
    deleted: bool = False,
) -> None:
    await connection.execute(
        """
        INSERT INTO yclients_booking_projection
            (external_id, booking_key, bot_marker_state, starts_at,
             scheduled_end_at, status, deleted, client_name, staff_name,
             service_names, synced_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7,
                'Безопасное имя', 'Мастер', ARRAY['Услуга'], $8)
        """,
        external_id,
        booking_key,
        marker_state,
        starts_at,
        scheduled_end_at,
        status,
        deleted,
        synced_at,
    )


async def _seed_successful_sync(connection, finished_at: datetime) -> None:
    await connection.execute(
        """
        INSERT INTO scheduler_jobs
            (id, kind, run_at, payload, idempotency_key, status, finished_at)
        VALUES ($1, 'yclients_booking_projection_sync', $2, '{}'::jsonb,
                $3, 'finished', $2)
        """,
        uuid4(),
        finished_at,
        f"projection-finished:{uuid4()}",
    )


async def _seed_unsuccessful_sync(
    connection,
    *,
    status: str,
    error_code: str,
    updated_at: datetime,
) -> None:
    await connection.execute(
        """
        INSERT INTO scheduler_jobs
            (id, kind, run_at, payload, idempotency_key, status, attempts,
             finished_at, last_error_code, created_at, updated_at)
        VALUES ($1, 'yclients_booking_projection_sync', $2::timestamptz, '{}'::jsonb,
                $3::text, $4::text, 1,
                CASE WHEN $4::text = 'failed' THEN $2::timestamptz END,
                $5::text, $2::timestamptz, $2::timestamptz)
        """,
        uuid4(),
        updated_at,
        f"projection-failed:{uuid4()}",
        status,
        error_code,
    )


async def test_calendar_returns_exact_week_from_all_yclients_sources(
    database,
    migrated_database_url,
):
    monday = datetime(2026, 8, 9, 21, 0, tzinfo=UTC)
    connection = await asyncpg.connect(migrated_database_url)
    try:
        booking_key = uuid4()
        await _seed_booking(
            connection,
            status="confirmed",
            starts_at=monday + timedelta(hours=12),
            phase="confirmed",
            error_code=None,
            updated_at=NOW,
            external_id="701",
            booking_key=booking_key,
        )
        await _seed_projection(
            connection,
            external_id="701",
            booking_key=booking_key,
            marker_state="valid",
            starts_at=monday + timedelta(hours=12),
        )
        await _seed_projection(
            connection,
            external_id="702",
            booking_key=None,
            marker_state="absent",
            starts_at=monday + timedelta(days=2, hours=10),
        )
        await _seed_projection(
            connection,
            external_id="703",
            booking_key=None,
            marker_state="absent",
            starts_at=monday + timedelta(days=8),
        )

        result = await list_calendar_bookings(
            database,
            week_start=monday,
            week_end=monday + timedelta(days=7),
            now=NOW,
        )

        assert [(item["external_id"], item["source"]) for item in result["items"]] == [
            ("701", "bot"),
            ("702", "other"),
        ]
    finally:
        await connection.close()


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


async def test_unified_booking_reconciliation_matrix_and_filters(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    synced_at = NOW - timedelta(minutes=5)
    try:
        keys = {name: uuid4() for name in (
            "in_sync", "changed", "local_missing", "provider_missing", "conflict"
        )}
        local_ids = {}
        external_ids = {
            "in_sync": "101",
            "changed": "102",
            "provider_missing": "105",
            "conflict": "106",
        }
        for index, name in enumerate(external_ids, 1):
            local_ids[name] = await _seed_booking(
                connection,
                status="confirmed",
                starts_at=NOW + timedelta(days=index),
                scheduled_end_at=NOW + timedelta(days=index, hours=1),
                phase="confirmed",
                error_code=None,
                updated_at=NOW + timedelta(minutes=index),
                external_id=external_ids[name],
                booking_key=keys[name],
            )
        await _seed_projection(
            connection,
            external_id="101",
            booking_key=keys["in_sync"],
            marker_state="valid",
            starts_at=NOW + timedelta(days=1),
            scheduled_end_at=NOW + timedelta(days=1, hours=1),
            synced_at=synced_at,
        )
        provider_changed_at = NOW + timedelta(days=10)
        await _seed_projection(
            connection,
            external_id="102",
            booking_key=keys["changed"],
            marker_state="valid",
            starts_at=provider_changed_at,
            status="cancelled",
            synced_at=synced_at,
        )
        await _seed_projection(
            connection,
            external_id="103",
            booking_key=None,
            marker_state="absent",
            starts_at=NOW + timedelta(days=3),
            synced_at=synced_at,
        )
        await _seed_projection(
            connection,
            external_id="104",
            booking_key=keys["local_missing"],
            marker_state="valid",
            starts_at=NOW + timedelta(days=4),
            synced_at=synced_at,
        )
        await _seed_projection(
            connection,
            external_id="106",
            booking_key=None,
            marker_state="invalid",
            starts_at=NOW + timedelta(days=4),
            synced_at=synced_at,
        )
        for external_id, starts_at in (
            ("107", NOW - timedelta(days=31)),
            ("108", NOW + timedelta(days=91)),
        ):
            await _seed_projection(
                connection,
                external_id=external_id,
                booking_key=None,
                marker_state="absent",
                starts_at=starts_at,
                synced_at=synced_at,
            )
        await _seed_successful_sync(connection, NOW - timedelta(minutes=1))

        upcoming = await list_bookings(
            database, view="upcoming", status=None, cursor=None, now=NOW
        )
        attention = await list_bookings(
            database, view="attention", status=None, cursor=None, now=NOW
        )
        rows = {item["reconciliation_state"]: item for item in upcoming["items"] + attention["items"]}
        assert {"y:107", "y:108"}.isdisjoint(
            item["row_key"] for item in upcoming["items"] + attention["items"]
        )

        assert rows["in_sync"]["source"] == "bot"
        assert rows["in_sync"]["id"] == local_ids["in_sync"]
        assert rows["in_sync"]["scenario_label"] == "Создание записи"
        assert rows["changed_in_yclients"]["source"] == "bot"
        assert rows["changed_in_yclients"]["starts_at"] == provider_changed_at
        assert rows["changed_in_yclients"]["status"] == "cancelled"
        assert rows["yclients_only"]["source"] == "other"
        assert rows["yclients_only"]["id"] is None
        assert rows["yclients_only"]["detail_id"] is None
        assert rows["yclients_only"]["customer_chat_id"] is None
        assert rows["yclients_only"]["scenario_label"] is None
        assert rows["local_missing"]["source"] == "bot"
        assert rows["provider_missing"]["source"] == "bot"
        assert rows["identity_conflict"]["source"] == "bot"
        for private_value in (
            "booking-snapshot", "scenario-state", "event-payload", "phone", "custom_fields"
        ):
            assert private_value not in repr(rows)
        assert "yclients_only" not in {
            item["reconciliation_state"] for item in attention["items"]
        }
        assert upcoming["freshness"] == {
            "last_success_at": synced_at,
            "stale": False,
        }

        other = await list_bookings(
            database, view="upcoming", status=None, source="other",
            reconciliation="all", cursor=None, limit=1, now=NOW
        )
        mismatch = await list_bookings(
            database, view="attention", status=None, source="all",
            reconciliation="mismatch", cursor=None, limit=1, now=NOW
        )
        cancelled = await list_bookings(
            database, view="attention", status="cancelled", source="all",
            reconciliation="all", cursor=None, limit=1, now=NOW
        )
        assert [item["source"] for item in other["items"]] == ["other"]
        assert mismatch["items"][0]["reconciliation_state"] in {
            "changed_in_yclients", "local_missing", "provider_missing", "identity_conflict"
        }
        assert [item["status"] for item in cancelled["items"]] == ["cancelled"]
    finally:
        await connection.close()


async def test_valid_marker_matches_local_by_key_without_duplicate_local_row(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    booking_key = uuid4()
    provider_starts_at = NOW + timedelta(days=2)
    provider_end_at = provider_starts_at + timedelta(hours=1)
    try:
        booking_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=1),
            phase="confirmed",
            error_code=None,
            updated_at=NOW,
            external_id="200",
            booking_key=booking_key,
            customer_id="42",
        )
        await _seed_projection(
            connection,
            external_id="201",
            booking_key=booking_key,
            marker_state="valid",
            starts_at=provider_starts_at,
            scheduled_end_at=provider_end_at,
            status="cancelled",
        )

        page = await list_bookings(
            database,
            view="attention",
            status=None,
            source="bot",
            reconciliation="mismatch",
            cursor=None,
            now=NOW,
        )

        assert len(page["items"]) == 1
        item = page["items"][0]
        assert item["row_key"] == "y:201"
        assert item["source"] == "bot"
        assert item["reconciliation_state"] == "identity_conflict"
        assert item["starts_at"] == provider_starts_at
        assert item["scheduled_end_at"] == provider_end_at
        assert item["status"] == "cancelled"
        assert item["detail_id"] == booking_id
        assert item["customer_chat_id"] == 42
        assert all(not row["row_key"].startswith("l:") for row in page["items"])
    finally:
        await connection.close()


async def test_duplicate_valid_markers_fail_closed_without_arbitrary_local_match(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    booking_key = uuid4()
    try:
        booking_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=1),
            phase="confirmed",
            error_code=None,
            updated_at=NOW,
            external_id="300",
            booking_key=booking_key,
            customer_id="42",
        )
        for external_id in ("300", "301"):
            await _seed_projection(
                connection,
                external_id=external_id,
                booking_key=booking_key,
                marker_state="valid",
                starts_at=NOW + timedelta(days=1),
            )

        first = await list_bookings(
            database,
            view="attention",
            status=None,
            source="bot",
            reconciliation="mismatch",
            cursor=None,
            limit=1,
            now=NOW,
        )
        second = await list_bookings(
            database,
            view="attention",
            status=None,
            source="bot",
            reconciliation="mismatch",
            cursor=first["next_cursor"],
            limit=1,
            now=NOW,
        )
        items = first["items"] + second["items"]

        assert [item["row_key"] for item in items] == ["y:301", "y:300"]
        assert all(item["source"] == "bot" for item in items)
        assert all(item["reconciliation_state"] == "identity_conflict" for item in items)
        assert all(item["detail_id"] == booking_id for item in items)
        assert all(item["customer_chat_id"] == 42 for item in items)
        assert all(not item["row_key"].startswith("l:") for item in items)
        assert first["has_more"] is True and second["has_more"] is False
    finally:
        await connection.close()


@pytest.mark.parametrize("marker_state", ["absent", "invalid", "different"])
async def test_known_external_id_with_bad_marker_is_bot_identity_conflict(
    database,
    migrated_database_url,
    marker_state,
):
    connection = await asyncpg.connect(migrated_database_url)
    booking_key = uuid4()
    try:
        booking_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=1),
            phase="confirmed",
            error_code=None,
            updated_at=NOW,
            external_id="400",
            booking_key=booking_key,
        )
        await _seed_projection(
            connection,
            external_id="400",
            booking_key=uuid4() if marker_state == "different" else None,
            marker_state="valid" if marker_state == "different" else marker_state,
            starts_at=NOW + timedelta(days=1),
        )

        page = await list_bookings(
            database, view="attention", status=None, cursor=None, now=NOW
        )
        item = page["items"][0]

        assert item["source"] == "bot"
        assert item["reconciliation_state"] == "identity_conflict"
        assert item["detail_id"] == booking_id
    finally:
        await connection.close()


async def test_invalid_marker_without_local_identity_is_unknown_conflict(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        await _seed_projection(
            connection,
            external_id="500",
            booking_key=None,
            marker_state="invalid",
            starts_at=NOW + timedelta(days=1),
        )

        page = await list_bookings(
            database, view="attention", status=None, cursor=None, now=NOW
        )
        item = page["items"][0]

        assert item["source"] == "unknown"
        assert item["reconciliation_state"] == "identity_conflict"
        assert item["detail_id"] is None
        assert item["customer_chat_id"] is None
    finally:
        await connection.close()


async def test_local_only_freshness_and_empty_snapshot_staleness(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        booking_id = await _seed_booking(
            connection,
            status="confirmed",
            starts_at=NOW + timedelta(days=1),
            phase="confirmed",
            error_code=None,
            updated_at=NOW,
            external_id="201",
        )
        unknown = await list_bookings(
            database, view="upcoming", status=None, cursor=None, now=NOW
        )
        assert unknown["items"][0]["id"] == booking_id
        assert unknown["items"][0]["reconciliation_state"] == "freshness_unknown"
        assert unknown["freshness"] == {"last_success_at": None, "stale": False}

        finished_at = NOW - timedelta(minutes=20)
        await _seed_successful_sync(connection, finished_at)
        exact_boundary = await list_bookings(
            database, view="attention", status=None, cursor=None, now=NOW
        )
        assert exact_boundary["items"][0]["reconciliation_state"] == "provider_missing"
        assert exact_boundary["freshness"] == {
            "last_success_at": finished_at,
            "stale": False,
        }
        await connection.execute(
            "UPDATE scheduler_jobs SET finished_at = $1 WHERE kind = 'yclients_booking_projection_sync'",
            finished_at - timedelta(seconds=1),
        )
        stale = await list_bookings(
            database, view="attention", status=None, cursor=None, now=NOW
        )
        assert stale["freshness"]["stale"] is True
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("status", "error_code", "expected_label"),
    [
        ("pending", "yclients_transport", "Сервис сверки временно недоступен"),
        ("failed", "yclients_projection_write", "Результат сверки не удалось сохранить"),
        ("failed", "private-provider-body", "Сверку не удалось выполнить"),
    ],
)
async def test_unsuccessful_projection_freshness_is_safe_and_restart_stable(
    database,
    migrated_database_url,
    status,
    error_code,
    expected_label,
):
    connection = await asyncpg.connect(migrated_database_url)
    failure_at = NOW - timedelta(minutes=3)
    try:
        await _seed_unsuccessful_sync(
            connection,
            status="failed",
            error_code="older-private-code",
            updated_at=failure_at - timedelta(minutes=1),
        )
        await _seed_unsuccessful_sync(
            connection,
            status=status,
            error_code=error_code,
            updated_at=failure_at,
        )

        page = await list_bookings(
            database, view="upcoming", status=None, cursor=None, now=NOW
        )

        assert page["freshness"] == {
            "last_success_at": None,
            "stale": False,
            "last_failure_at": failure_at,
            "last_failure_label": expected_label,
        }
        assert error_code not in repr(page["freshness"])
        assert "older-private-code" not in repr(page["freshness"])
    finally:
        await connection.close()


async def test_deleted_provider_only_row_is_safe_history(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        await _seed_projection(
            connection,
            external_id="250",
            booking_key=None,
            marker_state="absent",
            starts_at=NOW + timedelta(days=1),
            status="confirmed",
            deleted=True,
        )

        upcoming = await list_bookings(
            database, view="upcoming", status=None, cursor=None, now=NOW
        )
        history = await list_bookings(
            database, view="history", status=None, cursor=None, now=NOW
        )

        assert upcoming["items"] == []
        assert history["items"][0]["status"] == "cancelled"
        assert history["items"][0]["reconciliation_state"] == "yclients_only"
        assert history["items"][0]["detail_id"] is None
        assert history["items"][0]["customer_chat_id"] is None
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
    with pytest.raises(ValueError, match="booking source"):
        await list_bookings(
            None, view="upcoming", status=None, source="private", cursor=None
        )
    with pytest.raises(ValueError, match="booking reconciliation"):
        await list_bookings(
            None, view="upcoming", status=None, reconciliation="private", cursor=None
        )
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


async def test_valid_filters_are_bound_as_query_parameters():
    class RecordingConnection:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def transaction(self, **_kwargs):
            return self

        async def fetchrow(self, query, *args):
            self.calls.append((query, args))
            return {
                "projection_synced_at": None,
                "empty_snapshot_at": None,
                "last_error_code": None,
                "last_failure_at": None,
            }

        async def fetch(self, query, *args):
            self.calls.append((query, args))
            return []

    class RecordingDatabase:
        def __init__(self):
            self.connection = RecordingConnection()

        def acquire(self):
            return self.connection

    recording = RecordingDatabase()
    await list_bookings(
        recording,
        view="history",
        status="no_show",
        source="other",
        reconciliation="mismatch",
        cursor=None,
        now=NOW,
    )

    query, args = recording.connection.calls[-1]
    assert "$1::text" in query and "$2::text" in query and "$3::text" in query
    assert args[:3] == ("no_show", "other", "mismatch")


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


@pytest.mark.parametrize("view", ["upcoming", "history", "attention"])
async def test_provider_keyset_skips_insertion_before_y_cursor(
    database,
    migrated_database_url,
    view,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        if view == "upcoming":
            status, marker = "confirmed", "absent"
            first_at, next_at, inserted_at = (
                NOW + timedelta(days=2),
                NOW + timedelta(days=3),
                NOW + timedelta(days=1),
            )
            first_sync = next_sync = inserted_sync = NOW
        elif view == "history":
            status, marker = "cancelled", "absent"
            first_at, next_at, inserted_at = (
                NOW - timedelta(days=1),
                NOW - timedelta(days=2),
                NOW,
            )
            first_sync = next_sync = inserted_sync = NOW
        else:
            status, marker = "confirmed", "valid"
            first_at = next_at = inserted_at = NOW + timedelta(days=1)
            first_sync, next_sync, inserted_sync = (
                NOW + timedelta(minutes=2),
                NOW + timedelta(minutes=1),
                NOW + timedelta(minutes=3),
            )

        await _seed_projection(
            connection, external_id="301", booking_key=uuid4() if marker == "valid" else None,
            marker_state=marker, starts_at=first_at, status=status, synced_at=first_sync,
        )
        await _seed_projection(
            connection, external_id="302", booking_key=uuid4() if marker == "valid" else None,
            marker_state=marker, starts_at=next_at, status=status, synced_at=next_sync,
        )
        first_page = await list_bookings(
            database, view=view, status=None, cursor=None, limit=1, now=NOW
        )
        await _seed_projection(
            connection, external_id="300", booking_key=uuid4() if marker == "valid" else None,
            marker_state=marker, starts_at=inserted_at, status=status, synced_at=inserted_sync,
        )
        second_page = await list_bookings(
            database, view=view, status=None, cursor=first_page["next_cursor"],
            limit=1, now=NOW,
        )

        assert first_page["items"][0]["row_key"] == "y:301"
        assert second_page["items"][0]["row_key"] == "y:302"
        assert second_page["items"][0]["row_key"] != "y:300"
    finally:
        await connection.close()
