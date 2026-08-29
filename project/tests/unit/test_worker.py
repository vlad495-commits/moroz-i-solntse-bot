import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from moroz.booking.projection import PROJECTION_SYNC_KIND
from moroz.booking.catalog import CATALOG_SYNC_KIND
from moroz.booking.yclients_catalog import YclientsCatalogError
from moroz.booking.yclients_records import YclientsProjectionError
from moroz.common.queue import MAX_RETRIES, QueueTask
from moroz.notifications.models import JobResult, SchedulerJob
from moroz.security.llm_gateway import LLMResponse, LLMUsage
from moroz.retention import (
    RETENTION_CLEANUP_KIND,
    RetentionCleanupError,
)
from worker import main as worker_main


class ConsumerFailure(RuntimeError):
    pass


class CleanupFailure(RuntimeError):
    pass


async def _fake_lock():
    return object()


async def _fake_close():
    return None


class NoopRetentionCleanup:
    def __init__(self, *_args, **_kwargs):
        pass

    async def ensure_current(self, _now):
        pass


class UsageConnection:
    def __init__(self):
        self.executions = []

    async def execute(self, query, *args):
        self.executions.append((query, args))


@pytest.mark.asyncio
async def test_worker_persists_each_consumed_usage_as_its_own_row():
    connection = UsageConnection()
    result = LLMResponse(
        "Ответ",
        12,
        5,
        1,
        17,
        "answer-model",
        (
            LLMUsage("router", 3, 1, 0, 4, "router-model"),
            LLMUsage("answer", 9, 4, 1, 13, "answer-model"),
        ),
    )

    await worker_main._persist_token_usage(connection, 81, 82, result)

    persisted_usage = [args[2:] for _query, args in connection.executions]
    assert persisted_usage == [
        ("router", 3, 1, 0, 4, "router-model"),
        ("answer", 9, 4, 1, 13, "answer-model"),
    ]
    assert all("purpose" in query for query, _args in connection.executions)


@pytest.mark.asyncio
async def test_worker_persistence_falls_back_only_for_non_zero_legacy_answer():
    connection = UsageConnection()

    await worker_main._persist_token_usage(
        connection,
        81,
        82,
        LLMResponse("Ответ", 8, 5, 1, 13, "legacy-model"),
    )
    await worker_main._persist_token_usage(
        connection,
        81,
        82,
        LLMResponse("Локально", 0, 0, 0, 0, "security-local"),
    )

    assert [args[2:] for _query, args in connection.executions] == [
        ("answer", 8, 5, 1, 13, "legacy-model"),
    ]


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
    production = Path("/workspace/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "STAFF_TELEGRAM_CHAT_ID: ${STAFF_TELEGRAM_CHAT_ID:-}" in compose
    assert "TECHNICAL_ALERT_CHAT_ID: ${TECHNICAL_ALERT_CHAT_ID:-}" in compose
    assert "BUSINESS_ALERT_CHAT_ID: ${BUSINESS_ALERT_CHAT_ID:-}" in compose
    assert (
        "TECHNICAL_ALERT_CHAT_ID: "
        "${TECHNICAL_ALERT_CHAT_ID:?set TECHNICAL_ALERT_CHAT_ID outside Git}"
        in production
    )


@pytest.mark.asyncio
async def test_worker_task_failure_emits_runtime_alert_and_preserves_error():
    task = QueueTask(
        kind="process_message",
        payload={"personal_data": "+7 999 000-00-00"},
        idempotency_key="private-id",
    )
    primary = ConsumerFailure("private provider details")
    handler = AsyncMock(side_effect=primary)
    router = SimpleNamespace(emit=AsyncMock(return_value=True))

    with pytest.raises(ConsumerFailure) as raised:
        await worker_main._handle_with_alerts(task, handler, router)

    assert raised.value is primary
    router.emit.assert_awaited_once_with(
        code="worker_task_failed",
        subject="process_message",
        severity="critical",
        text="worker task failed error_type=ConsumerFailure",
    )


@pytest.mark.asyncio
async def test_worker_alert_failure_does_not_mask_original_error(caplog):
    task = QueueTask(kind="scheduler_job", payload={}, idempotency_key="private-id")
    primary = ConsumerFailure("private provider details")
    handler = AsyncMock(side_effect=primary)
    router = SimpleNamespace(emit=AsyncMock(side_effect=RuntimeError("chat secret")))

    with pytest.raises(ConsumerFailure) as raised:
        await worker_main._handle_with_alerts(task, handler, router)

    assert raised.value is primary
    assert "worker_alert_failed error_type=RuntimeError" in caplog.text
    assert "chat secret" not in caplog.text
    assert "private provider details" not in caplog.text


@pytest.mark.asyncio
async def test_input_security_alert_uses_only_static_allowlisted_fields():
    router = SimpleNamespace(emit=AsyncMock(return_value=True))

    callback = worker_main.build_input_security_alert(router)
    await callback("private-provider-payload")

    router.emit.assert_awaited_once_with(
        code="security_down",
        subject="input_security",
        severity="CRITICAL",
        text="Input Security classifier unavailable or invalid",
    )


@pytest.mark.asyncio
async def test_output_validator_alert_uses_only_static_allowlisted_fields():
    router = SimpleNamespace(emit=AsyncMock(return_value=True))

    callback = worker_main.build_output_validator_alert(router)
    await callback("validator_unavailable")

    router.emit.assert_awaited_once_with(
        code="validator_unavailable",
        subject="output_validator",
        severity="ERROR",
        text="Output validator unavailable or invalid",
    )


@pytest.mark.asyncio
async def test_context_compactor_alert_uses_only_static_allowlisted_fields():
    router = SimpleNamespace(emit=AsyncMock(return_value=True))

    callback = worker_main.build_context_compactor_alert(router)
    await callback("compact_unavailable")

    router.emit.assert_awaited_once_with(
        code="compact_unavailable",
        subject="context_compactor",
        severity="ERROR",
        text="Context compactor unavailable or invalid",
    )


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
        "projection_sync": None,
        "catalog_sync": None,
        "retention_cleanup": None,
    }


