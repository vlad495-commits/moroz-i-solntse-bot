import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
)

from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import DeliveryResult, TelegramSender
from moroz.reactivation.policy import ProgramPolicy, template_checksum
from moroz.reactivation.repository import ReactivationRepository
from moroz.reactivation.repository import PROGRAM_LOCK_SUBJECT


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class FakeTelegram:
    def __init__(self, error=None, *, blocked=False):
        self.error = error
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.blocked:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(message_id=len(self.calls))


def _telegram_error(kind):
    method = SimpleNamespace()
    if kind is TelegramRetryAfter:
        return kind(method, "private retry", 5)
    return kind(method, "private error")


@pytest_asyncio.fixture
async def delivery(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=8)
    await database.connect()
    version_id = uuid4()
    policy = ProgramPolicy()
    async with database.acquire() as connection:
        owner_id = await connection.fetchval(
            """
            INSERT INTO admin_users
                (username, role, password_hash, totp_secret, enabled)
            VALUES ('delivery-owner', 'owner', 'x', 'x', true)
            RETURNING id
            """
        )
        await connection.execute(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days, reminder_enabled,
                 reminder_after_days, cooldown_days, main_text, reminder_text,
                 template_checksum)
            VALUES ($1, 1, 'active', 90, true, 5, 90, $2, $3, $4)
            """,
            version_id, policy.main_text, policy.reminder_text,
            template_checksum(policy),
        )
        await connection.execute(
            """
            UPDATE reactivation_settings
            SET mode = 'active', active_version_id = $1,
                legal_status = 'approved', legal_reference = 'review',
                legal_approved_at = $2, legal_approved_by = $3
            WHERE id = 1
            """,
            version_id, NOW, owner_id,
        )
    repository = ReactivationRepository(
        database, business_alert_chat_id="90001", clock=lambda: NOW
    )
    try:
        yield database, repository, owner_id
    finally:
        await database.close()


async def _seed_and_reserve(database, repository, user_id):
    event_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version,
                 proof_text_hash, source, source_event_id, occurred_at)
            VALUES ($1, 'telegram', $2, 'granted', 'v1', 'proof',
                    'telegram_explicit', $3, $4)
            """,
            event_id, user_id, f"grant-{user_id}", NOW - timedelta(days=200),
        )
        await connection.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, active, consent_version, granted_at,
                 source, proof_event_id, proof_text_hash, updated_at)
            VALUES ($1, 'telegram', $2, true, 'v1', $3,
                    'telegram_explicit', $4, 'proof', $5)
            """,
            uuid4(), user_id, NOW - timedelta(days=200), event_id, NOW,
        )
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 identity_source, identity_verified_at,
                 last_completed_visit_at, history_synced_at,
                 recent_bookings_synced_at, source_version, sync_status,
                 updated_at)
            VALUES ('telegram', $1, $2, 'verified', 'test', $3, $4,
                    $3, $3, 'test-v1', 'current', $3)
            """,
            user_id, f"client-{user_id}", NOW, NOW - timedelta(days=200),
        )
    await repository.run_planner_cycle(NOW, planner_limit=100, step_claim_limit=100)
    async with database.acquire() as connection:
        return await connection.fetchval(
            """
            SELECT step.outbound_id
            FROM reactivation_journey_steps AS step
            JOIN reactivation_journeys AS journey ON journey.id = step.journey_id
            WHERE journey.user_id = $1 AND step.step_kind = 'main'
            """,
            user_id,
        )


def _sender(database, repository, telegram):
    return TelegramSender(
        telegram,
        MessageRepository(database),
        pre_send_guard=repository.pre_send_guard,
        delivery_hook=repository.delivery_hook,
        clock=lambda: NOW,
    )


