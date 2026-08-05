import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from moroz.booking.interaction import Interaction
from moroz.booking.models import SlotQuery
from moroz.common.queue import MAX_RETRIES, QueueTask
from moroz.notifications.models import JobResult, SchedulerJob
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


def test_worker_compose_forwards_staff_chat_id():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")

    assert "STAFF_TELEGRAM_CHAT_ID: ${STAFF_TELEGRAM_CHAT_ID:-}" in compose


def test_compose_shares_booking_gate_without_leaking_yclients_to_bot():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  yclients-smoke:\n", 1)[0]
    bot = compose.split("\n  bot:\n", 1)[1].split("\n  admin:\n", 1)[0]
    gate = (
        "BOOKING_INTERACTIONS_ENABLED: "
        "${BOOKING_INTERACTIONS_ENABLED:-false}"
    )

    assert gate in worker
    assert gate in bot
    for secret in (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_SERVICE_ALLOWLIST",
        "YCLIENTS_STAFF_ALLOWLIST",
    ):
        assert secret in worker
        assert secret not in bot


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


@pytest.mark.parametrize("kind", ["callback", "contact"])
def test_non_text_payload_guard_raises_safe_retryable_error(kind, caplog):
    secret = "private callback or phone"

    with pytest.raises(
        RuntimeError,
        match="non-text interaction requires structured dispatcher",
    ) as raised:
        worker_main._require_text_payloads(
            [{"kind": kind, "text": "[private]", "data": {"secret": secret}}]
        )

    assert secret not in str(raised.value)
    assert secret not in caplog.text


def _persisted_payload(
    *,
    update_id="1",
    chat_id="101",
    user_id="101",
    kind="text",
    text="Вопрос",
    data=None,
):
    return {
        "update_id": update_id,
        "message_id": "10",
        "channel": "telegram",
        "chat_id": chat_id,
        "user_id": user_id,
        "text": text,
        "received_at": "2026-08-01T12:00:00+00:00",
        "correlation_id": str(uuid4()),
        "kind": kind,
        "data": {} if data is None else data,
    }


def test_text_payloads_normalize_as_one_private_interaction():
    interaction = worker_main._normalize_persisted_interaction(
        [
            _persisted_payload(update_id="1", text="Первое"),
            _persisted_payload(update_id="2", text="Второе"),
        ],
        "process_message:1,2",
        processing_consent=False,
    )

    assert interaction == Interaction.text(
        interaction.owner,
        "process_message:1,2",
        "Первое\nВторое",
    )
    assert interaction.owner.chat_id == interaction.owner.customer_id == "101"


@pytest.mark.parametrize(
    "payloads",
    [
        [_persisted_payload(chat_id="101", user_id="102")],
        [
            _persisted_payload(update_id="1"),
            _persisted_payload(
                update_id="2",
                kind="callback",
                text="[booking callback]",
                data={"callback_data": "booking:opaque"},
            ),
        ],
        [
            _persisted_payload(
                kind="callback",
                text="[booking callback]",
                data={"callback_data": "booking:opaque", "extra": "no"},
            )
        ],
        [
            _persisted_payload(
                kind="contact",
                text="[contact]",
                data={"phone_number": 79990000000},
            )
        ],
    ],
)
def test_persisted_identity_kind_and_data_schema_fail_closed(payloads, caplog):
    with pytest.raises(ValueError, match="persisted interaction is invalid"):
        worker_main._normalize_persisted_interaction(
            payloads,
            "process_message:strict",
            processing_consent=True,
        )

    assert "79990000000" not in caplog.text
    assert "booking:opaque" not in caplog.text


def test_contact_requires_current_durable_consent_without_leaking_phone(caplog):
    phone = "+79990000000"
    payload = _persisted_payload(
        kind="contact",
        text="[contact]",
        data={"phone_number": phone},
    )

    with pytest.raises(ValueError, match="persisted interaction is invalid") as raised:
        worker_main._normalize_persisted_interaction(
            [payload],
            "process_message:contact",
            processing_consent=False,
        )

    assert phone not in str(raised.value)
    assert phone not in caplog.text


def test_enabled_booking_runtime_rejects_disabled_or_incomplete_mode():
    with pytest.raises(RuntimeError, match="booking mode must be ready"):
        worker_main._build_booking_dispatcher(
            object(),
            enabled=True,
            mode="disabled",
            service_allowlist=(),
            staff_allowlist=(),
            env={},
        )

    with pytest.raises(ValueError, match="staff Telegram chat"):
        worker_main._build_booking_dispatcher(
            object(),
            enabled=True,
            mode="mock",
            service_allowlist=("1",),
            staff_allowlist=("7",),
            env={},
        )
    with pytest.raises(ValueError, match="YCLIENTS booking configuration"):
        worker_main._build_booking_dispatcher(
            object(),
            enabled=True,
            mode="real",
            service_allowlist=("1",),
            staff_allowlist=("7",),
            env={
                "YCLIENTS_COMPANY_ID": "42",
                "STAFF_TELEGRAM_CHAT_ID": "900001",
            },
        )


