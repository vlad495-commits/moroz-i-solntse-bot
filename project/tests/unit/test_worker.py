import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from moroz.common.queue import QueueTask
from worker import main as worker_main


class ConsumerFailure(RuntimeError):
    pass


class CleanupFailure(RuntimeError):
    pass


async def _fake_lock():
    return object()


async def _fake_close():
    return None


def test_worker_reads_explicit_pipeline_settings_without_aggregate_settings():
    source = Path("/workspace/worker/main.py").read_text(encoding="utf-8")

    assert "Settings" not in source
    for name in (
        "RABBITMQ_URL",
        "DATABASE_URL",
        "REDIS_URL",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert f'os.environ["{name}"]' in source
    assert "os.getenv" not in source


class FakeQueue:
    def __init__(self, result, *, close_error=None):
        self.result = result
        self.close_error = close_error
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
        if self.close_error:
            raise self.close_error


class FakePump:
    def __init__(self, error=None):
        self.error = error
        self.started = asyncio.Event()
        self.stopped = False

    async def run(self, stop):
        self.started.set()
        if self.error:
            raise self.error
        await stop.wait()
        self.stopped = True


class StubbornPump:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, _stop):
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()


class StubbornCleanupQueue(FakeQueue):
    def __init__(self):
        super().__init__("wait")
        self.release = asyncio.Event()

    async def consume(self, _handler, readiness=None):
        self.started.set()
        if readiness:
            readiness(True)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            await self.release.wait()
        finally:
            if readiness:
                readiness(False)


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
async def test_consumer_failure_wins_over_queue_close_failure():
    primary = ConsumerFailure("consumer failed")
    queue = FakeQueue(
        primary,
        close_error=CleanupFailure("queue close failed"),
    )

    with pytest.raises(ConsumerFailure) as raised:
        await worker_main._supervise(queue, asyncio.Event())

    assert raised.value is primary
    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_supervisor_cleanup_only_error_surfaces():
    cleanup_error = CleanupFailure("queue close failed")
    queue = FakeQueue("wait", close_error=cleanup_error)
    stop = asyncio.Event()
    supervised = asyncio.create_task(worker_main._supervise(queue, stop))
    await queue.started.wait()

    stop.set()
    with pytest.raises(CleanupFailure) as raised:
        await supervised

    assert raised.value is cleanup_error


@pytest.mark.asyncio
async def test_supervisor_runs_and_stops_pipeline_pump():
    queue = FakeQueue("wait")
    pump = FakePump()
    stop = asyncio.Event()
    supervised = asyncio.create_task(
        worker_main._supervise(queue, stop, pump=pump)
    )
    await asyncio.wait_for(pump.started.wait(), timeout=1)

    stop.set()
    await supervised

    assert pump.stopped
    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_supervisor_propagates_pipeline_pump_failure():
    queue = FakeQueue("wait")
    pump = FakePump(ConsumerFailure("pump failed"))

    with pytest.raises(ConsumerFailure, match="pump failed"):
        await worker_main._supervise(queue, asyncio.Event(), pump=pump)

    assert queue.cancelled
    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_stop_cancels_stubborn_pump_with_bounded_wait(monkeypatch):
    queue = FakeQueue("wait")
    pump = StubbornPump()
    stop = asyncio.Event()
    monkeypatch.setattr(
        worker_main, "SUPERVISOR_SHUTDOWN_TIMEOUT_SECONDS", 0.01
    )
    supervised = asyncio.create_task(
        worker_main._supervise(queue, stop, pump=pump)
    )
    await pump.started.wait()

    stop.set()
    await asyncio.wait_for(supervised, timeout=0.5)

    assert pump.cancelled.is_set()
    assert queue.close_calls == 1
    pump.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_supervisor_owns_and_cancels_prompt_reload_listener():
    queue = FakeQueue("wait")
    stop = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def prompt_listener():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    supervised = asyncio.create_task(
        worker_main._supervise(
            queue,
            stop,
            prompt_listener=prompt_listener,
        )
    )
    await started.wait()

    stop.set()
    await supervised

    assert cancelled.is_set()
    assert queue.close_calls == 1


