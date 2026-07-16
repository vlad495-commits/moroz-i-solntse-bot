from contextlib import asynccontextmanager
from uuid import uuid4

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Response

from config import DATABASE_URL, TELEGRAM_BOT_TOKEN
from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage
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
        if not message or message.text is None or message.from_user is None:
            return Response(status_code=200)

        user_id = str(message.from_user.id)
        if not await webhook_app.state.consent_service.has_processing_consent(
            "telegram", user_id
        ):
            await telegram.send_message(
                chat_id=message.chat.id,
                text=CONSENT_PROMPT,
                reply_markup=CONSENT_KEYBOARD,
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
