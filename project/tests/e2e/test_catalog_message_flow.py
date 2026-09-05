from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.booking.catalog import (
    CatalogGrounding,
    CatalogService,
    CatalogVariant,
)
from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import process_message_key
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.messaging.router import RouteDecision, RouterVerdict
from moroz.security.llm_gateway import LLMResponse
from moroz.security.input_security import (
    InputSecurityDecision,
    InputSecurityVerdict,
)
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts
from worker.main import MessageTaskHandler


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class FakeTelegram:
    async def send_message(self, **_kwargs):
        return SimpleNamespace(message_id=1)


class ForbiddenGateway:
    def __init__(self):
        self.calls = 0

    async def complete(self, _request):
        self.calls += 1
        raise AssertionError("simple catalog answer must not call LLM")


class SecurityOnlyGateway(ForbiddenGateway):
    async def complete(self, request):
        self.calls += 1
        if request.purpose != "security":
            raise AssertionError("stale catalog must not call answer LLM")
        return LLMResponse(
            "OK",
            1, 1, 0, 2, "security-test",
        )


class PriceRouter:
    def __init__(self):
        self.calls = 0

    async def route(self, _text, _context):
        self.calls += 1
        return RouterVerdict(RouteDecision('consultation', .99, 'price', 'Криотерапия'))


class AllowingInputSecurity:
    async def classify(self, _masked_text):
        return InputSecurityVerdict(
            InputSecurityDecision("allow", "llm", "ok")
        )


class CatalogRepository:
    def __init__(self, grounding):
        self.grounding = grounding
        self.calls = []

    async def ground(self, connection, text, now):
        self.calls.append((connection, text, now))
        return self.grounding


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=2)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def grounding():
    return CatalogGrounding(
        "fresh",
        (
            CatalogService(
                "20", "Криотерапия", "Крио",
                (
                    CatalogVariant(
                        "10", "Анна", Decimal("1230.00"),
                        Decimal("1230.00"), 3,
                    ),
                ),
            ),
        ),
        "price",
        False,
    )


def incoming(update_id="catalog-1", text="Сколько стоит криотерапия?"):
    return IncomingMessage(
        update_id=update_id,
        message_id=update_id,
        channel="telegram",
        chat_id="42",
        user_id="7",
        text=text,
        received_at=NOW,
        correlation_id=uuid4(),
    )


async def test_fresh_simple_catalog_reply_is_atomic_and_duplicate_safe(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming())
    gateway = ForbiddenGateway()
    router = PriceRouter()
    pipeline = SecurityPipeline(
        gateway,
        "",
        extract_structured_facts(""),
        router=router,
        input_security=AllowingInputSecurity(),
    )

    async def llm(text, context, *, recent_message_count, catalog):
        return await pipeline.respond(
            text, context, recent_message_count=recent_message_count,
            catalog=catalog,
        )

    catalog_repository = CatalogRepository(grounding())
    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
        catalog_repository=catalog_repository,
        catalog_grounding_enabled=True,
        clock=lambda: NOW,
    )
    task = QueueTask(
        kind="process_message",
        payload={"chat_id": "42", "update_ids": ["catalog-1"]},
        idempotency_key=process_message_key(["catalog-1"]),
    )

    await handler.handle(task)
    await handler.handle(task)

    async with database.acquire() as connection:
        messages = await connection.fetch(
            "SELECT role, content FROM messages ORDER BY id"
        )
        outbound = await connection.fetch(
            "SELECT text, status FROM outbound_messages"
        )
        usage = await connection.fetchrow(
            "SELECT prompt_tokens, completion_tokens, total_tokens, model "
            "FROM token_usage"
        )
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert "1 230 ₽" in messages[-1]["content"]
    assert len(outbound) == 1
    assert usage is None
    assert len(catalog_repository.calls) == 1
    assert gateway.calls == 0
    assert router.calls == 1


async def test_pre_yclients_mode_skips_catalog_and_uses_normal_answer(database):
    repository = MessageRepository(database)
    assert await repository.accept(
        incoming("catalog-disabled", "Сколько стоит криокапсула?")
    )
    catalog_repository = CatalogRepository(grounding())
    calls = []

    async def llm(text, context, *, recent_message_count):
        calls.append((text, context, recent_message_count))
        return LLMResponse(
            "Криокапсула — 1 500 ₽ по базе знаний.",
            10, 5, 0, 15, "answer-test",
        )

    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
        catalog_repository=catalog_repository,
        catalog_grounding_enabled=False,
        clock=lambda: NOW,
    )
    await handler.handle(QueueTask(
        kind="process_message",
        payload={"chat_id": "42", "update_ids": ["catalog-disabled"]},
        idempotency_key=process_message_key(["catalog-disabled"]),
    ))

    async with database.acquire() as connection:
        answer = await connection.fetchval(
            "SELECT content FROM messages WHERE role = 'assistant'"
        )
    assert answer == "Криокапсула — 1 500 ₽ по базе знаний."
    assert len(calls) == 1
    assert catalog_repository.calls == []


