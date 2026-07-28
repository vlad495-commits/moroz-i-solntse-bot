import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from webhook import create_app


pytestmark = pytest.mark.asyncio


class FakeConnection:
    def __init__(self, *, error=None, delay=0):
        self.error = error
        self.delay = delay
        self.queries = []

    async def fetchval(self, query):
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return 1


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class FakeDatabase:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


class ForbiddenDependency:
    def __getattr__(self, name):
        raise AssertionError(f"health endpoint contacted forbidden dependency: {name}")


def health_app(database):
    app = create_app(
        database_url="postgresql://not-used",
        bot=ForbiddenDependency(),
        webhook_secret="health-test-secret",
    )
    app.state.database = database
    app.state.redis = ForbiddenDependency()
    app.state.telegram = ForbiddenDependency()
    return app


async def get_health(database):
    app = health_app(database)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get("/healthz")


async def test_health_returns_fixed_ok_body_when_postgres_is_ready():
    connection = FakeConnection()

    response = await get_health(FakeDatabase(connection))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert connection.queries == ["SELECT 1"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(
            "postgresql://admin:secret@internal-db/private contains sensitive detail"
        ),
        TimeoutError("internal dependency timed out"),
    ],
)
async def test_health_failure_returns_fixed_safe_unavailable_body(error):
    response = await get_health(FakeDatabase(FakeConnection(error=error)))

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "postgres" not in response.text.lower()
    assert "secret" not in response.text.lower()
    assert "internal" not in response.text.lower()


async def test_health_timeout_returns_fixed_unavailable_body(monkeypatch):
    monkeypatch.setattr("webhook.HEALTH_TIMEOUT_SECONDS", 0.001)

    response = await get_health(FakeDatabase(FakeConnection(delay=0.1)))

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
