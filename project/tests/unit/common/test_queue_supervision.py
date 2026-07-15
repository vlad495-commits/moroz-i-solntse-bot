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
async def test_consume_tracks_actual_callback_and_propagates_fatal_identity():
    adapter = RabbitQueue("amqp://unused", retry_delays=(0, 0, 0))
    broker_queue = FakeConsumerQueue()
    adapter._queue = broker_queue
    fatal = BrokerFailure("ack failed")
    release_probe = asyncio.Event()
    tracked = []
    callback_holder = {}
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    exception_contexts = []
    loop.set_exception_handler(lambda _loop, context: exception_contexts.append(context))

    async def handle(_task):
        await release_probe.wait()
        current = asyncio.current_task()
        tracked.append(
            current is callback_holder["task"] and current in adapter._inflight
        )

    try:
        consumer = asyncio.create_task(adapter.consume(handle))
        await adapter.ready.wait()
        callback = broker_queue.deliver(FakeMessage(ack_error=fatal))
        callback_holder["task"] = callback
        release_probe.set()

        with pytest.raises(BrokerFailure) as raised:
            await asyncio.wait_for(consumer, timeout=1)
        await callback
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert raised.value is fatal
    assert tracked == [True]
    assert exception_contexts == []
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
async def test_consume_has_hard_bound_when_callback_suppresses_cancellation():
    adapter = RabbitQueue(
        "amqp://unused",
        retry_delays=(0, 0, 0),
        drain_timeout=0.01,
        cancel_timeout=0.01,
    )
    broker_queue = FakeConsumerQueue()
    adapter._queue = broker_queue
    started = asyncio.Event()
    cancellation_suppressed = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    exception_contexts = []
    loop.set_exception_handler(lambda _loop, context: exception_contexts.append(context))

    async def handle(_task):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellation_suppressed.set()
            await release.wait()

    consumer = asyncio.create_task(adapter.consume(handle))
    await adapter.ready.wait()
    callback = broker_queue.deliver(FakeMessage())
    await started.wait()
    started_at = loop.time()
    consumer.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(consumer), timeout=0.15)
        elapsed = loop.time() - started_at
        await asyncio.wait_for(cancellation_suppressed.wait(), timeout=0.1)
    finally:
        release.set()
        await callback
        if not consumer.done():
            with pytest.raises(asyncio.CancelledError):
                await consumer
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_exception_handler)

    assert elapsed < 0.15
    assert callback not in adapter._inflight
    assert exception_contexts == []


@pytest.mark.asyncio
async def test_readiness_file_is_absent_whenever_ready_clears_during_drain(tmp_path):
    for attempt in range(20):
        adapter = RabbitQueue(
            "amqp://unused", retry_delays=(0, 0, 0), drain_timeout=0.5
        )
        broker_queue = FakeConsumerQueue()
        adapter._queue = broker_queue
        readiness_path = tmp_path / f"ready-{attempt}"
        started = asyncio.Event()
        release = asyncio.Event()

        def publish_readiness(active):
            if active:
                readiness_path.write_text("ready", encoding="utf-8")
            else:
                readiness_path.unlink(missing_ok=True)

        async def handle(_task):
            started.set()
            await release.wait()

        consumer = asyncio.create_task(
            adapter.consume(handle, readiness=publish_readiness)
        )
        await wait_until(lambda: adapter.ready.is_set() or consumer.done())
        if consumer.done():
            await consumer
        assert readiness_path.exists()
        callback = broker_queue.deliver(FakeMessage())
        await started.wait()
        consumer.cancel()
        await wait_until(lambda: broker_queue.cancelled and not adapter.ready.is_set())

        assert not readiness_path.exists()
        assert not callback.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await callback


@pytest.mark.asyncio
async def test_readiness_publish_failure_stops_consumer():
    adapter = RabbitQueue("amqp://unused", retry_delays=(0, 0, 0))
    broker_queue = FakeConsumerQueue()
    adapter._queue = broker_queue
    failure = OSError("readiness unavailable")

    def fail_readiness(active):
        if active:
            raise failure

    with pytest.raises(OSError) as raised:
        await adapter.consume(
            lambda _task: asyncio.sleep(0), readiness=fail_readiness
        )

    assert raised.value is failure
    assert broker_queue.cancelled
    assert not adapter.ready.is_set()


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