@pytest.mark.asyncio
async def test_projection_scheduler_job_needs_only_repository_and_coordinator():
    job_id = uuid4()
    completed = []

    class SchedulerRepository:
        async def get_claimed(self, requested):
            assert requested == job_id
            return SchedulerJob(
                id=job_id, kind=PROJECTION_SYNC_KIND, run_at=None, payload={},
                idempotency_key="projection:current", attempts=0,
                booking_key=None, booking_starts_at=None,
            )

        async def complete(self, job, result):
            completed.append((job.id, result))

    class ProjectionSync:
        def __init__(self):
            self.jobs = []

        async def run(self, job):
            self.jobs.append(job)
            return JobResult.skipped("projection_busy")

    projection_sync = ProjectionSync()
    handler = worker_main.MessageTaskHandler(
        object(), object(), object(),
        scheduler_repository=SchedulerRepository(), projection_sync=projection_sync,
    )

    await handler.handle(
        QueueTask(
            kind="scheduler_job", payload={"job_id": str(job_id)},
            idempotency_key=f"scheduler_job:{job_id}",
        )
    )

    assert projection_sync.jobs[0].id == job_id
    assert completed == [(job_id, JobResult.skipped("projection_busy"))]


@pytest.mark.asyncio
async def test_staging_scheduler_smoke_is_terminal_without_dependencies():
    job_id = uuid4()
    completed = []

    class SchedulerRepository:
        async def get_claimed(self, requested):
            assert requested == job_id
            return SchedulerJob(
                id=job_id,
                kind="staging_scheduler_smoke",
                run_at=None,
                payload={},
                idempotency_key=f"staging_scheduler_smoke:{job_id}",
                attempts=0,
                booking_key=None,
                booking_starts_at=None,
            )

        async def complete(self, job, result):
            completed.append((job.id, result))

    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
    )

    await handler.handle(
        QueueTask(
            kind="scheduler_job",
            payload={"job_id": str(job_id)},
            idempotency_key=f"scheduler_job:{job_id}",
        )
    )

    assert completed == [
        (job_id, JobResult.skipped("staging_scheduler_smoke"))
    ]


@pytest.mark.asyncio
async def test_retention_scheduler_job_needs_only_repository_and_coordinator():
    job_id = uuid4()
    completed = []
    job = SchedulerJob(
        id=job_id,
        kind=RETENTION_CLEANUP_KIND,
        run_at=datetime(2026, 8, 18, tzinfo=UTC),
        payload={},
        idempotency_key="retention_cleanup:2026-08-18",
        attempts=0,
        booking_key=None,
        booking_starts_at=None,
    )

    class SchedulerRepository:
        async def get_claimed(self, requested):
            assert requested == job_id
            return job

        async def complete(self, received, result):
            completed.append((received.id, result))

    retention = SimpleNamespace(run=AsyncMock(return_value=JobResult.sent()))
    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
        retention_cleanup=retention,
    )

    await handler.handle(
        QueueTask(
            kind="scheduler_job",
            payload={"job_id": str(job_id)},
            idempotency_key=f"scheduler_job:{job_id}",
        )
    )

    retention.run.assert_awaited_once_with(job)
    assert completed == [(job_id, JobResult.sent())]


