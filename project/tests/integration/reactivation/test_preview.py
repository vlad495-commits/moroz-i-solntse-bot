import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.reactivation.policy import ProgramPolicy
import moroz.reactivation.repository as repository_module
from moroz.reactivation.repository import ActivationBlocked, ReactivationRepository


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
SECRET = "integration-preview-secret"


@pytest_asyncio.fixture
async def database(migrated_database_url):
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def actors(database):
    async with database.acquire() as connection:
        owner_id = await connection.fetchval(
            """
            INSERT INTO admin_users
                (username, role, password_hash, totp_secret, enabled)
            VALUES ('preview-owner', 'owner', 'x', 'x', true)
            RETURNING id
            """
        )
        admin_id = await connection.fetchval(
            """
            INSERT INTO admin_users
                (username, role, password_hash, totp_secret, enabled)
            VALUES ('preview-admin', 'admin', 'x', 'x', true)
            RETURNING id
            """
        )
    return owner_id, admin_id


@pytest_asyncio.fixture
async def repository(database, actors):
    owner_id, _ = actors
    value = ReactivationRepository(
        database,
        session_secret=SECRET,
        business_alert_chat_id="90001",
    )
    version_id = await value.create_draft(ProgramPolicy(), owner_id, NOW)
    return value, version_id, owner_id


