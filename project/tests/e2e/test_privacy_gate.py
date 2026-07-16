import json
from datetime import datetime
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from config import NON_TEXT_REPLY
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


def telegram_text_update(text="Секретный текст", *, update_id=900):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100,
            "date": 1_768_478_400,
            "chat": {"id": 42, "type": "private"},
            "from": {
                "id": 7,
                "is_bot": False,
                "first_name": "Тест",
            },
            "text": text,
        },
    }


def telegram_consent_callback(*, update_id=901):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-1",
            "from": {
                "id": 7,
                "is_bot": False,
                "first_name": "Тест",
            },
            "chat_instance": "test-chat",
            "data": CONSENT_CALLBACK_DATA,
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
async def client(migrated_database_url, fake_telegram):
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


async def test_consented_update_is_persisted_once_by_update_id(
    client, db
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