@pytest.mark.asyncio
async def test_duplicate_system_job_deliveries_execute_retention_once():
    job_id = uuid4()
    job = SchedulerJob(
        id=job_id,
        kind=RETENTION_CLEANUP_KIND,
        run_at=datetime(2026, 8, 18, tzinfo=UTC),
        payload={},
        idempotency_key="retention_cleanup:2026-08-18",
        attempts=0,
        booking_key=None,
        booking_starts_at=None,
    )

    class SchedulerRepository:
        def __init__(self):
            self.claimed = job
            self.completed = 0

        async def get_claimed(self, _requested):
            return self.claimed

        async def complete(self, _job, _result):
            self.completed += 1
            self.claimed = None

    class RetentionCleanup:
        def __init__(self):
            self.runs = 0

        async def run(self, _job):
            self.runs += 1
            await asyncio.sleep(0)
            return JobResult.sent()

    repository = SchedulerRepository()
    retention = RetentionCleanup()
    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=repository,
        retention_cleanup=retention,
    )
    task = QueueTask(
        kind="scheduler_job",
        payload={"job_id": str(job_id)},
        idempotency_key=f"scheduler_job:{job_id}",
    )

    await asyncio.gather(handler.handle(task), handler.handle(task))

    assert retention.runs == 1
    assert repository.completed == 1