async def _consent(
    connection,
    user_id: str,
    *,
    proven: bool = True,
    active: bool = True,
    suppressed: bool = False,
):
    consent_id = uuid4()
    proof_id = uuid4() if proven else None
    if proof_id is not None:
        await connection.execute(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version,
                 proof_text_hash, source, source_event_id, occurred_at,
                 created_at)
            VALUES ($1, 'telegram', $2, 'granted', 'marketing-v1',
                    'proof-hash', 'telegram_explicit', $3, $4, $4)
            """,
            proof_id,
            user_id,
            f"event-{consent_id}",
            NOW - timedelta(days=200),
        )
    await connection.execute(
        """
        INSERT INTO marketing_consents
            (id, channel, user_id, consent_version, active, granted_at,
             revoked_at, source, proof_event_id, proof_text_hash,
             suppressed_at, suppression_reason, suppression_source,
             created_at, updated_at)
        VALUES ($1, 'telegram', $2, 'marketing-v1', $3, $4, $5,
                $6, $7, $8, $9, $10, $11, $4, $4)
        """,
        consent_id,
        user_id,
        active,
        NOW - timedelta(days=200),
        None if active else NOW - timedelta(days=1),
        "telegram_explicit" if proven else "legacy_unproven",
        proof_id,
        "proof-hash" if proven else None,
        NOW - timedelta(hours=1) if suppressed else None,
        "user_stop" if suppressed else None,
        "telegram_explicit" if suppressed else None,
    )
    return consent_id


async def _candidate(
    connection,
    user_id: str,
    *,
    identity_status: str = "verified",
    last_visit: datetime | None = None,
    inbound: datetime | None = None,
    future_booking: datetime | None = None,
    history_synced: datetime | None = None,
    recent_synced: datetime | None = None,
    sync_status: str = "current",
):
    await connection.execute(
        """
        INSERT INTO customer_activity_projection
            (channel, user_id, yclients_client_id, identity_status,
             last_completed_visit_at, last_meaningful_inbound_at,
             next_active_booking_at, history_synced_at,
             recent_bookings_synced_at, sync_status, created_at, updated_at)
        VALUES ('telegram', $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
        """,
        user_id,
        f"client-{user_id}" if identity_status == "verified" else None,
        identity_status,
        last_visit if last_visit is not None else NOW - timedelta(days=100),
        inbound,
        future_booking,
        history_synced if history_synced is not None else NOW - timedelta(hours=1),
        recent_synced if recent_synced is not None else NOW - timedelta(minutes=5),
        sync_status,
        NOW - timedelta(minutes=5),
    )


async def _seed_eligible(connection, user_id: str):
    await _consent(connection, user_id)
    await _candidate(connection, user_id)


async def _ready(repository, version_id, owner_id):
    await repository.preview_version(version_id, actor_id=owner_id, now=NOW)
    outbound_id = await repository.queue_test_send(version_id, owner_id, NOW)
    async with repository._database.acquire() as connection:
        await connection.execute(
            "UPDATE outbound_messages SET status = 'sent' WHERE id = $1",
            outbound_id,
        )
    assert await repository.record_test_sent(outbound_id, NOW)
    await repository.approve_legal(owner_id, "legal-review-2026-08-30", NOW)


async def _test_outbound_id(database, version_id):
    async with database.acquire() as connection:
        return await connection.fetchval(
            "SELECT test_outbound_id FROM reactivation_program_versions WHERE id = $1",
            version_id,
        )


async def test_preview_counts_every_consent_once_by_priority(repository, database):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        for user_id in ("100000001", "100000002"):
            await _seed_eligible(connection, user_id)
        for user_id in ("200000001", "200000002"):
            await _consent(connection, user_id, proven=False)
            await _candidate(connection, user_id)
        await _seed_eligible(connection, "300000001")
        await connection.execute(
            "UPDATE customer_activity_projection SET next_active_booking_at = $1 "
            "WHERE user_id = '300000001'",
            NOW + timedelta(days=1),
        )
        await _seed_eligible(connection, "400000001")
        await connection.execute(
            "UPDATE customer_activity_projection SET last_meaningful_inbound_at = $1 "
            "WHERE user_id = '400000001'",
            NOW - timedelta(days=1),
        )
        await _consent(connection, "500000001")
        await _candidate(connection, "500000001", identity_status="unverified")
        await _consent(connection, "600000001", active=False)
        await _candidate(connection, "600000001")
        await _seed_eligible(connection, "700000001")
        await connection.execute(
            "UPDATE customer_activity_projection SET history_synced_at = $1 "
            "WHERE user_id = '700000001'",
            NOW - timedelta(hours=25),
        )
        await _seed_eligible(connection, "800000001")
        await connection.execute(
            "UPDATE customer_activity_projection SET recent_bookings_synced_at = $1 "
            "WHERE user_id = '800000001'",
            NOW - timedelta(minutes=16),
        )
        await _seed_eligible(connection, "900000001")
        await connection.execute(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status,
                 activity_anchor_at, created_at, updated_at)
            VALUES ($1, 'telegram', '900000001', $2, 'active', $3, $4, $4)
            """,
            uuid4(),
            version_id,
            NOW - timedelta(days=100),
            NOW - timedelta(days=40),
        )
        await _seed_eligible(connection, "990000001")
        await connection.execute(
            """
            INSERT INTO human_mode (customer_id, enabled, reason_code, enabled_at)
            VALUES ('990000001', true, 'manual', $1)
            """,
            NOW - timedelta(minutes=1),
        )

    preview = await value.preview_version(version_id, actor_id=owner_id, now=NOW)

    assert preview.total == 12
    assert sum(preview.excluded_by_reason.values()) + preview.eligible == 12
    assert preview.eligible == 2
    assert preview.planned_main == 2
    assert preview.planned_reminder == 2
    assert preview.excluded_by_reason["no_proven_consent"] == 2
    assert preview.excluded_by_reason["future_booking"] == 1
    assert len(preview.masked_samples) == 2
    assert all("10000000" not in sample for sample in preview.masked_samples)


async def test_preview_is_deterministic_and_dry_run_has_no_side_effects(
    repository, database
):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")

    first = await value.preview_version(version_id, actor_id=owner_id, now=NOW)
    second = await value.preview_version(version_id, actor_id=owner_id, now=NOW)

    assert first == second
    assert first.masked_samples == ("telegram:***6789",)
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journeys") == 0
        assert await connection.fetchval("SELECT count(*) FROM task_outbox") == 0
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 0


