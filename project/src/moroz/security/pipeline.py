from __future__ import annotations

from collections.abc import Iterable

from moroz.messaging.router import route_message
from moroz.security.guardrails import check_input
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    NonRetryableLLMError,
)
from moroz.security.pii import PiiSession, UnknownPlaceholder
from moroz.security.validator import StructuredFacts, validate_output


INPUT_BLOCK_REPLY = "Не могу обработать этот запрос. Переформулируйте, пожалуйста."
STOP_REPLY = "Хорошо, больше не продолжаю этот диалог."
MEDICAL_ESCALATION_REPLY = (
    "По этому вопросу нужна оценка специалиста. "
    "При остром состоянии обратитесь за неотложной помощью."
)
SAFE_OUTPUT_FALLBACK = (
    "Сейчас не могу дать надёжный ответ. Пожалуйста, обратитесь к администратору."
)

_GUARD_PROMPT = (
    "Classify the masked user text. Reply with exactly ALLOW or BLOCK."
)


def _zero(text: str, model: str = "security-local") -> LLMResponse:
    return LLMResponse(text, 0, 0, 0, 0, model)


def _aggregate(
    responses: Iterable[LLMResponse],
    text: str,
    model: str,
) -> LLMResponse:
    items = tuple(responses)
    return LLMResponse(
        text=text,
        prompt_tokens=sum(item.prompt_tokens for item in items),
        completion_tokens=sum(item.completion_tokens for item in items),
        cached_tokens=sum(item.cached_tokens for item in items),
        total_tokens=sum(item.total_tokens for item in items),
        model=model,
    )


class SecurityPipeline:
    def __init__(
        self,
        gateway: object,
        system_prompt: str,
        facts: StructuredFacts,
    ) -> None:
        self.gateway = gateway
        self.system_prompt = system_prompt
        self.facts = facts

    async def respond(
        self,
        user_message: str,
        context: list[dict[str, str]],
        *,
        recent_message_count: int = 1,
    ) -> LLMResponse:
        decision = check_input(
            user_message,
            recent_message_count=recent_message_count,
        )
        if decision.action == "block":
            return _zero(INPUT_BLOCK_REPLY)
        if decision.action == "stop":
            return _zero(STOP_REPLY)
        if decision.action == "escalate":
            return _zero(MEDICAL_ESCALATION_REPLY)

        session = PiiSession()
        masked_context = [
            {
                "role": message["role"],
                "content": session.mask(message.get("content", "")).text,
            }
            for message in context
            if message.get("role") in {"user", "assistant"}
        ]
        masked_current = session.mask(user_message)
        forbidden_raw = session.raw_values()
        accumulated: list[LLMResponse] = []

        if decision.action == "review":
            try:
                guard_response = await self.gateway.complete(
                    LLMRequest(
                        messages=(
                            {"role": "system", "content": _GUARD_PROMPT},
                            {"role": "user", "content": masked_current.text},
                        ),
                        purpose="guard",
                    )
                )
            except (LLMUnavailable, NonRetryableLLMError):
                return _aggregate(
                    accumulated,
                    INPUT_BLOCK_REPLY,
                    "security-fallback",
                )
            accumulated.append(guard_response)
            guard_result = guard_response.text.strip()
            if guard_result != "ALLOW":
                return _aggregate(
                    accumulated,
                    INPUT_BLOCK_REPLY,
                    "security-fallback",
                )

        route = route_message(user_message)
        route_metadata = (
            f"ROUTE intents={','.join(route.intents)}; "
            f"requires_clarification={int(route.requires_clarification)}"
        )
        owned_system = "\n\n".join(
            part for part in (self.system_prompt, route_metadata) if part
        )
        base_messages = (
            {"role": "system", "content": owned_system},
            *masked_context,
            {"role": "user", "content": masked_current.text},
        )
        validator_code: str | None = None

        for _ in range(2):
            messages = base_messages
            if validator_code is not None:
                messages = (
                    {
                        "role": "system",
                        "content": (
                            f"{owned_system}\n\n"
                            f"VALIDATOR_RETRY code={validator_code}"
                        ),
                    },
                    *base_messages[1:],
                )
            try:
                answer = await self.gateway.complete(
                    LLMRequest(messages=messages, purpose="answer")
                )
            except (LLMUnavailable, NonRetryableLLMError):
                return _aggregate(
                    accumulated,
                    SAFE_OUTPUT_FALLBACK,
                    "security-fallback",
                )
            accumulated.append(answer)
            verdict = validate_output(
                answer.text,
                self.facts,
                masked_current.placeholders,
                forbidden_raw=forbidden_raw,
            )
            if verdict.ok:
                try:
                    restored = session.restore_validated(
                        answer.text,
                        masked_current.placeholders,
                    )
                except UnknownPlaceholder:
                    validator_code = "unknown_placeholder"
                    continue
                return _aggregate(accumulated, restored, answer.model)
            validator_code = verdict.code

        return _aggregate(
            accumulated,
            SAFE_OUTPUT_FALLBACK,
            "security-fallback",
        )