@pytest.mark.asyncio
async def test_supervisor_uses_one_deadline_for_all_stubborn_tasks(
    monkeypatch, tmp_path
):
    queue = StubbornCleanupQueue()
    pump = StubbornPump()
    prompt_started = asyncio.Event()
    prompt_cancelled = asyncio.Event()
    prompt_release = asyncio.Event()
    readiness = tmp_path / "worker-ready"

    async def stubborn_prompt():
        prompt_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            prompt_cancelled.set()
            await prompt_release.wait()

    async def release_later():
        await asyncio.sleep(0.2)
        queue.release.set()
        pump.release.set()
        prompt_release.set()

    monkeypatch.setattr(
        worker_main,
        "SUPERVISOR_SHUTDOWN_TIMEOUT_SECONDS",
        0.05,
        raising=False,
    )
    stop = asyncio.Event()
    supervised = asyncio.create_task(
        worker_main._supervise(
            queue,
            stop,
            readiness,
            pump=pump,
            prompt_listener=stubborn_prompt,
        )
    )
    await asyncio.gather(
        queue.started.wait(),
        pump.started.wait(),
        prompt_started.wait(),
    )
    release_task = asyncio.create_task(release_later())
    started_at = asyncio.get_running_loop().time()

    stop.set()
    await supervised

    elapsed = asyncio.get_running_loop().time() - started_at
    assert elapsed < 0.15
    assert queue.cancelled
    assert pump.cancelled.is_set()
    assert prompt_cancelled.is_set()
    assert queue.close_calls == 1
    assert not readiness.exists()
    await release_task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_startup_failure_closes_every_created_runtime_resource(
    monkeypatch,
):
    closed = []

    class RuntimeDatabase:
        async def connect(self):
            pass

        async def close(self):
            closed.append("database")

    class RuntimeRedis:
        async def ping(self):
            raise AssertionError("Redis must not gate worker startup")

        async def aclose(self):
            closed.append("redis")

    class RuntimeQueue:
        async def connect(self):
            pass

        async def close(self):
            closed.append("queue")

    class RuntimeSession:
        async def close(self):
            closed.append("telegram")

    class RuntimeRepository:
        async def reconcile_stale_outbound_deliveries(self):
            return 0

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://unused")
    monkeypatch.setattr(
        worker_main,
        "Database",
        lambda *args, **kwargs: RuntimeDatabase(),
    )
    monkeypatch.setattr(
        worker_main.redis,
        "from_url",
        lambda *args, **kwargs: RuntimeRedis(),
    )
    monkeypatch.setattr(
        worker_main,
        "RabbitQueue",
        lambda *args, **kwargs: RuntimeQueue(),
    )
    monkeypatch.setattr(
        worker_main,
        "Bot",
        lambda *args, **kwargs: SimpleNamespace(session=RuntimeSession()),
    )
    monkeypatch.setattr(
        worker_main,
        "MessageRepository",
        lambda *args, **kwargs: RuntimeRepository(),
    )
    monkeypatch.setattr(
        worker_main, "_acquire_worker_lock", lambda _database: _fake_lock()
    )
    monkeypatch.setattr(
        worker_main, "_release_worker_lock", lambda _lock: _fake_close()
    )
    monkeypatch.setattr(
        worker_main,
        "init_llm",
        lambda: (_ for _ in ()).throw(RuntimeError("LLM startup failed")),
    )

    with pytest.raises(RuntimeError, match="LLM startup failed"):
        await worker_main.run()

    assert set(closed) == {"queue", "database", "redis", "telegram"}


@pytest.mark.asyncio
async def test_outer_resource_cleanup_honors_existing_shutdown_deadline():
    release = asyncio.Event()

    async def stubborn_close():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()

    deadline = asyncio.get_running_loop().time() + 0.02
    started_at = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="resource cleanup exceeded"):
        await worker_main._cleanup_all(
            stubborn_close(),
            deadline=deadline,
        )

    assert asyncio.get_running_loop().time() - started_at < 0.1
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_primary", [True, False])
async def test_run_attempts_all_cleanup_and_preserves_error_precedence(
    monkeypatch, with_primary
):
    closed = []
    primary = ConsumerFailure("runtime primary")
    queue_cleanup = CleanupFailure("queue cleanup")
    telegram_cleanup = CleanupFailure("telegram cleanup")

    class RuntimeDatabase:
        async def connect(self):
            pass

        async def close(self):
            closed.append("database")

    class RuntimeRedis:
        async def ping(self):
            pass

        async def aclose(self):
            closed.append("redis")

    class RuntimeQueue:
        async def connect(self):
            pass

        async def close(self):
            closed.append("queue")
            if not with_primary:
                raise queue_cleanup

    class RuntimeSession:
        async def close(self):
            closed.append("telegram")
            raise telegram_cleanup

    class RuntimeRepository:
        async def reconcile_stale_outbound_deliveries(self):
            return 0

    async def supervise(*args, **kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://unused")
    monkeypatch.setattr(
        worker_main, "Database", lambda *args, **kwargs: RuntimeDatabase()
    )
    monkeypatch.setattr(
        worker_main.redis,
        "from_url",
        lambda *args, **kwargs: RuntimeRedis(),
    )
    monkeypatch.setattr(
        worker_main, "RabbitQueue", lambda *args, **kwargs: RuntimeQueue()
    )
    monkeypatch.setattr(
        worker_main,
        "Bot",
        lambda *args, **kwargs: SimpleNamespace(session=RuntimeSession()),
    )
    monkeypatch.setattr(
        worker_main,
        "MessageRepository",
        lambda *args, **kwargs: RuntimeRepository(),
    )
    monkeypatch.setattr(
        worker_main, "_acquire_worker_lock", lambda _database: _fake_lock()
    )
    monkeypatch.setattr(
        worker_main, "_release_worker_lock", lambda _lock: _fake_close()
    )
    monkeypatch.setattr(worker_main, "_supervise", supervise)
    monkeypatch.setattr(
        worker_main,
        "init_llm",
        (
            (lambda: (_ for _ in ()).throw(primary))
            if with_primary
            else (lambda: None)
        ),
    )

    expected = primary if with_primary else queue_cleanup
    with pytest.raises(type(expected)) as raised:
        await worker_main.run()

    assert raised.value is expected
    assert set(closed) == {"queue", "telegram", "redis", "database"}


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
