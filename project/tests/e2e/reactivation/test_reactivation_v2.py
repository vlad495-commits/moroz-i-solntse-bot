from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError

from moroz.common.queue import QueueTask
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.reactivation.policy import (
    EligibilityInput,
    ProgramPolicy,
    evaluate_eligibility,
    template_checksum,
)
from moroz.reactivation.repository import ReactivationRepository
from tests.e2e.test_privacy_gate import (
    MARKETING_DISABLE_CALLBACK_DATA,
    MARKETING_ENABLE_CALLBACK_DATA,
    client,
    db,
    fake_telegram,
    grant_policy_consent,
    message_database,
    redis_client,
    telegram_consent_callback,
    telegram_text_update,
)
from worker.main import MessageTaskHandler


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime.fromtimestamp(1_768_478_400, UTC)
PRIVATE_SENTINEL = "+79990001122 private-reactivation-text"

E2E_CASES = (
    "consent_grant_and_revoke",
    "eligibility_89_90_91_days",
    "main_then_reminder",
    "reply_cancels_reminder",
    "booking_cancels_reminder",
    "stop_suppresses_before_llm",
    "future_booking_excluded",
    "human_mode_and_escalation_excluded",
    "customer_deletion_blocks_send",
    "duplicate_restart_and_stale_claim",
    "delivery_unknown_pauses_without_retry",
    "dry_run_has_no_outbound",
)


@dataclass(frozen=True, slots=True)
class CaseResult:
    passed: bool
    safe_failure_code: str | None = None


