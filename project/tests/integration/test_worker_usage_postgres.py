from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository
from moroz.security.llm_gateway import LLMResponse, LLMUsage
from worker.main import MessageTaskHandler


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def test_message_transaction_persists_physical_usage_once(database):
    await MessageRepository(database).accept(
        IncomingMessage(
            update_id="usage-1",
            message_id="message-1",
            channel="telegram",
            chat_id="81",
            user_id="82",
            text="Синтетический вопрос",
            received_at=datetime(2026, 8, 25, tzinfo=UTC),
            correlation_id=uuid4(),
        )
    )
    calls = 0

    async def llm(_text, _context, **_options):
        nonlocal calls
        calls += 1
        return LLMResponse(
            "Синтетический ответ",
            12,
            5,
            1,
            17,
            "answer-model",
            (
                LLMUsage("router", 3, 1, 0, 4, "router-model"),
                LLMUsage("compact", 2, 1, 0, 3, "compact-model"),
                LLMUsage("answer", 9, 4, 1, 13, "answer-model"),
            ),
        )

    handler = MessageTaskHandler(database, llm, telegram=None)
    task = QueueTask(
        kind="process_message",
        payload={"update_ids": ["usage-1"]},
        idempotency_key="process_message:usage-1",
    )

    await handler.handle(task)
    await handler.handle(task)

    async with database.acquire() as connection:
        message = await connection.fetchrow(
            "SELECT id, llm_usage_tracked FROM messages "
            "WHERE chat_id = 81 AND role = 'user'"
        )
        rows = await connection.fetch(
            "SELECT source_message_id, purpose, prompt_tokens, "
            "completion_tokens, cached_tokens, total_tokens, model "
            "FROM token_usage ORDER BY id"
        )

    assert calls == 1
    assert message["llm_usage_tracked"] is True
    assert {row["source_message_id"] for row in rows} == {message["id"]}
    assert [tuple(row.values()) for row in rows] == [
        (message["id"], "router", 3, 1, 0, 4, "router-model"),
        (message["id"], "compact", 2, 1, 0, 3, "compact-model"),
        (message["id"], "answer", 9, 4, 1, 13, "answer-model"),
    ]


async def test_human_mode_marks_message_observed_without_usage(database):
    async with database.acquire() as connection:
        await connection.execute(
            "INSERT INTO human_mode "
            "(customer_id, enabled, reason_code, enabled_at) "
            "VALUES ('93', true, 'admin_handoff', $1)",
            datetime(2026, 8, 30, tzinfo=UTC),
        )
    await MessageRepository(database).accept(
        IncomingMessage(
            update_id="human-usage-1",
            message_id="human-message-1",
            channel="telegram",
            chat_id="93",
            user_id="94",
            text="Сообщение оператору",
            received_at=datetime(2026, 8, 30, tzinfo=UTC),
            correlation_id=uuid4(),
        )
    )

    async def forbidden_llm(*_args, **_kwargs):
        raise AssertionError("human mode must not call LLM")

    await MessageTaskHandler(database, forbidden_llm, telegram=None).handle(
        QueueTask(
            kind="process_message",
            payload={"update_ids": ["human-usage-1"]},
            idempotency_key="process_message:human-usage-1",
        )
    )
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT llm_usage_tracked FROM messages WHERE chat_id = 93"
        )
        usage_count = await connection.fetchval(
            "SELECT count(*) FROM token_usage WHERE chat_id = 93"
        )
    assert row["llm_usage_tracked"] is True
    assert usage_count == 0


async def test_worker_passes_only_40_previous_messages_without_current_input(database):
    async with database.acquire() as connection:
        await connection.executemany(
            "INSERT INTO messages (chat_id, user_id, role, content) "
            "VALUES ($1, $2, $3, $4)",
            [
                (91, 92, "user" if index % 2 == 0 else "assistant", f"old-{index}")
                for index in range(45)
            ],
        )
    await MessageRepository(database).accept(
        IncomingMessage(
            update_id="context-1",
            message_id="message-context-1",
            channel="telegram",
            chat_id="91",
            user_id="92",
            text="CURRENT-BUFFERED-INPUT",
            received_at=datetime(2026, 8, 26, tzinfo=UTC),
            correlation_id=uuid4(),
        )
    )
    captured = {}

    async def llm(text, context, **_options):
        captured["text"] = text
        captured["context"] = context
        return LLMResponse("Ответ", 0, 0, 0, 0, "local")

    await MessageTaskHandler(database, llm, telegram=None).handle(
        QueueTask(
            kind="process_message",
            payload={"update_ids": ["context-1"]},
            idempotency_key="process_message:context-1",
        )
    )

    assert captured["text"] == "CURRENT-BUFFERED-INPUT"
    assert [item["content"] for item in captured["context"]] == [
        f"old-{index}" for index in range(5, 45)
    ]
    assert all(
        item["content"] != "CURRENT-BUFFERED-INPUT"
        for item in captured["context"]
    )
