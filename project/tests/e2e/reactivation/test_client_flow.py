from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from config import BOT_PAUSE_KEY, MARKETING_DISABLED_REPLY
from moroz.reactivation.policy import ProgramPolicy, template_checksum
from tests.e2e.test_privacy_gate import (
    MARKETING_DISABLE_CALLBACK_DATA,
    client,
    db,
    fake_telegram,
    grant_policy_consent,
    redis_client,
    telegram_consent_callback,
    telegram_photo_update,
    telegram_text_update,
)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

REACTIVATION_BOOK_CALLBACK_DATA = "reactivation:book"
REACTIVATION_ASK_CALLBACK_DATA = "reactivation:ask"
BOOK_REPLY = (
    "Напишите, пожалуйста, какую процедуру хотите и на какой день — "
    "помогу подобрать время."
)
ASK_REPLY = "Напишите, пожалуйста, ваш вопрос — я помогу."
RECEIVED_AT = datetime.fromtimestamp(1_768_478_400, UTC)


async def _seed_sent_main_with_reminder(
    db, *, reminder_status="scheduled", first_sent_at=None
):
    policy = ProgramPolicy()
    version_id = uuid4()
    journey_id = uuid4()
    main_id = uuid4()
    reminder_id = uuid4()
    outbound_id = uuid4() if reminder_status == "reserved" else None
    first_sent_at = first_sent_at or RECEIVED_AT - timedelta(days=1)
    await db.execute(
        """
        INSERT INTO reactivation_program_versions
            (id, version_number, status, inactivity_days, reminder_enabled,
             reminder_after_days, cooldown_days, main_text, reminder_text,
             template_checksum)
        VALUES ($1, 1, 'draft', 90, true, 5, 90, $2, $3, $4)
        """,
        version_id,
        policy.main_text,
        policy.reminder_text,
        template_checksum(policy),
    )
    await db.execute(
        """
        INSERT INTO reactivation_journeys
            (id, channel, user_id, program_version_id, status,
             activity_anchor_at, first_sent_at)
        VALUES ($1, 'telegram', '7', $2, 'active', $3, $4)
        """,
        journey_id,
        version_id,
        first_sent_at - timedelta(days=90),
        first_sent_at,
    )
    await db.execute(
        """
        INSERT INTO reactivation_journey_steps
            (id, journey_id, step_kind, status, due_at, sent_at,
             idempotency_key)
        VALUES ($1, $2, 'main', 'sent', $3, $3, $4)
        """,
        main_id,
        journey_id,
        first_sent_at,
        f"reactivation:{journey_id}:main",
    )
    if outbound_id is not None:
        await db.execute(
            """
            INSERT INTO outbound_messages
                (id, channel, chat_id, text, idempotency_key, status)
            VALUES ($1, 'telegram', '7', 'reminder', $2, 'pending')
            """,
            outbound_id,
            f"reactivation:{journey_id}:reminder",
        )
    await db.execute(
        """
        INSERT INTO reactivation_journey_steps
            (id, journey_id, step_kind, status, due_at, idempotency_key,
             outbound_id)
        VALUES ($1, $2, 'reminder', $3, $4, $5, $6)
        """,
        reminder_id,
        journey_id,
        reminder_status,
        first_sent_at + timedelta(days=5),
        f"reactivation:{journey_id}:reminder",
        outbound_id,
    )
    return journey_id


async def _journey_state(db, journey_id):
    journey = await db.fetchrow(
        "SELECT status, close_reason, replied_at FROM reactivation_journeys "
        "WHERE id = $1",
        journey_id,
    )
    reminder = await db.fetchrow(
        "SELECT status, terminal_reason FROM reactivation_journey_steps "
        "WHERE journey_id = $1 AND step_kind = 'reminder'",
        journey_id,
    )
    return journey, reminder


@pytest.mark.parametrize("reminder_status", ["scheduled", "reserved"])
async def test_real_text_closes_journey_and_cancels_unsent_reminder(
    client, db, reminder_status
):
    await grant_policy_consent(client)
    journey_id = await _seed_sent_main_with_reminder(
        db, reminder_status=reminder_status
    )

    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(text="Подскажите, пожалуйста"),
    )

    journey, reminder = await _journey_state(db, journey_id)
    assert response.status_code == 200
    assert tuple(journey.values()) == ("closed", "responded", RECEIVED_AT)
    assert tuple(reminder.values()) == ("cancelled", "responded")
    if reminder_status == "reserved":
        assert await db.fetchval(
            "SELECT status FROM outbound_messages WHERE idempotency_key = $1",
            f"reactivation:{journey_id}:reminder",
        ) == "cancelled"
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 1
    assert await db.fetchval(
        "SELECT last_meaningful_inbound_at FROM customer_activity_projection "
        "WHERE channel = 'telegram' AND user_id = '7'"
    ) == RECEIVED_AT


