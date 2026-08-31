import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
import secrets
from uuid import uuid4

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Request, Response
import redis.asyncio as redis
from redis.exceptions import RedisError

from logging_config import configure_logging
from config import (
    BOT_PAUSE_KEY,
    BOT_PAUSED_REPLY,
    CONSENT_ADS_LABEL,
    CONSENT_DONE_LABEL,
    CONSENT_NEED_PII_REPLY,
    CONSENT_PII_LABEL,
    CONSENT_PROMPT,
    CONSENT_THANKS,
    DATABASE_URL,
    INPUT_TOO_LONG_REPLY,
    MAX_INPUT_LENGTH,
    MARKETING_CONSENT_CLAUSE,
    MARKETING_DISABLED_REPLY,
    MARKETING_DISABLE_LABEL,
    MARKETING_ENABLED_REPLY,
    MARKETING_ENABLE_LABEL,
    MARKETING_STATUS_REPLY,
    NON_TEXT_REPLY,
    POLICY_URL,
    REDIS_URL,
    START_REPLY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET,
)
from moroz.common.db import Database
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.ingress import decide_ingress
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository
from moroz.messaging.service import MessageService
from moroz.messaging.telegram import deliver_claimed_outbound
from moroz.privacy import deletion_marker_key
from moroz.privacy import customer_lock_subject
from moroz.reactivation.policy import is_stop_request
from moroz.reactivation.repository import ReactivationRepository
from moroz.security.consent import (
    PROCESSING_CONSENT_VERSION,
    ConsentService,
)


CONSENT_PII_CALLBACK_DATA = "consent:set:pii:on"
CONSENT_PII_CLEAR_CALLBACK_DATA = "consent:set:pii:off"
CONSENT_ADS_CALLBACK_DATA = "consent:set:ads:on"
CONSENT_ADS_CLEAR_CALLBACK_DATA = "consent:set:ads:off"
CONSENT_DONE_CALLBACK_DATA = "consent:done"
MARKETING_ENABLE_CALLBACK_DATA = "marketing:enable"
MARKETING_DISABLE_CALLBACK_DATA = "marketing:disable"
MARKETING_SOURCE = "telegram_explicit"
REACTIVATION_BOOK_CALLBACK_DATA = "reactivation:book"
REACTIVATION_ASK_CALLBACK_DATA = "reactivation:ask"
REACTIVATION_CALLBACK_REPLIES = {
    REACTIVATION_BOOK_CALLBACK_DATA: (
        "Напишите, пожалуйста, какую процедуру хотите и на какой день — "
        "помогу подобрать время."
    ),
    REACTIVATION_ASK_CALLBACK_DATA: "Напишите, пожалуйста, ваш вопрос — я помогу.",
}
_LEGACY_CONSENT_PII_CALLBACK_DATA = "consent:t:pii"
_LEGACY_CONSENT_ADS_CALLBACK_DATA = "consent:t:ads"
_CONSENT_CALLBACK_TARGETS = {
    CONSENT_PII_CALLBACK_DATA: ("pii", True),
    CONSENT_PII_CLEAR_CALLBACK_DATA: ("pii", False),
    CONSENT_ADS_CALLBACK_DATA: ("ads", True),
    CONSENT_ADS_CLEAR_CALLBACK_DATA: ("ads", False),
    _LEGACY_CONSENT_PII_CALLBACK_DATA: ("pii", True),
    _LEGACY_CONSENT_ADS_CALLBACK_DATA: ("ads", True),
}
HEALTH_TIMEOUT_SECONDS = 2.0
configure_logging()
logger = logging.getLogger(__name__)


def _consent_state_key(chat_id: int, user_id: int) -> str:
    return f"consent:state:telegram:{chat_id}:{user_id}"


def _consent_keyboard(checked: set[str] | None = None) -> InlineKeyboardMarkup:
    checked = checked or set()
    pii_box = "☑" if "pii" in checked else "☐"
    ads_box = "☑" if "ads" in checked else "☐"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{pii_box} {CONSENT_PII_LABEL}",
                    callback_data=(
                        CONSENT_PII_CLEAR_CALLBACK_DATA
                        if "pii" in checked
                        else CONSENT_PII_CALLBACK_DATA
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{ads_box} {CONSENT_ADS_LABEL}",
                    callback_data=(
                        CONSENT_ADS_CLEAR_CALLBACK_DATA
                        if "ads" in checked
                        else CONSENT_ADS_CALLBACK_DATA
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=CONSENT_DONE_LABEL,
                    callback_data=CONSENT_DONE_CALLBACK_DATA,
                )
            ],
        ]
    )


def _consent_prompt() -> str:
    return CONSENT_PROMPT.replace("{policy_url}", POLICY_URL)