async def test_preview_hmac_never_receives_raw_recipient_or_provider_ids(
    monkeypatch, repository, database
):
    value, version_id, owner_id = repository
    raw_recipient = "telegram-user-raw-123456"
    async with database.acquire() as connection:
        consent_id = await _consent(connection, raw_recipient)
        await _candidate(connection, raw_recipient)
    payloads = []
    original = repository_module.hmac.new

    def capture(key, message, digestmod):
        payloads.append(message.decode("utf-8"))
        return original(key, message, digestmod)

    monkeypatch.setattr(repository_module.hmac, "new", capture)

    await value.preview_version(version_id, actor_id=owner_id, now=NOW)

    assert len(payloads) == 1
    assert str(consent_id) in payloads[0]
    assert raw_recipient not in payloads[0]
    assert f"client-{raw_recipient}" not in payloads[0]


async def test_preview_without_admin_secret_fails_closed(database, actors):
    owner_id, _ = actors
    value = ReactivationRepository(database)
    async with database.acquire() as connection:
        version_id = await connection.fetchval(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days,
                 reminder_enabled, reminder_after_days, cooldown_days,
                 main_text, reminder_text, template_checksum, created_by,
                 created_at)
            VALUES ($1, 1, 'draft', 90, true, 5, 90, 'main', 'reminder',
                    'invalid-until-checked', $2, $3)
            RETURNING id
            """,
            uuid4(), owner_id, NOW,
        )

    with pytest.raises(ValueError, match="admin session secret"):
        await value.preview_version(version_id, actor_id=owner_id, now=NOW)


@pytest.mark.parametrize(
    "missing_gate",
    ["fresh_preview", "same_checksum", "current_watermarks", "test_sent", "legal_approved"],
)
async def test_activation_fails_closed_for_every_gate(
    repository, database, missing_gate
):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
    await _ready(value, version_id, owner_id)
    async with database.acquire() as connection:
        if missing_gate == "fresh_preview":
            await connection.execute(
                "UPDATE reactivation_program_versions SET preview_created_at = $2 "
                "WHERE id = $1",
                version_id,
                NOW - timedelta(minutes=30),
            )
        elif missing_gate == "same_checksum":
            await connection.execute(
                "UPDATE reactivation_program_versions SET main_text = main_text || '!' "
                "WHERE id = $1",
                version_id,
            )
        elif missing_gate == "current_watermarks":
            await connection.execute(
                "UPDATE marketing_consents SET updated_at = $1 WHERE user_id = '123456789'",
                NOW + timedelta(seconds=1),
            )
        elif missing_gate == "test_sent":
            await connection.execute(
                "UPDATE reactivation_program_versions SET test_sent_at = NULL WHERE id = $1",
                version_id,
            )
        else:
            await connection.execute(
                "UPDATE reactivation_settings SET legal_status = 'pending' WHERE id = 1"
            )

    with pytest.raises(ActivationBlocked) as error:
        await value.activate_version(version_id, owner_id, NOW)
    assert error.value.code == missing_gate


@pytest.mark.parametrize("mutation", ["consent", "inbound", "booking", "journey"])
async def test_preview_invalidates_on_each_population_mutation(
    repository, database, mutation
):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
        if mutation == "journey":
            await connection.execute(
                """
                INSERT INTO reactivation_journeys
                    (id, channel, user_id, program_version_id, status,
                     activity_anchor_at, closed_at, created_at, updated_at)
                VALUES ($1, 'telegram', '123456789', $2, 'closed', $3, $4, $4, $4)
                """,
                uuid4(), version_id, NOW - timedelta(days=100), NOW - timedelta(days=40),
            )
    await _ready(value, version_id, owner_id)
    async with database.acquire() as connection:
        if mutation == "consent":
            await connection.execute(
                "UPDATE marketing_consents SET updated_at = $1 WHERE user_id = '123456789'",
                NOW + timedelta(seconds=1),
            )
        elif mutation == "inbound":
            await connection.execute(
                "UPDATE customer_activity_projection SET last_meaningful_inbound_at = $1 "
                "WHERE user_id = '123456789'",
                NOW - timedelta(days=2),
            )
        elif mutation == "booking":
            await connection.execute(
                "UPDATE customer_activity_projection SET next_active_booking_at = $1 "
                "WHERE user_id = '123456789'",
                NOW + timedelta(days=2),
            )
        else:
            await connection.execute(
                "UPDATE reactivation_journeys SET updated_at = $1 WHERE user_id = '123456789'",
                NOW + timedelta(seconds=1),
            )

    with pytest.raises(ActivationBlocked) as error:
        await value.activate_version(version_id, owner_id, NOW)
    assert error.value.code == "current_watermarks"


@pytest.mark.parametrize("source", ["consent", "activity"])
async def test_preview_fingerprint_keeps_each_source_timestamp(
    repository, database, source
):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
        consent_updated_at = NOW - timedelta(minutes=3 if source == "consent" else 1)
        activity_updated_at = NOW - timedelta(minutes=3 if source == "activity" else 1)
        await connection.execute(
            "UPDATE marketing_consents SET updated_at = $1 WHERE user_id = '123456789'",
            consent_updated_at,
        )
        await connection.execute(
            "UPDATE customer_activity_projection SET updated_at = $1 "
            "WHERE user_id = '123456789'",
            activity_updated_at,
        )
    await _ready(value, version_id, owner_id)
    async with database.acquire() as connection:
        table = (
            "marketing_consents"
            if source == "consent"
            else "customer_activity_projection"
        )
        await connection.execute(
            f"UPDATE {table} SET updated_at = $1 WHERE user_id = '123456789'",
            NOW - timedelta(minutes=2),
        )

    with pytest.raises(ActivationBlocked) as error:
        await value.activate_version(version_id, owner_id, NOW)
    assert error.value.code == "current_watermarks"


async def test_preview_fingerprint_keeps_every_journey_row(
    repository, database
):
    value, version_id, owner_id = repository
    old_id = uuid4()
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
        await connection.executemany(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status,
                 activity_anchor_at, closed_at, created_at, updated_at)
            VALUES ($1, 'telegram', '123456789', $2, 'closed', $3, $4, $4, $5)
            """,
            [
                (
                    old_id,
                    version_id,
                    NOW - timedelta(days=300),
                    NOW - timedelta(days=250),
                    NOW - timedelta(days=240),
                ),
                (
                    uuid4(),
                    version_id,
                    NOW - timedelta(days=220),
                    NOW - timedelta(days=200),
                    NOW - timedelta(days=100),
                ),
            ],
        )
    await _ready(value, version_id, owner_id)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE reactivation_journeys SET updated_at = $2 WHERE id = $1",
            old_id,
            NOW - timedelta(days=230),
        )

    with pytest.raises(ActivationBlocked) as error:
        await value.activate_version(version_id, owner_id, NOW)
    assert error.value.code == "current_watermarks"


