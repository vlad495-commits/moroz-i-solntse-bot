import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from enum import StrEnum
from uuid import UUID

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

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
    retry: bool


def classify_delivery_error(
    error: BaseException, *, managed: bool
) -> DeliveryErrorDecision:
    if isinstance(error, asyncio.CancelledError):
        return DeliveryErrorDecision("delivery_unknown", "cancelled", False)
    if isinstance(error, TelegramRetryAfter):
        return DeliveryErrorDecision("pending", "telegram_retry_after", True)
    if isinstance(error, TelegramForbiddenError):
        status = "failed" if managed else "pending"
        return DeliveryErrorDecision(status, "telegram_forbidden", not managed)
    if isinstance(error, TelegramNotFound):
        status = "failed" if managed else "pending"
        return DeliveryErrorDecision(status, "telegram_not_found", not managed)
    if isinstance(error, TelegramBadRequest):
        status = "failed" if managed else "pending"
        return DeliveryErrorDecision(status, "telegram_bad_request", not managed)
    if isinstance(error, TelegramNetworkError):
        return DeliveryErrorDecision(
            "delivery_unknown", "telegram_network", False
        )
    if isinstance(error, TimeoutError):
        return DeliveryErrorDecision("delivery_unknown", "timeout", False)
    return DeliveryErrorDecision("pending", "telegram_error", True)


def _is_managed(outbound: OutboundMessage) -> bool:
    return outbound.delivery_options.get("delivery_policy") == "reactivation"


async def _mark_post_send_unknown(
    repository,
    outbound,
    error,
    *,
    delivery_hook,
    now,
) -> None:
    try:
        await asyncio.shield(
            repository.mark_outbound_delivery_unknown(
                outbound.id,
                delivery_hook=delivery_hook if _is_managed(outbound) else None,
                error_code="post_send_completion",
                now=now,
            )
        )
    except BaseException as mark_error:
        logger.error(
            "post_send_delivery_unknown_mark_failed outbound_id=%s error_type=%s",
            outbound.id,
            type(mark_error).__name__,
        )
    logger.error(
        "post_send_completion_failed outbound_id=%s error_type=%s",
        outbound.id,
        type(error).__name__,
    )


async def deliver_claimed_outbound(
    telegram,
    repository: MessageRepository,
    outbound: OutboundMessage,
    *,
    context_cache=None,
    pre_send_guard: PreSendGuard | None = None,
    delivery_hook: DeliveryHook | None = None,
    clock=lambda: datetime.now(UTC),
) -> DeliveryResult:
    managed = _is_managed(outbound)
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
            await repository.mark_outbound_delivery_unknown(
                outbound.id,
                delivery_hook=delivery_hook if managed else None,
                error_code=decision.error_code,
                now=current_time,
            )
            logger.error(
                "telegram_delivery_unknown outbound_id=%s code=%s count=1",
                outbound.id,
                decision.error_code,
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            return DeliveryResult.DELIVERY_UNKNOWN
        if decision.outbound_status == "failed":
            await repository.mark_outbound_failed(
                outbound.id,
                decision.error_code,
                delivery_hook=delivery_hook,
                now=current_time,
            )
            logger.error(
                "telegram_delivery_failed outbound_id=%s code=%s count=1",
                outbound.id,
                decision.error_code,
            )
            return DeliveryResult.FAILED
        await repository.release_outbound_delivery(outbound.id)
        raise
    try:
        context_chat_id = await repository.mark_outbound_sent(
            outbound.id,
            str(sent_message.message_id),
            delivery_hook=delivery_hook if managed else None,
            now=clock(),
        )
    except asyncio.CancelledError as error:
        await _mark_post_send_unknown(
            repository,
            outbound,
            error,
            delivery_hook=delivery_hook,
            now=clock(),
        )
        raise
    except Exception as error:
        await _mark_post_send_unknown(
            repository,
            outbound,
            error,
            delivery_hook=delivery_hook,
            now=clock(),
        )
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
        clock=lambda: datetime.now(UTC),
    ):
        self._telegram = telegram
        self._repository = repository
        self._context_cache = context_cache
        self._pre_send_guard = pre_send_guard
        self._delivery_hook = delivery_hook
        self._clock = clock

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
            clock=self._clock,
        )