async def test_human_mode_never_reads_catalog_or_calls_llm(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("catalog-human"))
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('42', true, 'low_feedback_rating', $1, now())
            """,
            uuid4(),
        )

    class ForbiddenCatalog:
        async def ground(self, *_args):
            raise AssertionError("human mode must not read catalog")

    async def forbidden_llm(*_args, **_kwargs):
        raise AssertionError("human mode must not call LLM")

    handler = MessageTaskHandler(
        database,
        forbidden_llm,
        TelegramSender(FakeTelegram(), repository),
        catalog_repository=ForbiddenCatalog(),
        catalog_grounding_enabled=True,
        clock=lambda: NOW,
    )
    await handler.handle(QueueTask(
        kind="process_message",
        payload={"chat_id": "42", "update_ids": ["catalog-human"]},
        idempotency_key=process_message_key(["catalog-human"]),
    ))

    async with database.acquire() as connection:
        roles = await connection.fetch("SELECT role FROM messages")
        outbound = await connection.fetchval(
            "SELECT count(*) FROM outbound_messages"
        )
    assert [row["role"] for row in roles] == ["user"]
    assert outbound == 0


async def test_complex_catalog_grounding_reaches_llm_without_extra_history(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("catalog-complex", "Сравни криотерапию"))
    complex_grounding = CatalogGrounding(
        "fresh", grounding().services, None, False,
    )
    catalog_repository = CatalogRepository(complex_grounding)
    calls = []

    async def llm(text, context, *, recent_message_count, catalog):
        resolved = await catalog(RouteDecision('consultation', .99, service='Криотерапия'))
        calls.append((text, context, recent_message_count, resolved))
        return LLMResponse("Сравнение по актуальному каталогу", 4, 3, 0, 7, "fake")

    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
        catalog_repository=catalog_repository,
        catalog_grounding_enabled=True,
        clock=lambda: NOW,
    )
    await handler.handle(QueueTask(
        kind="process_message",
        payload={"chat_id": "42", "update_ids": ["catalog-complex"]},
        idempotency_key=process_message_key(["catalog-complex"]),
    ))

    async with database.acquire() as connection:
        contents = await connection.fetch(
            "SELECT content FROM messages ORDER BY id"
        )
    assert calls[0][3] == complex_grounding
    assert [row["content"] for row in contents] == [
        "Сравни криотерапию",
        "Сравнение по актуальному каталогу",
    ]
    assert "UNTRUSTED_CATALOG_DATA" not in "".join(
        row["content"] for row in contents
    )


async def test_catalog_reply_rolls_back_when_outbound_insert_fails(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("catalog-rollback"))
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_catalog_outbound() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'forced catalog outbound failure';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await connection.execute(
            """
            CREATE TRIGGER reject_catalog_outbound
            BEFORE INSERT ON outbound_messages
            FOR EACH ROW EXECUTE FUNCTION reject_catalog_outbound()
            """
        )

    gateway = ForbiddenGateway()
    pipeline = SecurityPipeline(
        gateway,
        "",
        extract_structured_facts(""),
        router=PriceRouter(),
        input_security=AllowingInputSecurity(),
    )

    async def llm(text, context, *, recent_message_count, catalog):
        return await pipeline.respond(
            text, context, recent_message_count=recent_message_count,
            catalog=catalog,
        )

    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
        catalog_repository=CatalogRepository(grounding()),
        catalog_grounding_enabled=True,
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(Exception, match="forced catalog outbound failure"):
            await handler.handle(QueueTask(
                kind="process_message",
                payload={"chat_id": "42", "update_ids": ["catalog-rollback"]},
                idempotency_key=process_message_key(["catalog-rollback"]),
            ))
    finally:
        async with database.acquire() as connection:
            await connection.execute(
                "DROP TRIGGER reject_catalog_outbound ON outbound_messages"
            )
            await connection.execute("DROP FUNCTION reject_catalog_outbound()")

    async with database.acquire() as connection:
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM messages) AS messages,
                (SELECT count(*) FROM token_usage) AS usage,
                (SELECT count(*) FROM outbound_messages) AS outbound,
                (SELECT status FROM message_inbox
                 WHERE external_message_id = 'catalog-rollback') AS inbox_status
            """
        )
    assert tuple(counts.values()) == (0, 0, 0, "accepted")


async def test_stale_catalog_never_reuses_price_from_history(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("catalog-stale"))
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO messages (chat_id, user_id, role, content)
            VALUES (42, 7, 'assistant', 'Старая цена 9999 руб.')
            """
        )
    stale = CatalogGrounding("stale", (), "price", False)
    gateway = SecurityOnlyGateway()
    pipeline = SecurityPipeline(gateway, "", extract_structured_facts(""), router=PriceRouter())

    async def llm(text, context, *, recent_message_count, catalog):
        return await pipeline.respond(
            text, context, recent_message_count=recent_message_count,
            catalog=catalog,
        )

    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
        catalog_repository=CatalogRepository(stale),
        catalog_grounding_enabled=True,
        clock=lambda: NOW,
    )
    await handler.handle(QueueTask(
        kind="process_message",
        payload={"chat_id": "42", "update_ids": ["catalog-stale"]},
        idempotency_key=process_message_key(["catalog-stale"]),
    ))

    async with database.acquire() as connection:
        answer = await connection.fetchval(
            "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        )
    assert "9999" not in answer
    assert "администратор" in answer.lower()
    assert gateway.calls == 1
