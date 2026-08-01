import asyncio
import json
import logging
import os
import signal
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot
import redis.asyncio as redis
from redis.exceptions import RedisError

from config import (
    BOOKING_CONFIRMATION_TTL_SECONDS,
    BOOKING_HORIZON_DAYS,
    BOOKING_INTERACTIONS_ENABLED,
    BOOKING_MODE,
    CONTEXT_MESSAGES_LIMIT,
    YCLIENTS_SERVICE_ALLOWLIST,
    YCLIENTS_STAFF_ALLOWLIST,
)
from llm import (
    generate_response,
    init_llm,
    prompt_reload_listener,
    route_intent,
)
from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.dispatcher import DispatchResult, MessageDispatcher
from moroz.booking.interaction import BookingOwner, Interaction, WorkflowReply
from moroz.booking.mock_catalog import MockBookingCatalog
from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import Slot, SlotQuery
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.workflow import BookingWorkflow
from moroz.booking.workflow_repository import BookingWorkflowRepository
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_catalog import YclientsCatalogAdapter
from moroz.booking.yclients_http import YclientsConfig, YclientsHttpClient
from moroz.common.config import database_url_from_env
from moroz.common.db import Database
from moroz.common.queue import MAX_RETRIES, QueueTask, RabbitQueue
from moroz.messaging.buffer import BUFFER_TTL_SECONDS, MessageBuffer
from moroz.messaging.outbox import OutboxRelay, process_message_key
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.notifications.feedback import FeedbackService
from moroz.notifications.handlers import handle_scheduler_job
from moroz.notifications.lifecycle import LifecycleService
from moroz.notifications.ports import LocalBookingPort, NotificationOutbox
from moroz.notifications.repository import SchedulerJobRepository
from moroz.security.consent import PROCESSING_CONSENT_VERSION


logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")
READINESS_PATH = Path("/tmp/worker-ready")
PUMP_INTERVAL_SECONDS = 0.5
REDIS_RETRY_INTERVAL_SECONDS = 5.0
SUPERVISOR_SHUTDOWN_TIMEOUT_SECONDS = 24.0
WORKER_LOCK_NAME = "moroz:worker:singleton"
_PERSISTED_PAYLOAD_FIELDS = {
    "update_id",
    "message_id",
    "channel",
    "chat_id",
    "user_id",
    "text",
    "received_at",
    "correlation_id",
    "kind",
    "data",
}
_STRUCTURED_REJECTED_TEXT = (
    "Не удалось обработать действие. Нажмите «Записаться» и попробуйте снова."
)


async def handle(task: QueueTask) -> None:
    logger.error("No worker task handler is registered; task will be retried")
    raise NotImplementedError("No worker task handlers are registered")


def _require_text_payloads(payloads: list[dict[str, object]]) -> None:
    if any(payload.get("kind", "text") != "text" for payload in payloads):
        raise RuntimeError(
            "non-text interaction requires structured dispatcher"
        )


def _invalid_interaction() -> ValueError:
    return ValueError("persisted interaction is invalid")


def _private_telegram_id(value: object) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise _invalid_interaction()
    parsed = int(value)
    if parsed <= 0 or str(parsed) != value:
        raise _invalid_interaction()
    return value


