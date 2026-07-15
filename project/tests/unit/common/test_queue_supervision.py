import asyncio

import aio_pika
import pytest

from moroz.common.queue import QueueTask, RabbitQueue


class BrokerFailure(RuntimeError):
    pass


class FakeChannel:
    is_closed = False


class FakeConsumerQueue:
    def __init__(self):
        self.channel = FakeChannel()
        self.callback = None
        self.cancelled = False

    async def consume(self, callback, no_ack):
        assert no_ack is False
        self.callback = callback
        return "consumer-tag"

    async def cancel(self, consumer_tag):
        assert consumer_tag == "consumer-tag"
        self.cancelled = True

    def deliver(self, message):
        return asyncio.create_task(self.callback(message))


class FakeExchange:
    def __init__(self):
        self.messages = []

    async def publish(self, message, **kwargs):
        self.messages.append((message, kwargs))
        return True


class FakeMessage:
    def __init__(self, retry_count=0, ack_error=None):
        self.body = QueueTask("test", {}, "test:1").to_json().encode()
        self.headers = {"x-retry-count": retry_count}
        self.content_type = "application/json"
        self.message_id = "test:1"
        self.correlation_id = "correlation"
        self.ack_error = ack_error
        self.acked = False
        self.rejected = False

    async def ack(self):
        if self.ack_error:
            raise self.ack_error
        self.acked = True

    async def reject(self, requeue):
        assert requeue is True
        self.rejected = True


async def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_consume_propagates_fatal_callback_error_and_clears_readiness():
    adapter = RabbitQueue("amqp://unused", retry_delays=(0, 0, 0))
    broker_queue = FakeConsumerQueue()
    adapter._queue = broker_queue

    consumer = asyncio.create_task(adapter.consume(lambda _task: asyncio.sleep(0)))
    await adapter.ready.wait()
    callback = broker_queue.deliver(FakeMessage(ack_error=BrokerFailure("ack failed")))

    with pytest.raises(BrokerFailure, match="ack failed"):
        await asyncio.wait_for(consumer, timeout=1)
    await callback

    assert broker_queue.cancelled
    assert not adapter.ready.is_set()


@pytest.mark.asyncio
async def test_consume_stops_intake_then_drains_successful_inflight_callback():
    adapter = RabbitQueue(
        "amqp://unused", retry_delays=(0, 0, 0), drain_timeout=1
    )
    broker_queue = FakeConsumerQueue()
    adapter._queue = broker_queue
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(_task):
        started.set()
        await release.wait()

    consumer = asyncio.create_task(adapter.consume(handle))
    await adapter.ready.wait()
    callback = broker_queue.deliver(FakeMessage())
    await started.wait()
    consumer.cancel()
    await wait_until(lambda: broker_queue.cancelled and not adapter.ready.is_set())
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await consumer
    await callback


@pytest.mark.asyncio
async def test_consume_cancels_inflight_callback_after_drain_timeout():
    adapter = RabbitQueue(
        "amqp://unused", retry_delays=(0, 0, 0), drain_timeout=0.01
    )
    broker_queue = FakeConsumerQueue()
    adapter._queue = broker_queue
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(_task):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    consumer = asyncio.create_task(adapter.consume(handle))
    await adapter.ready.wait()
    callback = broker_queue.deliver(FakeMessage())
    await started.wait()
    consumer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await callback


@pytest.mark.asyncio
async def test_retry_delays_are_increasing_and_dlq_has_no_extra_delay():
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    adapter = RabbitQueue("amqp://unused", sleep=fake_sleep)
    adapter._exchange = FakeExchange()
    adapter._dead_letter_exchange = FakeExchange()

    async def fail(_task):
        raise ValueError("business failure")

    for retry_count in range(4):
        await adapter._handle(FakeMessage(retry_count), fail)

    assert delays == [1, 5, 30]
    assert len(adapter._exchange.messages) == 3
    assert len(adapter._dead_letter_exchange.messages) == 1
    assert adapter._dead_letter_exchange.messages[0][0].headers["x-retry-count"] == 3
