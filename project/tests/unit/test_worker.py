import asyncio
from pathlib import Path

import pytest

from moroz.common.queue import QueueTask
from worker import main as worker_main


class ConsumerFailure(RuntimeError):
    pass


def test_worker_reads_only_rabbitmq_url_without_aggregate_settings():
    source = Path("/workspace/worker/main.py").read_text(encoding="utf-8")

    assert "Settings" not in source
    assert 'os.environ["RABBITMQ_URL"]' in source
    assert "os.getenv" not in source


class FakeQueue:
    def __init__(self, result):
        self.result = result
        self.started = asyncio.Event()
        self.cancelled = False
        self.close_calls = 0
        self.ready = asyncio.Event()

    async def consume(self, _handler, readiness=None):
        self.ready.set()
        if readiness:
            readiness(True)
        self.started.set()
        try:
            if isinstance(self.result, Exception):
                raise self.result
            if self.result == "return":
                return
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.ready.clear()
            if readiness:
                readiness(False)

    async def close(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_unknown_task_fails_closed_without_logging_payload_or_identifiers(caplog):
    task = QueueTask(
        kind="unsupported",
        payload={"personal_data": "private payload"},
        idempotency_key="private identifier",
    )

    with pytest.raises(NotImplementedError, match="No worker task handlers"):
        await worker_main.handle(task)

    assert "private payload" not in caplog.text
    assert "private identifier" not in caplog.text


@pytest.mark.asyncio
async def test_consumer_failure_is_propagated_and_queue_closed_once():
    queue = FakeQueue(ConsumerFailure("consumer failed"))

    with pytest.raises(ConsumerFailure, match="consumer failed"):
        await worker_main._supervise(queue, asyncio.Event())

    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_consumer_normal_exit_is_failure_and_queue_closed_once():
    queue = FakeQueue("return")

    with pytest.raises(RuntimeError, match="Consumer stopped unexpectedly"):
        await worker_main._supervise(queue, asyncio.Event())

    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_stop_cancels_consumer_and_closes_queue_once():
    queue = FakeQueue("wait")
    stop = asyncio.Event()
    supervised = asyncio.create_task(worker_main._supervise(queue, stop))
    await asyncio.wait_for(queue.started.wait(), timeout=1)

    stop.set()
    await supervised

    assert queue.cancelled
    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_worker_readiness_file_exists_only_for_active_consumer(tmp_path):
    queue = FakeQueue("wait")
    stop = asyncio.Event()
    readiness = tmp_path / "worker-ready"
    readiness.write_text("stale", encoding="utf-8")

    supervised = asyncio.create_task(worker_main._supervise(queue, stop, readiness))
    await asyncio.wait_for(queue.started.wait(), timeout=1)
    for _ in range(100):
        if readiness.exists() and readiness.read_text(encoding="utf-8") == "ready":
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("readiness file was not refreshed")

    stop.set()
    await supervised

    assert not readiness.exists()


@pytest.mark.asyncio
async def test_worker_removes_readiness_file_after_consumer_failure(tmp_path):
    queue = FakeQueue(ConsumerFailure("consumer failed"))
    readiness = tmp_path / "worker-ready"

    with pytest.raises(ConsumerFailure):
        await worker_main._supervise(queue, asyncio.Event(), readiness)

    assert not readiness.exists()


@pytest.mark.asyncio
async def test_worker_fails_if_readiness_file_cannot_be_published(tmp_path):
    queue = FakeQueue("wait")
    missing_parent = tmp_path / "missing" / "worker-ready"

    with pytest.raises(FileNotFoundError):
        await worker_main._supervise(queue, asyncio.Event(), missing_parent)

    assert queue.close_calls == 1