async def test_stop_waits_for_inflight_send_and_blocks_next_send(delivery):
    database, repository, owner_id = delivery
    outbound_1 = await _seed_and_reserve(database, repository, "70001")
    outbound_2 = await _seed_and_reserve(database, repository, "70002")
    telegram = FakeTelegram(blocked=True)
    sender = _sender(database, repository, telegram)

    send = asyncio.create_task(sender.send(outbound_1))
    await asyncio.wait_for(telegram.started.wait(), timeout=3)
    stop = asyncio.create_task(repository.emergency_stop(owner_id, NOW))
    await asyncio.sleep(0.1)
    assert not stop.done()
    telegram.release.set()
    assert await asyncio.wait_for(send, timeout=3) == DeliveryResult.SENT
    await asyncio.wait_for(stop, timeout=3)

    assert await sender.send(outbound_2) == DeliveryResult.SKIPPED
    assert len(telegram.calls) == 1
    async with database.acquire() as connection:
        revision = await connection.fetchval(
            "SELECT program_revision FROM reactivation_settings WHERE id = 1"
        )
        audit_count = await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events "
            "WHERE action = 'reactivation.emergency_stopped'"
        )
    await repository.emergency_stop(owner_id, NOW + timedelta(seconds=1))
    async with database.acquire() as connection:
        state = await connection.fetchrow(
            "SELECT mode, stopped_at, program_revision "
            "FROM reactivation_settings WHERE id = 1"
        )
        second = await connection.fetchrow(
            """
            SELECT outbound.status, step.status, step.terminal_reason
            FROM outbound_messages AS outbound
            JOIN reactivation_journey_steps AS step ON step.outbound_id = outbound.id
            WHERE outbound.id = $1
            """,
            outbound_2,
        )
        repeated_audit_count = await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events "
            "WHERE action = 'reactivation.emergency_stopped'"
        )
    assert tuple(state.values()) == ("paused", NOW, revision)
    assert repeated_audit_count == audit_count
    assert tuple(second.values()) == ("cancelled", "cancelled", "program_paused")


