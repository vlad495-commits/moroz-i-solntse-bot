import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from enum import StrEnum
from typing import Awaitable, Callable
from uuid import UUID

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from moroz.common.queue import TaskRecoveryRequired
from moroz.messaging.models import OutboundMessage
from moroz.messaging.repository import DeliveryHook, MessageRepository, PreSendGuard


logger = logging.getLogger(__name__)


class DeliveryResult(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass(frozen=True, slots=True)
class DeliveryErrorDecision:
    outbound_status: str
    error_code: str


def classify_delivery_error(
    error: BaseException, *, managed: bool
) -> DeliveryErrorDecision:
    if isinstance(error, asyncio.CancelledError):
        return DeliveryErrorDecision("delivery_unknown", "cancelled")
    if isinstance(error, TelegramRetryAfter):
        return DeliveryErrorDecision("pending", "telegram_retry_after")
    if isinstance(error, TelegramForbiddenError):
        status = "failed" if managed else "pending"
        return DeliveryErrorDecision(status, "telegram_forbidden")
    if isinstance(error, TelegramNotFound):
        status = "failed" if managed else "pending"
        return DeliveryErrorDecision(status, "telegram_not_found")
    if isinstance(error, TelegramBadRequest):
        status = "failed" if managed else "pending"
        return DeliveryErrorDecision(status, "telegram_bad_request")
    if isinstance(error, TelegramNetworkError):
        return DeliveryErrorDecision("delivery_unknown", "telegram_network")
    if isinstance(error, TimeoutError):
        return DeliveryErrorDecision("delivery_unknown", "timeout")
    return DeliveryErrorDecision("pending", "telegram_error")


ManagedDeliveryCheck = Callable[[OutboundMessage], Awaitable[bool]]


async def _recover_post_provider(
    repository,
    outbound,
    *,
    retry_safe: bool,
) -> None:
    try:
        if retry_safe:
            durable = await asyncio.shield(
                repository.release_outbound_delivery(outbound.id)
            )
            recovery = "pending"
        else:
            durable = await asyncio.shield(
                repository.mark_outbound_delivery_unknown(outbound.id)
            )
            recovery = "delivery_unknown"
    except BaseException as error:
        raise TaskRecoveryRequired("delivery recovery requires redelivery") from error
    if not durable:
        raise TaskRecoveryRequired("delivery recovery requires redelivery")
    logger.error(
        "post_provider_completion_recovered code=%s count=1", recovery
    )


async def _complete_post_provider(
    operation,
    repository,
    outbound,
    *,
    retry_safe: bool,
    verify=lambda _result: True,
):
    try:
        result = await operation()
        if not verify(result):
            raise RuntimeError("terminal delivery status was not persisted")
        return True, result
    except BaseException as error:
        await _recover_post_provider(
            repository, outbound, retry_safe=retry_safe
        )
        if retry_safe or isinstance(error, asyncio.CancelledError):
            raise
        return False, None


async def deliver_claimed_outbound(
    telegram,
    repository: MessageRepository,
    outbound: OutboundMessage,
    *,
    context_cache=None,
    pre_send_guard: PreSendGuard | None = None,
    delivery_hook: DeliveryHook | None = None,
    managed_delivery_check: ManagedDeliveryCheck | None = None,
    clock=lambda: datetime.now(UTC),
) -> DeliveryResult:
    try:
        managed = bool(
            managed_delivery_check
            and await managed_delivery_check(outbound)
        )
    except BaseException:
        await asyncio.shield(repository.release_outbound_delivery(outbound.id))
        raise
    try:
        async with repository.fence_claimed_outbound(
            outbound,
            pre_send_guard=pre_send_guard,
        ) as current:
            if current is None:
                return DeliveryResult.SKIPPED
            send_arguments = {
                "chat_id": int(current.chat_id),
                "text": current.text,
            }
            reply_markup = current.delivery_options.get("reply_markup")
            if reply_markup is not None:
                send_arguments["reply_markup"] = (
                    InlineKeyboardMarkup.model_validate(reply_markup)
                )
            parse_mode = current.delivery_options.get("parse_mode")
            if parse_mode is not None:
                send_arguments["parse_mode"] = str(parse_mode)
            sent_message = await telegram.send_message(**send_arguments)
    except BaseException as error:
        if not isinstance(error, (Exception, asyncio.CancelledError)):
            await repository.release_outbound_delivery(outbound.id)
            raise
        decision = classify_delivery_error(error, managed=managed)
        current_time = clock()
        if decision.outbound_status == "delivery_unknown":
            await _complete_post_provider(
                lambda: repository.mark_outbound_delivery_unknown(
                    outbound.id,
                    delivery_hook=delivery_hook if managed else None,
                    error_code=decision.error_code,
                    now=current_time,
                ),
                repository,
                outbound,
                retry_safe=False,
                verify=bool,
            )
            logger.error(
                "telegram_delivery_unknown code=%s count=1",
                decision.error_code,
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            return DeliveryResult.DELIVERY_UNKNOWN
        if decision.outbound_status == "failed":
            await _complete_post_provider(
                lambda: repository.mark_outbound_failed(
                    outbound.id,
                    decision.error_code,
                    delivery_hook=delivery_hook,
                    now=current_time,
                ),
                repository,
                outbound,
                retry_safe=True,
                verify=bool,
            )
            logger.error(
                "telegram_delivery_failed code=%s count=1",
                decision.error_code,
            )
            return DeliveryResult.FAILED
        await repository.release_outbound_delivery(outbound.id)
        raise
    completed, context_chat_id = await _complete_post_provider(
        lambda: repository.mark_outbound_sent(
            outbound.id,
            str(sent_message.message_id),
            delivery_hook=delivery_hook if managed else None,
            now=clock(),
        ),
        repository,
        outbound,
        retry_safe=False,
    )
    if not completed:
        return DeliveryResult.DELIVERY_UNKNOWN
    if context_chat_id is not None and context_cache is not None:
        try:
            await context_cache.delete(f"chat:{context_chat_id}:messages")
        except Exception as error:
            logger.warning(
                "context_cache_invalidation_failed error_type=%s",
                type(error).__name__,
            )
    return DeliveryResult.SENT


class TelegramSender:
    def __init__(
        self,
        telegram,
        repository: MessageRepository,
        *,
        context_cache=None,
        pre_send_guard: PreSendGuard | None = None,
        delivery_hook: DeliveryHook | None = None,
        managed_delivery_check: ManagedDeliveryCheck | None = None,
        clock=lambda: datetime.now(UTC),
    ):
        self._telegram = telegram
        self._repository = repository
        self._context_cache = context_cache
        self._pre_send_guard = pre_send_guard
        self._delivery_hook = delivery_hook
        self._managed_delivery_check = managed_delivery_check
        self._clock = clock

    async def recover(self, outbound_id: UUID) -> None:
        try:
            durable = await asyncio.shield(
                self._repository.mark_outbound_delivery_unknown(
                    outbound_id,
                    delivery_hook=self._delivery_hook,
                    error_code="post_provider_recovery",
                    now=self._clock(),
                )
            )
        except BaseException as error:
            raise TaskRecoveryRequired(
                "delivery recovery requires redelivery"
            ) from error
        if not durable:
            raise TaskRecoveryRequired("delivery recovery requires redelivery")

    async def send(self, outbound_id: UUID) -> DeliveryResult:
        outbound = await self._repository.claim_outbound_delivery(outbound_id)
        if outbound is None:
            return DeliveryResult.SKIPPED
        return await deliver_claimed_outbound(
            self._telegram,
            self._repository,
            outbound,
            context_cache=self._context_cache,
            pre_send_guard=self._pre_send_guard,
            delivery_hook=self._delivery_hook,
            managed_delivery_check=self._managed_delivery_check,
            clock=self._clock,
        )
