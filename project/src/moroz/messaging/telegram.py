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


async def deliver_claimed_outbound(
    telegram,
    repository: MessageRepository,
    outbound: OutboundMessage,
    *,
    context_cache=None,
) -> DeliveryResult:
    try:
        send_arguments = {
            "chat_id": int(outbound.chat_id),
            "text": outbound.text,
        }
        reply_markup = outbound.delivery_options.get("reply_markup")
        if reply_markup is not None:
            send_arguments["reply_markup"] = (
                InlineKeyboardMarkup.model_validate(reply_markup)
            )
        parse_mode = outbound.delivery_options.get("parse_mode")
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
    context_chat_id = await repository.mark_outbound_sent(
        outbound.id,
        str(sent_message.message_id),
    )
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