@pytest.mark.asyncio
async def test_retention_failure_records_only_allowlisted_code():
    job_id = uuid4()
    failures = []
    job = SchedulerJob(
        id=job_id,
        kind=RETENTION_CLEANUP_KIND,
        run_at=datetime(2026, 8, 18, tzinfo=UTC),
        payload={},
        idempotency_key="retention_cleanup:2026-08-18",
        attempts=0,
        booking_key=None,
        booking_starts_at=None,
    )

    class SchedulerRepository:
        async def get_claimed(self, _requested):
            return job

        async def record_failure(self, received, *, error_code, terminal):
            failures.append((received.id, error_code, terminal))

    retention = SimpleNamespace(run=AsyncMock(side_effect=RetentionCleanupError()))
    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
        retention_cleanup=retention,
    )

    with pytest.raises(RetentionCleanupError):
        await handler.handle(
            QueueTask(
                kind="scheduler_job",
                payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )

    assert failures == [(job_id, "retention_cleanup_failed", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempts", "terminal", "code", "persisted_code"),
    [
        (0, False, "yclients_http_status", "yclients_http_status"),
        (MAX_RETRIES, True, "yclients_http_status", "yclients_http_status"),
        (0, False, "private provider details", "YclientsProjectionError"),
    ],
)
async def test_projection_failure_records_only_allowlisted_code(
    attempts, terminal, code, persisted_code
):
    job_id = uuid4()
    failures = []

    class SchedulerRepository:
        async def get_claimed(self, _requested):
            return SchedulerJob(
                id=job_id, kind=PROJECTION_SYNC_KIND, run_at=None, payload={},
                idempotency_key="projection:current", attempts=attempts,
                booking_key=None, booking_starts_at=None,
            )

        async def record_failure(self, job, *, error_code, terminal):
            failures.append((job.id, error_code, terminal))

    class ProjectionSync:
        async def run(self, _job):
            raise YclientsProjectionError(code)

    handler = worker_main.MessageTaskHandler(
        object(), object(), object(),
        scheduler_repository=SchedulerRepository(), projection_sync=ProjectionSync(),
    )

    with pytest.raises(YclientsProjectionError):
        await handler.handle(
            QueueTask(
                kind="scheduler_job", payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )

    assert failures == [(job_id, persisted_code, terminal)]


@pytest.mark.asyncio
async def test_projection_scheduler_job_without_coordinator_fails_closed():
    job_id = uuid4()
    loaded = []
    failures = []

    class SchedulerRepository:
        async def get_claimed(self, requested):
            loaded.append(requested)
            return SchedulerJob(
                id=job_id, kind=PROJECTION_SYNC_KIND, run_at=None, payload={},
                idempotency_key="projection:current", attempts=0,
                booking_key=None, booking_starts_at=None,
            )

        async def record_failure(self, job, *, error_code, terminal):
            failures.append((job.id, error_code, terminal))

    handler = worker_main.MessageTaskHandler(
        object(), object(), object(), scheduler_repository=SchedulerRepository()
    )

    with pytest.raises(RuntimeError, match="projection sync is not configured"):
        await handler.handle(
            QueueTask(
                kind="scheduler_job", payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )

    assert loaded == [job_id, job_id]
    assert failures == [(job_id, "RuntimeError", False)]


@pytest.mark.asyncio
async def test_scheduler_repository_is_checked_before_loading_a_job():
    handler = worker_main.MessageTaskHandler(object(), object(), object())
    job_id = uuid4()

    with pytest.raises(RuntimeError, match="scheduler job dependencies are not configured"):
        await handler.handle(
            QueueTask(
                kind="scheduler_job", payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )


def test_yclients_services_are_disabled_when_required_config_is_empty(
    monkeypatch,
):
    for name in (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
    ):
        monkeypatch.setenv(name, "")

    assert worker_main._build_yclients_services(object()) == (
        None, None, None, None,
    )


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
def test_yclients_services_reject_partial_required_config(
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
        worker_main._build_yclients_services(object())


def test_yclients_services_build_one_shared_config_graph(monkeypatch):
    database = object()
    built = []
    config = object()

    class Config:
        @classmethod
        def from_env(cls, _env):
            built.append(("config",))
            return config

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

    class RecordsReader:
        def __init__(self, received_config):
            assert received_config is config
            built.append(("reader", received_config))

    class ProjectionRepository:
        def __init__(self, received_database):
            assert received_database is database
            built.append(("projection_repository", received_database))

    class SchedulerRepository:
        def __init__(self, received_database):
            assert received_database is database
            built.append(("scheduler_repository", received_database))

    class ProjectionSync:
        def __init__(self, repository, reader, scheduler, *, clock):
            assert callable(clock)
            built.append(("projection_sync", repository, reader, scheduler))

    class CatalogReader:
        def __init__(self, received_config):
            assert received_config is config
            built.append(("catalog_reader", received_config))

    class CatalogRepository:
        def __init__(self, received_database):
            assert received_database is database
            built.append(("catalog_repository", received_database))

    class CatalogSync:
        def __init__(self, repository, reader, scheduler, *, clock):
            assert callable(clock)
            built.append(("catalog_sync", repository, reader, scheduler))

    monkeypatch.setenv("YCLIENTS_PARTNER_TOKEN", "partner")
    monkeypatch.setenv("YCLIENTS_USER_TOKEN", "user")
    monkeypatch.setenv("YCLIENTS_COMPANY_ID", "17")
    monkeypatch.setattr(worker_main, "YclientsAdapter", Adapter)
    monkeypatch.setattr(worker_main, "YclientsConfig", Config)
    monkeypatch.setattr(worker_main, "FeedbackService", Feedback)
    monkeypatch.setattr(worker_main, "LifecycleService", Lifecycle)
    monkeypatch.setattr(worker_main, "YclientsRecordsReader", RecordsReader)
    monkeypatch.setattr(worker_main, "ProjectionRepository", ProjectionRepository)
    monkeypatch.setattr(worker_main, "SchedulerJobRepository", SchedulerRepository)
    monkeypatch.setattr(worker_main, "ProjectionSyncCoordinator", ProjectionSync)
    monkeypatch.setattr(worker_main, "YclientsCatalogReader", CatalogReader)
    monkeypatch.setattr(worker_main, "CatalogRepository", CatalogRepository)
    monkeypatch.setattr(worker_main, "CatalogSyncCoordinator", CatalogSync)

    lifecycle, projection_sync, catalog_sync, catalog_repository = (
        worker_main._build_yclients_services(database)
    )

    assert isinstance(lifecycle, Lifecycle)
    assert isinstance(projection_sync, ProjectionSync)
    assert isinstance(catalog_sync, CatalogSync)
    assert isinstance(catalog_repository, CatalogRepository)
    assert [entry[0] for entry in built] == [
        "config", "adapter", "feedback", "lifecycle", "reader",
        "projection_repository", "scheduler_repository", "projection_sync",
        "catalog_reader", "catalog_repository", "scheduler_repository",
        "catalog_sync",
    ]


@pytest.mark.asyncio
async def test_configured_worker_ensures_current_projection_before_queue_consume(
    monkeypatch,
):
    events = []

    class RuntimeDatabase:
        async def connect(self):
            events.append("database_connect")

        async def close(self):
            events.append("database_close")

    class RuntimeRedis:
        async def aclose(self):
            events.append("redis_close")

    class RuntimeQueue:
        async def connect(self):
            events.append("queue_connect")

        async def close(self):
            events.append("queue_close")

    class RuntimeSession:
        async def close(self):
            events.append("telegram_close")

    class RuntimeRepository:
        async def reconcile_stale_outbound_deliveries(self):
            return 0

    class ProjectionSync:
        async def ensure_current(self, now):
            assert isinstance(now, datetime)
            assert now.tzinfo is UTC
            events.append("ensure_current")

    class CatalogSync:
        async def ensure_current(self, now):
            assert isinstance(now, datetime)
            assert now.tzinfo is UTC
            events.append("catalog_ensure_current")

    class RetentionCleanup:
        def __init__(
            self, received_database, scheduler, *, retention_days
        ):
            assert isinstance(received_database, RuntimeDatabase)
            assert scheduler is not None
            assert retention_days == 1095

        async def ensure_current(self, now):
            assert isinstance(now, datetime)
            assert now.tzinfo is UTC
            events.append("retention_ensure_current")

    async def supervise(*_args, **_kwargs):
        events.append("supervise")

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://unused")
    monkeypatch.setattr(worker_main, "Database", lambda *args, **kwargs: RuntimeDatabase())
    monkeypatch.setattr(worker_main.redis, "from_url", lambda *args, **kwargs: RuntimeRedis())
    monkeypatch.setattr(worker_main, "RabbitQueue", lambda *args, **kwargs: RuntimeQueue())
    monkeypatch.setattr(worker_main, "Bot", lambda *args, **kwargs: SimpleNamespace(session=RuntimeSession()))
    monkeypatch.setattr(worker_main, "MessageRepository", lambda *args, **kwargs: RuntimeRepository())
    monkeypatch.setattr(worker_main, "_acquire_worker_lock", lambda _database: _fake_lock())
    monkeypatch.setattr(worker_main, "_release_worker_lock", lambda _lock: _fake_close())
    monkeypatch.setattr(worker_main, "DATA_RETENTION_DAYS", 1095, raising=False)
    monkeypatch.setattr(
        worker_main,
        "RetentionCleanupCoordinator",
        RetentionCleanup,
        raising=False,
    )
    monkeypatch.setattr(
        worker_main,
        "_build_yclients_services",
        lambda _database: (None, ProjectionSync(), CatalogSync(), object()),
    )
    monkeypatch.setattr(worker_main, "init_llm", lambda: None)
    monkeypatch.setattr(worker_main, "_supervise", supervise)

    await worker_main.run()

    assert events.count("ensure_current") == 1
    assert events.count("catalog_ensure_current") == 1
    assert events.count("retention_ensure_current") == 1
    assert events.index("database_connect") < events.index("ensure_current") < events.index("queue_connect")
    assert events.index("database_connect") < events.index(
        "retention_ensure_current"
    ) < events.index("queue_connect")


@pytest.mark.asyncio
async def test_catalog_scheduler_failure_records_only_allowlisted_code():
    job_id = uuid4()
    failures = []

    class SchedulerRepository:
        async def get_claimed(self, _requested):
            return SchedulerJob(
                id=job_id, kind=CATALOG_SYNC_KIND, run_at=None, payload={},
                idempotency_key="catalog:current", attempts=0,
                booking_key=None, booking_starts_at=None,
            )

        async def record_failure(self, job, *, error_code, terminal):
            failures.append((job.id, error_code, terminal))

    class CatalogSync:
        async def run(self, _job):
            raise YclientsCatalogError("private provider body")

    handler = worker_main.MessageTaskHandler(
        object(), object(), object(),
        scheduler_repository=SchedulerRepository(), catalog_sync=CatalogSync(),
    )

    with pytest.raises(YclientsCatalogError):
        await handler.handle(QueueTask(
            kind="scheduler_job", payload={"job_id": str(job_id)},
            idempotency_key=f"scheduler_job:{job_id}",
        ))

    assert failures == [(job_id, "YclientsCatalogError", False)]


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
        worker_main, "RetentionCleanupCoordinator", NoopRetentionCleanup
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
    monkeypatch.setattr(
        worker_main, "RetentionCleanupCoordinator", NoopRetentionCleanup
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
