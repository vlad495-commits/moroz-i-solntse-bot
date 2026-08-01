import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.booking.dispatcher import MessageDispatcher
from moroz.booking.workflow_repository import BookingWorkflowRepository
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository
from worker.main import MessageTaskHandler


pytestmark = pytest.mark.asyncio
pytest_plugins = ("tests.integration.conftest",)
auth = importlib.import_module("auth")
routes = importlib.import_module("escalation_routes")


def _user(role="owner"):
    return auth.AuthenticatedUser(
        id=7,
        username="operator",
        role=role,
        csrf_token="known-csrf",
        session_id="session-id",
    )


class _Request:
    client = SimpleNamespace(host="127.0.0.1")
    headers = {"user-agent": "pytest"}
    scope = {"root_path": "/admin"}


async def _seed(database_url, *, customer_id="700001", with_message=True):
    escalation_id = uuid4()
    async with asyncpg.create_pool(database_url, min_size=1, max_size=1) as pool:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO escalations
                    (id, source, customer_id, status, reason_code, payload)
                VALUES ($1, 'booking', $2, 'open', 'booking_outcome_unknown',
                        $3::jsonb)
                """,
                escalation_id,
                customer_id,
                json.dumps({"scenario_id": str(uuid4())}),
            )
            await connection.execute(
                """
                INSERT INTO human_mode
                    (customer_id, enabled, reason_code, escalation_id, enabled_at)
                VALUES ($1, true, 'booking_outcome_unknown', $2, now())
                """,
                customer_id,
                escalation_id,
            )
            if with_message:
                await connection.execute(
                    """
                    INSERT INTO message_inbox
                        (id, channel, external_message_id, chat_id, payload,
                         correlation_id)
                    VALUES ($1, 'telegram', $2, $3, $4::jsonb, $5)
                    """,
                    uuid4(),
                    f"update-{uuid4()}",
                    customer_id,
                    json.dumps(
                        {"kind": "text", "text": "Новое сообщение клиента"}
                    ),
                    uuid4(),
                )
    return escalation_id


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=3)
    await database.connect()
    routes.database._pool = database
    try:
        yield database
    finally:
        routes.database._pool = None
        await database.close()


async def test_auth_role_and_csrf_checks_happen_before_database(monkeypatch):
    async def current_user(_request):
        return _user(role="admin")

    class ForbiddenPool:
        def acquire(self):
            raise AssertionError("database touched before role/csrf checks")

    monkeypatch.setattr(routes, "get_current_user", current_user)
    monkeypatch.setattr(routes.database, "_pool", ForbiddenPool())
    with pytest.raises(HTTPException) as denied:
        await routes.reply_escalation(
            _Request(), uuid4(), text="Ответ", csrf_token="known-csrf"
        )
    assert denied.value.status_code == 403

    async def owner(_request):
        return _user()

    monkeypatch.setattr(routes, "get_current_user", owner)
    with pytest.raises(HTTPException) as bad_csrf:
        await routes.resolve_escalation(
            _Request(), uuid4(), reason="Проверено", csrf_token="wrong"
        )
    assert bad_csrf.value.status_code == 403

    async def bootstrap_owner(_request):
        return auth.AuthenticatedUser(
            id=None,
            username="bootstrap",
            role="owner",
            csrf_token="known-csrf",
            session_id=None,
        )

    monkeypatch.setattr(routes, "get_current_user", bootstrap_owner)
    with pytest.raises(HTTPException) as no_actor:
        await routes.reply_escalation(
            _Request(), uuid4(), text="Ответ", csrf_token="known-csrf"
        )
    assert no_actor.value.status_code == 403


async def test_list_shows_open_escalation_and_new_customer_message(
    database, migrated_database_url, monkeypatch
):
    await _seed(migrated_database_url)

    async def current_user(_request):
        return _user(role="manager")

    monkeypatch.setattr(routes, "get_current_user", current_user)
    rows = await routes.list_open_escalations_data()
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "booking_outcome_unknown"
    assert rows[0]["messages"] == ["Новое сообщение клиента"]
    assert "payload" not in rows[0]


async def test_reply_is_durable_audited_replay_safe_and_keeps_human_mode(
    database, migrated_database_url, monkeypatch
):
    escalation_id = await _seed(migrated_database_url)

    async def current_user(_request):
        return _user(role="manager")

    monkeypatch.setattr(routes, "get_current_user", current_user)
    for _ in range(2):
        response = await routes.reply_escalation(
            _Request(),
            escalation_id,
            text="  Мы разбираемся и скоро ответим.  ",
            csrf_token="known-csrf",
        )
        assert response.status_code == 302

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM outbound_messages
               WHERE idempotency_key LIKE 'escalation_reply:%') outbounds,
              (SELECT count(*) FROM task_outbox
               WHERE kind='send_outbound') tasks,
              (SELECT count(*) FROM admin_audit_events
               WHERE action='escalation.reply') audits,
              (SELECT enabled FROM human_mode WHERE escalation_id=$1) enabled
            """,
            escalation_id,
        )
    assert tuple(row.values()) == (1, 1, 1, True)


