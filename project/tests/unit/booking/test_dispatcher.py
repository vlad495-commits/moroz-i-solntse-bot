from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moroz.booking.dispatcher import MessageDispatcher
from moroz.booking.interaction import (
    BookingOwner,
    Interaction,
    IntentVerdict,
    WorkflowReply,
)


OWNER = BookingOwner("telegram", "101", "101")
CONTEXT = [{"role": "assistant", "content": "Чем помочь?"}]


def _consultant_result(text: str = "Консультация") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        prompt_tokens=11,
        completion_tokens=7,
        cached_tokens=3,
        total_tokens=18,
        model="consultant-test",
    )


def _dispatcher(*, human_mode=False, active=None, route="faq"):
    repository = SimpleNamespace(
        is_human_mode=AsyncMock(return_value=human_mode),
        get_active=AsyncMock(return_value=active),
    )
    workflow = SimpleNamespace(
        handle=AsyncMock(return_value=WorkflowReply("Workflow", {})),
        start_create=AsyncMock(
            return_value=WorkflowReply("Выберите услуги", {})
        ),
    )
    router = AsyncMock(return_value=IntentVerdict(route, 0.95))
    consultant = AsyncMock(return_value=_consultant_result())
    dispatcher = MessageDispatcher(
        repository,
        workflow,
        router=router,
        consultant=consultant,
    )
    return dispatcher, repository, workflow, router, consultant


@pytest.mark.asyncio
async def test_human_mode_precedes_workflow_router_and_consultant():
    dispatcher, repository, workflow, router, consultant = _dispatcher(
        human_mode=True,
        active=object(),
    )
    interaction = Interaction.callback(
        OWNER,
        "process_message:1",
        "booking:opaque-token",
    )

    result = await dispatcher.dispatch(interaction, CONTEXT, 2)

    assert "сохранено" in result.reply.text.lower()
    assert "подтвержд" not in result.reply.text.lower()
    repository.get_active.assert_not_awaited()
    workflow.handle.assert_not_awaited()
    router.assert_not_awaited()
    consultant.assert_not_awaited()
    assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["/book", "Записаться"])
async def test_exact_create_entry_bypasses_router_and_consultant(value):
    dispatcher, _, workflow, router, consultant = _dispatcher()
    interaction = Interaction.text(OWNER, f"process_message:{value}", value)

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert result.reply.text == "Выберите услуги"
    workflow.start_create.assert_awaited_once_with(
        OWNER,
        interaction.idempotency_key,
    )
    router.assert_not_awaited()
    consultant.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_containing_create_button_still_uses_safety_router():
    dispatcher, _, workflow, router, consultant = _dispatcher(route="faq")
    interaction = Interaction.text(
        OWNER,
        "process_message:2",
        "Подскажите, как Записаться потом",
    )

    await dispatcher.dispatch(interaction, CONTEXT, 4)

    workflow.start_create.assert_not_awaited()
    router.assert_awaited_once_with(
        interaction.text_value,
        CONTEXT,
        recent_message_count=4,
    )
    consultant.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["callback", "contact", "active"])
async def test_structured_and_active_scenario_bypass_router_and_consultant(kind):
    active = object() if kind == "active" else None
    dispatcher, _, workflow, router, consultant = _dispatcher(active=active)
    if kind == "callback":
        interaction = Interaction.callback(
            OWNER,
            "process_message:3",
            "booking:opaque-token",
        )
    elif kind == "contact":
        interaction = Interaction.contact(
            OWNER,
            "process_message:4",
            contact_user_id="101",
            phone_number="+79990000000",
            personal_data_processing_allowed=True,
        )
    else:
        interaction = Interaction.text(
            OWNER,
            "process_message:5",
            "Иван",
        )

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert result.reply.text == "Workflow"
    workflow.handle.assert_awaited_once_with(interaction)
    router.assert_not_awaited()
    consultant.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_booking_create_starts_workflow_without_consultant():
    dispatcher, _, workflow, router, consultant = _dispatcher(
        route="booking_create"
    )
    interaction = Interaction.text(
        OWNER,
        "process_message:6",
        "Хочу подобрать время",
    )

    result = await dispatcher.dispatch(interaction, CONTEXT, 3)

    assert result.reply.text == "Выберите услуги"
    router.assert_awaited_once()
    workflow.start_create.assert_awaited_once_with(
        OWNER,
        interaction.idempotency_key,
    )
    consultant.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["booking_reschedule", "booking_cancel"])
async def test_change_routes_are_safe_and_never_mutate_before_task_8(route):
    dispatcher, _, workflow, _, consultant = _dispatcher(route=route)
    interaction = Interaction.text(OWNER, "process_message:7", "Моя запись")

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert "мои записи" in result.reply.text.lower()
    assert "подтвержд" not in result.reply.text.lower()
    workflow.start_create.assert_not_awaited()
    workflow.handle.assert_not_awaited()
    consultant.assert_not_awaited()
    assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["complaint", "medical_risk"])
async def test_sensitive_routes_return_truthful_deterministic_reply(route):
    dispatcher, _, workflow, _, consultant = _dispatcher(route=route)
    interaction = Interaction.text(OWNER, "process_message:8", "Мне плохо")

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert "запись подтверждена" not in result.reply.text.lower()
    assert "передано администратору" not in result.reply.text.lower()
    workflow.start_create.assert_not_awaited()
    consultant.assert_not_awaited()
    assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["faq", "other"])
async def test_consultant_routes_call_existing_consultant_once_and_keep_usage(route):
    dispatcher, _, workflow, _, consultant = _dispatcher(route=route)
    interaction = Interaction.text(OWNER, "process_message:9", "Вопрос")

    result = await dispatcher.dispatch(interaction, CONTEXT, 5)

    consultant.assert_awaited_once_with(
        "Вопрос",
        CONTEXT,
        recent_message_count=5,
    )
    workflow.start_create.assert_not_awaited()
    assert result.reply == WorkflowReply("Консультация", {})
    assert result.usage is not None
    assert result.usage.total_tokens == 18
    assert result.usage.model == "consultant-test"


@pytest.mark.asyncio
async def test_unknown_returns_clarification_without_mutation_or_consultant():
    dispatcher, _, workflow, _, consultant = _dispatcher(route="unknown")
    interaction = Interaction.text(OWNER, "process_message:10", "Неясно")

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert "уточните" in result.reply.text.lower()
    assert result.reply.delivery_options["reply_markup"]["keyboard"]
    workflow.start_create.assert_not_awaited()
    workflow.handle.assert_not_awaited()
    consultant.assert_not_awaited()
    assert result.usage is None


@pytest.mark.asyncio
async def test_router_timeout_is_unknown_and_never_blocks_or_calls_consultant():
    dispatcher, _, workflow, router, consultant = _dispatcher()
    router.side_effect = TimeoutError
    interaction = Interaction.text(OWNER, "process_message:timeout", "Вопрос")

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert "уточните" in result.reply.text.lower()
    workflow.start_create.assert_not_awaited()
    consultant.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_callback_fails_closed_before_workflow_and_llms():
    dispatcher, _, workflow, router, consultant = _dispatcher()
    interaction = Interaction.callback(
        OWNER,
        "process_message:11",
        "booking:bad:token",
    )

    result = await dispatcher.dispatch(interaction, CONTEXT, 1)

    assert "обнов" in result.reply.text.lower()
    workflow.handle.assert_not_awaited()
    router.assert_not_awaited()
    consultant.assert_not_awaited()