async def test_program_transition_first_blocks_send_then_guard_cancels(delivery):
    database, repository, _ = delivery
    outbound_id = await _seed_and_reserve(database, repository, "70003")
    telegram = FakeTelegram()
    sender = _sender(database, repository, telegram)
    async with database.acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                PROGRAM_LOCK_SUBJECT,
            )
            send = asyncio.create_task(sender.send(outbound_id))
            await asyncio.sleep(0.1)
            assert not send.done()
            await connection.execute(
                "UPDATE reactivation_settings SET mode = 'paused', "
                "stopped_at = $1 WHERE id = 1",
                NOW,
            )
            await transaction.commit()
        finally:
            if not send.done() and connection.is_in_transaction():
                await transaction.rollback()
    assert await asyncio.wait_for(send, timeout=3) == DeliveryResult.SKIPPED
    assert telegram.calls == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("revoke", "consent_revoked"),
        ("suppress", "consent_revoked"),
        ("inbound", "recent_activity"),
        ("future_booking", "future_booking"),
        ("stale_activity", "stale_history"),
        ("delete", "consent_revoked"),
        ("human", "human_mode"),
        ("escalation", "open_escalation"),
        ("legal", "legal_unavailable"),
        ("version", "version_changed"),
    ],
)
async def test_pre_send_guard_rechecks_all_terminal_controls(
    delivery, mutation, reason
):
    database, repository, _ = delivery
    user_id = {
        "revoke": "81001",
        "suppress": "81002",
        "inbound": "81003",
        "future_booking": "81004",
        "stale_activity": "81005",
        "delete": "81006",
        "human": "81007",
        "escalation": "81008",
        "legal": "81009",
        "version": "81010",
    }[mutation]
    outbound_id = await _seed_and_reserve(database, repository, user_id)
    async with database.acquire() as connection:
        if mutation == "revoke":
            await connection.execute(
                "UPDATE marketing_consents SET active = false WHERE user_id = $1",
                user_id,
            )
        elif mutation == "suppress":
            await connection.execute(
                "UPDATE marketing_consents SET active = false, suppressed_at = $2 "
                "WHERE user_id = $1",
                user_id, NOW,
            )
            await connection.execute(
                """
                UPDATE reactivation_journey_steps AS step
                SET status = 'cancelled', terminal_reason = 'suppressed'
                FROM reactivation_journeys AS journey
                WHERE journey.id = step.journey_id AND journey.user_id = $1
                """,
                user_id,
            )
        elif mutation == "inbound":
            await connection.execute(
                "UPDATE customer_activity_projection "
                "SET last_meaningful_inbound_at = $2 WHERE user_id = $1",
                user_id, NOW,
            )
        elif mutation == "future_booking":
            await connection.execute(
                "UPDATE customer_activity_projection "
                "SET next_active_booking_at = $2 WHERE user_id = $1",
                user_id, NOW + timedelta(days=1),
            )
        elif mutation == "stale_activity":
            await connection.execute(
                "UPDATE customer_activity_projection "
                "SET history_synced_at = $2 WHERE user_id = $1",
                user_id, NOW - timedelta(days=2),
            )
        elif mutation == "delete":
            await connection.execute(
                "DELETE FROM marketing_consents WHERE user_id = $1", user_id
            )
        elif mutation == "human":
            await connection.execute(
                "INSERT INTO human_mode "
                "(customer_id, enabled, reason_code, enabled_at) "
                "VALUES ($1, true, 'admin_handoff', $2)", user_id, NOW
            )
        elif mutation == "legal":
            await connection.execute(
                "UPDATE reactivation_settings SET legal_status = 'pending' "
                "WHERE id = 1"
            )
        elif mutation == "version":
            await connection.execute(
                "UPDATE reactivation_program_versions SET status = 'retired' "
                "WHERE id = (SELECT active_version_id "
                "FROM reactivation_settings WHERE id = 1)"
            )
        else:
            await connection.execute(
                "INSERT INTO escalations "
                "(id, source, customer_id, status, reason_code, payload) "
                "VALUES ($1, 'test', $2, 'open', 'test', '{}'::jsonb)",
                uuid4(), user_id,
            )

    telegram = FakeTelegram()
    assert await _sender(database, repository, telegram).send(
        outbound_id
    ) == DeliveryResult.SKIPPED
    assert telegram.calls == []
    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT outbound.status, step.status, step.terminal_reason,
                   journey.status
            FROM outbound_messages AS outbound
            JOIN reactivation_journey_steps AS step ON step.outbound_id = outbound.id
            JOIN reactivation_journeys AS journey ON journey.id = step.journey_id
            WHERE outbound.id = $1
            """,
            outbound_id,
        )
    assert tuple(state.values()) == ("cancelled", "cancelled", reason, "closed")


@pytest.mark.parametrize(
    ("error", "result", "outbound", "step", "mode", "suppressed"),
    [
        (_telegram_error(TelegramForbiddenError), DeliveryResult.FAILED,
         "failed", "failed", "active", True),
        (_telegram_error(TelegramNotFound), DeliveryResult.FAILED,
         "failed", "failed", "active", True),
        (_telegram_error(TelegramBadRequest), DeliveryResult.FAILED,
         "failed", "failed", "paused", False),
        (_telegram_error(TelegramNetworkError), DeliveryResult.DELIVERY_UNKNOWN,
         "delivery_unknown", "delivery_unknown", "paused", False),
        (TimeoutError("private timeout"), DeliveryResult.DELIVERY_UNKNOWN,
         "delivery_unknown", "delivery_unknown", "paused", False),
    ],
)
async def test_delivery_error_updates_linked_state_atomically(
    delivery, error, result, outbound, step, mode, suppressed, caplog
):
    database, repository, _ = delivery
    outbound_id = await _seed_and_reserve(database, repository, "82001")

    assert await _sender(
        database, repository, FakeTelegram(error)
    ).send(outbound_id) == result

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT outbound.status, step.status, journey.status,
                   settings.mode, consent.suppressed_at IS NOT NULL AS suppressed
            FROM outbound_messages AS outbound
            JOIN reactivation_journey_steps AS step ON step.outbound_id = outbound.id
            JOIN reactivation_journeys AS journey ON journey.id = step.journey_id
            JOIN reactivation_settings AS settings ON settings.id = 1
            JOIN marketing_consents AS consent
              ON consent.channel = journey.channel AND consent.user_id = journey.user_id
            WHERE outbound.id = $1
            """,
            outbound_id,
        )
    assert tuple(state.values()) == (outbound, step, "closed", mode, suppressed)
    assert "private error" not in caplog.text
    assert "private timeout" not in caplog.text
    if mode == "paused":
        async with database.acquire() as connection:
            payload = await connection.fetchval(
                "SELECT after FROM admin_audit_events "
                "WHERE action = 'reactivation.delivery_auto_paused'"
            )
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert set(payload) == {"mode", "code", "count"}
        assert payload["count"] == 1


