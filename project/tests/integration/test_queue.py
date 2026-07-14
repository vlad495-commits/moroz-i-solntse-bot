import json
import os
from dataclasses import FrozenInstanceError

import aio_pika
import pytest
import pytest_asyncio
from aio_pika.exceptions import ChannelInvalidStateError

from moroz.common.queue import QueueTask, RabbitQueue
from worker.main import handle as worker_handle


@pytest_asyncio.fixture
async def rabbit_queue():
    queue = RabbitQueue(os.environ["RABBITMQ_URL"])
    await queue.connect()

    admin_connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
    admin_channel = await admin_connection.channel()
    tasks = await admin_channel.get_queue("tasks")
    dead_letters = await admin_channel.get_queue("tasks.dlq")
    await tasks.purge()
    await dead_letters.purge()
    try:
        yield queue, tasks, dead_letters
    finally:
        await queue.close()
        await admin_connection.close()


def test_queue_task_json_round_trip_and_is_immutable():
    task = QueueTask(
        kind="ping",
        payload={"value": 7},
        idempotency_key="ping:7",
    )

    assert QueueTask.from_json(task.to_json()) == task
    with pytest.raises(FrozenInstanceError):
        task.kind = "changed"


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        "{}",
        json.dumps({"kind": "", "payload": {}, "idempotency_key": "key"}),
        json.dumps({"kind": "ping", "payload": [], "idempotency_key": "key"}),
        json.dumps({"kind": "ping", "payload": {}, "idempotency_key": ""}),
    ],
)
def test_queue_task_rejects_malformed_json(raw):
    with pytest.raises(ValueError):
        QueueTask.from_json(raw)


@pytest.mark.asyncio
async def test_queue_round_trip(rabbit_queue):
    queue, tasks, _ = rabbit_queue
    received = []

    async def handle(task):
        received.append(task)

    await queue.publish(
        QueueTask(kind="ping", payload={"value": 7}, idempotency_key="ping:7")
    )
    await queue.consume_one(handle)

    assert received[0].payload == {"value": 7}
    assert await tasks.get(fail=False) is None


@pytest.mark.asyncio
async def test_failed_task_is_retried_three_times_then_sent_to_dlq(rabbit_queue):
    queue, tasks, dead_letters = rabbit_queue
    calls = 0
    task = QueueTask(
        kind="always-fails",
        payload={"value": 9},
        idempotency_key="always-fails:9",
    )

    async def fail(_task):
        nonlocal calls
        calls += 1
        raise RuntimeError("expected handler failure")

    await queue.publish(task)
    for _ in range(4):
        await queue.consume_one(fail)

    assert calls == 4
    assert await tasks.get(fail=False) is None
    dead_letter = await dead_letters.get(fail=False)
    assert dead_letter is not None
    assert dead_letter.message_id == task.idempotency_key
    assert dead_letter.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert dead_letter.headers["x-retry-count"] == 3
    assert QueueTask.from_json(dead_letter.body.decode()) == task
    await dead_letter.ack()
    assert await dead_letters.get(fail=False) is None


@pytest.mark.asyncio
async def test_queue_can_connect_again_after_close(rabbit_queue):
    queue, tasks, _ = rabbit_queue
    received = []

    async def handle(task):
        received.append(task)

    await queue.close()
    await queue.connect()
    await queue.publish(
        QueueTask(kind="reconnect", payload={}, idempotency_key="reconnect:1")
    )
    await queue.consume_one(handle)

    assert received[0].kind == "reconnect"
    assert await tasks.get(fail=False) is None


@pytest.mark.asyncio
async def test_failed_republish_keeps_original_for_reconnect(rabbit_queue):
    queue, tasks, _ = rabbit_queue
    task = QueueTask(
        kind="publish-failure",
        payload={},
        idempotency_key="publish-failure:1",
    )
    received = []

    async def close_channel_then_fail(_task):
        await queue._channel.close()
        raise RuntimeError("handler failure before unavailable republish")

    async def handle(redelivered):
        received.append(redelivered)

    await queue.publish(task)
    with pytest.raises(ChannelInvalidStateError):
        await queue.consume_one(close_channel_then_fail)

    await queue.connect()
    await queue.consume_one(handle)

    assert received == [task]
    assert await tasks.get(fail=False) is None


@pytest.mark.asyncio
async def test_raw_message_preserves_metadata_and_normalizes_invalid_retry_header(
    rabbit_queue,
):
    queue, tasks, dead_letters = rabbit_queue
    handler_calls = 0
    publisher = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
    channel = await publisher.channel(publisher_confirms=True)
    exchange = await channel.get_exchange("tasks")

    async def handle(_task):
        nonlocal handler_calls
        handler_calls += 1

    try:
        await exchange.publish(
            aio_pika.Message(
                body=b"not-json",
                correlation_id="raw-correlation",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers={
                    "custom-header": "preserved",
                    "x-retry-count": "invalid",
                },
                message_id="raw-message",
            ),
            routing_key="tasks",
        )
        for _ in range(4):
            await queue.consume_one(handle)

        assert handler_calls == 0
        assert await tasks.get(fail=False) is None
        dead_letter = await dead_letters.get(fail=False)
        assert dead_letter is not None
        assert dead_letter.correlation_id == "raw-correlation"
        assert dead_letter.headers["custom-header"] == "preserved"
        assert dead_letter.headers["x-retry-count"] == 3
        await dead_letter.ack()
    finally:
        await publisher.close()


@pytest.mark.asyncio
async def test_unsupported_worker_task_retries_then_reaches_dlq(rabbit_queue):
    queue, tasks, dead_letters = rabbit_queue
    task = QueueTask(
        kind="unsupported",
        payload={"personal_data": "must not be logged"},
        idempotency_key="unsupported:1",
    )

    await queue.publish(task)
    for _ in range(4):
        await queue.consume_one(worker_handle)

    assert await tasks.get(fail=False) is None
    dead_letter = await dead_letters.get(fail=False)
    assert dead_letter is not None
    assert QueueTask.from_json(dead_letter.body.decode()) == task
    assert dead_letter.headers["x-retry-count"] == 3
    await dead_letter.ack()
