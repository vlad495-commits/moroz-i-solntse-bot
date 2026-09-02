import asyncio
from hashlib import sha256
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as redis
from aiogram.types import InlineKeyboardMarkup
from httpx import ASGITransport, AsyncClient

from config import (
    BOT_PAUSE_KEY,
    BOT_PAUSED_REPLY,
    INPUT_TOO_LONG_REPLY,
    MARKETING_CONSENT_CLAUSE,
    MARKETING_DISABLED_REPLY,
    MARKETING_ENABLED_REPLY,
    MARKETING_ENABLE_LABEL,
    MARKETING_DISABLE_LABEL,
    MAX_INPUT_LENGTH,
    NON_TEXT_REPLY,
    START_REPLY,
)
from customer_data_deletion import delete_customer_data
from moroz.common.db import Database
from moroz.messaging.ingress import IngressDecision
from moroz.messaging.repository import MessageRepository
import webhook as webhook_module
from webhook import create_app


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

CONSENT_PII_CALLBACK_DATA = "consent:set:pii:on"
CONSENT_PII_CLEAR_CALLBACK_DATA = "consent:set:pii:off"
CONSENT_ADS_CALLBACK_DATA = "consent:set:ads:on"
CONSENT_ADS_CLEAR_CALLBACK_DATA = "consent:set:ads:off"
CONSENT_DONE_CALLBACK_DATA = "consent:done"
MARKETING_ENABLE_CALLBACK_DATA = "marketing:enable"
MARKETING_DISABLE_CALLBACK_DATA = "marketing:disable"
CONSENT_PROMPT = (
    "Чтобы начать, отметьте согласия и нажмите «Готово»\n\n"
    "1) Согласен с политикой конфиденциальности\n"
    "2) Хочу получать в этом боте сообщения об акциях, новостях и "
    "специальных предложениях (включая рекламные)\n\n"
    '<a href="https://example.com/privacy">Политика конфиденциальности</a>'
)
CONSENT_PII_LABEL = "Согласен с политикой"
CONSENT_ADS_LABEL = "Согласен на рассылку"
CONSENT_DONE_LABEL = "Готово"
CONSENT_NEED_PII_REPLY = "Без согласия с политикой продолжить не получится"
CONSENT_THANKS = "Спасибо! Теперь я могу ответить на ваш вопрос."
WEBHOOK_SECRET = "test-webhook-secret"


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeTelegram:
    def __init__(self):
        self.session = FakeSession()
        self.sent_messages = []
        self.edited_reply_markups = []
        self.chat_actions = []
        self.answered_callback_ids = []
        self.send_error = None

    @property
    def last_text(self):
        return self.sent_messages[-1]["text"]

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(message_id=700 + len(self.sent_messages))

    async def edit_message_reply_markup(self, **kwargs):
        self.edited_reply_markups.append(kwargs)
        return True

    async def send_chat_action(self, **kwargs):
        self.chat_actions.append(kwargs)
        return True

    async def answer_callback_query(self, callback_query_id, **_kwargs):
        self.answered_callback_ids.append(callback_query_id)
        return True


def telegram_text_update(
    text="Секретный текст",
    *,
    update_id=900,
    chat_id=42,
    chat_type="private",
    user_id=7,
):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100,
            "date": 1_768_478_400,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Тест",
            },
            "text": text,
        },
    }


def telegram_consent_callback(
    *,
    update_id=901,
    chat_id=42,
    chat_type="private",
    user_id=7,
    data=CONSENT_DONE_CALLBACK_DATA,
    callback_id="callback-1",
    message_date=1_768_478_400,
):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Тест",
            },
            "chat_instance": "test-chat",
            "data": data,
            "message": {
                "message_id": 99,
                "date": message_date,
                "chat": {"id": chat_id, "type": chat_type},
            },
        },
    }


def telegram_photo_update(*, update_id=903):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 101,
            "date": 1_768_478_400,
            "chat": {"id": 42, "type": "private"},
            "from": {
                "id": 7,
                "is_bot": False,
                "first_name": "Тест",
            },
            "photo": [
                {
                    "file_id": "photo-file",
                    "file_unique_id": "photo-unique",
                    "width": 100,
                    "height": 100,
                }
            ],
        },
    }


@pytest.fixture
def fake_telegram():
    return FakeTelegram()


@pytest_asyncio.fixture
async def db(migrated_database_url):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def message_database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def redis_client():
    client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def client(migrated_database_url, fake_telegram, redis_client):
    app = create_app(
        database_url=migrated_database_url,
        bot=fake_telegram,
        webhook_secret=WEBHOOK_SECRET,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        ) as http_client:
            yield http_client
    assert fake_telegram.session.closed is True