async def test_retry_after_is_the_only_managed_provider_retry(delivery):
    database, repository, _ = delivery
    outbound_id = await _seed_and_reserve(database, repository, "82002")

    with pytest.raises(TelegramRetryAfter):
        await _sender(
            database,
            repository,
            FakeTelegram(_telegram_error(TelegramRetryAfter)),
        ).send(outbound_id)

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT outbound.status, step.status, settings.mode
            FROM outbound_messages AS outbound
            JOIN reactivation_journey_steps AS step ON step.outbound_id = outbound.id
            JOIN reactivation_settings AS settings ON settings.id = 1
            WHERE outbound.id = $1
            """,
            outbound_id,
        )
    assert tuple(state.values()) == ("pending", "reserved", "active")


async def test_cancelled_delivery_is_unknown_and_pauses_before_propagating(delivery):
    database, repository, _ = delivery
    outbound_id = await _seed_and_reserve(database, repository, "82004")

    with pytest.raises(asyncio.CancelledError):
        await _sender(
            database, repository, FakeTelegram(asyncio.CancelledError())
        ).send(outbound_id)

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT outbound.status, step.status, settings.mode
            FROM outbound_messages AS outbound
            JOIN reactivation_journey_steps AS step ON step.outbound_id = outbound.id
            JOIN reactivation_settings AS settings ON settings.id = 1
            WHERE outbound.id = $1
            """,
            outbound_id,
        )
    assert tuple(state.values()) == (
        "delivery_unknown", "delivery_unknown", "paused"
    )


@pytest.mark.parametrize("error", [None, _telegram_error(TelegramBadRequest)])
async def test_test_send_records_proof_only_after_success(delivery, error):
    database, repository, owner_id = delivery
    async with database.acquire() as connection:
        version_id = await connection.fetchval(
            "SELECT active_version_id FROM reactivation_settings WHERE id = 1"
        )
    outbound_id = await repository.queue_test_send(version_id, owner_id, NOW)

    telegram = FakeTelegram(error)
    sender = _sender(database, repository, telegram)
    result = await sender.send(outbound_id)

    async with database.acquire() as connection:
        sent_at = await connection.fetchval(
            "SELECT test_sent_at FROM reactivation_program_versions WHERE id = $1",
            version_id,
        )
    assert result == (DeliveryResult.SENT if error is None else DeliveryResult.FAILED)
    assert (sent_at == NOW) is (error is None)
    assert await sender.send(outbound_id) == DeliveryResult.SKIPPED
    assert len(telegram.calls) == 1


async def test_test_send_guard_rejects_changed_version_text(delivery):
    database, repository, owner_id = delivery
    async with database.acquire() as connection:
        version_id = await connection.fetchval(
            "SELECT active_version_id FROM reactivation_settings WHERE id = 1"
        )
    outbound_id = await repository.queue_test_send(version_id, owner_id, NOW)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE reactivation_program_versions SET main_text = main_text || '!' "
            "WHERE id = $1",
            version_id,
        )

    telegram = FakeTelegram()
    assert await _sender(database, repository, telegram).send(
        outbound_id
    ) == DeliveryResult.SKIPPED
    assert telegram.calls == []
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT test_sent_at FROM reactivation_program_versions WHERE id = $1",
            version_id,
        ) is None


async def test_stale_claim_is_terminalized_and_reconciled_without_resend(delivery):
    database, repository, _ = delivery
    outbound_id = await _seed_and_reserve(database, repository, "82003")
    messages = MessageRepository(database)
    assert await messages.claim_outbound_delivery(outbound_id) is not None

    assert await messages.reconcile_stale_outbound_deliveries() == 1
    assert await repository.reconcile_delivery_unknowns(NOW) == 1
    assert await repository.reconcile_delivery_unknowns(NOW) == 0

    telegram = FakeTelegram()
    assert await _sender(database, repository, telegram).send(
        outbound_id
    ) == DeliveryResult.SKIPPED
    assert telegram.calls == []
    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT outbound.status, step.status, journey.close_reason,
                   settings.mode
            FROM outbound_messages AS outbound
            JOIN reactivation_journey_steps AS step ON step.outbound_id = outbound.id
            JOIN reactivation_journeys AS journey ON journey.id = step.journey_id
            JOIN reactivation_settings AS settings ON settings.id = 1
            WHERE outbound.id = $1
            """,
            outbound_id,
        )
    assert tuple(state.values()) == (
        "delivery_unknown", "delivery_unknown", "delivery_unknown", "paused"
    )