@pytest.mark.parametrize("source", ["human_mode", "escalation"])
async def test_preview_fingerprint_keeps_safe_control_row_state(
    repository, database, source
):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
        if source == "human_mode":
            await connection.execute(
                """
                INSERT INTO human_mode
                    (customer_id, enabled, reason_code, enabled_at, expires_at)
                VALUES ('123456789', true, 'manual', $1, $2)
                """,
                NOW - timedelta(minutes=20),
                NOW + timedelta(hours=1),
            )
        else:
            await connection.execute(
                """
                INSERT INTO escalations
                    (id, source, customer_id, status, reason_code, payload,
                     created_at, resolved_at)
                VALUES ($1, 'test', '123456789', 'resolved', 'test',
                        '{}'::jsonb, $2, $3)
                """,
                uuid4(),
                NOW - timedelta(hours=2),
                NOW - timedelta(minutes=10),
            )
    await _ready(value, version_id, owner_id)
    async with database.acquire() as connection:
        if source == "human_mode":
            await connection.execute(
                "UPDATE human_mode SET enabled_at = $1 WHERE customer_id = '123456789'",
                NOW - timedelta(minutes=15),
            )
        else:
            await connection.execute(
                "UPDATE escalations SET created_at = $1 WHERE customer_id = '123456789'",
                NOW - timedelta(hours=1),
            )

    with pytest.raises(ActivationBlocked) as error:
        await value.activate_version(version_id, owner_id, NOW)
    assert error.value.code == "current_watermarks"