def test_disabled_booking_runtime_preserves_legacy_without_dependencies():
    assert worker_main._build_booking_dispatcher(
        object(),
        enabled=False,
        mode="disabled",
        service_allowlist=(),
        staff_allowlist=(),
        env={},
    ) is None


@pytest.mark.asyncio
async def test_mock_booking_runtime_is_exact_and_clock_deterministic():
    fixed_now = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    build = lambda: worker_main._build_booking_dispatcher(
        object(),
        enabled=True,
        mode="mock",
        service_allowlist=("1", "2"),
        staff_allowlist=("7",),
        env={"STAFF_TELEGRAM_CHAT_ID": "900001"},
        now=lambda: fixed_now,
    )
    dispatcher = build()

    workflow = dispatcher._workflow
    assert type(workflow._catalog).__name__ == "MockBookingCatalog"
    assert type(workflow._booking_port).__name__ == "_RuntimeMockYclientsAdapter"
    query = SlotQuery(
        ("1",),
        fixed_now,
        fixed_now + timedelta(days=14),
        None,
    )
    single = await workflow._booking_port.list_slots(query)
    repeated = await build()._workflow._booking_port.list_slots(query)
    multiple = await workflow._booking_port.list_slots(
        SlotQuery(
            ("1", "2"),
            fixed_now,
            fixed_now + timedelta(days=14),
            None,
        )
    )

    assert single
    assert [item.id for item in single] == [item.id for item in repeated]
    assert [item.starts_at for item in single] == [
        item.starts_at for item in repeated
    ]
    assert {item.service_ids for item in single} == {("1",)}
    assert {item.service_ids for item in multiple} == {("1", "2")}
    assert [item.id for item in single] != [item.id for item in multiple]
    assert all(
        item.starts_at.tzinfo is not None
        for item in single
    )


def test_real_booking_runtime_shares_one_http_boundary_without_network_call():
    dispatcher = worker_main._build_booking_dispatcher(
        object(),
        enabled=True,
        mode="real",
        service_allowlist=("1",),
        staff_allowlist=("7",),
        env={
            "YCLIENTS_PARTNER_TOKEN": "synthetic-partner",
            "YCLIENTS_USER_TOKEN": "synthetic-user",
            "YCLIENTS_COMPANY_ID": "42",
            "YCLIENTS_BASE_URL": "https://provider.invalid",
            "STAFF_TELEGRAM_CHAT_ID": "900001",
        },
    )

    workflow = dispatcher._workflow
    assert workflow._booking_port._http is workflow._catalog._client


@pytest.mark.asyncio
async def test_real_booking_preflight_runs_same_readonly_gate(monkeypatch):
    calls = []

    async def fake_check(catalog, availability, **kwargs):
        calls.append((catalog, availability, kwargs))
        return object()

    monkeypatch.setattr(worker_main, "run_readonly_check", fake_check)

    await worker_main._preflight_real_booking(
        mode="real",
        service_allowlist=("1",),
        staff_allowlist=("7",),
        env={
            "YCLIENTS_PARTNER_TOKEN": "synthetic-partner",
            "YCLIENTS_USER_TOKEN": "synthetic-user",
            "YCLIENTS_COMPANY_ID": "42",
            "YCLIENTS_BASE_URL": "https://provider.invalid",
        },
        now=lambda: datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
    )

    assert len(calls) == 1
    catalog, availability, kwargs = calls[0]
    assert catalog._client is availability._http
    assert kwargs == {
        "service_ids": ("1",),
        "staff_ids": ("7",),
        "environment_label": "worker-startup",
        "now": datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        "horizon_days": 14,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "mock"])
async def test_non_real_booking_preflight_is_network_free(monkeypatch, mode):
    check = AsyncMock(side_effect=AssertionError("must stay network-free"))
    monkeypatch.setattr(worker_main, "run_readonly_check", check)

    assert await worker_main._preflight_real_booking(
        mode=mode,
        service_allowlist=(),
        staff_allowlist=(),
        env={},
    ) is None
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_task_handler_processes_scheduler_job():
    job_id = uuid4()
    completed = []
    lifecycle = object()

    class SchedulerRepository:
        async def get_claimed(self, requested):
            assert requested == job_id
            return SchedulerJob(
                id=job_id,
                kind="booking_created",
                run_at=None,
                payload={},
                idempotency_key="booking:1:booking_created",
                attempts=0,
                booking_key=uuid4(),
                booking_starts_at=None,
            )

        async def complete(self, job, result):
            completed.append((job.id, result))

    scheduler_handler = AsyncMock(return_value=JobResult.sent())

    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
        booking_port="booking-port",
        notification_outbox="outbox",
        lifecycle=lifecycle,
        scheduler_handler=scheduler_handler,
    )

    await handler.handle(
        QueueTask(
            kind="scheduler_job",
            payload={"job_id": str(job_id)},
            idempotency_key=f"scheduler_job:{job_id}",
        )
    )

    assert completed == [(job_id, JobResult.sent())]
    assert scheduler_handler.await_args.args[0].id == job_id
    assert scheduler_handler.await_args.kwargs == {
        "booking_port": "booking-port",
        "outbox": "outbox",
        "lifecycle": lifecycle,
    }


