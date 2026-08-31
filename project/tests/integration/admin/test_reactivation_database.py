from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.reactivation.policy import ProgramPolicy
from moroz.security.consent import ConsentService
from reactivation_database import (
    activate_version as activate_program_version,
    approve_legal,
    create_campaign,
    create_draft,
    get_dashboard,
    get_marketing_consent,
    get_page_data,
    get_settings,
    queue_campaign,
    save_settings,
    set_marketing_consent,
    preview_version,
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


async def _grant_proven_marketing_consent(database, user_id: str):
    return await ConsentService(database).grant_marketing(
        channel="telegram",
        user_id=user_id,
        proof_text="Точный тестовый текст рекламного согласия",
        source="telegram_explicit",
        source_event_id=f"test-callback:{uuid4()}",
        occurred_at=NOW,
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


async def test_admin_cannot_grant_but_can_revoke_marketing_consent(
    database, migrated_database_url
):
    with pytest.raises(ValueError, match="cannot grant"):
        await set_marketing_consent(
            database,
            channel="telegram",
            user_id="42",
            consent_version="marketing-v1",
            active=True,
        )

    connection = await asyncpg.connect(migrated_database_url)
    event_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version,
                 proof_text_hash, source, source_event_id, occurred_at)
            VALUES ($1, 'telegram', '42', 'granted', 'marketing-v1',
                    'proof', 'telegram_explicit', 'callback-42', now())
            """,
            event_id,
        )
        await connection.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, consent_version, active, granted_at,
                 source, proof_event_id, proof_text_hash)
            VALUES ($1, 'telegram', '42', 'marketing-v1', true, now(),
                    'telegram_explicit', $2, 'proof')
            """,
            uuid4(),
            event_id,
        )
    finally:
        await connection.close()

    revoked = await set_marketing_consent(
        database,
        channel="telegram",
        user_id="42",
        consent_version="must-not-replace-granted-version",
        active=False,
    )
    assert revoked["active"] is False
    assert revoked["consent_version"] == "marketing-v1"
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
        ("sleeping", 1, 120, True),
        ("sleeping", 1, 10, False),
        ("regular", 2, 10, True),
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
    await _grant_proven_marketing_consent(database, customer_id)
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
    await _grant_proven_marketing_consent(database, "revoked")
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
    await _grant_proven_marketing_consent(database, "human-mode")

    campaign_id = await create_campaign(
        database, segment="sleeping", created_by=1
    )
    queued = await queue_campaign(database, campaign_id=campaign_id, now=NOW)

    assert queued["recipient_count"] == 0
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_deliveries"
        ) == 0


async def test_v2_admin_wrappers_keep_owner_gate_and_dry_run(monkeypatch, database):
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "admin-wrapper-secret")
    monkeypatch.setenv("BUSINESS_ALERT_CHAT_ID", "")
    async with database.acquire() as connection:
        owner_id = await connection.fetchval(
            """
            INSERT INTO admin_users
                (username, role, password_hash, totp_secret, enabled)
            VALUES ('wrapper-owner', 'owner', 'x', 'x', true)
            RETURNING id
            """
        )
        admin_id = await connection.fetchval(
            """
            INSERT INTO admin_users
                (username, role, password_hash, totp_secret, enabled)
            VALUES ('wrapper-admin', 'admin', 'x', 'x', true)
            RETURNING id
            """
        )

    with pytest.raises(PermissionError):
        await create_draft(
            database, policy=ProgramPolicy(), actor_id=admin_id, now=NOW
        )
    version_id = await create_draft(
        database, policy=ProgramPolicy(), actor_id=owner_id, now=NOW
    )
    preview = await preview_version(
        database, version_id, actor_id=owner_id, now=NOW
    )
    await approve_legal(
        database,
        actor_id=owner_id,
        reference="wrapper-legal-review",
        now=NOW,
    )
    activated = await activate_program_version(
        database, version_id, actor_id=owner_id, now=NOW
    )
    dashboard = await get_dashboard(database, actor_id=owner_id)

    assert preview.total == 0
    assert activated["status"] == "active"
    assert dashboard["settings"]["mode"] == "dry_run"
    assert dashboard["settings"]["active_version_id"] == version_id