def _normalize_persisted_interaction(
    payloads: list[dict[str, object]],
    idempotency_key: str,
    *,
    processing_consent: bool,
) -> Interaction:
    if not payloads or not idempotency_key:
        raise _invalid_interaction()
    for payload in payloads:
        if set(payload) != _PERSISTED_PAYLOAD_FIELDS:
            raise _invalid_interaction()
        if payload.get("channel") != "telegram":
            raise _invalid_interaction()
        for field in (
            "update_id",
            "message_id",
            "text",
            "received_at",
            "correlation_id",
        ):
            if not isinstance(payload.get(field), str):
                raise _invalid_interaction()
        try:
            received_at = datetime.fromisoformat(payload["received_at"])
            UUID(payload["correlation_id"])
        except (TypeError, ValueError):
            raise _invalid_interaction() from None
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise _invalid_interaction()
    chat_ids = {_private_telegram_id(item.get("chat_id")) for item in payloads}
    user_ids = {_private_telegram_id(item.get("user_id")) for item in payloads}
    if len(chat_ids) != 1 or chat_ids != user_ids:
        raise _invalid_interaction()
    owner_id = next(iter(chat_ids))
    owner = BookingOwner("telegram", owner_id, owner_id)
    kinds = {item.get("kind") for item in payloads}
    if kinds == {"text"}:
        if any(item.get("data") != {} for item in payloads):
            raise _invalid_interaction()
        return Interaction.text(
            owner,
            idempotency_key,
            "\n".join(str(item["text"]) for item in payloads),
        )
    if len(payloads) != 1 or len(kinds) != 1:
        raise _invalid_interaction()
    payload = payloads[0]
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _invalid_interaction()
    if kinds == {"callback"}:
        if set(data) != {"callback_data"} or not isinstance(
            data.get("callback_data"), str
        ):
            raise _invalid_interaction()
        return Interaction.callback(
            owner,
            idempotency_key,
            data["callback_data"],
        )
    if kinds == {"contact"}:
        if (
            set(data) != {"phone_number"}
            or not isinstance(data.get("phone_number"), str)
            or not data["phone_number"]
            or processing_consent is not True
        ):
            raise _invalid_interaction()
        return Interaction.contact(
            owner,
            idempotency_key,
            contact_user_id=owner_id,
            phone_number=data["phone_number"],
            personal_data_processing_allowed=True,
        )
    raise _invalid_interaction()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _RuntimeMockYclientsAdapter(MockYclientsAdapter):
    def __init__(
        self,
        slot_templates: list[Slot],
        service_allowlist: tuple[str, ...],
    ) -> None:
        super().__init__([])
        self._slot_templates = tuple(slot_templates)
        self._service_allowlist = frozenset(service_allowlist)

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        selected = tuple(query.service_ids)
        if (
            not selected
            or len(selected) != len(set(selected))
            or not set(selected).issubset(self._service_allowlist)
        ):
            return []
        selection_key = sha256(",".join(selected).encode()).hexdigest()[:10]
        slots = []
        for template in self._slot_templates:
            if (
                template.starts_at < query.starts_after
                or (
                    query.starts_before is not None
                    and template.starts_at >= query.starts_before
                )
                or (
                    query.staff_id is not None
                    and template.staff_id != query.staff_id
                )
            ):
                continue
            slot = Slot(
                f"{template.id}-{selection_key}",
                selected,
                template.staff_id,
                template.starts_at,
                30 * len(selected),
            )
            self._slots[slot.id] = slot
            if not self._is_occupied(slot.id):
                slots.append(slot)
        return slots


def _mock_booking_adapters(
    service_allowlist: tuple[str, ...],
    staff_allowlist: tuple[str, ...],
    *,
    now: datetime,
):
    services = tuple(
        CatalogService(value, f"Услуга {index}", 30)
        for index, value in enumerate(service_allowlist, 1)
    )
    staff = tuple(
        CatalogStaff(value, f"Мастер {index}", service_allowlist)
        for index, value in enumerate(staff_allowlist, 1)
    )
    timezone = ZoneInfo("Europe/Moscow")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("booking mock clock must be timezone-aware")
    local_now = now.astimezone(timezone)
    slots = []
    for day in range(1, 15):
        slot_date = local_now.date() + timedelta(days=day)
        for index, member in enumerate(staff):
            starts_at = datetime.combine(
                slot_date,
                time(10 + index % 8, 0),
                tzinfo=timezone,
            ).astimezone(UTC)
            slots.append(
                Slot(
                    f"mock-{day}-{member.id}",
                    service_allowlist,
                    member.id,
                    starts_at,
                    30 * len(service_allowlist),
                )
            )
    return (
        MockBookingCatalog(
            services,
            staff,
            service_allowlist,
            staff_allowlist,
        ),
        _RuntimeMockYclientsAdapter(slots, service_allowlist),
        timezone,
    )