async def grant_policy_consent(client, *, user_id=7, chat_id=42, update_id=901):
    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=update_id,
                chat_id=chat_id,
                user_id=user_id,
                data=CONSENT_PII_CALLBACK_DATA,
            ),
        )
    ).status_code == 200
    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=update_id + 1,
                chat_id=chat_id,
                user_id=user_id,
            ),
        )
    ).status_code == 200


async def test_webhook_rejects_missing_or_wrong_secret_before_json_parsing(
    migrated_database_url, fake_telegram
):
    app = create_app(
        database_url=migrated_database_url,
        bot=fake_telegram,
        webhook_secret=WEBHOOK_SECRET,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            missing = await http_client.post(
                "/telegram/webhook",
                content=b"not-json",
            )
            wrong = await http_client.post(
                "/telegram/webhook",
                content=b"not-json",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            )

    assert missing.status_code == wrong.status_code == 403
    assert fake_telegram.sent_messages == []


async def test_message_without_consent_is_not_persisted(
    client, db, fake_telegram
):
    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(),
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval("SELECT count(*) FROM messages") == 0
    assert await db.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 0
    assert fake_telegram.last_text == CONSENT_PROMPT
    assert fake_telegram.sent_messages[-1]["parse_mode"] == "HTML"
    keyboard = fake_telegram.sent_messages[-1]["reply_markup"].inline_keyboard
    assert [(row[0].text, row[0].callback_data) for row in keyboard] == [
        (f"☐ {CONSENT_PII_LABEL}", CONSENT_PII_CALLBACK_DATA),
        (f"☐ {CONSENT_ADS_LABEL}", CONSENT_ADS_CALLBACK_DATA),
        (CONSENT_DONE_LABEL, CONSENT_DONE_CALLBACK_DATA),
    ]


async def test_deletion_marker_blocks_new_telegram_ingress(
    client, db, redis_client, fake_telegram
):
    await grant_policy_consent(client)
    inbox_before = await db.fetchval("SELECT count(*) FROM message_inbox")
    outbound_before = await db.fetchval("SELECT count(*) FROM outbound_messages")
    sent_before = len(fake_telegram.sent_messages)
    await redis_client.set("privacy:deleting:telegram:42", "1", ex=300)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(update_id=990, text="Новый секрет"),
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == inbox_before
    assert (
        await db.fetchval("SELECT count(*) FROM outbound_messages")
        == outbound_before
    )
    assert len(fake_telegram.sent_messages) == sent_before
    assert fake_telegram.chat_actions == []


async def test_deletion_marker_blocks_consent_callback_mutation(
    client, db, redis_client, fake_telegram
):
    await redis_client.set("privacy:deleting:telegram:42", "1", ex=300)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(
            update_id=991,
            data=CONSENT_PII_CALLBACK_DATA,
        ),
    )

    assert response.status_code == 200
    assert await redis_client.get("consent:state:telegram:42:7") is None
    assert await db.fetchval("SELECT count(*) FROM processing_consents") == 0
    assert await db.fetchval("SELECT count(*) FROM outbound_messages") == 0
    assert fake_telegram.edited_reply_markups == []


async def test_consent_callback_rechecks_marker_after_customer_lock(
    client, db, redis_client, fake_telegram
):
    await redis_client.set("consent:state:telegram:42:7", "pii", ex=3600)
    transaction = db.transaction()
    await transaction.start()
    await db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "42"
    )
    callback = asyncio.create_task(
        client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=993,
                data=CONSENT_DONE_CALLBACK_DATA,
            ),
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert callback.done() is False
        await redis_client.set("privacy:deleting:telegram:42", "1", ex=300)
        await transaction.rollback()

        assert (await asyncio.wait_for(callback, timeout=3)).status_code == 200
        assert await db.fetchval("SELECT count(*) FROM processing_consents") == 0
        assert await db.fetchval("SELECT count(*) FROM outbound_messages") == 0
        assert await redis_client.get("consent:state:telegram:42:7") == "pii"
        assert fake_telegram.sent_messages == []
    finally:
        if not callback.done():
            callback.cancel()
        if db.is_in_transaction():
            await transaction.rollback()


