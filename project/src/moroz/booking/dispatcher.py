from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from moroz.booking.interaction import Interaction, IntentVerdict, WorkflowReply


_CALLBACK = re.compile(r"^booking:[A-Za-z0-9_-]{1,32}$")
_CREATE_ENTRIES = frozenset({"/book", "Записаться"})
_LIST_ENTRIES = frozenset({"/bookings", "Мои записи"})
_RESCHEDULE_ENTRIES = frozenset({"/reschedule", "Перенести запись"})
_CANCEL_ENTRIES = frozenset({"/cancel", "Отменить запись"})


class _WorkflowRepository(Protocol):
    async def is_human_mode(self, customer_id: str) -> bool: ...

    async def get_active(
        self,
        channel: str,
        chat_id: str,
        customer_id: str,
    ) -> object | None: ...


class _Workflow(Protocol):
    async def handle(self, interaction: Interaction) -> WorkflowReply: ...

    async def start_create(
        self,
        owner,
        idempotency_key: str,
    ) -> WorkflowReply: ...

    async def list_bookings(self, owner) -> WorkflowReply: ...

    async def start_reschedule(
        self,
        owner,
        idempotency_key: str,
    ) -> WorkflowReply: ...

    async def start_cancel(
        self,
        owner,
        idempotency_key: str,
    ) -> WorkflowReply: ...


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    model: str


@dataclass(frozen=True, slots=True)
class DispatchResult:
    reply: WorkflowReply
    usage: LLMUsage | None = None


class MessageDispatcher:
    def __init__(
        self,
        repository: _WorkflowRepository,
        workflow: _Workflow,
        *,
        router: Callable[..., Awaitable[IntentVerdict]],
        consultant: Callable[..., Awaitable[object]],
    ) -> None:
        self._repository = repository
        self._workflow = workflow
        self._router = router
        self._consultant = consultant

    async def dispatch(
        self,
        interaction: Interaction,
        context: list[dict[str, str]],
        recent_count: int,
    ) -> DispatchResult:
        owner = interaction.owner
        if owner.channel != "telegram" or owner.chat_id != owner.customer_id:
            raise ValueError("dispatcher requires exact private Telegram owner")
        if await self._repository.is_human_mode(owner.customer_id):
            return DispatchResult(
                WorkflowReply(
                    "Сообщение сохранено. Сейчас диалог ведёт администратор.",
                    {},
                )
            )
        if interaction.kind == "callback":
            if not _CALLBACK.fullmatch(interaction.callback_data or ""):
                return DispatchResult(_refresh_reply())
            return DispatchResult(await self._workflow.handle(interaction))
        if interaction.kind == "contact":
            if (
                interaction.contact_user_id != owner.customer_id
                or not interaction.personal_data_processing_allowed
            ):
                return DispatchResult(_refresh_reply())
            return DispatchResult(await self._workflow.handle(interaction))

        text = interaction.text_value or ""
        if text in _CREATE_ENTRIES:
            return DispatchResult(
                await self._workflow.start_create(
                    owner,
                    interaction.idempotency_key,
                )
            )
        if text in _LIST_ENTRIES:
            return DispatchResult(await self._workflow.list_bookings(owner))
        if text in _RESCHEDULE_ENTRIES:
            return DispatchResult(
                await self._workflow.start_reschedule(
                    owner,
                    interaction.idempotency_key,
                )
            )
        if text in _CANCEL_ENTRIES:
            return DispatchResult(
                await self._workflow.start_cancel(
                    owner,
                    interaction.idempotency_key,
                )
            )
        active = await self._repository.get_active(
            owner.channel,
            owner.chat_id,
            owner.customer_id,
        )
        if active is not None:
            return DispatchResult(await self._workflow.handle(interaction))
        try:
            verdict = await self._router(
                text,
                context,
                recent_message_count=recent_count,
            )
        except TimeoutError:
            verdict = IntentVerdict("unknown", 0.0)
        if verdict.route == "booking_create":
            return DispatchResult(
                await self._workflow.start_create(
                    owner,
                    interaction.idempotency_key,
                )
            )
        if verdict.route == "booking_reschedule":
            return DispatchResult(
                await self._workflow.start_reschedule(
                    owner,
                    interaction.idempotency_key,
                )
            )
        if verdict.route == "booking_cancel":
            return DispatchResult(
                await self._workflow.start_cancel(
                    owner,
                    interaction.idempotency_key,
                )
            )
        if verdict.route == "complaint":
            return DispatchResult(
                WorkflowReply(
                    "Мне жаль, что возникла проблема. Опишите ситуацию — "
                    "сообщение сохранится в диалоге для дальнейшего ответа.",
                    {},
                )
            )
        if verdict.route == "medical_risk":
            return DispatchResult(
                WorkflowReply(
                    "Я не могу давать медицинские рекомендации. "
                    "При ухудшении самочувствия обратитесь за медицинской помощью.",
                    {},
                )
            )
        if verdict.route in {"faq", "other"}:
            response = await self._consultant(
                text,
                context,
                recent_message_count=recent_count,
            )
            return DispatchResult(
                WorkflowReply(_response_text(response), {}),
                _response_usage(response),
            )
        return DispatchResult(_clarification_reply())


def _response_text(response: object) -> str:
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text:
        raise ValueError("consultant response text is invalid")
    return text


def _response_usage(response: object) -> LLMUsage:
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "total_tokens",
    )
    values = tuple(getattr(response, field, None) for field in fields)
    model = getattr(response, "model", None)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("consultant token usage is invalid")
    if not isinstance(model, str) or not model:
        raise ValueError("consultant model is invalid")
    return LLMUsage(*values, model)


def _clarification_options() -> dict[str, object]:
    return {
        "reply_markup": {
            "keyboard": [
                [{"text": "Записаться"}, {"text": "Мои записи"}],
                [{"text": "Задать вопрос"}],
            ],
            "resize_keyboard": True,
        }
    }


def _clarification_reply() -> WorkflowReply:
    return WorkflowReply(
        "Уточните, пожалуйста: хотите записаться или задать вопрос?",
        _clarification_options(),
    )


def _refresh_reply() -> WorkflowReply:
    return WorkflowReply(
        "Кнопка недействительна. Обновите сценарий через «Записаться».",
        _clarification_options(),
    )
