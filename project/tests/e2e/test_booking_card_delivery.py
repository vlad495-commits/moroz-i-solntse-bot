import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup

from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import DeliveryResult, TelegramSender


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=5)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


class Telegram:
    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self.edited = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=700 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=kwargs["message_id"])


async def enqueue(repository, card=None, *, chat_id="42", channel="telegram", text="Выберите время"):
    options = {} if card is None else {
        "booking_card": card,
        "reply_markup": {"inline_keyboard": [[{"text": "Назад", "callback_data": "back"}]]},
    }
    return await repository.enqueue_outbound(
        channel=channel, chat_id=chat_id, text=text,
        idempotency_key=str(uuid4()), delivery_options=options,
    )


async def row(database, outbound_id):
    async with database.acquire() as connection:
        return await connection.fetchrow("SELECT * FROM outbound_messages WHERE id=$1", outbound_id)


async def test_card_edits_same_message_and_does_not_persist_private_target(database):
    repository = MessageRepository(database)
    telegram = Telegram()
    sender = TelegramSender(telegram, repository)
    card = str(uuid4())
    first = await enqueue(repository, card)
    assert await sender.send(first) == DeliveryResult.SENT
    second = await enqueue(repository, card, text="Выберите дату")
    assert await sender.send(second) == DeliveryResult.SENT
    assert len(telegram.sent) == 1
    assert telegram.edited[0]["message_id"] == 701
    assert telegram.edited[0]["chat_id"] == 42
    assert telegram.edited[0]["text"] == "Выберите дату"
    assert isinstance(telegram.edited[0]["reply_markup"], InlineKeyboardMarkup)
    assert telegram.edited[0]["link_preview_options"].is_disabled
    saved = await row(database, second)
    assert saved["external_message_id"] == "701"
    assert "edit_message_id" not in json.loads(saved["delivery_options"])


@pytest.mark.parametrize("description, fallback", [
    ("Bad Request: message is not modified: specified new message content is the same", False),
    ("Bad Request: message to edit not found", True),
    ("Bad Request: message can't be edited", True),
])
async def test_known_edit_results(database, description, fallback):
    repository = MessageRepository(database)
    telegram = Telegram()
    sender = TelegramSender(telegram, repository)
    card = str(uuid4())
    assert await sender.send(await enqueue(repository, card)) == DeliveryResult.SENT
    telegram.error = TelegramBadRequest(method=EditMessageText(text="x"), message=description)
    second = await enqueue(repository, card)
    assert await sender.send(second) == DeliveryResult.SENT
    assert len(telegram.edited) == 1
    assert len(telegram.sent) == (2 if fallback else 1)
    assert (await row(database, second))["external_message_id"] == ("702" if fallback else "701")
    telegram.error = None
    assert await sender.send(await enqueue(repository, card)) == DeliveryResult.SENT
    assert telegram.edited[-1]["message_id"] == (702 if fallback else 701)


@pytest.mark.parametrize("error_type, description", [
    (TelegramNetworkError, "network failed"),
    (TelegramBadRequest, "Bad Request: unknown rejection"),
])
async def test_uncertain_edit_does_not_send_second_message(database, error_type, description):
    repository = MessageRepository(database)
    telegram = Telegram()
    sender = TelegramSender(telegram, repository)
    card = str(uuid4())
    await sender.send(await enqueue(repository, card))
    telegram.error = error_type(method=EditMessageText(text="x"), message=description)
    second = await enqueue(repository, card)
    if error_type is TelegramNetworkError:
        assert await sender.send(second) == DeliveryResult.DELIVERY_UNKNOWN
        assert (await row(database, second))["status"] == "delivery_unknown"
    else:
        with pytest.raises(TelegramBadRequest):
            await sender.send(second)
    assert len(telegram.edited) == 1
    assert len(telegram.sent) == 1


async def test_consultation_final_and_other_card_or_chat_send_new_messages(database):
    repository = MessageRepository(database)
    telegram = Telegram()
    sender = TelegramSender(telegram, repository)
    card = str(uuid4())
    await sender.send(await enqueue(repository, card))
    for options in (
        {"text": "Консультация"}, {"text": "Вы записаны"},
        {"card": str(uuid4())}, {"card": card, "chat_id": "99"},
        {"card": card, "channel": "other"},
    ):
        assert await sender.send(await enqueue(repository, **options)) == DeliveryResult.SENT
    assert len(telegram.sent) == 6
    assert not telegram.edited


async def test_deleted_claimed_card_is_not_edited(database):
    repository = MessageRepository(database)
    telegram = Telegram()
    sender = TelegramSender(telegram, repository)
    card = str(uuid4())
    await sender.send(await enqueue(repository, card))
    second = await enqueue(repository, card)
    claimed = await repository.claim_outbound_delivery(second)
    async with database.acquire() as connection:
        await connection.execute("DELETE FROM outbound_messages WHERE id=$1", second)
    async with repository.fence_claimed_outbound(claimed) as current:
        assert current is None