def _callback_occurred_at(callback_message) -> datetime:
    # Inaccessible Telegram callback messages carry Unix epoch. Keeping that
    # stable old value is deterministic and safer than inventing handler time.
    occurred_at = getattr(callback_message, "date", None)
    return occurred_at or datetime.fromtimestamp(0, UTC)


def _marketing_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=MARKETING_ENABLE_LABEL,
                    callback_data=MARKETING_ENABLE_CALLBACK_DATA,
                ),
                InlineKeyboardButton(
                    text=MARKETING_DISABLE_LABEL,
                    callback_data=MARKETING_DISABLE_CALLBACK_DATA,
                ),
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
            webhook_app.state.database = database
            webhook_app.state.telegram = telegram
            webhook_app.state.redis = redis_client
            webhook_app.state.consent_service = ConsentService(database)
            webhook_app.state.message_repository = MessageRepository(database)
            webhook_app.state.reactivation_repository = ReactivationRepository(
                database
            )
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

    @webhook_app.get("/healthz")
    async def health():
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
                async with webhook_app.state.database.acquire() as connection:
                    await connection.fetchval("SELECT 1")
        except Exception as error:
            logger.warning(
                "health_probe_failed error_type=%s",
                type(error).__name__,
            )
            return Response(
                content='{"status":"unavailable"}',
                status_code=503,
                media_type="application/json",
            )
        return Response(
            content='{"status":"ok"}',
            media_type="application/json",
        )

    async def is_bot_paused() -> bool:
        try:
            return bool(await webhook_app.state.redis.get(BOT_PAUSE_KEY))
        except RedisError:
            return False

    async def is_customer_deletion_active(
        chat_id: int, *, fail_closed: bool = False
    ) -> bool:
        try:
            return bool(
                await webhook_app.state.redis.get(
                    deletion_marker_key("telegram", str(chat_id))
                )
            )
        except RedisError as error:
            logger.warning(
                "privacy_deletion_marker_read_failed error_type=%s",
                type(error).__name__,
            )
            return fail_closed

    async def send_static_reply(
        *,
        update_id: int,
        chat_id: int,
        text: str,
        reply_kind: str,
        delivery_options: dict[str, object] | None = None,
    ) -> None:
        repository = webhook_app.state.message_repository
        async with webhook_app.state.database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(str(chat_id)),
                )
                if await is_customer_deletion_active(
                    chat_id,
                    fail_closed=True,
                ):
                    return
                outbound_id = await repository.enqueue_outbound_in_transaction(
                    connection,
                    channel="telegram",
                    chat_id=str(chat_id),
                    text=text,
                    idempotency_key=f"telegram:{reply_kind}:{update_id}",
                    delivery_options=delivery_options,
                )
        await deliver_static_reply(outbound_id)

    async def deliver_static_reply(outbound_id) -> None:
        repository = webhook_app.state.message_repository
        outbound = await repository.claim_outbound_delivery(outbound_id)
        if outbound is None:
            return
        await deliver_claimed_outbound(
            webhook_app.state.telegram,
            repository,
            outbound,
        )

    async def consent_checked(chat_id: int, user_id: int) -> set[str]:
        raw = await webhook_app.state.redis.get(_consent_state_key(chat_id, user_id))
        return {item for item in (raw or "").split(",") if item}

    async def save_consent_checked(
        chat_id: int, user_id: int, checked: set[str]
    ) -> None:
        await webhook_app.state.redis.set(
            _consent_state_key(chat_id, user_id),
            ",".join(sorted(checked)),
            ex=3600,
        )

    async def grant_explicit_marketing(
        connection,
        *,
        user_id: str,
        source_event_id: str,
        occurred_at: datetime,
    ) -> None:
        suppressed = await connection.fetchval(
            "SELECT suppressed_at IS NOT NULL FROM marketing_consents "
            "WHERE channel = 'telegram' AND user_id = $1",
            user_id,
        )
        if suppressed:
            await webhook_app.state.consent_service.unsuppress_marketing(
                channel="telegram",
                user_id=user_id,
                proof_text=MARKETING_CONSENT_CLAUSE,
                source=MARKETING_SOURCE,
                source_event_id=source_event_id,
                occurred_at=occurred_at,
                connection=connection,
            )
        await webhook_app.state.consent_service.grant_marketing(
            channel="telegram",
            user_id=user_id,
            proof_text=MARKETING_CONSENT_CLAUSE,
            source=MARKETING_SOURCE,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
            connection=connection,
        )

    async def opt_out_marketing(
        connection,
        *,
        user_id: str,
        source_event_id: str,
        occurred_at: datetime,
    ) -> None:
        await webhook_app.state.consent_service.revoke_marketing(
            channel="telegram",
            user_id=user_id,
            source=MARKETING_SOURCE,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
            connection=connection,
        )
        await webhook_app.state.consent_service.suppress_marketing(
            channel="telegram",
            user_id=user_id,
            reason="user_stop",
            source=MARKETING_SOURCE,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
            connection=connection,
        )

    async def record_customer_inbound(
        *, chat_id: int, user_id: int, occurred_at: datetime, kind: str
    ) -> bool:
        async with webhook_app.state.database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(str(chat_id)),
                )
                if await is_customer_deletion_active(chat_id, fail_closed=True):
                    return False
                return await webhook_app.state.reactivation_repository.record_inbound(
                    "telegram",
                    str(user_id),
                    occurred_at,
                    kind,
                    connection=connection,
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
            await telegram.answer_callback_query(callback.id)
            if (
                callback.message is None
                or callback.message.chat.type != ChatType.PRIVATE
            ):
                return Response(status_code=200)
            if await is_customer_deletion_active(callback.message.chat.id):
                return Response(status_code=200)
            if callback.data in REACTIVATION_CALLBACK_REPLIES:
                await record_customer_inbound(
                    chat_id=callback.message.chat.id,
                    user_id=callback.from_user.id,
                    occurred_at=_callback_occurred_at(callback.message),
                    kind="button",
                )
                reply_kind = callback.data.replace(":", "_")
                await send_static_reply(
                    update_id=update.update_id,
                    chat_id=callback.message.chat.id,
                    text=REACTIVATION_CALLBACK_REPLIES[callback.data],
                    reply_kind=reply_kind,
                )
                return Response(status_code=200)
            if callback.data in {
                MARKETING_ENABLE_CALLBACK_DATA,
                MARKETING_DISABLE_CALLBACK_DATA,
            }:
                async with webhook_app.state.database.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                            customer_lock_subject(
                                str(callback.message.chat.id)
                            ),
                        )
                        if await is_customer_deletion_active(
                            callback.message.chat.id,
                            fail_closed=True,
                        ):
                            return Response(status_code=200)
                        event = {
                            "user_id": str(callback.from_user.id),
                            "source_event_id": str(update.update_id),
                            "occurred_at": _callback_occurred_at(
                                callback.message
                            ),
                        }
                        if callback.data == MARKETING_ENABLE_CALLBACK_DATA:
                            await grant_explicit_marketing(
                                connection, **event
                            )
                            reply = MARKETING_ENABLED_REPLY
                            reply_kind = "marketing_enabled"
                        else:
                            await webhook_app.state.reactivation_repository.record_inbound(
                                "telegram",
                                str(callback.from_user.id),
                                event["occurred_at"],
                                "marketing_disable",
                                connection=connection,
                            )
                            await opt_out_marketing(connection, **event)
                            reply = MARKETING_DISABLED_REPLY
                            reply_kind = "marketing_disabled"
                await send_static_reply(
                    update_id=update.update_id,
                    chat_id=callback.message.chat.id,
                    text=reply,
                    reply_kind=reply_kind,
                )
                return Response(status_code=200)
            target = _CONSENT_CALLBACK_TARGETS.get(callback.data)
            if target is not None:
                kind, enabled = target
                async with webhook_app.state.database.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                            customer_lock_subject(str(callback.message.chat.id)),
                        )
                        if await is_customer_deletion_active(
                            callback.message.chat.id,
                            fail_closed=True,
                        ):
                            return Response(status_code=200)
                        checked = await consent_checked(
                            callback.message.chat.id,
                            callback.from_user.id,
                        )
                        if (kind in checked) == enabled:
                            return Response(status_code=200)
                        if enabled:
                            checked.add(kind)
                        else:
                            checked.remove(kind)
                        await save_consent_checked(
                            callback.message.chat.id,
                            callback.from_user.id,
                            checked,
                        )
                await telegram.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    reply_markup=_consent_keyboard(checked),
                )
                return Response(status_code=200)
            if callback.data == CONSENT_DONE_CALLBACK_DATA:
                if await webhook_app.state.consent_service.has_processing_consent(
                    "telegram", str(callback.from_user.id)
                ):
                    return Response(status_code=200)
                checked = await consent_checked(
                    callback.message.chat.id,
                    callback.from_user.id,
                )
                if "pii" not in checked:
                    await send_static_reply(
                        update_id=update.update_id,
                        chat_id=callback.message.chat.id,
                        text=CONSENT_NEED_PII_REPLY,
                        reply_kind="consent_need_pii",
                    )
                    return Response(status_code=200)
                outbound_id = None
                async with webhook_app.state.database.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                            customer_lock_subject(str(callback.message.chat.id)),
                        )
                        if await is_customer_deletion_active(
                            callback.message.chat.id,
                            fail_closed=True,
                        ):
                            return Response(status_code=200)
                        if await connection.fetchval(
                            "SELECT EXISTS (SELECT 1 FROM processing_consents "
                            "WHERE channel = 'telegram' AND user_id = $1 "
                            "AND consent_version = $2)",
                            str(callback.from_user.id),
                            PROCESSING_CONSENT_VERSION,
                        ):
                            return Response(status_code=200)
                        await webhook_app.state.consent_service.grant_processing_consent(
                            "telegram",
                            str(callback.from_user.id),
                            PROCESSING_CONSENT_VERSION,
                            connection=connection,
                        )
                        if "ads" in checked:
                            await grant_explicit_marketing(
                                connection,
                                user_id=str(callback.from_user.id),
                                source_event_id=str(update.update_id),
                                occurred_at=_callback_occurred_at(
                                    callback.message
                                ),
                            )
                        await webhook_app.state.redis.delete(
                            _consent_state_key(
                                callback.message.chat.id,
                                callback.from_user.id,
                            )
                        )
                        outbound_id = await webhook_app.state.message_repository.enqueue_outbound_in_transaction(
                            connection,
                            channel="telegram",
                            chat_id=str(callback.message.chat.id),
                            text=CONSENT_THANKS,
                            idempotency_key=(
                                f"telegram:consent_thanks:{update.update_id}"
                            ),
                            delivery_options=None,
                        )
                if outbound_id is not None:
                    await deliver_static_reply(outbound_id)
            return Response(status_code=200)

        message = update.message
        if not message:
            return Response(status_code=200)
        if message.chat.type != ChatType.PRIVATE:
            return Response(status_code=200)
        if await is_customer_deletion_active(message.chat.id):
            return Response(status_code=200)
        if message.from_user is not None:
            await record_customer_inbound(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                occurred_at=message.date,
                kind=(
                    "stop"
                    if message.text is not None and is_stop_request(message.text)
                    else "message"
                ),
            )
        if (
            message.from_user is not None
            and message.text is not None
            and is_stop_request(message.text)
        ):
            async with webhook_app.state.database.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        customer_lock_subject(str(message.chat.id)),
                    )
                    if await is_customer_deletion_active(
                        message.chat.id,
                        fail_closed=True,
                    ):
                        return Response(status_code=200)
                    await opt_out_marketing(
                        connection,
                        user_id=str(message.from_user.id),
                        source_event_id=str(update.update_id),
                        occurred_at=message.date,
                    )
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=MARKETING_DISABLED_REPLY,
                reply_kind="marketing_disabled",
            )
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
            ingress = decide_ingress(
                has_text=False,
                has_processing_consent=False,
            )
            if ingress.action == "reply" and ingress.code == "nontext":
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
        if command == "/marketing":
            state = await webhook_app.state.consent_service.get_marketing_status(
                "telegram", str(message.from_user.id)
            )
            status = (
                MARKETING_ENABLED_REPLY
                if state.active
                else MARKETING_DISABLED_REPLY
            )
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=(
                    f"{MARKETING_STATUS_REPLY}\n\n"
                    f"{MARKETING_CONSENT_CLAUSE}\n\n{status}"
                ),
                reply_kind="marketing_status",
                delivery_options={
                    "reply_markup": _marketing_keyboard().model_dump(
                        mode="json"
                    )
                },
            )
            return Response(status_code=200)
        if command == "/start":
            await send_static_reply(
                update_id=update.update_id,
                chat_id=message.chat.id,
                text=START_REPLY,
                reply_kind="start",
            )
            return Response(status_code=200)

        user_id = str(message.from_user.id)
        has_processing_consent = (
            await webhook_app.state.consent_service.has_processing_consent(
                "telegram", user_id
            )
        )
        ingress = decide_ingress(
            has_text=True,
            has_processing_consent=has_processing_consent,
        )
        if ingress.action == "reply":
            if ingress.code == "consent_required":
                await send_static_reply(
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    text=_consent_prompt(),
                    reply_kind="consent_prompt",
                    delivery_options={
                        "parse_mode": "HTML",
                        "reply_markup": _consent_keyboard().model_dump(
                            mode="json"
                        ),
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

        accepted = await webhook_app.state.message_service.accept_consented(
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
        if accepted:
            try:
                await telegram.send_chat_action(
                    chat_id=message.chat.id,
                    action="typing",
                )
            except Exception as error:
                logger.warning(
                    "telegram_typing_failed error_type=%s",
                    type(error).__name__,
                )
        return Response(status_code=200)

    return webhook_app


app = create_app()