async def test_consent_checkbox_rechecks_marker_after_customer_lock(
    client, db, redis_client, fake_telegram
):
    transaction = db.transaction()
    await transaction.start()
    await db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "42"
    )
    callback = asyncio.create_task(
        client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=995,
                data=CONSENT_PII_CALLBACK_DATA,
            ),
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert callback.done() is False
        await redis_client.set("privacy:deleting:telegram:42", "owner", ex=300)
        await transaction.rollback()

        assert (await asyncio.wait_for(callback, timeout=3)).status_code == 200
        assert await redis_client.get("consent:state:telegram:42:7") is None
        assert fake_telegram.edited_reply_markups == []
    finally:
        if not callback.done():
            callback.cancel()
        if db.is_in_transaction():
            await transaction.rollback()


async def test_static_reply_rechecks_marker_after_customer_lock(
    client, db, redis_client, fake_telegram
):
    transaction = db.transaction()
    await transaction.start()
    await db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "42"
    )
    request = asyncio.create_task(
        client.post(
            "/telegram/webhook",
            json=telegram_text_update(update_id=994, text="/start"),
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert request.done() is False
        await redis_client.set("privacy:deleting:telegram:42", "owner", ex=300)
        await transaction.rollback()

        assert (await asyncio.wait_for(request, timeout=3)).status_code == 200
        assert await db.fetchval("SELECT count(*) FROM outbound_messages") == 0
        assert fake_telegram.sent_messages == []
    finally:
        if not request.done():
            request.cancel()
        if db.is_in_transaction():
            await transaction.rollback()


async def test_message_after_deletion_returns_to_consent_flow(
    client, db, message_database, redis_client, fake_telegram
):
    await grant_policy_consent(client)
    await db.execute(
        "INSERT INTO messages (chat_id, user_id, role, content) "
        "VALUES (42, 7, 'user', 'old secret')"
    )

    result = await delete_customer_data(
        pool=message_database,
        redis_client=redis_client,
        chat_id=42,
        actor_id=1,
        ip_address=None,
        user_agent=None,
    )
    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(update_id=992, text="Новое обращение"),
    )

    assert result.status == "deleted"
    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM messages WHERE chat_id = 42") == 0
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert fake_telegram.last_text == CONSENT_PROMPT


async def test_consent_done_without_policy_refuses_and_keeps_gate(
    client, db, fake_telegram
):
    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(),
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM processing_consents") == 0
    assert fake_telegram.last_text == CONSENT_NEED_PII_REPLY


async def test_consent_checkbox_toggles_markup_without_persisting(
    client, db, fake_telegram
):
    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(data=CONSENT_PII_CALLBACK_DATA),
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM processing_consents") == 0
    keyboard = fake_telegram.edited_reply_markups[-1]["reply_markup"].inline_keyboard
    assert [(row[0].text, row[0].callback_data) for row in keyboard] == [
        (f"☑ {CONSENT_PII_LABEL}", CONSENT_PII_CLEAR_CALLBACK_DATA),
        (f"☐ {CONSENT_ADS_LABEL}", CONSENT_ADS_CALLBACK_DATA),
        (CONSENT_DONE_LABEL, CONSENT_DONE_CALLBACK_DATA),
    ]


async def test_duplicate_consent_checkbox_callback_is_idempotent(
    client, db, redis_client, fake_telegram
):
    update = telegram_consent_callback(
        update_id=930,
        data=CONSENT_PII_CALLBACK_DATA,
    )

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    assert first.status_code == duplicate.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM processing_consents") == 0
    assert await redis_client.get("consent:state:telegram:42:7") == "pii"
    assert len(fake_telegram.edited_reply_markups) == 1


async def test_duplicate_consent_done_callback_is_idempotent(
    client, db, fake_telegram
):
    await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(
            update_id=931,
            data=CONSENT_PII_CALLBACK_DATA,
        ),
    )
    done = telegram_consent_callback(update_id=932)

    first = await client.post("/telegram/webhook", json=done)
    duplicate = await client.post("/telegram/webhook", json=done)

    assert first.status_code == duplicate.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM processing_consents") == 1
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        CONSENT_THANKS
    ]


async def test_checked_policy_done_persists_only_versioned_consent(
    client, db, fake_telegram
):
    await grant_policy_consent(client, update_id=901)

    consent = await db.fetchrow(
        """
        SELECT channel, user_id, consent_version, granted_at
        FROM processing_consents
        """
    )
    assert tuple(consent.values())[:3] == ("telegram", "7", "v1")
    assert isinstance(consent["granted_at"], datetime)
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert fake_telegram.last_text == CONSENT_THANKS


