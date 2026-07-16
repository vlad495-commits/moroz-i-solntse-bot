import asyncio
import logging
from enum import StrEnum
from uuid import UUID

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
) -> DeliveryResult:
    send_arguments = {"chat_id": int(outbound.chat_id), "text": outbound.text}
    reply_markup = outbound.delivery_options.get("reply_markup")
    if reply_markup is not None:
        send_arguments["reply_markup"] = InlineKeyboardMarkup.model_validate(
            reply_markup
        )
    try:
        sent_message = await telegram.send_message(**send_arguments)
    except asyncio.CancelledError:
        await repository.mark_outbound_delivery_unknown(outbound.id)
        logger.error(
            "telegram_delivery_unknown outbound_id=%s error_type=CancelledError",
            outbound.id,
        )
        raise
    except Exception as error:
        await repository.mark_outbound_delivery_unknown(outbound.id)
        logger.error(
            "telegram_delivery_unknown outbound_id=%s error_type=%s",
            outbound.id,
            type(error).__name__,
        )
        return DeliveryResult.DELIVERY_UNKNOWN
    await repository.mark_outbound_sent(outbound.id, str(sent_message.message_id))
    return DeliveryResult.SENT


class TelegramSender:
    def __init__(self, telegram, repository: MessageRepository):
        self._telegram = telegram
        self._repository = repository

    async def send(self, outbound_id: UUID) -> DeliveryResult:
        outbound = await self._repository.claim_outbound_delivery(outbound_id)
        if outbound is None:
            return DeliveryResult.SKIPPED
        return await deliver_claimed_outbound(
            self._telegram,
            self._repository,
            outbound,
        )