@pytest.mark.parametrize(
    ("callback_data", "reply"),
    [
        (REACTIVATION_BOOK_CALLBACK_DATA, BOOK_REPLY),
        (REACTIVATION_ASK_CALLBACK_DATA, ASK_REPLY),
    ],
)
async def test_client_button_closes_journey_without_injecting_llm_message(
    client, db, fake_telegram, callback_data, reply
):
    message_date = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    first_sent_at = message_date + timedelta(microseconds=500_000)
    journey_id = await _seed_sent_main_with_reminder(
        db, first_sent_at=first_sent_at
    )
    update = telegram_consent_callback(
        update_id=920,
        callback_id="reactivation-callback",
        data=callback_data,
        message_date=int(message_date.timestamp()),
    )

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    journey, reminder = await _journey_state(db, journey_id)
    assert first.status_code == duplicate.status_code == 200
    assert tuple(journey.values())[:2] == ("closed", "responded")
    assert journey["replied_at"] > first_sent_at
    assert tuple(reminder.values()) == ("cancelled", "responded")
    assert fake_telegram.last_text == reply
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 0
    assert await db.fetchval(
        "SELECT count(*) FROM outbound_messages WHERE idempotency_key = $1",
        f"telegram:reactivation_{callback_data.rsplit(':', 1)[1]}:920",
    ) == 1


@pytest.mark.parametrize("paused", [False, True])
async def test_non_text_inbound_closes_journey_even_when_paused(
    client, db, redis_client, fake_telegram, paused
):
    journey_id = await _seed_sent_main_with_reminder(db)
    if paused:
        await redis_client.set(BOT_PAUSE_KEY, "1")

    response = await client.post(
        "/telegram/webhook", json=telegram_photo_update(update_id=921)
    )

    journey, reminder = await _journey_state(db, journey_id)
    assert response.status_code == 200
    assert tuple(journey.values()) == ("closed", "responded", RECEIVED_AT)
    assert tuple(reminder.values()) == ("cancelled", "responded")
    assert fake_telegram.sent_messages


async def test_disable_button_closes_journey_and_reuses_suppression(
    client, db, fake_telegram
):
    journey_id = await _seed_sent_main_with_reminder(db)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(
            update_id=922,
            data=MARKETING_DISABLE_CALLBACK_DATA,
        ),
    )

    journey, reminder = await _journey_state(db, journey_id)
    consent = await db.fetchrow(
        "SELECT active, suppression_reason FROM marketing_consents "
        "WHERE channel = 'telegram' AND user_id = '7'"
    )
    assert response.status_code == 200
    assert tuple(journey.values()) == ("closed", "suppressed", None)
    # Revoke and suppress share one transaction; the earlier terminal step
    # reason stays consent_revoked while the journey has the stronger outcome.
    assert tuple(reminder.values()) == ("cancelled", "consent_revoked")
    assert tuple(consent.values()) == (False, "user_stop")
    assert fake_telegram.last_text == MARKETING_DISABLED_REPLY


async def test_callback_after_closed_journey_does_not_rewrite_attribution(
    client, db
):
    journey_id = await _seed_sent_main_with_reminder(db)
    original = RECEIVED_AT - timedelta(hours=2)
    await db.execute(
        "UPDATE reactivation_journeys SET status = 'closed', "
        "close_reason = 'responded', replied_at = $2, closed_at = $2 "
        "WHERE id = $1",
        journey_id,
        original,
    )

    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(
            update_id=923,
            data=REACTIVATION_ASK_CALLBACK_DATA,
        ),
    )

    journey = await db.fetchrow(
        "SELECT status, close_reason, replied_at FROM reactivation_journeys "
        "WHERE id = $1",
        journey_id,
    )
    assert response.status_code == 200
    assert tuple(journey.values()) == ("closed", "responded", original)