async def test_test_send_uses_only_configured_alert_and_callback_sets_sent_at(
    repository, database
):
    value, version_id, owner_id = repository
    await value.preview_version(version_id, actor_id=owner_id, now=NOW)

    outbound_id = await value.queue_test_send(version_id, owner_id, NOW)

    async with database.acquire() as connection:
        outbound = await connection.fetchrow(
            "SELECT chat_id, idempotency_key, status FROM outbound_messages WHERE id = $1",
            outbound_id,
        )
        sent_at = await connection.fetchval(
            "SELECT test_sent_at FROM reactivation_program_versions WHERE id = $1",
            version_id,
        )
    assert tuple(outbound) == (
        "90001",
        f"reactivation-test:{version_id}:" + (await value.get_dashboard(owner_id))["versions"][0]["template_checksum"],
        "pending",
    )
    assert sent_at is None
    assert not await value.record_test_sent(outbound_id, NOW)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE outbound_messages SET status = 'sent' WHERE id = $1", outbound_id
        )
    delivery_callback = ReactivationRepository(database)
    assert await delivery_callback.record_test_sent(outbound_id, NOW)


@pytest.mark.parametrize("mutation", ["configured_chat", "channel", "chat_id", "text"])
async def test_test_sent_gate_binds_exact_current_delivery_proof(
    repository, database, mutation
):
    value, version_id, owner_id = repository
    await _ready(value, version_id, owner_id)
    outbound_id = await _test_outbound_id(database, version_id)
    if mutation == "configured_chat":
        value.business_alert_chat_id = "90002"
    else:
        column, replacement = {
            "channel": ("channel", "email"),
            "chat_id": ("chat_id", "90002"),
            "text": ("text", "tampered test text"),
        }[mutation]
        async with database.acquire() as connection:
            await connection.execute(
                f"UPDATE outbound_messages SET {column} = $2 WHERE id = $1",
                outbound_id,
                replacement,
            )

    with pytest.raises(ActivationBlocked) as error:
        await value.activate_version(version_id, owner_id, NOW)
    assert error.value.code == "test_sent"


async def test_delivery_callback_and_requeue_are_audit_idempotent(
    repository, database
):
    value, version_id, owner_id = repository
    await value.preview_version(version_id, actor_id=owner_id, now=NOW)
    outbound_id = await value.queue_test_send(version_id, owner_id, NOW)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE outbound_messages SET status = 'sent' WHERE id = $1", outbound_id
        )
    assert await value.record_test_sent(outbound_id, NOW)
    async with database.acquire() as connection:
        audit_count = await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events "
            "WHERE action IN ('reactivation.test_sent', 'reactivation.test_queued')"
        )

    assert not await value.record_test_sent(outbound_id, NOW + timedelta(seconds=1))
    assert await value.queue_test_send(
        version_id, owner_id, NOW + timedelta(seconds=1)
    ) == outbound_id

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events "
            "WHERE action IN ('reactivation.test_sent', 'reactivation.test_queued')"
        ) == audit_count
        assert await connection.fetchval(
            "SELECT test_sent_at FROM reactivation_program_versions WHERE id = $1",
            version_id,
        ) == NOW


