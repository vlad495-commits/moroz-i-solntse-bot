import importlib
from datetime import UTC, datetime

import pytest


user_repository = importlib.import_module("user_repository")


class FakeConnection:
    def __init__(self):
        self.fetchrow_calls = []
        self.execute_calls = []
        self.row = None

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


@pytest.mark.asyncio
async def test_get_active_session_returns_joined_enabled_user_and_updates_last_seen(monkeypatch):
    connection = FakeConnection()
    connection.row = {
        "session_id": "sid",
        "user_id": 7,
        "username": "owner",
        "role": "owner",
        "csrf_token": "csrf-token",
        "expires_at": datetime.now(UTC),
    }
    monkeypatch.setattr(user_repository.database, "_pool", FakePool(connection))

    session = await user_repository.get_active_session("sid")

    assert session["session_id"] == "sid"
    assert session["user_id"] == 7
    assert session["username"] == "owner"
    assert session["role"] == "owner"
    assert session["csrf_token"] == "csrf-token"
    assert "admin_sessions" in connection.fetchrow_calls[0][0]
    assert "expires_at > now()" in connection.fetchrow_calls[0][0]
    assert "enabled = TRUE" in connection.fetchrow_calls[0][0]
    assert "UPDATE admin_sessions" in connection.execute_calls[0][0]
    assert connection.execute_calls[0][1] == ("sid",)


@pytest.mark.asyncio
async def test_get_active_session_returns_none_for_missing_or_expired_session(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(user_repository.database, "_pool", FakePool(connection))

    session = await user_repository.get_active_session("missing")

    assert session is None
    assert connection.execute_calls == []


@pytest.mark.asyncio
async def test_delete_session_removes_single_session(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(user_repository.database, "_pool", FakePool(connection))

    await user_repository.delete_session("sid")

    assert "DELETE FROM admin_sessions" in connection.execute_calls[0][0]
    assert connection.execute_calls[0][1] == ("sid",)