async def test_ads_checkbox_grants_proven_marketing_consent(
    client, db, fake_telegram
):
    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_text_update("Покажите условия", update_id=99),
        )
    ).status_code == 200
    opt_in_screen = fake_telegram.last_text
    visible_clause = next(
        line.removeprefix("2) ")
        for line in opt_in_screen.splitlines()
        if MARKETING_CONSENT_CLAUSE in line
    )
    assert visible_clause == MARKETING_CONSENT_CLAUSE
    assert 'href="https://example.com/privacy"' in opt_in_screen

    for update_id, data, callback_id in (
        (100, CONSENT_PII_CALLBACK_DATA, "callback-pii"),
        (101, CONSENT_ADS_CALLBACK_DATA, "callback-ads"),
        (102, CONSENT_DONE_CALLBACK_DATA, "callback-done"),
    ):
        response = await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=update_id,
                data=data,
                callback_id=callback_id,
            ),
        )
        assert response.status_code == 200

    state = await db.fetchrow(
        """
        SELECT consent.active, consent.consent_version,
               consent.proof_text_hash, event.source_event_id
        FROM marketing_consents AS consent
        JOIN marketing_consent_events AS event
          ON event.id = consent.proof_event_id
        WHERE consent.channel = 'telegram' AND consent.user_id = '7'
        """
    )
    assert tuple(state.values()) == (
        True,
        "marketing-v1",
        sha256(visible_clause.encode()).hexdigest(),
        "102",
    )
    assert fake_telegram.answered_callback_ids == [
        "callback-pii",
        "callback-ads",
        "callback-done",
    ]


async def test_unchecked_ads_does_not_grant_marketing_consent(
    client, db, fake_telegram
):
    await grant_policy_consent(client, update_id=110)

    assert await db.fetchval("SELECT count(*) FROM marketing_consents") == 0
    assert await db.fetchval("SELECT count(*) FROM marketing_consent_events") == 0
    assert fake_telegram.answered_callback_ids == ["callback-1", "callback-1"]


async def test_old_consent_card_can_grant_marketing_later(
    client, db, redis_client, fake_telegram
):
    await grant_policy_consent(client, update_id=140)
    assert await db.fetchval("SELECT count(*) FROM marketing_consents") == 0

    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=142,
                data=CONSENT_ADS_CALLBACK_DATA,
                callback_id="callback-late-ads",
            ),
        )
    ).status_code == 200
    keyboard = fake_telegram.edited_reply_markups[-1][
        "reply_markup"
    ].inline_keyboard
    assert [(row[0].text, row[0].callback_data) for row in keyboard] == [
        (f"☑ {CONSENT_PII_LABEL}", CONSENT_PII_CLEAR_CALLBACK_DATA),
        (f"☑ {CONSENT_ADS_LABEL}", CONSENT_ADS_CLEAR_CALLBACK_DATA),
        (CONSENT_DONE_LABEL, CONSENT_DONE_CALLBACK_DATA),
    ]

    done = telegram_consent_callback(
        update_id=143,
        callback_id="callback-late-done",
    )
    assert (await client.post("/telegram/webhook", json=done)).status_code == 200
    assert (await client.post("/telegram/webhook", json=done)).status_code == 200

    state = await db.fetchrow(
        """
        SELECT consent.active, consent.proof_text_hash,
               event.source_event_id
        FROM marketing_consents AS consent
        JOIN marketing_consent_events AS event
          ON event.id = consent.proof_event_id
        """
    )
    assert tuple(state.values()) == (
        True,
        sha256(MARKETING_CONSENT_CLAUSE.encode()).hexdigest(),
        "143",
    )
    assert await redis_client.get("consent:state:telegram:42:7") is None
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        CONSENT_THANKS,
        MARKETING_ENABLED_REPLY,
    ]
    assert await db.fetchval(
        "SELECT count(*) FROM marketing_consent_events"
    ) == 1


