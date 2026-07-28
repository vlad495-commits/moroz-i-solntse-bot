import importlib

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import AuthenticatedUser


system_metrics = importlib.import_module("system_metrics")
metrics_routes = importlib.import_module("metrics_routes")


def owner():
    return AuthenticatedUser(
        id=1,
        username="owner",
        role="owner",
        csrf_token="csrf",
        session_id="session",
    )


def admin():
    return AuthenticatedUser(
        id=2,
        username="admin",
        role="admin",
        csrf_token="csrf",
        session_id="session",
    )


class FakeRedis:
    def __init__(self, error=None, close_error=None):
        self.error = error
        self.close_error = close_error
        self.closed = False

    async def ping(self):
        if self.error:
            raise self.error
        return True

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakeResponse:
    def __init__(self, text="", error=None):
        self.text = text
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error


class FakeHttpClient:
    def __init__(self, responses, close_error=None):
        self.responses = list(responses)
        self.close_error = close_error
        self.urls = []
        self.closed = False

    async def get(self, url):
        self.urls.append(url)
        return self.responses.pop(0)

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def snapshot():
    return {
        "bot_inbound_messages_total": 12,
        "worker_processed_messages_total": 9,
        "inbox_accepted_messages": 3,
        "inbox_oldest_age_seconds": 14.5,
        "task_outbox_pending_messages": 2,
        "task_outbox_published_total": 10,
        "outbound_messages": {
            "pending": 1,
            "sent": 8,
            "delivery_unknown": 1,
        },
        "scheduler_jobs": {
            "pending": 2,
            "finished": 7,
            "failed": 1,
        },
        "retained_llm_calls": 6,
        "retained_llm_tokens": 1234,
        "open_escalations": 2,
    }


@pytest.mark.asyncio
async def test_collector_exports_real_postgres_redis_and_rabbit_metrics():
    redis_client = FakeRedis()
    rabbit_client = FakeHttpClient(
        [
            FakeResponse(
                "\n".join(
                    (
                        '# TYPE rabbitmq_detailed_queue_messages_ready gauge',
                        'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="tasks"} 4',
                        'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="tasks.dlq"} 2',
                        'rabbitmq_detailed_queue_messages{vhost="/",queue="other"} 99',
                    )
                )
            ),
        ]
    )

    registry = await system_metrics.collect_system_metrics(
        postgres_loader=lambda: _async_value(snapshot()),
        redis_client=redis_client,
        rabbit_client=rabbit_client,
        rabbitmq_metrics_url="http://rabbitmq:15692",
    )
    text = registry.to_prometheus()

    assert "moroz_postgres_available 1.0" in text
    assert "moroz_redis_available 1.0" in text
    assert "moroz_rabbitmq_available 1.0" in text
    assert "moroz_bot_inbound_messages_total 12.0" in text
    assert "moroz_worker_processed_messages_total 9.0" in text
    assert "moroz_inbox_accepted_messages 3.0" in text
    assert "moroz_inbox_oldest_age_seconds 14.5" in text
    assert "moroz_task_outbox_pending_messages 2.0" in text
    assert "moroz_task_outbox_published_total 10.0" in text
    assert 'moroz_outbound_messages{status="sent"} 8.0' in text
    assert 'moroz_scheduler_jobs{status="failed"} 1.0' in text
    assert "moroz_retained_llm_calls 6.0" in text
    assert "moroz_retained_llm_tokens 1234.0" in text
    assert "moroz_open_escalations 2.0" in text
    assert 'moroz_queue_ready_messages{queue="tasks"} 4.0' in text
    assert 'moroz_queue_ready_messages{queue="tasks.dlq"} 2.0' in text
    assert rabbit_client.urls == [
        (
            "http://rabbitmq:15692/metrics/detailed"
            "?family=queue_coarse_metrics&vhost=%2F"
        ),
    ]
    assert redis_client.closed is True
    assert rabbit_client.closed is True