class ReactivationHarness:
    def __init__(self, client, db, database, telegram, redis_client, caplog):
        self.client = client
        self.db = db
        self.database = database
        self.telegram = telegram
        self.redis = redis_client
        self.caplog = caplog
        self.repository = ReactivationRepository(
            database, business_alert_chat_id="90001", clock=lambda: NOW
        )

    async def run(self, case: str) -> CaseResult:
        await getattr(self, case)()
        audits = await self.db.fetch("SELECT action, before, after FROM admin_audit_events")
        assert PRIVATE_SENTINEL not in self.caplog.text
        assert PRIVATE_SENTINEL not in repr(audits)
        return CaseResult(True)

    async def _activate(self, mode="active"):
        policy = ProgramPolicy()
        version_id = uuid4()
        owner_id = await self.db.fetchval(
            "INSERT INTO admin_users "
            "(username, role, password_hash, totp_secret, enabled) "
            "VALUES ($1, 'owner', 'x', 'x', true) RETURNING id",
            f"e2e-owner-{uuid4()}",
        )
        await self.db.execute(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days, reminder_enabled,
                 reminder_after_days, cooldown_days, main_text, reminder_text,
                 template_checksum)
            VALUES ($1, 1, 'active', 90, true, 5, 90, $2, $3, $4)
            """,
            version_id,
            policy.main_text,
            policy.reminder_text,
            template_checksum(policy),
        )
        await self.db.execute(
            """
            UPDATE reactivation_settings
            SET mode=$1, active_version_id=$2, legal_status='approved',
                legal_reference='e2e-approved', legal_approved_at=$3,
                legal_approved_by=$4
            WHERE id=1
            """,
            mode,
            version_id,
            NOW,
            owner_id,
        )
        return version_id

    async def _eligible(self, user_id: str, *, inactive_days=200, future=False):
        event_id = uuid4()
        await self.db.execute(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version, proof_text_hash,
                 source, source_event_id, occurred_at)
            VALUES ($1, 'telegram', $2, 'granted', 'marketing-v1', 'proof',
                    'telegram_explicit', $3, $4)
            """,
            event_id,
            user_id,
            f"grant-{user_id}",
            NOW - timedelta(days=inactive_days + 1),
        )
        await self.db.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, active, consent_version, granted_at,
                 source, proof_event_id, proof_text_hash, updated_at)
            VALUES ($1, 'telegram', $2, true, 'marketing-v1', $3,
                    'telegram_explicit', $4, 'proof', $5)
            """,
            uuid4(), user_id, NOW - timedelta(days=inactive_days + 1), event_id, NOW,
        )
        await self.db.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 identity_source, identity_verified_at, last_completed_visit_at,
                 next_active_booking_at, history_synced_at,
                 recent_bookings_synced_at, source_version, sync_status, updated_at)
            VALUES ('telegram', $1, $2, 'verified', 'e2e', $3, $4, $5,
                    $3, $3, 'e2e-v1', 'current', $3)
            """,
            user_id,
            f"client-{user_id}",
            NOW,
            NOW - timedelta(days=inactive_days),
            NOW + timedelta(days=1) if future else None,
        )

    async def _plan(self, user_id: str):
        assert await self.repository.run_planner_cycle(NOW) == 1
        return await self.db.fetchval(
            "SELECT step.outbound_id FROM reactivation_journey_steps step "
            "JOIN reactivation_journeys journey ON journey.id=step.journey_id "
            "WHERE journey.user_id=$1 AND step.step_kind='main'",
            user_id,
        )

    def _worker(self, *, now=NOW):
        sender = TelegramSender(
            self.telegram,
            MessageRepository(self.database),
            pre_send_guard=self.repository.pre_send_guard,
            delivery_hook=self.repository.delivery_hook,
            managed_delivery_check=self.repository.is_linked_outbound,
            clock=lambda: now,
        )
        return MessageTaskHandler(self.database, object(), sender)

    @staticmethod
    def _task(outbound_id, **kwargs):
        return QueueTask(
            kind="send_outbound",
            payload={"outbound_id": str(outbound_id)},
            idempotency_key=f"send_outbound:{outbound_id}",
            **kwargs,
        )

    async def consent_grant_and_revoke(self):
        await grant_policy_consent(self.client)
        for update_id, data, callback_id in (
            (1200, MARKETING_ENABLE_CALLBACK_DATA, "grant"),
            (1201, MARKETING_DISABLE_CALLBACK_DATA, "revoke"),
        ):
            response = await self.client.post(
                "/telegram/webhook",
                json=telegram_consent_callback(
                    update_id=update_id, data=data, callback_id=callback_id
                ),
            )
            assert response.status_code == 200
        assert self.telegram.answered_callback_ids[-2:] == ["grant", "revoke"]
        assert await self.db.fetchval(
            "SELECT active FROM marketing_consents WHERE user_id='7'"
        ) is False

    async def eligibility_89_90_91_days(self):
        for days, expected in ((89, False), (90, True), (91, True)):
            value = EligibilityInput(
                identity_status="verified", consent_active=True,
                consent_proven=True, suppressed=False,
                last_completed_visit_at=NOW - timedelta(days=days),
                last_meaningful_inbound_at=None, next_active_booking_at=None,
                history_synced_at=NOW, recent_bookings_synced_at=NOW,
                sync_status="current", has_active_journey=False,
                latest_journey_started_at=None, human_mode=False,
                has_open_escalation=False, deletion_active=False,
            )
            assert evaluate_eligibility(value, ProgramPolicy(), NOW).eligible is expected

    async def main_then_reminder(self):
        await self._activate()
        await self._eligible("7001")
        main = await self._plan("7001")
        await self._worker().handle(self._task(main))
        await self.db.execute(
            "UPDATE customer_activity_projection SET history_synced_at=$1, "
            "recent_bookings_synced_at=$1 WHERE user_id='7001'",
            NOW + timedelta(days=6),
        )
        assert await self.repository.run_planner_cycle(
            NOW + timedelta(days=6), planner_limit=0
        ) == 1
        reminder = await self.db.fetchval(
            "SELECT outbound_id FROM reactivation_journey_steps "
            "WHERE step_kind='reminder'"
        )
        await self._worker(now=NOW + timedelta(days=6)).handle(self._task(reminder))
        assert len(self.telegram.sent_messages) == 2

    async def reply_cancels_reminder(self):
        await grant_policy_consent(self.client)
        await self._activate()
        await self._eligible("7")
        main = await self._plan("7")
        await self._worker().handle(self._task(main))
        response = await self.client.post(
            "/telegram/webhook",
            json=telegram_text_update(text=PRIVATE_SENTINEL, update_id=1210),
        )
        assert response.status_code == 200
        assert await self.db.fetchval(
            "SELECT status FROM reactivation_journey_steps WHERE step_kind='reminder'"
        ) == "cancelled"

    async def booking_cancels_reminder(self):
        await self._activate()
        await self._eligible("7002")
        main = await self._plan("7002")
        await self._worker().handle(self._task(main))
        await self.db.execute(
            """
            INSERT INTO yclients_booking_projection
                (external_id, bot_marker_state, starts_at, status, deleted,
                 service_names, synced_at, client_id, record_created_at)
            VALUES ('e2e-booking', 'absent', $1, 'confirmed', false,
                    '{}'::text[], $2, 'client-7002', $2)
            """,
            NOW + timedelta(days=2), NOW + timedelta(minutes=1),
        )
        await self.repository.refresh_outcomes(NOW + timedelta(minutes=2))
        assert await self.db.fetchval(
            "SELECT close_reason FROM reactivation_journeys WHERE user_id='7002'"
        ) == "booked"

    async def stop_suppresses_before_llm(self):
        await grant_policy_consent(self.client)
        await self._activate()
        await self._eligible("7")
        await self._plan("7")
        response = await self.client.post(
            "/telegram/webhook",
            json=telegram_text_update(text="стоп", update_id=1220),
        )
        assert response.status_code == 200
        assert await self.db.fetchval(
            "SELECT suppression_reason FROM marketing_consents WHERE user_id='7'"
        ) == "user_stop"
        assert await self.db.fetchval("SELECT count(*) FROM message_inbox") == 0
        assert await self.db.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind='process_message'"
        ) == 0

    async def future_booking_excluded(self):
        await self._activate()
        await self._eligible("7003", future=True)
        assert await self.repository.run_planner_cycle(NOW) == 0
        assert await self.db.fetchval("SELECT count(*) FROM outbound_messages") == 0

    async def human_mode_and_escalation_excluded(self):
        await self._activate()
        for user_id in ("7004", "7005"):
            await self._eligible(user_id)
        await self.db.execute(
            "INSERT INTO human_mode (customer_id, enabled, reason_code, enabled_at) "
            "VALUES ('7004', true, 'admin_handoff', $1)", NOW,
        )
        await self.db.execute(
            "INSERT INTO escalations "
            "(id, source, customer_id, status, reason_code, payload) "
            "VALUES ($1, 'e2e', '7005', 'open', 'e2e', '{}'::jsonb)", uuid4(),
        )
        assert await self.repository.run_planner_cycle(NOW) == 0
        assert await self.db.fetchval("SELECT count(*) FROM outbound_messages") == 0

    async def customer_deletion_blocks_send(self):
        await self._activate()
        await self._eligible("7006")
        outbound = await self._plan("7006")
        await self.db.execute("DELETE FROM marketing_consents WHERE user_id='7006'")
        await self.db.execute(
            "DELETE FROM customer_activity_projection WHERE user_id='7006'"
        )
        await self._worker().handle(self._task(outbound))
        assert self.telegram.sent_messages == []
        assert await self.db.fetchval(
            "SELECT status FROM outbound_messages WHERE id=$1", outbound
        ) == "cancelled"

    async def duplicate_restart_and_stale_claim(self):
        await self._activate()
        await self._eligible("7007")
        outbound = await self._plan("7007")
        task = self._task(outbound)
        worker = self._worker()
        await worker.handle(task)
        await worker.handle(task)
        assert len(self.telegram.sent_messages) == 1
        assert await self.db.fetchval(
            "SELECT status FROM outbound_messages WHERE id=$1", outbound
        ) == "sent"
        await self._eligible("7010")
        stale = await self._plan("7010")
        assert await MessageRepository(self.database).claim_outbound_delivery(stale)
        await worker.handle(self._task(stale, recovery_candidate=True))
        assert len(self.telegram.sent_messages) == 1
        assert await self.db.fetchval(
            "SELECT status FROM outbound_messages WHERE id=$1", stale
        ) == "delivery_unknown"

    async def delivery_unknown_pauses_without_retry(self):
        await self._activate()
        await self._eligible("7008")
        outbound = await self._plan("7008")
        self.telegram.send_error = TelegramNetworkError(
            SimpleNamespace(), "private network failure"
        )
        await self._worker().handle(self._task(outbound))
        await self._worker().handle(self._task(outbound))
        assert len(self.telegram.sent_messages) == 1
        assert await self.db.fetchval(
            "SELECT status FROM outbound_messages WHERE id=$1", outbound
        ) == "delivery_unknown"
        assert await self.db.fetchval(
            "SELECT mode FROM reactivation_settings WHERE id=1"
        ) == "paused"
        assert await self.db.fetchval(
            "SELECT count(*) FROM admin_audit_events "
            "WHERE action='reactivation.delivery_auto_paused'"
        ) == 1

    async def dry_run_has_no_outbound(self):
        await self._activate(mode="dry_run")
        await self._eligible("7009")
        assert await self.repository.run_planner_cycle(NOW) == 0
        assert await self.db.fetchval("SELECT count(*) FROM outbound_messages") == 0
        assert self.telegram.sent_messages == []


@pytest_asyncio.fixture
async def reactivation_harness(
    client, db, message_database, fake_telegram, redis_client, caplog
):
    yield ReactivationHarness(
        client, db, message_database, fake_telegram, redis_client, caplog
    )


@pytest.mark.parametrize("case", E2E_CASES)
async def test_reactivation_v2_case(case, reactivation_harness):
    result = await reactivation_harness.run(case)
    assert result.passed, result.safe_failure_code