def _build_booking_dispatcher(
    database: Database,
    *,
    enabled: bool,
    mode: str,
    service_allowlist: tuple[str, ...],
    staff_allowlist: tuple[str, ...],
    env,
    now: Callable[[], datetime] = _utc_now,
) -> MessageDispatcher | None:
    if not enabled:
        return None
    if mode == "disabled":
        raise RuntimeError("booking mode must be ready when interactions are enabled")
    if not service_allowlist or not staff_allowlist:
        raise ValueError("booking allowlists are incomplete")
    staff_chat_id = str(env.get("STAFF_TELEGRAM_CHAT_ID", "")).strip()
    if not staff_chat_id:
        raise ValueError("staff Telegram chat is not configured")
    if mode == "mock":
        catalog, booking_port, timezone = _mock_booking_adapters(
            service_allowlist,
            staff_allowlist,
            now=now(),
        )
    elif mode == "real":
        required = (
            "YCLIENTS_PARTNER_TOKEN",
            "YCLIENTS_USER_TOKEN",
            "YCLIENTS_COMPANY_ID",
        )
        if any(not str(env.get(name, "")).strip() for name in required):
            raise ValueError("YCLIENTS booking configuration is incomplete")
        config = YclientsConfig.from_env(env)
        http = YclientsHttpClient(config)
        catalog = YclientsCatalogAdapter(
            http,
            str(config.company_id),
            service_allowlist,
            staff_allowlist,
        )
        booking_port = YclientsAdapter(config, http=http)
        timezone = ZoneInfo(config.timezone_name)
    else:
        raise ValueError("booking mode is invalid")
    workflow_repository = BookingWorkflowRepository(database)
    workflow = BookingWorkflow(
        catalog,
        booking_port,
        workflow_repository,
        BookingService(
            booking_port,
            BookingRepository(
                database,
                staff_chat_id=staff_chat_id,
            ),
        ),
        now=now,
        timezone=timezone,
        horizon_days=BOOKING_HORIZON_DAYS,
        confirmation_ttl_seconds=BOOKING_CONFIRMATION_TTL_SECONDS,
    )
    return MessageDispatcher(
        workflow_repository,
        workflow,
        router=route_intent,
        consultant=generate_response,
    )