@pytest.mark.parametrize("transition", ["activate", "set_mode_active"])
async def test_activation_fence_serializes_concurrent_consent_writer(
    repository, database, migrated_database_url, transition
):
    value, version_id, owner_id = repository
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
    await _ready(value, version_id, owner_id)
    if transition == "set_mode_active":
        await value.activate_version(version_id, owner_id, NOW)
        await value.set_mode("paused", owner_id, NOW)
    population_checked = asyncio.Event()
    release_activation = asyncio.Event()
    original_population = value._population

    async def pause_after_population(connection, version, now):
        result = await original_population(connection, version, now)
        population_checked.set()
        await release_activation.wait()
        return result

    value._population = pause_after_population
    activation = asyncio.create_task(
        value.activate_version(version_id, owner_id, NOW)
        if transition == "activate"
        else value.set_mode("active", owner_id, NOW)
    )
    await population_checked.wait()

    writer_connection = await asyncpg.connect(migrated_database_url)

    async def revoke():
        try:
            connection = writer_connection
            async with connection.transaction():
                await connection.execute(
                    "UPDATE marketing_consents SET active = false, revoked_at = $1, "
                    "updated_at = $1 WHERE user_id = '123456789'",
                    NOW + timedelta(seconds=1),
                )
        finally:
            await writer_connection.close()

    writer = asyncio.create_task(revoke())
    try:
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        writer_was_blocked = not done
    finally:
        release_activation.set()
    activated = await activation
    await writer

    assert writer_was_blocked, "consent writer committed inside activation recheck gap"
    assert activated["status" if transition == "activate" else "mode"] == "active"
    async with database.acquire() as connection:
        assert not await connection.fetchval(
            "SELECT active FROM marketing_consents WHERE user_id = '123456789'"
        )


async def test_blank_alert_chat_skips_test_gate(database, actors):
    owner_id, _ = actors
    value = ReactivationRepository(database, session_secret=SECRET, business_alert_chat_id="")
    version_id = await value.create_draft(ProgramPolicy(), owner_id, NOW)
    await value.preview_version(version_id, actor_id=owner_id, now=NOW)
    await value.approve_legal(owner_id, "legal-review", NOW)

    activated = await value.activate_version(version_id, owner_id, NOW)

    assert activated["status"] == "active"


async def test_only_owner_can_activate(repository, actors):
    value, version_id, _ = repository
    _, admin_id = actors

    with pytest.raises(PermissionError):
        await value.activate_version(version_id, admin_id, NOW)


async def test_activation_retires_previous_version_and_rechecks_mode(
    database, actors
):
    owner_id, _ = actors
    value = ReactivationRepository(database, session_secret=SECRET, business_alert_chat_id="")
    first = await value.create_draft(ProgramPolicy(), owner_id, NOW)
    async with database.acquire() as connection:
        await _seed_eligible(connection, "123456789")
    await value.preview_version(first, actor_id=owner_id, now=NOW)
    await value.approve_legal(owner_id, "legal-review", NOW)
    await value.activate_version(first, owner_id, NOW)
    second = await value.create_draft(
        ProgramPolicy(inactivity_days=120, cooldown_days=120), owner_id, NOW
    )
    await value.preview_version(second, actor_id=owner_id, now=NOW)
    await value.activate_version(second, owner_id, NOW)

    async with database.acquire() as connection:
        statuses = await connection.fetch(
            "SELECT id, status FROM reactivation_program_versions ORDER BY version_number"
        )
    assert [tuple(row) for row in statuses] == [(first, "retired"), (second, "active")]

    await value.set_mode("paused", owner_id, NOW)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE marketing_consents SET updated_at = $1", NOW + timedelta(seconds=1)
        )
    with pytest.raises(ActivationBlocked) as error:
        await value.set_mode("active", owner_id, NOW)
    assert error.value.code == "current_watermarks"


async def test_audit_snapshots_have_before_after_but_never_message_text(repository, database):
    value, version_id, owner_id = repository
    await value.preview_version(version_id, actor_id=owner_id, now=NOW)
    await value.approve_legal(owner_id, "legal-review", NOW)
    without_test_gate = ReactivationRepository(
        database, session_secret=SECRET, business_alert_chat_id=""
    )
    await without_test_gate.activate_version(version_id, owner_id, NOW)

    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT action, before, after FROM admin_audit_events "
            "WHERE object_type LIKE 'reactivation%' ORDER BY id"
        )
    assert rows
    assert all(row["before"] is not None and row["after"] is not None for row in rows)
    serialized = repr([dict(row) for row in rows])
    assert ProgramPolicy().main_text not in serialized
    assert ProgramPolicy().reminder_text not in serialized
