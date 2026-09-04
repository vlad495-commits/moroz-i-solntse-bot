import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from aiogram import Bot
import redis.asyncio as redis
from redis.exceptions import RedisError

from config import (
    CONTEXT_MESSAGES_LIMIT,
    DATA_RETENTION_DAYS,
    TELEGRAM_YCLIENTS_BOOKING_ENABLED,
    YCLIENTS_CATALOG_GROUNDING_ENABLED,
)
from llm import generate_response, init_llm, prompt_reload_listener
from moroz.booking.catalog import (
    CATALOG_SYNC_KIND,
    CatalogRepository,
    CatalogSyncCoordinator,
)
from moroz.booking.admin_commands import (
    ADMIN_BOOKING_COMMAND_KINDS,
    AdminBookingCommandRepository,
    AdminBookingCommandService,
)
from moroz.booking.projection import (
    PROJECTION_SYNC_KIND,
    ProjectionRepository,
    ProjectionSyncCoordinator,
)
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.telegram import (
    BookingReply,
    TelegramBookingCoordinator,
    persistent_menu_command,
)
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_catalog import YclientsCatalogError, YclientsCatalogReader
from moroz.booking.yclients_http import YclientsConfig
from moroz.booking.yclients_records import YclientsProjectionError, YclientsRecordsReader
from moroz.common.alerts import AlertRouter
from moroz.common.config import database_url_from_env
from moroz.common.db import Database
from moroz.common.queue import MAX_RETRIES, QueueTask, RabbitQueue
from moroz.messaging.buffer import BUFFER_TTL_SECONDS, MessageBuffer
from moroz.messaging.outbox import OutboxRelay, process_message_key
from moroz.messaging.repository import MessageRepository
from moroz.messaging.router import route_message
from moroz.messaging.telegram import TelegramSender, main_menu_options
from moroz.notifications.feedback import FeedbackService
from moroz.notifications.handlers import (
    STAGING_SCHEDULER_SMOKE_KIND,
    handle_scheduler_job,
)
from moroz.notifications.lifecycle import LifecycleService
from moroz.notifications.ports import LocalBookingPort, NotificationOutbox
from moroz.notifications.repository import SchedulerJobRepository
from moroz.privacy import customer_lock_subject
from moroz.retention import (
    RETENTION_CLEANUP_KIND,
    RETENTION_ERROR_CODE,
    RetentionCleanupError,
    RetentionCleanupCoordinator,
)
from moroz.reactivation.activity import (
    ActivityRepository,
    ActivitySyncCoordinator,
    YclientsClientHistoryReader,
)
from moroz.reactivation.repository import ReactivationRepository
from moroz.reactivation.service import (
    REACTIVATION_ACTIVITY_SYNC_KIND,
    REACTIVATION_TICK_KIND,
    ReactivationCoordinator,
)
from moroz.security.llm_gateway import LLMUsage


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
YCLIENTS_PROJECTION_ERROR_CODES = frozenset(
    {
        "yclients_transport",
        "yclients_http_status",
        "yclients_response_shape",
        "yclients_page_bound",
        "yclients_projection_write",
    }
)
YCLIENTS_CATALOG_ERROR_CODES = frozenset(
    {
        "yclients_catalog_transport",
        "yclients_catalog_http_status",
        "yclients_catalog_response_shape",
        "yclients_catalog_bound",
        "yclients_catalog_write",
    }
)


async def handle(task: QueueTask) -> None:
    logger.error("No worker task handler is registered; task will be retried")
    raise NotImplementedError("No worker task handlers are registered")


async def _handle_with_alerts(task: QueueTask, handler, alert_router) -> None:
    try:
        await handler(task)
    except Exception as error:
        try:
            await alert_router.emit(
                code="worker_task_failed",
                subject=task.kind,
                severity="critical",
                text=f"worker task failed error_type={type(error).__name__}",
            )
        except Exception as alert_error:
            logger.error(
                "worker_alert_failed error_type=%s",
                type(alert_error).__name__,
            )
        raise