async def test_resolve_is_atomic_audited_owner_bound_and_replay_safe(
    database, migrated_database_url, monkeypatch
):
    escalation_id = await _seed(migrated_database_url)
    other_id = await _seed(migrated_database_url, customer_id="700002")

    async def current_user(_request):
        return _user()

    monkeypatch.setattr(routes, "get_current_user", current_user)
    response = await routes.resolve_escalation(
        _Request(),
        escalation_id,
        reason="Проверено вручную",
        csrf_token="known-csrf",
    )
    assert response.status_code == 302
    with pytest.raises(HTTPException) as stale:
        await routes.resolve_escalation(
            _Request(),
            escalation_id,
            reason="Повтор",
            csrf_token="known-csrf",
        )
    assert stale.value.status_code == 409

    async with database.acquire() as connection:
        resolved = await connection.fetchrow(
            """
            SELECT status, resolved_by, resolution_reason, resolved_at
            FROM escalations WHERE id=$1
            """,
            escalation_id,
        )
        modes = await connection.fetch(
            """SELECT escalation_id, enabled FROM human_mode
               WHERE escalation_id=ANY($1::uuid[]) ORDER BY escalation_id""",
            [escalation_id, other_id],
        )
        audits = await connection.fetchval(
            """SELECT count(*) FROM admin_audit_events
               WHERE action='escalation.resolve' AND object_id=$1""",
            str(escalation_id),
        )
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "7"
    assert resolved["resolution_reason"] == "Проверено вручную"
    assert resolved["resolved_at"] is not None
    assert {row["escalation_id"]: row["enabled"] for row in modes} == {
        escalation_id: False,
        other_id: True,
    }
    assert audits == 1


async def test_new_inbound_is_durable_while_human_mode_bypasses_all_ai_and_booking(
    database, migrated_database_url
):
    await _seed(migrated_database_url, with_message=False)
    repository = MessageRepository(database)
    incoming = IncomingMessage(
        update_id="human-mode-update",
        message_id="human-mode-message",
        channel="telegram",
        chat_id="700001",
        user_id="700001",
        text="Есть новости?",
        received_at=datetime(2026, 8, 2, 0, 30, tzinfo=UTC),
        correlation_id=uuid4(),
    )
    assert await repository.accept(incoming)
    workflow = SimpleNamespace(
        handle=AsyncMock(),
        start_create=AsyncMock(),
        list_bookings=AsyncMock(),
        start_reschedule=AsyncMock(),
        start_cancel=AsyncMock(),
    )
    router = AsyncMock()
    consultant = AsyncMock()
    dispatcher = MessageDispatcher(
        BookingWorkflowRepository(database),
        workflow,
        router=router,
        consultant=consultant,
    )
    handler = MessageTaskHandler(
        database,
        consultant,
        AsyncMock(),
        dispatcher=dispatcher,
        booking_interactions_enabled=True,
    )

    await handler.handle(
        QueueTask(
            kind="process_message",
            payload={"chat_id": "700001", "update_ids": ["human-mode-update"]},
            idempotency_key="process_message:human-mode-update",
        )
    )

    async with database.acquire() as connection:
        status = await connection.fetchval(
            """SELECT status FROM message_inbox
               WHERE external_message_id='human-mode-update'"""
        )
        messages = await connection.fetch(
            """SELECT role, content FROM messages
               WHERE chat_id=700001 ORDER BY id"""
        )
    assert status == "processed"
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Есть новости?"
    assert "сохранено" in messages[1]["content"].lower()
    router.assert_not_awaited()
    consultant.assert_not_awaited()
    for method in vars(workflow).values():
        method.assert_not_awaited()


@pytest.mark.parametrize("text", ["", "   ", "x" * 4001])
async def test_reply_rejects_empty_or_unbounded_text_before_db(text, monkeypatch):
    async def current_user(_request):
        return _user()

    class ForbiddenPool:
        def acquire(self):
            raise AssertionError("database touched before input validation")

    monkeypatch.setattr(routes, "get_current_user", current_user)
    monkeypatch.setattr(routes.database, "_pool", ForbiddenPool())
    with pytest.raises(HTTPException) as invalid:
        await routes.reply_escalation(
            _Request(), uuid4(), text=text, csrf_token="known-csrf"
        )
    assert invalid.value.status_code == 422