@pytest.mark.asyncio
async def test_collector_marks_failed_sources_without_inventing_samples():
    sensitive = RuntimeError(
        "amqp://owner:secret@rabbitmq and phone +79990000000 must not leak"
    )
    rabbit_client = FakeHttpClient([FakeResponse(error=sensitive)])

    async def broken_postgres():
        raise sensitive

    registry = await system_metrics.collect_system_metrics(
        postgres_loader=broken_postgres,
        redis_client=FakeRedis(error=sensitive),
        rabbit_client=rabbit_client,
        rabbitmq_metrics_url="http://rabbitmq:15692",
    )
    text = registry.to_prometheus()

    assert "moroz_postgres_available 0.0" in text
    assert "moroz_redis_available 0.0" in text
    assert "moroz_rabbitmq_available 0.0" in text
    assert "moroz_bot_inbound_messages_total" not in text
    assert "moroz_queue_ready_messages" not in text
    assert "secret" not in text
    assert "+7999" not in text


@pytest.mark.asyncio
async def test_collector_degrades_when_redis_client_cannot_be_created(monkeypatch):
    def fail_redis_client(*_args, **_kwargs):
        raise ValueError("redis://owner:secret@internal must not leak")

    monkeypatch.setattr(system_metrics.redis, "from_url", fail_redis_client)
    rabbit_client = FakeHttpClient(
        [
            FakeResponse(
                'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="tasks"} 0\n'
                'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="tasks.dlq"} 0\n'
            ),
        ]
    )

    registry = await system_metrics.collect_system_metrics(
        postgres_loader=lambda: _async_value(snapshot()),
        rabbit_client=rabbit_client,
        rabbitmq_metrics_url="http://rabbitmq:15692",
    )

    assert "moroz_redis_available 0.0" in registry.to_prometheus()
    assert "secret" not in registry.to_prometheus()


@pytest.mark.asyncio
async def test_collector_ignores_dependency_cleanup_errors():
    sensitive = RuntimeError("secret cleanup detail")
    redis_client = FakeRedis(close_error=sensitive)
    rabbit_client = FakeHttpClient(
        [
            FakeResponse(
                'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="tasks"} 0\n'
                'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="tasks.dlq"} 0\n'
            ),
        ],
        close_error=sensitive,
    )

    registry = await system_metrics.collect_system_metrics(
        postgres_loader=lambda: _async_value(snapshot()),
        redis_client=redis_client,
        rabbit_client=rabbit_client,
        rabbitmq_metrics_url="http://rabbitmq:15692",
    )
    text = registry.to_prometheus()

    assert "moroz_redis_available 1.0" in text
    assert "moroz_rabbitmq_available 1.0" in text
    assert redis_client.closed is True
    assert rabbit_client.closed is True
    assert "secret" not in text


@pytest.mark.asyncio
async def test_metrics_route_checks_owner_before_collecting(monkeypatch):
    collected = False

    async def current_user(_request):
        return admin()

    async def forbidden_collect():
        nonlocal collected
        collected = True
        raise AssertionError("collector must not run before RBAC")

    monkeypatch.setattr(metrics_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        metrics_routes, "collect_system_metrics", forbidden_collect, raising=False
    )
    app = FastAPI()
    app.include_router(metrics_routes.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/metrics")

    assert response.status_code == 403
    assert collected is False


@pytest.mark.asyncio
async def test_metrics_route_returns_fresh_collector_output(monkeypatch):
    calls = 0

    async def current_user(_request):
        return owner()

    async def collect():
        nonlocal calls
        calls += 1
        registry = system_metrics.MetricsRegistry()
        registry.set_gauge("moroz_postgres_available", 1)
        return registry

    monkeypatch.setattr(metrics_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        metrics_routes, "collect_system_metrics", collect, raising=False
    )
    app = FastAPI()
    app.include_router(metrics_routes.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.get("/metrics")
        second = await client.get("/metrics")

    assert first.status_code == second.status_code == 200
    assert first.text == second.text == "moroz_postgres_available 1.0\n"
    assert calls == 2


async def _async_value(value):
    return value