async def _has_processing_consent(connection, user_id: str) -> bool:
    return bool(
        await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM processing_consents
                WHERE channel = 'telegram'
                  AND user_id = $1
                  AND consent_version = $2
            )
            """,
            user_id,
            PROCESSING_CONSENT_VERSION,
        )
    )


async def _reject_structured_in_transaction(
    connection,
    repository: MessageRepository,
    *,
    chat_id: str,
    accepted,
    reply_key: str,
) -> None:
    await repository.enqueue_outbound_in_transaction(
        connection,
        channel="telegram",
        chat_id=chat_id,
        text=_STRUCTURED_REJECTED_TEXT,
        idempotency_key=reply_key,
        delivery_options={
            "reply_markup": {
                "keyboard": [[{"text": "Записаться"}]],
                "resize_keyboard": True,
            }
        },
    )
    await connection.execute(
        """
        UPDATE message_inbox
        SET status = 'processed',
            payload = jsonb_build_object(
                'update_id', external_message_id,
                'channel', channel,
                'chat_id', chat_id,
                'kind', 'rejected',
                'data', jsonb_build_object('rejected', true)
            )
        WHERE channel = 'telegram'
          AND external_message_id = ANY($1::text[])
        """,
        [row["external_message_id"] for row in accepted],
    )


def _safe_interaction_history(interaction: Interaction) -> str:
    if interaction.kind == "callback":
        return "[Действие в сценарии записи]"
    if interaction.kind == "contact":
        return "[Контакт передан через Telegram]"
    return interaction.text_value or ""


def _ensure_contact_reply_is_safe(
    interaction: Interaction,
    reply: WorkflowReply,
) -> None:
    if interaction.kind != "contact":
        return
    phone = interaction.phone_number or ""
    material = reply.text + json.dumps(
        reply.delivery_options,
        ensure_ascii=False,
    )
    if phone and phone in material:
        raise RuntimeError("contact data escaped workflow state")


class MessageTaskHandler:
    def __init__(
        self,
        database: Database,
        llm,
        telegram: TelegramSender,
        *,
        scheduler_repository: SchedulerJobRepository | None = None,
        booking_port=None,
        notification_outbox=None,
        lifecycle=None,
        dispatcher: MessageDispatcher | None = None,
        booking_interactions_enabled: bool = False,
        scheduler_handler=handle_scheduler_job,
    ):
        if booking_interactions_enabled and dispatcher is None:
            raise RuntimeError("booking dispatcher is required when enabled")
        self._database = database
        self._llm = llm
        self._telegram = telegram
        self._repository = MessageRepository(database)
        self._scheduler_repository = scheduler_repository
        self._booking_port = booking_port
        self._notification_outbox = notification_outbox
        self._lifecycle = lifecycle
        self._dispatcher = dispatcher
        self._booking_interactions_enabled = booking_interactions_enabled
        self._scheduler_handler = scheduler_handler

    async def handle(self, task: QueueTask) -> None:
        if task.kind == "process_message":
            await self._process_message(task)
            return
        if task.kind == "send_outbound":
            outbound_id = task.payload.get("outbound_id")
            if not isinstance(outbound_id, str):
                raise ValueError("send_outbound requires outbound_id")
            await self._telegram.send(UUID(outbound_id))
            return
        if task.kind == "scheduler_job":
            await self._process_scheduler_job(task)
            return
        logger.error("Unsupported worker task kind")
        raise NotImplementedError("Unsupported worker task")

    async def _process_scheduler_job(self, task: QueueTask) -> None:
        raw_job_id = task.payload.get("job_id")
        if not isinstance(raw_job_id, str):
            raise ValueError("scheduler_job requires job_id")
        job_id = UUID(raw_job_id)
        if task.idempotency_key != f"scheduler_job:{job_id}":
            raise ValueError("scheduler_job idempotency key does not match job_id")
        if (
            self._scheduler_repository is None
            or self._booking_port is None
            or self._notification_outbox is None
        ):
            raise RuntimeError("scheduler job dependencies are not configured")
        job = await self._scheduler_repository.get_claimed(job_id)
        if job is None:
            return
        try:
            result = await self._scheduler_handler(
                job,
                booking_port=self._booking_port,
                outbox=self._notification_outbox,
                lifecycle=self._lifecycle,
            )
        except Exception as error:
            await self._scheduler_repository.record_failure(
                job,
                error_code=type(error).__name__,
                terminal=job.attempts >= MAX_RETRIES,
            )
            raise
        await self._scheduler_repository.complete(job, result)

    async def _process_message(self, task: QueueTask) -> None:
        chat_id = task.payload.get("chat_id")
        update_ids = task.payload.get("update_ids")
        if (
            not isinstance(chat_id, str)
            or not isinstance(update_ids, list)
            or not update_ids
            or any(not isinstance(value, str) for value in update_ids)
            or len(set(update_ids)) != len(update_ids)
        ):
            raise ValueError("invalid process_message payload")
        if task.idempotency_key != process_message_key(update_ids):
            raise ValueError("process_message idempotency key does not match updates")
        numeric_chat_id = int(chat_id)
        reply_key = f"reply:{task.idempotency_key}"

        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    chat_id,
                )
                inbox_rows = await connection.fetch(
                    """
                    SELECT external_message_id, payload, status, ingress_sequence
                    FROM message_inbox
                    WHERE channel = 'telegram'
                      AND chat_id = $1
                      AND (
                          status = 'accepted'
                          OR external_message_id = ANY($2::text[])
                      )
                    ORDER BY ingress_sequence
                    FOR UPDATE
                    """,
                    chat_id,
                    update_ids,
                )
                requested = {
                    row["external_message_id"]: row
                    for row in inbox_rows
                    if row["external_message_id"] in update_ids
                }
                if len(requested) != len(update_ids):
                    raise ValueError("process_message inbox rows are missing")
                if [
                    row["external_message_id"]
                    for row in inbox_rows
                    if row["external_message_id"] in requested
                ] != update_ids:
                    raise ValueError(
                        "process_message update ids are outside ingress order"
                    )
                if any(
                    row["status"] not in {"accepted", "processed"}
                    for row in requested.values()
                ):
                    raise ValueError("process_message inbox status is invalid")

                accepted = [
                    row
                    for row in inbox_rows
                    if row["status"] == "accepted"
                    and row["external_message_id"] in requested
                ]
                if not accepted:
                    return
                all_accepted = [
                    row for row in inbox_rows if row["status"] == "accepted"
                ]
                if [
                    row["external_message_id"]
                    for row in all_accepted[: len(accepted)]
                ] != [row["external_message_id"] for row in accepted]:
                    raise ValueError(
                        "process_message has an earlier accepted inbox row"
                    )

                payloads = []
                for row in accepted:
                    payload = row["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    if (
                        not isinstance(payload, dict)
                        or payload.get("update_id") != row["external_message_id"]
                        or payload.get("chat_id") != chat_id
                        or not isinstance(payload.get("text"), str)
                        or not isinstance(payload.get("user_id"), str)
                    ):
                        raise ValueError("process_message persisted payload is invalid")
                    payloads.append(payload)
                interaction = None
                if self._booking_interactions_enabled:
                    processing_consent = False
                    if (
                        len(payloads) == 1
                        and payloads[0].get("kind") == "contact"
                        and isinstance(payloads[0].get("user_id"), str)
                    ):
                        processing_consent = await _has_processing_consent(
                            connection,
                            payloads[0]["user_id"],
                        )
                    try:
                        interaction = _normalize_persisted_interaction(
                            payloads,
                            task.idempotency_key,
                            processing_consent=processing_consent,
                        )
                    except ValueError:
                        await _reject_structured_in_transaction(
                            connection,
                            self._repository,
                            chat_id=chat_id,
                            accepted=accepted,
                            reply_key=reply_key,
                        )
                        return
                    user_id = int(interaction.owner.customer_id)
                    persisted_text = _safe_interaction_history(interaction)
                else:
                    try:
                        _require_text_payloads(payloads)
                    except RuntimeError:
                        await _reject_structured_in_transaction(
                            connection,
                            self._repository,
                            chat_id=chat_id,
                            accepted=accepted,
                            reply_key=reply_key,
                        )
                        return
                    user_ids = {payload["user_id"] for payload in payloads}
                    if len(user_ids) != 1:
                        raise ValueError("process_message spans multiple users")
                    user_id = int(user_ids.pop())
                    persisted_text = "\n".join(
                        payload["text"] for payload in payloads
                    )

                rows = await connection.fetch(
                    """
                    SELECT role, content
                    FROM messages
                    WHERE chat_id = $1
                    ORDER BY created_at DESC, id DESC
                    LIMIT $2
                    """,
                    numeric_chat_id,
                    CONTEXT_MESSAGES_LIMIT,
                )
                context = [
                    {"role": row["role"], "content": row["content"]}
                    for row in reversed(rows)
                ]
                recent_message_count = await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM message_inbox
                    WHERE channel = 'telegram'
                      AND chat_id = $1
                      AND created_at >= now() - interval '1 minute'
                    """,
                    chat_id,
                )
                if interaction is None:
                    llm_result = await self._llm(
                        persisted_text,
                        context,
                        recent_message_count=int(recent_message_count),
                    )
                    result = DispatchResult(
                        WorkflowReply(llm_result.text, {}),
                        usage=llm_result,
                    )
                else:
                    result = await self._dispatcher.dispatch(
                        interaction,
                        context,
                        int(recent_message_count),
                    )
                    _ensure_contact_reply_is_safe(interaction, result.reply)

                await connection.execute(
                    """
                    INSERT INTO messages (chat_id, user_id, role, content)
                    VALUES ($1, $2, 'user', $3),
                           ($1, $2, 'assistant', $4)
                    """,
                    numeric_chat_id,
                    user_id,
                    persisted_text,
                    result.reply.text,
                )
                if result.usage is not None:
                    await connection.execute(
                        """
                        INSERT INTO token_usage
                            (chat_id, user_id, prompt_tokens, completion_tokens,
                             cached_tokens, total_tokens, model)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        numeric_chat_id,
                        user_id,
                        result.usage.prompt_tokens,
                        result.usage.completion_tokens,
                        result.usage.cached_tokens,
                        result.usage.total_tokens,
                        result.usage.model,
                    )
                await self._repository.enqueue_outbound_in_transaction(
                    connection,
                    channel="telegram",
                    chat_id=chat_id,
                    text=result.reply.text,
                    idempotency_key=reply_key,
                    delivery_options=result.reply.delivery_options,
                )
                if interaction is not None and interaction.kind == "contact":
                    await connection.execute(
                        """
                        UPDATE message_inbox
                        SET payload = jsonb_set(
                            payload,
                            '{data}',
                            '{"contact_shared": true}'::jsonb,
                            true
                        )
                        WHERE channel = 'telegram'
                          AND external_message_id = ANY($1::text[])
                        """,
                        [row["external_message_id"] for row in accepted],
                    )
                await connection.execute(
                    """
                    UPDATE message_inbox
                    SET status = 'processed'
                    WHERE channel = 'telegram'
                      AND external_message_id = ANY($1::text[])
                    """,
                    [row["external_message_id"] for row in accepted],
                )


class PipelinePump:
    def __init__(
        self,
        buffer: MessageBuffer,
        relay: OutboxRelay,
        repository: MessageRepository,
    ):
        self._buffer = buffer
        self._relay = relay
        self._repository = repository
        self._redis_available = True

    async def run_once(self) -> int:
        try:
            for chat_id in await self._buffer.due_chat_ids():
                await self._buffer.flush(chat_id)
            self._redis_available = True
        except RedisError as error:
            self._redis_available = False
            logger.warning(
                "pipeline_buffer_unavailable error_type=%s",
                type(error).__name__,
            )
        await self._repository.enqueue_stale_accepted_messages(
            older_than_seconds=BUFFER_TTL_SECONDS
        )
        return await self._relay.publish_pending()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=PUMP_INTERVAL_SECONDS
                    if self._redis_available
                    else REDIS_RETRY_INTERVAL_SECONDS
                )
            except TimeoutError:
                pass


def _remove_readiness(path: Path) -> None:
    path.unlink(missing_ok=True)


def _publish_readiness(path: Path, active: bool) -> None:
    if active:
        path.write_text("ready", encoding="utf-8")
    else:
        _remove_readiness(path)


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _raise_after_cleanup(
    primary_error: BaseException | None,
    results,
) -> None:
    cleanup_error = None
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(
                "cleanup_failed error_type=%s", type(result).__name__
            )
            if cleanup_error is None:
                cleanup_error = result
    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error


async def _cleanup_all(
    *operations,
    primary_error: BaseException | None = None,
    prior_results=(),
    deadline: float | None = None,
) -> None:
    tasks = tuple(asyncio.create_task(operation) for operation in operations)
    if deadline is None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        _raise_after_cleanup(primary_error, (*prior_results, *results))
        return
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    results = []
    for task in tasks:
        if task not in done:
            continue
        try:
            results.append(task.result())
        except BaseException as error:
            results.append(error)
    if pending:
        for task in pending:
            task.cancel()
            task.add_done_callback(_consume_task_result)
        results.append(TimeoutError("resource cleanup exceeded shutdown deadline"))
    _raise_after_cleanup(primary_error, (*prior_results, *results))


async def _acquire_worker_lock(database: Database):
    context = database.acquire()
    connection = await context.__aenter__()
    try:
        acquired = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
            WORKER_LOCK_NAME,
        )
        if not acquired:
            raise RuntimeError("another worker is already active")
    except BaseException:
        await context.__aexit__(None, None, None)
        raise
    return context, connection


async def _release_worker_lock(lock) -> None:
    context, connection = lock
    try:
        await connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            WORKER_LOCK_NAME,
        )
    finally:
        await context.__aexit__(None, None, None)


class ShutdownBudget:
    def __init__(self):
        self._deadline = None

    def deadline(self) -> float:
        if self._deadline is None:
            self._deadline = (
                asyncio.get_running_loop().time()
                + SUPERVISOR_SHUTDOWN_TIMEOUT_SECONDS
            )
        return self._deadline


async def _stop_background_tasks(
    tasks: tuple[asyncio.Task, ...],
    deadline: float,
) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    for task in done:
        _consume_task_result(task)
    for task in pending:
        task.add_done_callback(_consume_task_result)


async def _close_queue_before(queue: RabbitQueue, deadline: float) -> None:
    task = asyncio.create_task(queue.close())
    await asyncio.sleep(0)
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    done, pending = await asyncio.wait((task,), timeout=remaining)
    if done:
        task.result()
    else:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise TimeoutError("queue close exceeded supervisor deadline")


async def _supervise(
    queue: RabbitQueue,
    stop: asyncio.Event,
    readiness_path: Path = READINESS_PATH,
    *,
    handler=handle,
    pump: PipelinePump | None = None,
    prompt_listener=None,
    shutdown_budget: ShutdownBudget | None = None,
) -> None:
    shutdown_budget = shutdown_budget or ShutdownBudget()
    _remove_readiness(readiness_path)
    consumer = asyncio.create_task(
        queue.consume(
            handler,
            readiness=lambda active: _publish_readiness(readiness_path, active),
        )
    )
    waiter = asyncio.create_task(stop.wait())
    pump_task = asyncio.create_task(pump.run(stop)) if pump else None
    prompt_task = (
        asyncio.create_task(prompt_listener()) if prompt_listener else None
    )
    primary_error = None
    try:
        watched = {consumer, waiter}
        if pump_task:
            watched.add(pump_task)
        if prompt_task:
            watched.add(prompt_task)
        done, _ = await asyncio.wait(
            watched,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if waiter in done:
            pass
        elif consumer in done:
            await consumer
            raise RuntimeError("Consumer stopped unexpectedly")
        elif pump_task in done:
            await pump_task
            raise RuntimeError("Pipeline pump stopped unexpectedly")
        elif prompt_task in done:
            await prompt_task
            raise RuntimeError("Prompt reload listener stopped unexpectedly")
    except BaseException as error:
        primary_error = error
    finally:
        stop.set()
        cleanup_results = []
        try:
            _remove_readiness(readiness_path)
        except BaseException as error:
            cleanup_results.append(error)
        deadline = shutdown_budget.deadline()
        close_reserve = min(
            2.0, SUPERVISOR_SHUTDOWN_TIMEOUT_SECONDS / 10
        )
        tasks = tuple(
            task
            for task in (consumer, waiter, pump_task, prompt_task)
            if task is not None
        )
        try:
            await _stop_background_tasks(tasks, deadline - close_reserve)
        except BaseException as error:
            cleanup_results.append(error)
        try:
            await _close_queue_before(queue, deadline)
        except BaseException as error:
            cleanup_results.append(error)
        _raise_after_cleanup(primary_error, cleanup_results)


def _build_lifecycle_service(database: Database):
    required = (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    )
    present = tuple(bool(os.environ.get(name, "").strip()) for name in required)
    if not any(present):
        return None
    if not all(present):
        raise ValueError("YCLIENTS lifecycle configuration is incomplete")
    config = YclientsConfig.from_env(os.environ)
    return LifecycleService(
        database,
        YclientsAdapter(config),
        FeedbackService(database),
    )


async def run() -> None:
    _remove_readiness(READINESS_PATH)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    database_url = os.environ["DATABASE_URL"] or database_url_from_env(
        os.environ, required=True
    )
    redis_url = os.environ["REDIS_URL"]
    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
    queue = RabbitQueue(os.environ["RABBITMQ_URL"])
    database = Database(database_url, min_size=1, max_size=5)
    redis_client = redis.from_url(redis_url, decode_responses=True)
    telegram = Bot(token=telegram_token)
    shutdown_budget = ShutdownBudget()
    worker_lock = None
    primary_error = None
    try:
        await database.connect()
        worker_lock = await _acquire_worker_lock(database)
        booking_dispatcher = _build_booking_dispatcher(
            database,
            enabled=BOOKING_INTERACTIONS_ENABLED,
            mode=BOOKING_MODE,
            service_allowlist=YCLIENTS_SERVICE_ALLOWLIST,
            staff_allowlist=YCLIENTS_STAFF_ALLOWLIST,
            env=os.environ,
        )
        lifecycle = _build_lifecycle_service(database)
        repository = MessageRepository(database)
        reconciled = await repository.reconcile_stale_outbound_deliveries()
        if reconciled:
            logger.warning(
                "stale_outbound_deliveries_terminalized count=%d", reconciled
            )
        await queue.connect()
        init_llm()
        task_handler = MessageTaskHandler(
            database,
            generate_response,
            TelegramSender(telegram, repository),
            scheduler_repository=SchedulerJobRepository(database),
            booking_port=LocalBookingPort(database),
            notification_outbox=NotificationOutbox(
                repository,
                staff_chat_id=os.environ.get("STAFF_TELEGRAM_CHAT_ID", ""),
            ),
            lifecycle=lifecycle,
            dispatcher=booking_dispatcher,
            booking_interactions_enabled=BOOKING_INTERACTIONS_ENABLED,
        )
        pump = PipelinePump(
            MessageBuffer(redis_client, database),
            OutboxRelay(database, queue),
            repository,
        )
        logger.info("Worker started")
        await _supervise(
            queue,
            stop,
            handler=task_handler.handle,
            pump=pump,
            prompt_listener=prompt_reload_listener,
            shutdown_budget=shutdown_budget,
        )
    except BaseException as error:
        primary_error = error
    finally:
        readiness_error = None
        try:
            _remove_readiness(READINESS_PATH)
        except BaseException as error:
            readiness_error = error

        async def close_database():
            try:
                if worker_lock is not None:
                    await _release_worker_lock(worker_lock)
            finally:
                await database.close()

        await _cleanup_all(
            queue.close(),
            telegram.session.close(),
            redis_client.aclose(),
            close_database(),
            primary_error=primary_error,
            prior_results=(readiness_error,),
            deadline=shutdown_budget.deadline(),
        )
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
