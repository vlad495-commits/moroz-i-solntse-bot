import asyncio
import logging
from enum import StrEnum
from uuid import UUID

from aiogram.exceptions import TelegramNetworkError
from aiogram.types import InlineKeyboardMarkup

from moroz.messaging.models import OutboundMessage
from moroz.messaging.repository import MessageRepository


logger = logging.getLogger(__name__)


class DeliveryResult(StrEnum):
    SENT = "sent"
    SKIPPED = "skipped"
    DELIVERY_UNKNOWN = "delivery_unknown"


async def _mark_post_send_unknown(repository, outbound_id, error) -> None:
    try:
        await asyncio.shield(repository.mark_outbound_delivery_unknown(outbound_id))
    except BaseException as mark_error:
        logger.error(
            "post_send_delivery_unknown_mark_failed outbound_id=%s error_type=%s",
            outbound_id,
            type(mark_error).__name__,
        )
    logger.error(
        "post_send_completion_failed outbound_id=%s error_type=%s",
        outbound_id,
        type(error).__name__,
    )


async def deliver_claimed_outbound(
    telegram,
    repository: MessageRepository,
    outbound: OutboundMessage,
    *,
    context_cache=None,
) -> DeliveryResult:
    try:
        async with repository.fence_claimed_outbound(outbound) as current:
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
    except asyncio.CancelledError:
        await repository.mark_outbound_delivery_unknown(outbound.id)
        logger.error(
            "telegram_delivery_unknown outbound_id=%s error_type=CancelledError",
            outbound.id,
        )
        raise
    except (TelegramNetworkError, TimeoutError) as error:
        await repository.mark_outbound_delivery_unknown(outbound.id)
        logger.error(
            "telegram_delivery_unknown outbound_id=%s error_type=%s",
            outbound.id,
            type(error).__name__,
        )
        return DeliveryResult.DELIVERY_UNKNOWN
    except Exception:
        await repository.release_outbound_delivery(outbound.id)
        raise
    try:
        context_chat_id = await repository.mark_outbound_sent(
            outbound.id,
            str(sent_message.message_id),
        )
    except asyncio.CancelledError as error:
        await _mark_post_send_unknown(repository, outbound.id, error)
        raise
    except Exception as error:
        await _mark_post_send_unknown(repository, outbound.id, error)
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
    ):
        self._telegram = telegram
        self._repository = repository
        self._context_cache = context_cache

    async def send(self, outbound_id: UUID) -> DeliveryResult:
        outbound = await self._repository.claim_outbound_delivery(outbound_id)
        if outbound is None:
            return DeliveryResult.SKIPPED
        return await deliver_claimed_outbound(
            self._telegram,
            self._repository,
            outbound,
            context_cache=self._context_cache,
        )
