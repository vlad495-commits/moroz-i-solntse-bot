import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as redis
from httpx import ASGITransport, AsyncClient

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.router import RouteDecision, RouterVerdict
from moroz.messaging.outbox import OutboxRelay, process_message_key
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.security.llm_gateway import (
    LLMResponse,
    NonRetryableLLMError,
    PrimaryReserveGateway,
    RetryableLLMError,
)
from moroz.security.output_validator import (
    OutputValidationDecision,
    OutputValidationVerdict,
)
from moroz.security.pipeline import (
    INPUT_BLOCK_REPLY,
    MEDICAL_ESCALATION_REPLY,
    SAFE_OUTPUT_FALLBACK,
    SecurityPipeline,
)
from moroz.security.validator import extract_structured_facts
from webhook import create_app
from worker.main import MessageTaskHandler, PipelinePump


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
WEBHOOK_SECRET = "test-webhook-secret"


class FakeTelegram:
    def __init__(self):
        self.sent_messages = []
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        return None

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_messages))


class RecordingQueue:
    def __init__(self):
        self.tasks = []

    async def publish(self, task):
        self.tasks.append(task)


class ForbiddenGateway:
    def __init__(self):
        self.calls = 0

    async def complete(self, _request):
        self.calls += 1
        raise AssertionError("local security decision must not call a provider")


class ScriptedProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BlockingSecurityGateway:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if request.purpose == "security":
            self.started.set()
            await self.release.wait()
            return _response("OK")
        if request.purpose == "validator":
            return _response(
                json.dumps({"action": "allow", "category": "safe"})
            )
        return _response("Безопасный ответ.")


class AllowingOutputValidator:
    async def validate(self, **_kwargs):
        return OutputValidationVerdict(
            OutputValidationDecision("allow", "llm", "safe")
        )


class AllowingInputSecurity:
    async def classify(self, _masked_text):
        from moroz.security.input_security import (
            InputSecurityDecision,
            InputSecurityVerdict,
        )

        return InputSecurityVerdict(InputSecurityDecision("allow", "llm", "ok"))


class ImmediateRouter:
    def __init__(self, decision):
        self.decision = decision
        self.started = asyncio.Event()
        self.calls = []

    async def route(self, text, context):
        self.calls.append((text, context))
        self.started.set()
        return RouterVerdict(self.decision, ())


class ForbiddenRouter:
    def __init__(self):
        self.calls = 0

    async def route(self, _text, _context):
        self.calls += 1
        raise AssertionError("local security decision must not call Router")


def _response(text: str = "Безопасный ответ.") -> LLMResponse:
    return LLMResponse(text, 1, 1, 0, 2, "scripted")


def _incoming(update_id: str) -> IncomingMessage:
    return IncomingMessage(
        update_id=update_id,
        message_id=update_id,
        channel="telegram",
        chat_id="42",
        user_id="7",
        text=f"Сообщение {update_id}",
        received_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        correlation_id=uuid4(),
    )


def _telegram_update() -> dict:
    return {
        "update_id": 1200,
        "message": {
            "message_id": 1200,
            "date": 1_768_478_400,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "Тест"},
            "text": "Текст до согласия",
        },
    }


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=5)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def redis_client():
    client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def test_worker_passes_exact_recent_count_and_persists_local_reply(database):
    repository = MessageRepository(database)
    update_ids = [str(1300 + index) for index in range(11)]
    for update_id in update_ids:
        assert await repository.accept(_incoming(update_id))

    gateway = ForbiddenGateway()
    pipeline = SecurityPipeline(gateway, "", extract_structured_facts(""))
    recent_counts = []

    async def secured_llm(text, context, *, recent_message_count):
        recent_counts.append(recent_message_count)
        return await pipeline.respond(
            text,
            context,
            recent_message_count=recent_message_count,
        )

    handler = MessageTaskHandler(
        database,
        secured_llm,
        TelegramSender(FakeTelegram(), repository),
    )
    await handler.handle(
        QueueTask(
            kind="process_message",
            payload={"chat_id": "42", "update_ids": update_ids},
            idempotency_key=process_message_key(update_ids),
        )
    )

    async with database.acquire() as connection:
        processed = await connection.fetchval(
            "SELECT count(*) FROM message_inbox WHERE status = 'processed'"
        )
        messages = await connection.fetch(
            "SELECT role, content FROM messages ORDER BY id"
        )
        outbound = await connection.fetchrow(
            "SELECT text, status FROM outbound_messages"
        )
        send_tasks = await connection.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind = 'send_outbound'"
        )

    assert recent_counts == [11]
    assert gateway.calls == 0
    assert processed == 11
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == INPUT_BLOCK_REPLY
    assert tuple(outbound.values()) == (INPUT_BLOCK_REPLY, "pending")
    assert send_tasks == 1


