import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
)

from moroz.messaging.models import OutboundMessage
from moroz.messaging.telegram import (
    DeliveryResult,
    TelegramSender,
    classify_delivery_error,
)


NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


def _error(kind):
    method = SimpleNamespace()
    if kind is TelegramRetryAfter:
        return kind(method, "private retry detail", 5)
    return kind(method, "private provider detail")


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (_error(TelegramForbiddenError), "failed", "telegram_forbidden"),
        (_error(TelegramNotFound), "failed", "telegram_not_found"),
        (_error(TelegramBadRequest), "failed", "telegram_bad_request"),
        (_error(TelegramRetryAfter), "pending", "telegram_retry_after"),
        (_error(TelegramNetworkError), "delivery_unknown", "telegram_network"),
        (TimeoutError("private timeout"), "delivery_unknown", "timeout"),
        (asyncio.CancelledError(), "delivery_unknown", "cancelled"),
    ],
)
def test_managed_delivery_error_matrix(error, status, code):
    decision = classify_delivery_error(error, managed=True)

    assert decision.outbound_status == status
    assert decision.error_code == code
    assert not hasattr(decision, "retry")


class FakeRepository:
    def __init__(self, outbound):
        self.outbound = outbound
        self.status = "pending"

    async def claim_outbound_delivery(self, _outbound_id):
        if self.status != "pending":
            return None
        self.status = "sending"
        return self.outbound

    @asynccontextmanager
    async def fence_claimed_outbound(self, outbound, *, pre_send_guard=None):
        if pre_send_guard is not None and not await pre_send_guard(None, outbound):
            yield None
            return
        yield outbound

    async def mark_outbound_sent(
        self, outbound_id, external_message_id, *, delivery_hook=None, now=None
    ):
        async def transition():
            self.status = "sent"
            return self.outbound

        if delivery_hook is not None:
            await delivery_hook(None, self.outbound, "sent", None, now, transition)
        else:
            await transition()

    async def mark_outbound_failed(
        self, outbound_id, error_code, *, delivery_hook=None, now=None
    ):
        async def transition():
            self.status = "failed"
            return self.outbound

        if delivery_hook is not None:
            await delivery_hook(
                None, self.outbound, "failed", error_code, now, transition
            )
        else:
            await transition()
        return True

    async def mark_outbound_delivery_unknown(
        self, outbound_id, *, delivery_hook=None, error_code=None, now=None
    ):
        async def transition():
            self.status = "delivery_unknown"
            return self.outbound

        if delivery_hook is not None:
            await delivery_hook(
                None, self.outbound, "delivery_unknown", error_code, now, transition
            )
        else:
            await transition()
        return True

    async def release_outbound_delivery(self, _outbound_id):
        self.status = "pending"
        return True


class FakeTelegram:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def send_message(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(message_id=42)


async def _managed(_outbound):
    return True


async def _generic(_outbound):
    return False


def _outbound(*, managed=True):
    return OutboundMessage(
        id=uuid4(),
        channel="telegram",
        chat_id="42",
        text="safe test text",
        delivery_options=(
            {"delivery_policy": "reactivation"} if managed else {}
        ),
        idempotency_key="test",
    )


@pytest.mark.asyncio
async def test_managed_bad_request_is_terminal_and_calls_atomic_hook(caplog):
    outbound = _outbound()
    repository = FakeRepository(outbound)
    hook_calls = []

    async def hook(*args):
        hook_calls.append(args[2:4])
        await args[-1]()

    sender = TelegramSender(
        FakeTelegram(_error(TelegramBadRequest)),
        repository,
        delivery_hook=hook,
        managed_delivery_check=_managed,
        clock=lambda: NOW,
    )

    assert await sender.send(outbound.id) == DeliveryResult.FAILED
    assert repository.status == "failed"
    assert hook_calls == [("failed", "telegram_bad_request")]
    for private in (
        str(outbound.id), outbound.chat_id, outbound.text, "private provider detail"
    ):
        assert private not in caplog.text


@pytest.mark.asyncio
async def test_unmanaged_bad_request_keeps_existing_retry_semantics():
    outbound = _outbound(managed=False)
    repository = FakeRepository(outbound)

    with pytest.raises(TelegramBadRequest):
        await TelegramSender(
            FakeTelegram(_error(TelegramBadRequest)), repository
        ).send(outbound.id)

    assert repository.status == "pending"


@pytest.mark.asyncio
async def test_forged_reactivation_delivery_option_is_not_authoritative():
    outbound = _outbound(managed=True)
    repository = FakeRepository(outbound)

    with pytest.raises(TelegramBadRequest):
        await TelegramSender(
            FakeTelegram(_error(TelegramBadRequest)),
            repository,
            managed_delivery_check=_generic,
        ).send(outbound.id)

    assert repository.status == "pending"


@pytest.mark.asyncio
async def test_retry_after_releases_and_reraises_without_delivery_hook():
    outbound = _outbound()
    repository = FakeRepository(outbound)
    hook_calls = []

    async def hook(*args):
        hook_calls.append(args)

    with pytest.raises(TelegramRetryAfter):
        await TelegramSender(
            FakeTelegram(_error(TelegramRetryAfter)),
            repository,
            delivery_hook=hook,
            managed_delivery_check=_managed,
        ).send(outbound.id)

    assert repository.status == "pending"
    assert hook_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("db unavailable"), asyncio.CancelledError()])
async def test_managed_check_failure_releases_claim_before_provider(error):
    outbound = _outbound()
    repository = FakeRepository(outbound)
    telegram = FakeTelegram()

    async def broken_check(_outbound):
        raise error

    with pytest.raises(type(error)):
        await TelegramSender(
            telegram,
            repository,
            managed_delivery_check=broken_check,
        ).send(outbound.id)

    assert repository.status == "pending"
    assert telegram.calls == 0
