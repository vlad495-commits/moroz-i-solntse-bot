import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import aio_pika
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustChannel,
    AbstractRobustConnection,
    AbstractRobustExchange,
    AbstractRobustQueue,
)


logger = logging.getLogger(__name__)

TASKS_EXCHANGE = "tasks"
TASKS_QUEUE = "tasks"
TASKS_ROUTING_KEY = "tasks"
DEAD_LETTER_EXCHANGE = "tasks.dlx"
DEAD_LETTER_QUEUE = "tasks.dlq"
DEAD_LETTER_ROUTING_KEY = "tasks.dlq"
DEAD_LETTER_TTL_MS = 2_592_000_000
MAX_RETRIES = 3
RETRY_HEADER = "x-retry-count"
DEFAULT_RETRY_DELAYS = (1, 5, 30)
DEFAULT_DRAIN_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class QueueTask:
    """Frozen task envelope with an intentionally mutable JSON payload."""

    kind: str
    payload: dict[str, Any]
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "payload": self.payload,
                "idempotency_key": self.idempotency_key,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "QueueTask":
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid queue task JSON") from error
        if not isinstance(data, dict) or set(data) != {
            "kind",
            "payload",
            "idempotency_key",
        }:
            raise ValueError("invalid queue task object")
        return cls(
            kind=data["kind"],
            payload=data["payload"],
            idempotency_key=data["idempotency_key"],
        )


class QueuePort(Protocol):
    async def publish(self, task: QueueTask) -> None: ...


TaskHandler = Callable[[QueueTask], Awaitable[None]]