async def test_pre_consent_update_has_no_inbox_history_security_or_provider_call(
    database,
    redis_client,
    migrated_database_url,
):
    telegram = FakeTelegram()
    app = create_app(
        database_url=migrated_database_url,
        redis_url=os.environ["REDIS_URL"],
        bot=telegram,
        webhook_secret=WEBHOOK_SECRET,
    )
    security_calls = 0
    provider = ForbiddenGateway()

    async def security_boundary(_text, _context, *, recent_message_count):
        nonlocal security_calls
        security_calls += 1
        return await SecurityPipeline(
            provider,
            "",
            extract_structured_facts(""),
        ).respond(
            "safe",
            [],
            recent_message_count=recent_message_count,
        )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        ) as client:
            response = await client.post("/telegram/webhook", json=_telegram_update())

        queue = RecordingQueue()
        repository = MessageRepository(database)
        await PipelinePump(
            MessageBuffer(redis_client, database),
            OutboxRelay(database, queue),
            repository,
        ).run_once()
        handler = MessageTaskHandler(
            database,
            security_boundary,
            TelegramSender(telegram, repository),
        )
        for task in queue.tasks:
            await handler.handle(task)

    async with database.acquire() as connection:
        inbox = await connection.fetchval("SELECT count(*) FROM message_inbox")
        history = await connection.fetchval("SELECT count(*) FROM messages")
        process_tasks = await connection.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
        )

    assert response.status_code == 200
    assert inbox == history == process_tasks == 0
    assert security_calls == provider.calls == 0


async def test_no_downstream_state_before_allow_and_no_synthetic_route_state(database):
    repository = MessageRepository(database)
    update_id = "router-gate"
    assert await repository.accept(_incoming(update_id))
    gateway = BlockingSecurityGateway()
    router = ImmediateRouter(RouteDecision("escalation", 0.91))
    pipeline = SecurityPipeline(
        gateway,
        "",
        extract_structured_facts(""),
        router=router,
    )

    async def secured_llm(text, context, *, recent_message_count):
        return await pipeline.respond(
            text,
            context,
            recent_message_count=recent_message_count,
        )

    handler = MessageTaskHandler(
        database,
        secured_llm,
        TelegramSender(FakeTelegram(), repository),
    )
    task = asyncio.create_task(
        handler.handle(
            QueueTask(
                kind="process_message",
                payload={"chat_id": "42", "update_ids": [update_id]},
                idempotency_key=process_message_key([update_id]),
            )
        )
    )
    await asyncio.wait_for(gateway.started.wait(), 1)
    await asyncio.wait_for(router.started.wait(), 1)

    async with database.acquire() as connection:
        before_allow = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM booking_scenarios) AS booking,
                (SELECT count(*) FROM escalations) AS handoff,
                (SELECT count(*) FROM outbound_messages) AS outbound
            """
        )
    assert tuple(before_allow.values()) == (0, 0, 0)

    gateway.release.set()
    await task

    async with database.acquire() as connection:
        after_allow = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM booking_scenarios) AS booking,
                (SELECT count(*) FROM escalations) AS handoff,
                (SELECT count(*) FROM outbound_messages) AS outbound
            """
        )
    assert tuple(after_allow.values()) == (0, 0, 1)
    answer_request = next(
        request for request in gateway.requests if request.purpose == "answer"
    )
    answer_system = answer_request.messages[0]["content"]
    assert "route=escalation" in answer_system
    assert "source=llm" in answer_system


async def test_medical_risk_is_authoritative_and_never_calls_router():
    gateway = ForbiddenGateway()
    router = ForbiddenRouter()

    result = await SecurityPipeline(
        gateway,
        "",
        extract_structured_facts(""),
        router=router,
    ).respond("Не могу дышать", [])

    assert result.text == MEDICAL_ESCALATION_REPLY
    assert gateway.calls == 0
    assert router.calls == 0


@pytest.mark.parametrize(
    ("user_message", "placeholder"),
    [
        ("Телефон +7 999 123-45-67", "<PII_PHONE_1>"),
        ("Почта test@example.ru", "<PII_EMAIL_1>"),
        ("Меня зовут Анна Иванова", "<PII_NAME_1>"),
        ("Адрес: г. Москва, ул. Тверская, д. 1", "<PII_ADDRESS_1>"),
        ("Карта 4111 1111 1111 1111", "<PII_PAYMENT_1>"),
        ("Диагноз: сахарный диабет", "<PII_MEDICAL_1>"),
    ],
)
async def test_security_pipeline_masks_each_critical_pii_class(
    user_message,
    placeholder,
):
    primary = ScriptedProvider([_response()])
    result = await SecurityPipeline(
        PrimaryReserveGateway(primary),
        "",
        extract_structured_facts(""),
        input_security=AllowingInputSecurity(),
    ).respond(user_message, [])

    assert result.text == "Безопасный ответ."
    assert primary.calls == 1
    sent = repr(primary.requests)
    assert placeholder in sent
    assert user_message not in sent


@pytest.mark.parametrize(
    ("primary_outcome", "reserve_outcomes", "primary_calls", "reserve_calls"),
    [
        (RetryableLLMError(), [_response()], 1, 1),
        (RetryableLLMError(), [RetryableLLMError()], 1, 1),
        (NonRetryableLLMError(), [_response()], 1, 0),
    ],
)
async def test_security_pipeline_enforces_provider_fallback_matrix(
    primary_outcome,
    reserve_outcomes,
    primary_calls,
    reserve_calls,
):
    primary = ScriptedProvider([primary_outcome])
    reserve = ScriptedProvider(reserve_outcomes)
    result = await SecurityPipeline(
        PrimaryReserveGateway(primary, reserve),
        "",
        extract_structured_facts(""),
        input_security=AllowingInputSecurity(),
        output_validator=AllowingOutputValidator(),
    ).respond("Сколько стоит криокапсула?", [])

    assert primary.calls == primary_calls
    assert reserve.calls == reserve_calls
    assert result.text in {"Безопасный ответ.", SAFE_OUTPUT_FALLBACK}