def test_lifecycle_service_is_disabled_when_required_config_is_empty(
    monkeypatch,
):
    for name in (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    ):
        monkeypatch.setenv(name, "")

    assert worker_main._build_lifecycle_service(object()) is None


@pytest.mark.parametrize(
    "configured",
    [
        {"YCLIENTS_PARTNER_TOKEN": "partner"},
        {"YCLIENTS_USER_TOKEN": "user"},
        {"YCLIENTS_COMPANY_ID": "17"},
        {
            "YCLIENTS_PARTNER_TOKEN": "partner",
            "YCLIENTS_USER_TOKEN": "user",
        },
    ],
)
def test_lifecycle_service_rejects_partial_required_config(
    monkeypatch,
    configured,
):
    for name in (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    ):
        monkeypatch.setenv(name, configured.get(name, ""))

    with pytest.raises(
        ValueError,
        match="YCLIENTS lifecycle configuration is incomplete",
    ):
        worker_main._build_lifecycle_service(object())


def test_lifecycle_service_builds_one_real_adapter_graph(monkeypatch):
    database = object()
    built = []

    class Adapter:
        def __init__(self, config):
            built.append(("adapter", config))

    class Feedback:
        def __init__(self, received_database):
            assert received_database is database
            built.append(("feedback", received_database))

    class Lifecycle:
        def __init__(self, received_database, adapter, feedback):
            assert received_database is database
            built.append(("lifecycle", adapter, feedback))

    monkeypatch.setenv("YCLIENTS_PARTNER_TOKEN", "partner")
    monkeypatch.setenv("YCLIENTS_USER_TOKEN", "user")
    monkeypatch.setenv("YCLIENTS_COMPANY_ID", "17")
    monkeypatch.setattr(worker_main, "YclientsAdapter", Adapter)
    monkeypatch.setattr(worker_main, "FeedbackService", Feedback)
    monkeypatch.setattr(worker_main, "LifecycleService", Lifecycle)

    service = worker_main._build_lifecycle_service(database)

    assert isinstance(service, Lifecycle)
    assert [entry[0] for entry in built] == ["adapter", "feedback", "lifecycle"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempts", "terminal"),
    [(0, False), (MAX_RETRIES, True)],
)
async def test_scheduler_job_failure_tracks_retry_and_terminal_dlq(
    attempts,
    terminal,
):
    job_id = uuid4()
    failures = []

    class SchedulerRepository:
        async def get_claimed(self, _requested):
            return SchedulerJob(
                id=job_id,
                kind="booking_created",
                run_at=None,
                payload={},
                idempotency_key="booking:1:booking_created",
                attempts=attempts,
                booking_key=uuid4(),
                booking_starts_at=None,
            )

        async def record_failure(self, job, *, error_code, terminal):
            failures.append((job.id, error_code, terminal))

    async def failing_handler(_job, **_kwargs):
        raise RuntimeError("private provider details")

    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
        booking_port="booking-port",
        notification_outbox="outbox",
        scheduler_handler=failing_handler,
    )

    with pytest.raises(RuntimeError, match="private provider details"):
        await handler.handle(
            QueueTask(
                kind="scheduler_job",
                payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )

    assert failures == [(job_id, "RuntimeError", terminal)]


@pytest.mark.asyncio
async def test_lifecycle_job_without_service_uses_existing_failure_path():
    job_id = uuid4()
    booking_key = uuid4()
    starts_at = object()
    failures = []

    class SchedulerRepository:
        async def get_claimed(self, _requested):
            return SchedulerJob(
                id=job_id,
                kind="no_show_check",
                run_at=None,
                payload={},
                idempotency_key="booking:1:no_show_check",
                attempts=0,
                booking_key=booking_key,
                booking_starts_at=starts_at,
            )

        async def record_failure(self, job, *, error_code, terminal):
            failures.append((job.id, error_code, terminal))

    class BookingPort:
        async def get_booking(self, requested):
            assert requested == booking_key
            return SimpleNamespace(
                booking_key=booking_key,
                starts_at=starts_at,
                status="confirmed",
            )

    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
        booking_port=BookingPort(),
        notification_outbox=object(),
        lifecycle=None,
    )

    with pytest.raises(
        RuntimeError,
        match="lifecycle service is not configured",
    ):
        await handler.handle(
            QueueTask(
                kind="scheduler_job",
                payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )

    assert failures == [(job_id, "RuntimeError", False)]


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