class RabbitQueue(QueuePort):
    def __init__(
        self,
        url: str,
        *,
        retry_delays: tuple[float, float, float] = DEFAULT_RETRY_DELAYS,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not url:
            raise ValueError("RabbitMQ URL is required")
        if len(retry_delays) != MAX_RETRIES or any(
            delay < 0 for delay in retry_delays
        ):
            raise ValueError("retry_delays must contain three non-negative values")
        if drain_timeout < 0:
            raise ValueError("drain_timeout must be non-negative")
        self._url = url
        self._retry_delays = retry_delays
        self._drain_timeout = drain_timeout
        self._sleep = sleep
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None
        self._exchange: AbstractRobustExchange | None = None
        self._queue: AbstractRobustQueue | None = None
        self._dead_letter_exchange: AbstractRobustExchange | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task] = set()
        self.ready = asyncio.Event()

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            if (
                self._connection is not None
                and not self._connection.is_closed
                and self._channel is not None
                and not self._channel.is_closed
            ):
                return

            old_connection, self._connection = self._connection, None
            self._channel = None
            self._exchange = None
            self._queue = None
            self._dead_letter_exchange = None
            if old_connection is not None and not old_connection.is_closed:
                await old_connection.close()

            connection = await aio_pika.connect_robust(self._url)
            try:
                channel = await connection.channel(
                    publisher_confirms=True,
                    on_return_raises=True,
                )
                await channel.set_qos(prefetch_count=4)
                exchange = await channel.declare_exchange(
                    TASKS_EXCHANGE,
                    aio_pika.ExchangeType.DIRECT,
                    durable=True,
                )
                queue = await channel.declare_queue(TASKS_QUEUE, durable=True)
                await queue.bind(exchange, routing_key=TASKS_ROUTING_KEY)

                dead_letter_exchange = await channel.declare_exchange(
                    DEAD_LETTER_EXCHANGE,
                    aio_pika.ExchangeType.DIRECT,
                    durable=True,
                )
                dead_letter_queue = await channel.declare_queue(
                    DEAD_LETTER_QUEUE,
                    durable=True,
                    arguments={"x-message-ttl": DEAD_LETTER_TTL_MS},
                )
                await dead_letter_queue.bind(
                    dead_letter_exchange,
                    routing_key=DEAD_LETTER_ROUTING_KEY,
                )
            except BaseException:
                await connection.close()
                raise

            self._connection = connection
            self._channel = channel
            self._exchange = exchange
            self._queue = queue
            self._dead_letter_exchange = dead_letter_exchange

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self.ready.clear()
            connection, self._connection = self._connection, None
            self._channel = None
            self._exchange = None
            self._queue = None
            self._dead_letter_exchange = None
            if connection is not None and not connection.is_closed:
                await connection.close()

    async def publish(self, task: QueueTask) -> None:
        exchange = self._require(self._exchange)
        await self._confirmed_publish(
            exchange,
            aio_pika.Message(
                body=task.to_json().encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=task.idempotency_key,
                headers={RETRY_HEADER: 0},
            ),
            TASKS_ROUTING_KEY,
        )

    async def consume_one(self, handler: TaskHandler) -> None:
        queue = self._require(self._queue)
        message = await queue.get(timeout=10, fail=True)
        await self._handle(message, handler)

    async def consume(self, handler: TaskHandler) -> None:
        queue = self._require(self._queue)
        fatal = asyncio.get_running_loop().create_future()
        stopping = asyncio.Event()

        async def callback(message: AbstractIncomingMessage) -> None:
            task = asyncio.create_task(self._handle(message, handler))
            self._inflight.add(task)
            try:
                await task
            except BaseException as error:
                if not stopping.is_set() and not fatal.done():
                    fatal.set_exception(error)
            finally:
                self._inflight.discard(task)

        consumer_tag = await queue.consume(callback, no_ack=False)
        self.ready.set()
        primary_error = None
        try:
            await fatal
        except BaseException as error:
            primary_error = error
        finally:
            stopping.set()
            cleanup_error = None
            try:
                if not queue.channel.is_closed:
                    await queue.cancel(consumer_tag)
            except BaseException as error:
                cleanup_error = error
            self.ready.clear()
            try:
                await self._drain_inflight()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
            if not fatal.done():
                fatal.cancel()
            if primary_error is not None:
                raise primary_error
            if cleanup_error is not None:
                raise cleanup_error

    async def _drain_inflight(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._drain_timeout
        await asyncio.sleep(0)
        while self._inflight:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.wait(tuple(self._inflight), timeout=remaining)
            await asyncio.sleep(0)
        pending = tuple(self._inflight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle(
        self,
        message: AbstractIncomingMessage,
        handler: TaskHandler,
    ) -> None:
        try:
            task = QueueTask.from_json(message.body.decode())
            await handler(task)
        except Exception:
            retry_count = self._retry_count(message)
            try:
                if retry_count < MAX_RETRIES:
                    await self._sleep(self._retry_delays[retry_count])
                    await self._republish(
                        message,
                        self._require(self._exchange),
                        TASKS_ROUTING_KEY,
                        retry_count + 1,
                    )
                else:
                    await self._republish(
                        message,
                        self._require(self._dead_letter_exchange),
                        DEAD_LETTER_ROUTING_KEY,
                        retry_count,
                    )
            except Exception as republish_error:
                try:
                    await message.reject(requeue=True)
                except Exception as reject_error:
                    raise republish_error from reject_error
                raise
            await message.ack()
            return
        await message.ack()

    async def _republish(
        self,
        original: AbstractIncomingMessage,
        exchange: AbstractRobustExchange,
        routing_key: str,
        retry_count: int,
    ) -> None:
        headers = dict(original.headers or {})
        headers[RETRY_HEADER] = retry_count
        await self._confirmed_publish(
            exchange,
            aio_pika.Message(
                body=original.body,
                content_type=original.content_type or "application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=original.message_id,
                correlation_id=original.correlation_id,
                headers=headers,
            ),
            routing_key,
        )

    @staticmethod
    async def _confirmed_publish(
        exchange: AbstractRobustExchange,
        message: aio_pika.Message,
        routing_key: str,
    ) -> None:
        confirmation = await exchange.publish(
            message,
            routing_key=routing_key,
            mandatory=True,
            timeout=10,
        )
        if not confirmation:
            raise RuntimeError("RabbitMQ did not confirm published message")

    @staticmethod
    def _retry_count(message: AbstractIncomingMessage) -> int:
        value = (message.headers or {}).get(RETRY_HEADER, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            logger.warning("Invalid %s header; treating it as zero", RETRY_HEADER)
            return 0
        return value

    @staticmethod
    def _require(resource):
        if resource is None:
            raise RuntimeError("RabbitQueue is not connected")
        return resource