async def test_old_consent_card_can_revoke_marketing_later(
    client, db, redis_client, fake_telegram
):
    for update_id, data in (
        (150, CONSENT_PII_CALLBACK_DATA),
        (151, CONSENT_ADS_CALLBACK_DATA),
        (152, CONSENT_DONE_CALLBACK_DATA),
    ):
        assert (
            await client.post(
                "/telegram/webhook",
                json=telegram_consent_callback(update_id=update_id, data=data),
            )
        ).status_code == 200

    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=153,
                data=CONSENT_ADS_CLEAR_CALLBACK_DATA,
            ),
        )
    ).status_code == 200
    keyboard = fake_telegram.edited_reply_markups[-1][
        "reply_markup"
    ].inline_keyboard
    assert [(row[0].text, row[0].callback_data) for row in keyboard] == [
        (f"☑ {CONSENT_PII_LABEL}", CONSENT_PII_CLEAR_CALLBACK_DATA),
        (f"☐ {CONSENT_ADS_LABEL}", CONSENT_ADS_CALLBACK_DATA),
        (CONSENT_DONE_LABEL, CONSENT_DONE_CALLBACK_DATA),
    ]

    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(update_id=154),
        )
    ).status_code == 200
    consent = await db.fetchrow(
        "SELECT active, suppression_reason FROM marketing_consents"
    )
    assert tuple(consent.values()) == (False, "user_stop")
    assert await redis_client.get("consent:state:telegram:42:7") is None
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        CONSENT_THANKS,
        MARKETING_DISABLED_REPLY,
    ]


async def test_marketing_command_and_callbacks_are_explicit_and_idempotent(
    client, db, fake_telegram
):
    command = await client.post(
        "/telegram/webhook",
        json=telegram_text_update("/marketing", update_id=120),
    )
    assert command.status_code == 200
    marketing_screen = fake_telegram.last_text
    visible_clause = next(
        line
        for line in marketing_screen.splitlines()
        if "сообщения об акциях" in line
    )
    assert visible_clause == MARKETING_CONSENT_CLAUSE
    assert marketing_screen.index(visible_clause) < (
        marketing_screen.index(MARKETING_DISABLED_REPLY)
    )
    keyboard = fake_telegram.sent_messages[-1]["reply_markup"].inline_keyboard
    assert [(button.text, button.callback_data) for button in keyboard[0]] == [
        (MARKETING_ENABLE_LABEL, MARKETING_ENABLE_CALLBACK_DATA),
        (MARKETING_DISABLE_LABEL, MARKETING_DISABLE_CALLBACK_DATA),
    ]

    enable = telegram_consent_callback(
        update_id=121,
        data=MARKETING_ENABLE_CALLBACK_DATA,
        callback_id="callback-enable",
    )
    received_before = datetime.now(UTC)
    assert (await client.post("/telegram/webhook", json=enable)).status_code == 200
    assert (await client.post("/telegram/webhook", json=enable)).status_code == 200
    received_after = datetime.now(UTC)
    assert await db.fetchval(
        "SELECT count(*) FROM marketing_consent_events WHERE action = 'granted'"
    ) == 1
    proof = await db.fetchrow(
        """
        SELECT event.proof_text_hash AS event_hash,
               consent.proof_text_hash AS materialized_hash,
               event.occurred_at
        FROM marketing_consent_events AS event
        JOIN marketing_consents AS consent ON consent.proof_event_id = event.id
        WHERE event.action = 'granted'
        """
    )
    visible_hash = sha256(visible_clause.encode()).hexdigest()
    assert (proof["event_hash"], proof["materialized_hash"]) == (
        visible_hash,
        visible_hash,
    )
    assert received_before <= proof["occurred_at"] <= received_after

    disable = telegram_consent_callback(
        update_id=122,
        data=MARKETING_DISABLE_CALLBACK_DATA,
        callback_id="callback-disable",
    )
    assert (await client.post("/telegram/webhook", json=disable)).status_code == 200
    state = await db.fetchrow(
        "SELECT active, suppression_reason FROM marketing_consents"
    )
    assert tuple(state.values()) == (False, "user_stop")

    reenable = telegram_consent_callback(
        update_id=123,
        data=MARKETING_ENABLE_CALLBACK_DATA,
        callback_id="callback-reenable",
    )
    assert (await client.post("/telegram/webhook", json=reenable)).status_code == 200
    assert [
        row["action"]
        for row in await db.fetch(
            "SELECT action FROM marketing_consent_events "
            "WHERE source_event_id = '123' ORDER BY created_at"
        )
    ] == ["unsuppressed", "granted"]
    state = await db.fetchrow(
        "SELECT active, suppression_reason FROM marketing_consents"
    )
    assert tuple(state.values()) == (True, None)
    assert fake_telegram.answered_callback_ids == [
        "callback-enable",
        "callback-enable",
        "callback-disable",
        "callback-reenable",
    ]


