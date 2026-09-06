from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import DeliveryResult, TelegramSender
from tests.e2e.test_message_delivery import database as database_fixture

database = database_fixture
pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


class Telegram:
    def __init__(self, error=None):
        self.sent, self.edited = [], []
        self.error = error

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=700 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        if self.error:
            raise self.error
        return True


def options(card="draft-one", *, edit=False):
    return {"booking_card": card, "edit_booking_card": edit,
            "reply_markup": {"inline_keyboard": [[{"text": "Выбрать", "callback_data": "x"}]]}}


async def deliver(repository, telegram, delivery_options, text="Карточка", chat_id="42"):
    outbound_id = await repository.enqueue_outbound(
        channel="telegram", chat_id=chat_id, text=text,
        idempotency_key=str(uuid4()), delivery_options=delivery_options,
    )
    result = await TelegramSender(telegram, repository).send(outbound_id)
    return outbound_id, result


@pytest.mark.parametrize("case", ["same", "other", "text", "contact", "chat", "final"])
async def test_only_same_draft_callback_edits(database, case):
    repository, telegram = MessageRepository(database), Telegram()
    await deliver(repository, telegram, options())
    current = options(edit=True)
    if case == "other":
        current["booking_card"] = "draft-two"
    elif case == "text":
        current["edit_booking_card"] = False
    elif case == "contact":
        current["reply_markup"] = {"keyboard": [[{"text": "Контакт", "request_contact": True}]]}
    elif case == "final":
        current = {}
    outbound_id, result = await deliver(repository, telegram, current, chat_id="43" if case == "chat" else "42")
    assert result == DeliveryResult.SENT
    assert len(telegram.edited) == (1 if case == "same" else 0)
    assert len(telegram.sent) == (1 if case == "same" else 2)
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT external_message_id FROM outbound_messages WHERE id=$1", outbound_id) == ("701" if case == "same" else "702")


@pytest.mark.parametrize("error, expected, sends", [
    (TelegramBadRequest(method="editMessageText", message="message is not modified"), DeliveryResult.SENT, 1),
    (TelegramBadRequest(method="editMessageText", message="message to edit not found"), DeliveryResult.SENT, 2),
    (TelegramBadRequest(method="editMessageText", message="message can't be edited"), DeliveryResult.SENT, 2),
    (TelegramNetworkError(method="editMessageText", message="network"), DeliveryResult.DELIVERY_UNKNOWN, 1),
    (TimeoutError(), DeliveryResult.DELIVERY_UNKNOWN, 1),
])
async def test_edit_errors_do_not_duplicate_uncertain_delivery(database, error, expected, sends):
    repository, telegram = MessageRepository(database), Telegram(error)
    await deliver(repository, telegram, options())
    _, result = await deliver(repository, telegram, options(edit=True))
    assert result == expected
    assert len(telegram.edited) == 1
    assert len(telegram.sent) == sends


async def test_plain_links_and_preview(database):
    repository, telegram = MessageRepository(database), Telegram()
    await deliver(repository, telegram, {}, "**Адрес** [Карта](https://example.com/map) https://example.com/**raw**")
    assert telegram.sent[0]["text"] == "Адрес Карта: https://example.com/map https://example.com/**raw**"
    assert telegram.sent[0]["link_preview_options"].is_disabled is True


async def test_callback_edit_never_targets_contact_or_forged_message_id(database):
    repository, telegram = MessageRepository(database), Telegram()
    contact = options()
    contact["reply_markup"] = {"keyboard": [[{"text": "Контакт", "request_contact": True}]]}
    await deliver(repository, telegram, contact)
    current = {**options(edit=True), "edit_message_id": "999"}
    await deliver(repository, telegram, current)
    assert not telegram.edited
    assert len(telegram.sent) == 2


async def test_real_callback_preserves_card_identity_and_contact_is_new(migrated_database_url):
    from moroz.booking.yclients_catalog import CatalogRecord
    from moroz.messaging.router import RouteDecision
    from tests.e2e.booking.telegram_helpers import coordinator, handle

    database, bookings, adapter, booking = await coordinator(
        migrated_database_url,
        catalog_records=tuple(CatalogRecord(str(index), "10", name, "Крио", "Анна", 1000, 1000, 60)
                              for index, name in [(331, "Криокапсула"), (332, "Криомассаж")]),
    )
    try:
        repository, telegram = MessageRepository(database), Telegram()
        base = {"customer_id": "42", "user_id": "7", "text": ""}
        first = await handle(booking, database, **base, update_id="first", kind="text", data={},
                             decision=RouteDecision("booking", 1, "create", services=("Крио",), date="2026-09-05"))
        await deliver(repository, telegram, first.outbound_options(first.text), first.text)
        raw = first.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        slots = await handle(booking, database, **base, update_id="second", kind="callback", data={"callback_data": raw})
        await deliver(repository, telegram, slots.outbound_options(slots.text, callback=True), slots.text)
        assert len(telegram.edited) == 1
        assert telegram.edited[0]["message_id"] == 701
        assert telegram.edited[0]["link_preview_options"].is_disabled is True
        raw = slots.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        contact = await handle(booking, database, **base, update_id="third", kind="callback", data={"callback_data": raw})
        await deliver(repository, telegram, contact.outbound_options(contact.text, callback=True), contact.text)
        assert len(telegram.sent) == 2
        assert len(telegram.edited) == 1
        assert telegram.sent[-1]["reply_markup"].keyboard[0][0].request_contact is True
        confirmation = await handle(
            booking, database, **base, update_id="fourth", kind="contact",
            data={"contact_user_id": "7", "phone_number": "+79001112233", "first_name": "Иван"},
        )
        await deliver(repository, telegram, confirmation.outbound_options(confirmation.text), confirmation.text)
        assert "Криокапсула" in telegram.sent[-1]["text"]
        assert "Иван" in telegram.sent[-1]["text"]
        stale = await handle(booking, database, **base, update_id="stale", kind="callback", data={"callback_data": raw})
        await deliver(repository, telegram, stale.outbound_options(stale.text, callback=True), stale.text)
        assert len(telegram.edited) == 1, "Stale callback must not erase full confirmation details"
        assert "booking_card" not in stale.delivery_options
        assert len(telegram.sent) == 4
        assert adapter.create_calls == 0
    finally:
        await database.close()
