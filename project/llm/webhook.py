from contextlib import asynccontextmanager
from uuid import uuid4

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Response

from config import DATABASE_URL, NON_TEXT_REPLY, TELEGRAM_BOT_TOKEN
from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage, OutboundMessage
from moroz.messaging.repository import MessageRepository
from moroz.security.consent import (
    PROCESSING_CONSENT_VERSION,
    ConsentService,
)


CONSENT_CALLBACK_DATA = f"processing_consent:{PROCESSING_CONSENT_VERSION}"
CONSENT_PROMPT = "Чтобы продолжить, подтвердите обработку данных."
CONSENT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Подтвердить",
                callback_data=CONSENT_CALLBACK_DATA,
            )
        ]
    ]
)


async def deliver_claimed_outbound(
    telegram,
    repository: MessageRepository,
    outbound: OutboundMessage,
) -> None:
    send_arguments = {
        "chat_id": int(outbound.chat_id),
        "text": outbound.text,
    }
    reply_markup = outbound.delivery_options.get("reply_markup")
    if reply_markup is not None:
        send_arguments["reply_markup"] = InlineKeyboardMarkup.model_validate(
            reply_markup
        )
    try:
        sent_message = await telegram.send_message(**send_arguments)
    except Exception:
        await repository.mark_outbound_delivery_unknown(outbound.id)
        return
    await repository.mark_outbound_sent(
        outbound.id,
        str(sent_message.message_id),
    )


def create_app(*, database_url=None, bot=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(webhook_app: FastAPI):
        resolved_database_url = database_url or DATABASE_URL
        if not resolved_database_url:
            raise RuntimeError("DATABASE_URL не задан")
        if bot is None and not TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

        telegram = bot or Bot(token=TELEGRAM_BOT_TOKEN)
        database = Database(resolved_database_url, min_size=1, max_size=5)
        try:
            await database.connect()
            webhook_app.state.telegram = telegram
            webhook_app.state.consent_service = ConsentService(database)
            webhook_app.state.message_repository = MessageRepository(database)
            yield
        finally:
            await database.close()
            await telegram.session.close()

    webhook_app = FastAPI(lifespan=lifespan)

    async def send_static_reply(
        *,
        update_id: int,
        chat_id: int,
        text: str,
        reply_kind: str,
        delivery_options: dict[str, object] | None = None,
    ) -> None:
        repository = webhook_app.state.message_repository
        outbound_id = await repository.enqueue_outbound(
            channel="telegram",
            chat_id=str(chat_id),
            text=text,
            idempotency_key=f"telegram:{reply_kind}:{update_id}",
            delivery_options=delivery_options,
        )
        outbound = await repository.claim_outbound_delivery(outbound_id)
        if outbound is None:
            return
        await deliver_claimed_outbound(
            webhook_app.state.telegram,
            repository,
            outbound,
        )

    @webhook_app.post("/telegram/webhook")
    async def telegram_webhook(payload: dict) -> Response:
        telegram = webhook_app.state.telegram
        update = Update.model_validate(payload, context={"bot": telegram})

        callback = update.callback_query
        if callback and callback.data == CONSENT_CALLBACK_DATA:
            await webhook_app.state.consent_service.grant_processing_consent(
                "telegram",
                str(callback.from_user.id),
                PROCESSING_CONSENT_VERSION,
            )
            return Response(status_code=200)

        message = update.message
        if not message:
            return Response(status_code=200)
        if message.text is None:
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=NON_TEXT_REPLY,
                reply_kind="non_text",
            )
            return Response(status_code=200)
        if message.from_user is None:
            return Response(status_code=200)

        user_id = str(message.from_user.id)
        if not await webhook_app.state.consent_service.has_processing_consent(
            "telegram", user_id
        ):
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=CONSENT_PROMPT,
                reply_kind="consent_prompt",
                delivery_options={
                    "reply_markup": CONSENT_KEYBOARD.model_dump(mode="json")
                },
            )
            return Response(status_code=200)

        await webhook_app.state.message_repository.accept(
            IncomingMessage(
                update_id=str(update.update_id),
                message_id=str(message.message_id),
                channel="telegram",
                chat_id=str(message.chat.id),
                user_id=user_id,
                text=message.text,
                received_at=message.date,
                correlation_id=uuid4(),
            )
        )
        return Response(status_code=200)

    return webhook_app


app = create_app()