async def test_inaccessible_callback_message_uses_server_receipt_time(
    client, db, fake_telegram
):
    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_text_update("/marketing", update_id=124),
        )
    ).status_code == 200
    callback = telegram_consent_callback(
        update_id=125,
        data=MARKETING_ENABLE_CALLBACK_DATA,
        callback_id="callback-inaccessible",
        message_date=0,
    )

    received_before = datetime.now(UTC)
    assert (
        await client.post("/telegram/webhook", json=callback)
    ).status_code == 200
    received_after = datetime.now(UTC)
    occurred_at = await db.fetchval(
        "SELECT occurred_at FROM marketing_consent_events "
        "WHERE action = 'granted'"
    )
    assert received_before <= occurred_at <= received_after
    assert "callback-inaccessible" in fake_telegram.answered_callback_ids


async def test_stop_revokes_and_suppresses_before_pause_or_llm(
    client, db, redis_client, fake_telegram
):
    for update_id, data in (
        (130, CONSENT_PII_CALLBACK_DATA),
        (131, CONSENT_ADS_CALLBACK_DATA),
        (132, CONSENT_DONE_CALLBACK_DATA),
    ):
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(update_id=update_id, data=data),
        )
    await redis_client.set(BOT_PAUSE_KEY, "1")

    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update("Не писать", update_id=133),
    )

    assert response.status_code == 200
    state = await db.fetchrow(
        "SELECT active, suppression_reason FROM marketing_consents"
    )
    assert tuple(state.values()) == (False, "user_stop")
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 0
    assert fake_telegram.last_text == MARKETING_DISABLED_REPLY
    assert BOT_PAUSED_REPLY not in [
        message["text"] for message in fake_telegram.sent_messages
    ]


async def test_callback_is_answered_even_when_deletion_blocks_mutation(
    client, redis_client, fake_telegram
):
    await redis_client.set("privacy:deleting:telegram:42", "1", ex=300)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(
            update_id=140,
            data=MARKETING_ENABLE_CALLBACK_DATA,
            callback_id="callback-deleting",
        ),
    )

    assert response.status_code == 200
    assert fake_telegram.answered_callback_ids == ["callback-deleting"]


async def test_stale_consent_is_upgraded_by_done_callback(
    client, db, redis_client, fake_telegram
):
    await db.execute(
        "INSERT INTO processing_consents "
        "(channel, user_id, consent_version) "
        "VALUES ('telegram', '7', 'legacy-v0')"
    )
    await redis_client.set("consent:state:telegram:42:7", "pii", ex=3600)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(update_id=996),
    )

    assert response.status_code == 200
    assert await db.fetchval(
        "SELECT count(*) FROM processing_consents "
        "WHERE channel = 'telegram' AND user_id = '7' "
        "AND consent_version = 'v1'"
    ) == 1
    assert fake_telegram.last_text == CONSENT_THANKS


async def test_group_messages_and_callbacks_are_ignored_before_any_durable_work(
    client, db, redis_client, fake_telegram
):
    responses = []
    for offset, user_id in enumerate((7, 8)):
        responses.append(
            await client.post(
                "/telegram/webhook",
                json=telegram_consent_callback(
                    update_id=920 + offset * 2,
                    chat_id=-10042,
                    chat_type="group",
                    user_id=user_id,
                ),
            )
        )
        responses.append(
            await client.post(
                "/telegram/webhook",
                json=telegram_text_update(
                    f"Групповой текст {user_id}",
                    update_id=921 + offset * 2,
                    chat_id=-10042,
                    chat_type="group",
                    user_id=user_id,
                ),
            )
        )

    assert all(response.status_code == 200 for response in responses)
    for table in (
        "processing_consents",
        "message_inbox",
        "outbound_messages",
        "task_outbox",
    ):
        assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert await redis_client.dbsize() == 0
    assert fake_telegram.sent_messages == []


async def test_consented_update_is_persisted_once_by_update_id(
    client, db, redis_client, fake_telegram
):
    await grant_policy_consent(client)
    update = telegram_text_update("Можно сохранить", update_id=903)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    message = await db.fetchrow(
        """
        SELECT channel, external_message_id, chat_id, payload
        FROM message_inbox
        """
    )
    assert first.status_code == duplicate.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 1
    assert tuple(message.values())[:3] == ("telegram", "903", "42")
    assert json.loads(message["payload"])["text"] == "Можно сохранить"
    entries = await redis_client.lrange("buffer:42", 0, -1)
    assert [json.loads(entry)["update_id"] for entry in entries] == ["903"]
    assert fake_telegram.chat_actions == [{"chat_id": 42, "action": "typing"}]
    assert await db.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 0


