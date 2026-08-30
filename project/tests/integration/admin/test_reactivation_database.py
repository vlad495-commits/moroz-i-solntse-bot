from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.common.db import Database
from reactivation_database import (
    create_campaign,
    get_marketing_consent,
    get_page_data,
    get_settings,
    queue_campaign,
    save_settings,
    set_marketing_consent,
)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    pool = Database(migrated_database_url, min_size=1, max_size=1)
    await pool.connect()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_booking(connection, *, customer_id: str, completed_at: datetime):
    scenario_id = uuid4()
    await connection.execute(
        """
        INSERT INTO booking_scenarios
            (id, kind, phase, idempotency_key, customer_id, state,
             created_at, updated_at)
        VALUES ($1, 'create', 'confirmed', $2, $3, '{}'::jsonb, $4, $4)
        """,
        scenario_id,
        f"reactivation:{uuid4()}",
        customer_id,
        completed_at,
    )
    await connection.execute(
        """
        INSERT INTO bookings
            (id, last_scenario_id, external_id, customer_id, slot_id,
             starts_at, scheduled_end_at, status, snapshot, booking_key,
             updated_at)
        VALUES ($1, $2, $3, $4, 'slot', $5, $6, 'completed',
                '{}'::jsonb, $7, $6)
        """,
        uuid4(),
        scenario_id,
        f"external-{uuid4()}",
        customer_id,
        completed_at - timedelta(hours=1),
        completed_at,
        uuid4(),
    )


async def test_processing_consent_never_becomes_marketing_consent(
    database,
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        await connection.execute(
            """
            INSERT INTO processing_consents (channel, user_id, consent_version)
            VALUES ('telegram', '42', 'v1')
            """
        )
    finally:
        await connection.close()

    assert await get_marketing_consent(
        database, channel="telegram", user_id="42"
    ) is None


async def test_marketing_consent_grant_and_revoke_are_explicit(database):
    granted = await set_marketing_consent(
        database,
        channel="telegram",
        user_id="42",
        consent_version="marketing-v1",
        active=True,
    )
    assert granted["active"] is True
    assert granted["granted_at"] is not None
    assert granted["revoked_at"] is None

    revoked = await set_marketing_consent(
        database,
        channel="telegram",
        user_id="42",
        consent_version="must-not-replace-granted-version",
        active=False,
    )
    assert revoked["active"] is False
    assert revoked["consent_version"] == "marketing-v1"
    assert revoked["granted_at"] == granted["granted_at"]
    assert revoked["revoked_at"] is not None


async def test_reactivation_settings_round_trip(database):
    initial = await get_settings(database)
    assert initial["sleeping_days"] == 90
    assert initial["discount_percent"] == 0

    saved = await save_settings(
        database,
        after_visit_days=2,
        sleeping_days=120,
        discount_percent=15,
        monthly_message_limit=2,
        ignore_limit=3,
        base_offer="Базовый оффер",
        llm_instruction="Не менять условия оффера.",
    )
    assert saved["after_visit_days"] == 2
    assert saved["sleeping_days"] == 120
    assert saved["discount_percent"] == 15
    assert saved["base_offer"] == "Базовый оффер"
    assert saved["llm_instruction"] == "Не менять условия оффера."


@pytest.mark.parametrize(
    ("segment", "visits", "age_days", "eligible"),
    (
        ("after_visit", 1, 2, True),
        ("after_visit", 1, 0, False),
        ("after_visit", 2, 2, False),
        ("sleeping", 1, 120, True),
        ("sleeping", 1, 10, False),
        ("regular", 2, 10, True),
        ("regular", 2, 120, False),
        ("regular", 1, 120, False),
    ),
)
async def test_campaign_queue_uses_deterministic_segments_and_active_consent(
    database,
    migrated_database_url,
    segment,
    visits,
    age_days,
    eligible,
):
    customer_id = f"customer-{segment}-{visits}-{age_days}"
    connection = await asyncpg.connect(migrated_database_url)
    try:
        for index in range(visits):
            await _seed_booking(
                connection,
                customer_id=customer_id,
                completed_at=NOW - timedelta(days=age_days + index),
            )
    finally:
        await connection.close()
    await set_marketing_consent(
        database,
        channel="telegram",
        user_id=customer_id,
        consent_version="marketing-v1",
        active=True,
    )
    await save_settings(
        database,
        after_visit_days=1,
        sleeping_days=90,
        discount_percent=10,
        monthly_message_limit=1,
        ignore_limit=2,
        base_offer="Оффер",
        llm_instruction="Инструкция",
    )

    campaign_id = await create_campaign(
        database, segment=segment, created_by=1
    )
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_deliveries "
            "WHERE campaign_id = $1 AND status = 'draft'",
            campaign_id,
        ) == (1 if eligible else 0)
    queued = await queue_campaign(
        database, campaign_id=campaign_id, now=NOW
    )

    assert queued["recipient_count"] == (1 if eligible else 0)
    page = await get_page_data(database)
    campaign = next(item for item in page["campaigns"] if item["id"] == campaign_id)
    assert campaign["status"] == "queued"
    assert campaign["sent_count"] == 0
    assert campaign["error_count"] == 0


async def test_revoked_consent_is_never_queued(database, migrated_database_url):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        await _seed_booking(
            connection,
            customer_id="revoked",
            completed_at=NOW - timedelta(days=120),
        )
    finally:
        await connection.close()
    await set_marketing_consent(
        database,
        channel="telegram",
        user_id="revoked",
        consent_version="marketing-v1",
        active=True,
    )
    await set_marketing_consent(
        database,
        channel="telegram",
        user_id="revoked",
        consent_version="marketing-v1",
        active=False,
    )

    campaign_id = await create_campaign(
        database, segment="sleeping", created_by=1
    )
    queued = await queue_campaign(database, campaign_id=campaign_id, now=NOW)

    assert queued["recipient_count"] == 0
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_deliveries"
        ) == 0


async def test_queue_never_adds_customer_absent_from_draft(
    database, migrated_database_url
):
    async def seed(customer_id):
        connection = await asyncpg.connect(migrated_database_url)
        try:
            await _seed_booking(
                connection,
                customer_id=customer_id,
                completed_at=NOW - timedelta(days=2),
            )
        finally:
            await connection.close()
        await set_marketing_consent(
            database,
            channel="telegram",
            user_id=customer_id,
            consent_version="marketing-v1",
            active=True,
        )

    await seed("previewed")
    campaign_id = await create_campaign(
        database, segment="after_visit", created_by=1
    )
    await seed("became-eligible-later")

    queued = await queue_campaign(database, campaign_id=campaign_id, now=NOW)

    assert queued["recipient_count"] == 1
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_deliveries "
            "WHERE campaign_id = $1 AND user_id = 'became-eligible-later'",
            campaign_id,
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM outbound_messages"
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox"
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM scheduler_jobs"
        ) == 0


async def test_active_human_mode_is_never_queued(database, migrated_database_url):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        await _seed_booking(
            connection,
            customer_id="human-mode",
            completed_at=NOW - timedelta(days=120),
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, enabled_at)
            VALUES ('human-mode', true, 'manual', $1)
            """,
            NOW,
        )
    finally:
        await connection.close()
    await set_marketing_consent(
        database,
        channel="telegram",
        user_id="human-mode",
        consent_version="marketing-v1",
        active=True,
    )

    campaign_id = await create_campaign(
        database, segment="sleeping", created_by=1
    )
    queued = await queue_campaign(database, campaign_id=campaign_id, now=NOW)

    assert queued["recipient_count"] == 0
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_deliveries"
        ) == 0
