from contextlib import asynccontextmanager
import secrets
from uuid import uuid4

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Request, Response
import redis.asyncio as redis
from redis.exceptions import RedisError

from config import (
    BOT_PAUSE_KEY,
    BOT_PAUSED_REPLY,
    DATABASE_URL,
    INPUT_TOO_LONG_REPLY,
    MAX_INPUT_LENGTH,
    NON_TEXT_REPLY,
    REDIS_URL,
    START_REPLY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET,
)
from moroz.common.db import Database
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository
from moroz.messaging.service import MessageService
from moroz.messaging.telegram import deliver_claimed_outbound
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


def create_app(
    *, database_url=None, redis_url=None, bot=None, webhook_secret=None
) -> FastAPI:
    resolved_webhook_secret = webhook_secret or TELEGRAM_WEBHOOK_SECRET

    @asynccontextmanager
    async def lifespan(webhook_app: FastAPI):
        resolved_database_url = database_url or DATABASE_URL
        if not resolved_database_url:
            raise RuntimeError("DATABASE_URL не задан")
        if bot is None and not TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
        if not resolved_webhook_secret:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET не задан")

        telegram = bot or Bot(token=TELEGRAM_BOT_TOKEN)
        database = Database(resolved_database_url, min_size=1, max_size=5)
        redis_client = redis.from_url(
            redis_url or REDIS_URL,
            decode_responses=True,
        )
        try:
            await database.connect()
            webhook_app.state.telegram = telegram
            webhook_app.state.redis = redis_client
            webhook_app.state.consent_service = ConsentService(database)
            webhook_app.state.message_repository = MessageRepository(database)
            webhook_app.state.message_service = MessageService(
                webhook_app.state.message_repository,
                MessageBuffer(redis_client, database),
                database,
            )
            yield
        finally:
            await redis_client.aclose()
            await database.close()
            await telegram.session.close()

    webhook_app = FastAPI(lifespan=lifespan)

    async def is_bot_paused() -> bool:
        try:
            return bool(await webhook_app.state.redis.get(BOT_PAUSE_KEY))
        except RedisError:
            return False

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
    async def telegram_webhook(request: Request) -> Response:
        supplied_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
        if supplied_secret is None or not secrets.compare_digest(
            supplied_secret, resolved_webhook_secret
        ):
            return Response(status_code=403)

        telegram = webhook_app.state.telegram
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": telegram})

        callback = update.callback_query
        if callback:
            if (
                callback.message is None
                or callback.message.chat.type != ChatType.PRIVATE
            ):
                return Response(status_code=200)
            if callback.data == CONSENT_CALLBACK_DATA:
                await webhook_app.state.consent_service.grant_processing_consent(
                    "telegram",
                    str(callback.from_user.id),
                    PROCESSING_CONSENT_VERSION,
                )
            return Response(status_code=200)

        message = update.message
        if not message:
            return Response(status_code=200)
        if message.chat.type != ChatType.PRIVATE:
            return Response(status_code=200)
        if await is_bot_paused():
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=BOT_PAUSED_REPLY,
                reply_kind="paused",
            )
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
        command = message.text.split(maxsplit=1)[0].split("@", 1)[0]
        if command == "/start":
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=START_REPLY,
                reply_kind="start",
            )
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
        if len(message.text) > MAX_INPUT_LENGTH:
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=INPUT_TOO_LONG_REPLY.format(limit=MAX_INPUT_LENGTH),
                reply_kind="too_long",
            )
            return Response(status_code=200)

        await webhook_app.state.message_service.accept(
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