async def test_redis_failure_after_consent_creates_single_message_task(
    migrated_database_url, db, fake_telegram
):
    await db.execute(
        """
        INSERT INTO processing_consents (channel, user_id, consent_version)
        VALUES ('telegram', '7', 'v1')
        """
    )
    app = create_app(
        database_url=migrated_database_url,
        bot=fake_telegram,
        redis_url="redis://127.0.0.1:1/0",
        webhook_secret=WEBHOOK_SECRET,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        ) as http_client:
            assert (
                await http_client.post(
                    "/telegram/webhook",
                    json=telegram_text_update("Не потерять", update_id=910),
                )
            ).status_code == 200

    task = await db.fetchrow(
        """
        SELECT kind, payload, idempotency_key, status
        FROM task_outbox
        WHERE kind = 'process_message'
        """
    )
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 1
    assert task["kind"] == "process_message"
    assert json.loads(task["payload"]) == {
        "update_ids": ["910"],
    }
    assert tuple(task.values())[2:] == ("process_message:910", "pending")


async def test_start_reply_is_durable_and_idempotent(client, db, fake_telegram):
    update = telegram_text_update("/start", update_id=911)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    assert first.status_code == duplicate.status_code == 200
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        START_REPLY
    ]
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval(
        "SELECT idempotency_key FROM outbound_messages"
    ) == "telegram:start:911"


async def test_paused_reply_is_durable_and_precedes_consent(
    client, db, redis_client, fake_telegram
):
    await redis_client.set(BOT_PAUSE_KEY, "1")
    update = telegram_text_update("Не сохранять", update_id=912)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    assert first.status_code == duplicate.status_code == 200
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        BOT_PAUSED_REPLY
    ]
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval(
        "SELECT idempotency_key FROM outbound_messages"
    ) == "telegram:paused:912"


async def test_overlength_reply_is_durable_after_consent_without_persisting_text(
    client, db, fake_telegram
):
    await grant_policy_consent(client, update_id=913)
    update = telegram_text_update("я" * (MAX_INPUT_LENGTH + 1), update_id=915)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    assert first.status_code == duplicate.status_code == 200
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        CONSENT_THANKS,
        INPUT_TOO_LONG_REPLY.format(limit=MAX_INPUT_LENGTH)
    ]
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    keys = await db.fetch("SELECT idempotency_key FROM outbound_messages")
    assert sorted(row["idempotency_key"] for row in keys) == [
        "telegram:consent_thanks:914",
        "telegram:too_long:915",
    ]


async def test_duplicate_no_consent_update_sends_one_durable_prompt(
    client, db, fake_telegram
):
    update = telegram_text_update(update_id=904)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    outbound = await db.fetchrow(
        """
        SELECT status, external_message_id, idempotency_key
        FROM outbound_messages
        """
    )
    assert first.status_code == duplicate.status_code == 200
    assert len(fake_telegram.sent_messages) == 1
    assert tuple(outbound.values()) == (
        "sent",
        "701",
        "telegram:consent_prompt:904",
    )
    assert await db.fetchval("SELECT count(*) FROM task_outbox") == 1
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0


async def test_unknown_prompt_result_is_not_retried(
    client, db, fake_telegram
):
    fake_telegram.send_error = TimeoutError("sensitive external failure")
    update = telegram_text_update(update_id=905)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    outbound = await db.fetchrow(
        """
        SELECT status, external_message_id, idempotency_key
        FROM outbound_messages
        """
    )
    assert first.status_code == duplicate.status_code == 200
    assert len(fake_telegram.sent_messages) == 1
    assert tuple(outbound.values()) == (
        "delivery_unknown",
        None,
        "telegram:consent_prompt:905",
    )
    assert await db.fetchval("SELECT count(*) FROM task_outbox") == 1
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0


async def test_non_text_update_sends_one_durable_static_reply(
    client, db, fake_telegram
):
    update = telegram_photo_update(update_id=906)

    first = await client.post("/telegram/webhook", json=update)
    duplicate = await client.post("/telegram/webhook", json=update)

    outbound = await db.fetchrow(
        """
        SELECT status, external_message_id, idempotency_key, text
        FROM outbound_messages
        """
    )
    assert first.status_code == duplicate.status_code == 200
    assert len(fake_telegram.sent_messages) == 1
    assert fake_telegram.last_text == NON_TEXT_REPLY
    assert tuple(outbound.values()) == (
        "sent",
        "701",
        "telegram:non_text:906",
        NON_TEXT_REPLY,
    )
    assert await db.fetchval("SELECT count(*) FROM task_outbox") == 1
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0


