import json
import os
from datetime import datetime
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as redis
from aiogram.types import InlineKeyboardMarkup
from httpx import ASGITransport, AsyncClient

from config import NON_TEXT_REPLY
from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository
from webhook import create_app


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

CONSENT_CALLBACK_DATA = "processing_consent:v1"
CONSENT_PROMPT = "Чтобы продолжить, подтвердите обработку данных."


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeTelegram:
    def __init__(self):
        self.session = FakeSession()
        self.sent_messages = []
        self.send_error = None

    @property
    def last_text(self):
        return self.sent_messages[-1]["text"]

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(message_id=700 + len(self.sent_messages))


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
):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-1",
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Тест",
            },
            "chat_instance": "test-chat",
            "data": CONSENT_CALLBACK_DATA,
            "message": {
                "message_id": 99,
                "date": 1_768_478_400,
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
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            yield http_client
    assert fake_telegram.session.closed is True


async def test_message_without_consent_is_not_persisted(
    client, db, fake_telegram
):
    response = await client.post(
        "/telegram/webhook",
        json=telegram_text_update(),
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert fake_telegram.last_text == CONSENT_PROMPT
    assert (
        fake_telegram.sent_messages[-1]["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
        == CONSENT_CALLBACK_DATA
    )


async def test_consent_callback_persists_only_versioned_consent(
    client, db
):
    response = await client.post(
        "/telegram/webhook",
        json=telegram_consent_callback(),
    )

    consent = await db.fetchrow(
        """
        SELECT channel, user_id, consent_version, granted_at
        FROM processing_consents
        """
    )
    assert response.status_code == 200
    assert tuple(consent.values())[:3] == ("telegram", "7", "v1")
    assert isinstance(consent["granted_at"], datetime)
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0


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
    client, db, redis_client
):
    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(),
        )
    ).status_code == 200
    update = telegram_text_update("Можно сохранить", update_id=902)

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
    assert tuple(message.values())[:3] == ("telegram", "902", "42")
    assert json.loads(message["payload"])["text"] == "Можно сохранить"
    entries = await redis_client.lrange("buffer:42", 0, -1)
    assert [json.loads(entry)["update_id"] for entry in entries] == ["902"]
    assert await db.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 0


async def test_redis_failure_after_consent_creates_single_message_task(
    migrated_database_url, db, fake_telegram
):
    app = create_app(
        database_url=migrated_database_url,
        bot=fake_telegram,
        redis_url="redis://127.0.0.1:1/0",
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            assert (
                await http_client.post(
                    "/telegram/webhook",
                    json=telegram_consent_callback(update_id=909),
                )
            ).status_code == 200
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
        "chat_id": "42",
        "update_ids": ["910"],
        "text": "Не потерять",
    }
    assert tuple(task.values())[2:] == ("process_message:910", "pending")


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
    fake_telegram.send_error = RuntimeError("sensitive external failure")
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


async def test_claimed_consent_outbound_rebuilds_keyboard_from_database(
    message_database, db, fake_telegram
):
    delivery_options = {
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "Подтвердить",
                        "callback_data": CONSENT_CALLBACK_DATA,
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
    assert claimed.delivery_options["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "Подтвердить",
        "callback_data": CONSENT_CALLBACK_DATA,
    }
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == CONSENT_CALLBACK_DATA
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