def _build_alert_router(redis_client, telegram: Bot):
    technical_chat_id = os.environ.get("TECHNICAL_ALERT_CHAT_ID", "").strip()
    if not technical_chat_id:
        return None

    async def send_alert(chat_id: str, text: str) -> None:
        await telegram.send_message(chat_id=chat_id, text=text)

    return AlertRouter(
        redis_client,
        send_alert,
        technical_chat_id=technical_chat_id,
        business_chat_id=os.environ.get("BUSINESS_ALERT_CHAT_ID", "").strip(),
    )


def build_input_security_alert(alert_router):
    async def alert(_code: str) -> None:
        await alert_router.emit(
            code="security_down",
            subject="input_security",
            severity="CRITICAL",
            text="Input Security classifier unavailable or invalid",
        )

    return alert


def build_output_validator_alert(alert_router):
    async def alert(code: str) -> None:
        await alert_router.emit(
            code=code,
            subject="output_validator",
            severity="ERROR",
            text="Output validator unavailable or invalid",
        )

    return alert


def build_context_compactor_alert(alert_router):
    async def alert(code: str) -> None:
        await alert_router.emit(
            code=code,
            subject="context_compactor",
            severity="ERROR",
            text="Context compactor unavailable or invalid",
        )

    return alert


async def _persist_token_usage(
    connection,
    chat_id: int,
    user_id: int,
    source_message_id: int,
    result,
) -> None:
    usages = getattr(result, "usage", ())
    if not usages and result.total_tokens > 0:
        usages = (
            LLMUsage(
                "answer",
                result.prompt_tokens,
                result.completion_tokens,
                result.cached_tokens,
                result.total_tokens,
                result.model,
            ),
        )
    for usage in usages:
        if usage.total_tokens <= 0:
            continue
        await connection.execute(
            """
            INSERT INTO token_usage
                (chat_id, user_id, source_message_id, purpose, prompt_tokens,
                 completion_tokens, cached_tokens, total_tokens, model)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            chat_id,
            user_id,
            source_message_id,
            usage.purpose,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cached_tokens,
            usage.total_tokens,
            usage.model,
        )


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
        projection_sync=None,
        catalog_sync=None,
        admin_booking_commands=None,
        retention_cleanup=None,
        reactivation=None,
        catalog_repository=None,
        booking_coordinator=None,
        catalog_grounding_enabled=False,
        clock=None,
        scheduler_handler=handle_scheduler_job,
    ):
        self._database = database
        self._llm = llm
        self._telegram = telegram
        self._repository = MessageRepository(database)
        self._scheduler_repository = scheduler_repository
        self._booking_port = booking_port
        self._notification_outbox = notification_outbox
        self._lifecycle = lifecycle
        self._projection_sync = projection_sync
        self._catalog_sync = catalog_sync
        self._admin_booking_commands = admin_booking_commands
        self._retention_cleanup = retention_cleanup
        self._reactivation = reactivation
        self._catalog_repository = (
            catalog_repository if catalog_grounding_enabled else None
        )
        self._booking_coordinator = booking_coordinator
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scheduler_handler = scheduler_handler
        self._system_scheduler_lock = asyncio.Lock()

    async def handle(self, task: QueueTask) -> None:
        if task.kind == "process_message":
            await self._process_message(task)
            return
        if task.kind == "send_outbound":
            outbound_id = task.payload.get("outbound_id")
            if not isinstance(outbound_id, str):
                raise ValueError("send_outbound requires outbound_id")
            if task.recovery_required:
                await self._telegram.recover(UUID(outbound_id))
                return
            if task.recovery_candidate and await self._telegram.recover_candidate(
                UUID(outbound_id)
            ):
                return
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
        if self._scheduler_repository is None:
            raise RuntimeError("scheduler job dependencies are not configured")
        job = await self._scheduler_repository.get_claimed(job_id)
        if job is None:
            return
        if job.kind in ADMIN_BOOKING_COMMAND_KINDS:
            if self._admin_booking_commands is None:
                raise RuntimeError("admin booking commands are not configured")
            async with self._system_scheduler_lock:
                job = await self._scheduler_repository.get_claimed(job_id)
                if job is None:
                    return
                try:
                    result = await self._admin_booking_commands.handle(job)
                except Exception as error:
                    await self._scheduler_repository.record_failure(
                        job,
                        error_code=_scheduler_error_code(error),
                        terminal=job.attempts >= MAX_RETRIES,
                    )
                    raise
                await self._scheduler_repository.complete(job, result)
            return
        if job.kind in {
            REACTIVATION_ACTIVITY_SYNC_KIND,
            REACTIVATION_TICK_KIND,
        }:
            if self._reactivation is None:
                raise RuntimeError("reactivation coordinator is not configured")
            async with self._system_scheduler_lock:
                job = await self._scheduler_repository.get_claimed(job_id)
                if job is None:
                    return
                try:
                    result = (
                        await self._reactivation.run_activity_sync(job)
                        if job.kind == REACTIVATION_ACTIVITY_SYNC_KIND
                        else await self._reactivation.run_tick(job)
                    )
                except Exception as error:
                    await self._scheduler_repository.record_failure(
                        job,
                        error_code=_scheduler_error_code(error),
                        terminal=job.attempts >= MAX_RETRIES,
                    )
                    raise
                await self._scheduler_repository.complete(job, result)
            return
        if job.kind in {
            PROJECTION_SYNC_KIND,
            CATALOG_SYNC_KIND,
            RETENTION_CLEANUP_KIND,
            STAGING_SCHEDULER_SMOKE_KIND,
        }:
            async with self._system_scheduler_lock:
                job = await self._scheduler_repository.get_claimed(job_id)
                if job is None:
                    return
                try:
                    result = await self._scheduler_handler(
                        job,
                        booking_port=self._booking_port,
                        outbox=self._notification_outbox,
                        lifecycle=self._lifecycle,
                        projection_sync=self._projection_sync,
                        catalog_sync=self._catalog_sync,
                        retention_cleanup=self._retention_cleanup,
                    )
                except Exception as error:
                    await self._scheduler_repository.record_failure(
                        job,
                        error_code=_scheduler_error_code(error),
                        terminal=job.attempts >= MAX_RETRIES,
                    )
                    raise
                await self._scheduler_repository.complete(job, result)
            return
        if self._booking_port is None or self._notification_outbox is None:
            raise RuntimeError("scheduler job dependencies are not configured")
        customer_id = job.payload.get("customer_id")
        if (
            not isinstance(customer_id, str)
            and job.booking_key is not None
            and hasattr(self._database, "acquire")
        ):
            async with self._database.acquire() as lookup:
                customer_id = await lookup.fetchval(
                    "SELECT customer_id FROM bookings WHERE booking_key = $1",
                    job.booking_key,
                )
        if not isinstance(customer_id, str) or not hasattr(
            self._database, "acquire"
        ):
            try:
                result = await self._scheduler_handler(
                    job,
                    booking_port=self._booking_port,
                    outbox=self._notification_outbox,
                    lifecycle=self._lifecycle,
                    projection_sync=self._projection_sync,
                    catalog_sync=self._catalog_sync,
                    retention_cleanup=self._retention_cleanup,
                )
            except Exception as error:
                await self._scheduler_repository.record_failure(
                    job,
                    error_code=_scheduler_error_code(error),
                    terminal=job.attempts >= MAX_RETRIES,
                )
                raise
            await self._scheduler_repository.complete(job, result)
            return

        async with self._database.acquire() as privacy_connection:
            async with privacy_connection.transaction():
                await privacy_connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(customer_id),
                )
                job = await self._scheduler_repository.get_claimed(job_id)
                if job is None:
                    return
                if job.booking_key is not None:
                    exists = await privacy_connection.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM bookings "
                        "WHERE booking_key = $1 AND customer_id = $2)",
                        job.booking_key,
                        customer_id,
                    )
                    if not exists:
                        return
                try:
                    result = await self._scheduler_handler(
                        job,
                        booking_port=self._booking_port,
                        outbox=self._notification_outbox,
                        lifecycle=self._lifecycle,
                        projection_sync=self._projection_sync,
                        catalog_sync=self._catalog_sync,
                        retention_cleanup=self._retention_cleanup,
                    )
                except Exception as error:
                    await self._scheduler_repository.record_failure(
                        job,
                        error_code=_scheduler_error_code(error),
                        terminal=job.attempts >= MAX_RETRIES,
                    )
                    raise
                await self._scheduler_repository.complete(job, result)

    async def _process_message(self, task: QueueTask) -> None:
        chat_id = task.payload.get("chat_id")
        update_ids = task.payload.get("update_ids")
        if (
            not isinstance(update_ids, list)
            or not update_ids
            or any(not isinstance(value, str) for value in update_ids)
            or len(set(update_ids)) != len(update_ids)
        ):
            raise ValueError("invalid process_message payload")
        if task.idempotency_key != process_message_key(update_ids):
            raise ValueError("process_message idempotency key does not match updates")
        if chat_id is None:
            async with self._database.acquire() as lookup:
                candidates = await lookup.fetch(
                    """
                    SELECT DISTINCT chat_id
                    FROM message_inbox
                    WHERE channel = 'telegram'
                      AND external_message_id = ANY($1::text[])
                    """,
                    update_ids,
                )
            if not candidates:
                return
            if len(candidates) != 1:
                raise ValueError("process_message updates span multiple chats")
            chat_id = candidates[0]["chat_id"]
        elif not isinstance(chat_id, str):
            raise ValueError("invalid process_message payload")
        numeric_chat_id = int(chat_id)
        reply_key = f"reply:{task.idempotency_key}"

        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(chat_id),
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
                    existing = await connection.fetchval(
                        "SELECT count(*) FROM message_inbox "
                        "WHERE channel = 'telegram' "
                        "AND external_message_id = ANY($1::text[])",
                        update_ids,
                    )
                    if existing == 0:
                        return
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
                kinds = [payload.get("kind", "text") for payload in payloads]
                if any(kind not in {"text", "callback", "contact"} for kind in kinds):
                    raise ValueError("process_message persisted kind is invalid")
                if any(kind != "text" for kind in kinds) and len(kinds) != 1:
                    raise ValueError("process_message interactions cannot be batched")
                interaction_kind = kinds[0]
                interaction_data = payloads[0].get("data", {})
                if not isinstance(interaction_data, dict):
                    raise ValueError("process_message persisted data is invalid")
                user_ids = {payload["user_id"] for payload in payloads}
                if len(user_ids) != 1:
                    raise ValueError("process_message spans multiple users")
                user_id = int(user_ids.pop())
                persisted_text = "\n".join(payload["text"] for payload in payloads)
                menu_index = next(
                    (
                        index
                        for index in range(len(payloads) - 1, -1, -1)
                        if persistent_menu_command(payloads[index]["text"])
                        is not None
                    ),
                    None,
                )
                accepted_ids = [row["external_message_id"] for row in accepted]

                human_mode = await connection.fetchval(
                    "SELECT enabled FROM human_mode WHERE customer_id = $1",
                    chat_id,
                )
                if human_mode:
                    await connection.execute(
                        """
                        INSERT INTO messages
                            (chat_id, user_id, role, content, llm_usage_tracked)
                        VALUES ($1, $2, 'user', $3, TRUE)
                        """,
                        numeric_chat_id,
                        user_id,
                        persisted_text,
                    )
                    await connection.execute(
                        """
                        UPDATE message_inbox
                        SET status = 'processed'
                        WHERE channel = 'telegram'
                          AND external_message_id = ANY($1::text[])
                        """,
                        accepted_ids,
                    )
                    return

                if interaction_kind == "text" and menu_index is not None:
                    menu_command = persistent_menu_command(
                        payloads[menu_index]["text"]
                    )
                    if menu_index == len(payloads) - 1:
                        persisted_text = str(menu_command)
                    else:
                        if self._booking_coordinator is not None:
                            await self._booking_coordinator.handle(
                                connection,
                                customer_id=chat_id,
                                user_id=str(user_id),
                                update_id=accepted_ids[menu_index],
                                text=str(menu_command),
                                kind="text",
                                data={},
                            )
                        persisted_text = "\n".join(
                            payload["text"] for payload in payloads[menu_index:]
                        )

                booking_reply = None
                booking_route = route_message(persisted_text).route
                if self._booking_coordinator is not None:
                    booking_reply = await self._booking_coordinator.handle(
                        connection,
                        customer_id=chat_id,
                        user_id=str(user_id),
                        update_id=accepted_ids[0],
                        text=persisted_text,
                        kind=interaction_kind,
                        data=interaction_data,
                    )
                elif booking_route in {"booking", "booking_management"}:
                    booking_reply = BookingReply(
                        "Запись внутри Telegram сейчас недоступна. Воспользуйтесь онлайн-записью или напишите администратору.",
                        {},
                    )
                if booking_reply is not None:
                    if not booking_reply.text:
                        await connection.execute(
                            "UPDATE message_inbox SET status = 'processed' "
                            "WHERE channel = 'telegram' AND external_message_id = ANY($1::text[])",
                            accepted_ids,
                        )
                        return
                    if persisted_text:
                        await connection.execute(
                            "INSERT INTO messages "
                            "(chat_id, user_id, role, content, llm_usage_tracked) "
                            "VALUES ($1, $2, 'user', $3, TRUE)",
                            numeric_chat_id,
                            user_id,
                            persisted_text,
                        )
                    await connection.execute(
                        "INSERT INTO messages (chat_id, user_id, role, content) "
                        "VALUES ($1, $2, 'assistant', $3)",
                        numeric_chat_id,
                        user_id,
                        booking_reply.text,
                    )
                    await self._repository.enqueue_outbound_in_transaction(
                        connection,
                        channel="telegram",
                        chat_id=chat_id,
                        text=booking_reply.text,
                        idempotency_key=reply_key,
                        delivery_options=booking_reply.delivery_options,
                    )
                    await connection.execute(
                        "UPDATE message_inbox SET status = 'processed' "
                        "WHERE channel = 'telegram' "
                        "AND external_message_id = ANY($1::text[])",
                        accepted_ids,
                    )
                    return

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
                catalog = None
                if self._catalog_repository is not None:
                    catalog = await self._catalog_repository.ground(
                        connection,
                        persisted_text,
                        self._clock(),
                    )
                llm_options = {
                    "recent_message_count": int(recent_message_count),
                }
                if catalog is not None:
                    llm_options["catalog"] = catalog
                if self._booking_coordinator is not None:
                    llm_options["booking_context"] = await self._booking_coordinator.routing_context(chat_id)

                async def dispatch(decision):
                    nonlocal booking_reply
                    if decision.route not in {"booking", "booking_management"}:
                        return None
                    if self._booking_coordinator is None:
                        return "Запись внутри Telegram сейчас недоступна. Воспользуйтесь онлайн-записью или напишите администратору."
                    booking_reply = await self._booking_coordinator.handle(
                        connection, customer_id=chat_id, user_id=str(user_id),
                        update_id=accepted_ids[0], text=persisted_text,
                        kind=interaction_kind, data=interaction_data, decision=decision,
                    )
                    return booking_reply.text if booking_reply is not None else None

                if self._booking_coordinator is not None:
                    llm_options["dispatch"] = dispatch
                result = await self._llm(persisted_text, context, **llm_options)

                source_message_id = await connection.fetchval(
                    """
                    INSERT INTO messages
                        (chat_id, user_id, role, content, llm_usage_tracked)
                    VALUES ($1, $2, 'user', $3, TRUE)
                    RETURNING id
                    """,
                    numeric_chat_id,
                    user_id,
                    persisted_text,
                )
                await connection.execute(
                    """
                    INSERT INTO messages (chat_id, user_id, role, content)
                    VALUES ($1, $2, 'assistant', $3)
                    """,
                    numeric_chat_id,
                    user_id,
                    result.text,
                )
                await _persist_token_usage(
                    connection,
                    numeric_chat_id,
                    user_id,
                    source_message_id,
                    result,
                )
                await self._repository.enqueue_outbound_in_transaction(
                    connection,
                    channel="telegram",
                    chat_id=chat_id,
                    text=result.text,
                    idempotency_key=reply_key,
                    delivery_options=(booking_reply.delivery_options if booking_reply is not None else
                                      main_menu_options() if result.model in {"router-fallback", "router-clarification", "booking-unavailable"} else {}),
                )
                await connection.execute(
                    """
                    UPDATE message_inbox
                    SET status = 'processed'
                    WHERE channel = 'telegram'
                      AND external_message_id = ANY($1::text[])
                    """,
                    accepted_ids,
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


def _scheduler_error_code(error: Exception) -> str:
    if isinstance(error, RetentionCleanupError):
        return RETENTION_ERROR_CODE
    if (
        isinstance(error, YclientsProjectionError)
        and error.code in YCLIENTS_PROJECTION_ERROR_CODES
    ):
        return error.code
    if (
        isinstance(error, YclientsCatalogError)
        and error.code in YCLIENTS_CATALOG_ERROR_CODES
    ):
        return error.code
    return type(error).__name__


def _build_yclients_services(
    database: Database,
    *,
    telegram_booking_enabled: bool = TELEGRAM_YCLIENTS_BOOKING_ENABLED,
) -> tuple[
    LifecycleService | None,
    ProjectionSyncCoordinator | None,
    CatalogSyncCoordinator | None,
    CatalogRepository | None,
    AdminBookingCommandService | None,
    ActivitySyncCoordinator | None,
    TelegramBookingCoordinator | None,
]:
    required = (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    )
    present = tuple(bool(os.environ.get(name, "").strip()) for name in required)
    if not any(present):
        return None, None, None, None, None, None, None
    if not all(present):
        raise ValueError("YCLIENTS lifecycle configuration is incomplete")
    config = YclientsConfig.from_env(os.environ)
    adapter = YclientsAdapter(config)
    scheduler_repository = SchedulerJobRepository(database)
    lifecycle = LifecycleService(
        database,
        adapter,
        FeedbackService(database),
    )
    reader = YclientsRecordsReader(config)
    projection_sync = ProjectionSyncCoordinator(
        ProjectionRepository(database),
        reader,
        SchedulerJobRepository(database),
        clock=lambda: datetime.now(UTC),
    )
    catalog_reader = YclientsCatalogReader(config)
    catalog_repository = CatalogRepository(database)
    catalog_sync = CatalogSyncCoordinator(
        catalog_repository,
        catalog_reader,
        SchedulerJobRepository(database),
        clock=lambda: datetime.now(UTC),
    )
    admin_booking_commands = AdminBookingCommandService(
        adapter,
        BookingRepository(database, schedule_notifications=False),
        AdminBookingCommandRepository(database),
        scheduler_repository,
        clock=lambda: datetime.now(UTC),
    )
    activity_sync = ActivitySyncCoordinator(
        ActivityRepository(database),
        YclientsClientHistoryReader(config),
        SchedulerJobRepository(database),
        clock=lambda: datetime.now(UTC),
    )
    telegram_booking = None
    if telegram_booking_enabled:
        telegram_booking_repository = BookingRepository(
            database, schedule_notifications=True
        )
        telegram_booking = TelegramBookingCoordinator(
            telegram_booking_repository,
            catalog_repository,
            BookingService(adapter, telegram_booking_repository),
            adapter,
        )
    return (
        lifecycle,
        projection_sync,
        catalog_sync,
        catalog_repository,
        admin_booking_commands,
        activity_sync,
        telegram_booking,
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
        scheduler_repository = SchedulerJobRepository(database)
        retention_cleanup = RetentionCleanupCoordinator(
            database,
            scheduler_repository,
            retention_days=DATA_RETENTION_DAYS,
        )
        await retention_cleanup.ensure_current(datetime.now(UTC))
        try:
            (
                lifecycle,
                projection_sync,
                catalog_sync,
                catalog_repository,
                admin_booking_commands,
                activity_sync,
                telegram_booking,
            ) = _build_yclients_services(database)
        except ValueError:
            if hasattr(database, "acquire"):
                await ReactivationRepository(
                    database
                ).fail_closed_yclients_unavailable(datetime.now(UTC))
            raise
        if projection_sync is not None:
            await projection_sync.ensure_current(datetime.now(UTC))
        if catalog_sync is not None:
            await catalog_sync.ensure_current(datetime.now(UTC))
        reactivation_repository = ReactivationRepository(
            database,
            business_alert_chat_id=os.environ.get(
                "BUSINESS_ALERT_CHAT_ID", ""
            ),
        )
        reactivation = None
        if activity_sync is not None:
            reactivation = ReactivationCoordinator(
                reactivation_repository,
                scheduler_repository,
                activity_sync,
                clock=lambda: datetime.now(UTC),
            )
            await reactivation.ensure_current(datetime.now(UTC))
        elif hasattr(database, "acquire"):
            await reactivation_repository.fail_closed_yclients_unavailable(
                datetime.now(UTC)
            )
        repository = MessageRepository(database)
        reconciled = await repository.reconcile_stale_outbound_deliveries()
        if reconciled:
            logger.warning(
                "stale_outbound_deliveries_terminalized count=%d", reconciled
            )
        reactivation_unknowns = 0
        if hasattr(database, "acquire"):
            reactivation_unknowns = (
                await reactivation.reconcile_delivery_unknowns()
                if reactivation is not None
                else await reactivation_repository.reconcile_delivery_unknowns(
                    datetime.now(UTC)
                )
            )
        if reactivation_unknowns:
            logger.error(
                "reactivation_delivery_unknowns_reconciled count=%d",
                reactivation_unknowns,
            )
        await queue.connect()
        alert_router = _build_alert_router(redis_client, telegram)
        if alert_router is None:
            init_llm()
        else:
            init_llm(
                build_input_security_alert(alert_router),
                build_output_validator_alert(alert_router),
                build_context_compactor_alert(alert_router),
            )
        task_handler = MessageTaskHandler(
            database,
            generate_response,
            TelegramSender(
                telegram,
                repository,
                context_cache=redis_client,
                pre_send_guard=reactivation_repository.pre_send_guard,
                delivery_hook=reactivation_repository.delivery_hook,
                managed_delivery_check=reactivation_repository.is_linked_outbound,
            ),
            scheduler_repository=scheduler_repository,
            booking_port=LocalBookingPort(database),
            notification_outbox=NotificationOutbox(
                repository,
                staff_chat_id=os.environ.get("STAFF_TELEGRAM_CHAT_ID", ""),
            ),
            lifecycle=lifecycle,
            projection_sync=projection_sync,
            catalog_sync=catalog_sync,
            admin_booking_commands=admin_booking_commands,
            retention_cleanup=retention_cleanup,
            reactivation=reactivation,
            catalog_repository=catalog_repository,
            catalog_grounding_enabled=YCLIENTS_CATALOG_GROUNDING_ENABLED,
            booking_coordinator=telegram_booking,
        )
        async def runtime_handler(task: QueueTask) -> None:
            if alert_router is None:
                await task_handler.handle(task)
                return
            await _handle_with_alerts(task, task_handler.handle, alert_router)

        pump = PipelinePump(
            MessageBuffer(redis_client, database),
            OutboxRelay(database, queue),
            repository,
        )
        logger.info("Worker started")
        await _supervise(
            queue,
            stop,
            handler=runtime_handler,
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