async def test_webhook_uses_shared_ingress_decision_for_nontext(
    client,
    fake_telegram,
    monkeypatch,
):
    calls = []
    original = webhook_module.decide_ingress

    def capture(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(webhook_module, "decide_ingress", capture)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_photo_update(update_id=916),
    )

    assert response.status_code == 200
    assert fake_telegram.last_text == NON_TEXT_REPLY
    assert calls == [
        {
            "has_text": False,
            "has_processing_consent": False,
        }
    ]


async def test_webhook_nontext_behavior_changes_with_substituted_ingress_decision(
    client,
    fake_telegram,
    monkeypatch,
):
    monkeypatch.setattr(
        webhook_module,
        "decide_ingress",
        lambda **_: IngressDecision("accept", None),
    )

    response = await client.post(
        "/telegram/webhook",
        json=telegram_photo_update(update_id=918),
    )

    assert response.status_code == 200
    assert fake_telegram.sent_messages == []


async def test_webhook_uses_shared_ingress_decision_for_missing_consent(
    client,
    fake_telegram,
    monkeypatch,
):
    calls = []
    original = webhook_module.decide_ingress

    def capture(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(webhook_module, "decide_ingress", capture)

    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(update_id=917),
    )

    assert response.status_code == 200
    assert fake_telegram.last_text == CONSENT_PROMPT
    assert calls == [
        {
            "has_text": True,
            "has_processing_consent": False,
        }
    ]


async def test_webhook_text_behavior_changes_with_substituted_ingress_decision(
    client,
    db,
    fake_telegram,
    monkeypatch,
):
    monkeypatch.setattr(
        webhook_module,
        "decide_ingress",
        lambda **_: IngressDecision("reply", "nontext"),
    )

    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(update_id=919),
    )

    assert response.status_code == 200
    assert fake_telegram.sent_messages == []
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0


async def test_claimed_consent_outbound_rebuilds_keyboard_from_database(
    message_database, db, fake_telegram
):
    delivery_options = {
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": f"☐ {CONSENT_PII_LABEL}",
                        "callback_data": CONSENT_PII_CALLBACK_DATA,
                    }
                ],
                [
                    {
                        "text": f"☐ {CONSENT_ADS_LABEL}",
                        "callback_data": CONSENT_ADS_CALLBACK_DATA,
                    }
                ],
                [
                    {
                        "text": CONSENT_DONE_LABEL,
                        "callback_data": CONSENT_DONE_CALLBACK_DATA,
                    }
                ]
            ]
        }
    }
    enqueue_repository = MessageRepository(message_database)
    outbound_id = await enqueue_repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text=CONSENT_PROMPT,
        idempotency_key="telegram:consent_prompt:907",
        delivery_options=delivery_options,
    )
    delivery_options["reply_markup"]["inline_keyboard"][0][0]["text"] = (
        "Изменено после enqueue"
    )

    claimed = await MessageRepository(
        message_database
    ).claim_outbound_delivery(outbound_id)
    from webhook import deliver_claimed_outbound

    await deliver_claimed_outbound(
        fake_telegram,
        MessageRepository(message_database),
        claimed,
    )

    markup = fake_telegram.sent_messages[0]["reply_markup"]
    stored_options = await db.fetchval(
        "SELECT delivery_options FROM outbound_messages WHERE id = $1",
        outbound_id,
    )
    assert claimed.delivery_options["reply_markup"]["inline_keyboard"] == [
        [{"text": f"☐ {CONSENT_PII_LABEL}", "callback_data": CONSENT_PII_CALLBACK_DATA}],
        [{"text": f"☐ {CONSENT_ADS_LABEL}", "callback_data": CONSENT_ADS_CALLBACK_DATA}],
        [{"text": CONSENT_DONE_LABEL, "callback_data": CONSENT_DONE_CALLBACK_DATA}],
    ]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == CONSENT_PII_CALLBACK_DATA
    assert json.loads(stored_options) == claimed.delivery_options
    assert await db.fetchval(
        "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
    ) == "sent"


async def test_claimed_outbound_with_empty_options_sends_without_markup(
    message_database, db, fake_telegram
):
    repository = MessageRepository(message_database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text=NON_TEXT_REPLY,
        idempotency_key="telegram:non_text:908",
    )

    claimed = await MessageRepository(
        message_database
    ).claim_outbound_delivery(outbound_id)
    from webhook import deliver_claimed_outbound

    await deliver_claimed_outbound(
        fake_telegram,
        MessageRepository(message_database),
        claimed,
    )

    assert claimed.delivery_options == {}
    assert fake_telegram.sent_messages == [
        {"chat_id": 42, "text": NON_TEXT_REPLY}
    ]
    assert await db.fetchval(
        "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
    ) == "sent"
