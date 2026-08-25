from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging

from moroz.messaging.router import (
    RouteDecision,
    bound_untrusted_context,
    deterministic_route,
    route_message,
)
from moroz.security.guardrails import check_input
from moroz.security.input_security import (
    LLMInputSecurityClassifier,
    needs_input_security_review,
)
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    LLMUsage,
    LLMUnavailable,
    NonRetryableLLMError,
)
from moroz.security.output_validator import LLMOutputValidator
from moroz.security.pii import PiiSession, UnknownPlaceholder
from moroz.security.validator import (
    StructuredFacts,
    extract_structured_facts,
    merge_structured_facts,
    validate_output,
)


logger = logging.getLogger(__name__)


INPUT_BLOCK_REPLY = (
    "Не могу обработать этот запрос. "
    "Могу помочь по услугам, подготовке, контактам и записи в центр."
)
STOP_REPLY = "Хорошо, больше не продолжаю этот диалог."
MEDICAL_ESCALATION_REPLY = (
    "Я не гарантирую лечение или медицинский результат. "
    "По этому вопросу нужна оценка профильного специалиста. "
    "При остром состоянии обратитесь за неотложной помощью."
)
SAFE_OUTPUT_FALLBACK = (
    "Сейчас не могу дать надёжный ответ. Пожалуйста, обратитесь к администратору."
)
MEDICAL_OUTPUT_FALLBACK = (
    "Я не гарантирую лечение или медицинский результат. "
    "По индивидуальной ситуации нужна консультация профильного специалиста."
)
SLOT_OUTPUT_FALLBACK = (
    "Я не могу подтвердить этот слот без актуального расписания. "
    "Проверьте доступность в онлайн-записи или уточните у администратора."
)
OFFTOPIC_REPLY = (
    "Я могу помочь по услугам, подготовке, контактам и записи в центр."
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
        usage=tuple(usage for item in items for usage in item.usage),
    )


def _usage_only(usages: tuple[LLMUsage, ...]) -> LLMResponse:
    return LLMResponse(
        "",
        sum(item.prompt_tokens for item in usages),
        sum(item.completion_tokens for item in usages),
        sum(item.cached_tokens for item in usages),
        sum(item.total_tokens for item in usages),
        usages[-1].model,
        usages,
    )


async def _cancel_and_drain(*tasks: asyncio.Task | None) -> None:
    active = tuple(task for task in tasks if task is not None)
    for task in active:
        if not task.done():
            task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)


def _confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "none"
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


class SecurityPipeline:
    def __init__(
        self,
        gateway: object,
        system_prompt: str,
        facts: StructuredFacts,
        router: object | None = None,
        input_security: object | None = None,
        output_validator: object | None = None,
    ) -> None:
        self.gateway = gateway
        self.system_prompt = system_prompt
        self.facts = facts
        self.router = router
        self.input_security = input_security or LLMInputSecurityClassifier(
            gateway
        )
        self.output_validator = output_validator or LLMOutputValidator(gateway)

    async def respond(
        self,
        user_message: str,
        context: list[dict[str, str]],
        *,
        recent_message_count: int = 1,
        catalog=None,
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
        masked_context = bound_untrusted_context([
            {
                "role": message["role"],
                "content": session.mask(message.get("content", "")).text,
            }
            for message in context
            if message.get("role") in {"user", "assistant"}
        ])
        masked_current = session.mask(user_message)
        forbidden_raw = session.raw_values()
        accumulated: list[LLMResponse] = []

        local_route = deterministic_route(masked_current.text)
        needs_router = local_route is None and self.router is not None
        needs_security_llm = needs_input_security_review(
            decision.action,
            route_unresolved=needs_router,
            has_context=bool(masked_context),
        )
        security_task = (
            asyncio.create_task(
                self.input_security.classify(
                    masked_current.text,
                    masked_context,
                )
            )
            if needs_security_llm
            else None
        )
        router_task = None
        try:
            if security_task is not None:
                try:
                    security_verdict = await security_task
                except Exception:
                    await _cancel_and_drain(router_task)
                    return _aggregate(
                        accumulated,
                        INPUT_BLOCK_REPLY,
                        "security-fallback",
                    )
                if security_verdict.usage:
                    accumulated.append(_usage_only(security_verdict.usage))
                if security_verdict.decision.action != "allow":
                    return _aggregate(
                        accumulated,
                        INPUT_BLOCK_REPLY,
                        (
                            "security-fallback"
                            if security_verdict.decision.source == "fallback"
                            else "security-llm"
                        ),
                    )

            if needs_router:
                router_task = asyncio.create_task(
                    self.router.route(masked_current.text, masked_context)
                )
                try:
                    router_verdict = await router_task
                except Exception:
                    local_route = RouteDecision(
                        ("unknown",),
                        True,
                        "fallback",
                        None,
                        "router_internal_error",
                    )
                else:
                    local_route = router_verdict.decision
                    if router_verdict.usage:
                        accumulated.append(_usage_only(router_verdict.usage))
            route = local_route or route_message(masked_current.text)
        except asyncio.CancelledError:
            await _cancel_and_drain(security_task, router_task)
            raise

        route_metadata = (
            f"ROUTE intents={','.join(route.intents)}; "
            f"requires_clarification={int(route.requires_clarification)}; "
            f"source={route.source}; "
            f"confidence={_confidence_bucket(route.confidence)}"
        )
        if "offtopic" in route.intents:
            return _aggregate(accumulated, OFFTOPIC_REPLY, "router-local")
        active_facts = self.facts
        catalog_block = ""
        if catalog is not None and "faq" in route.intents:
            catalog_block = catalog.data_block()
            extracted = extract_structured_facts(catalog_block)
            catalog_facts = StructuredFacts(
                prices=extracted.prices,
                public_contacts=frozenset(),
                slots=frozenset(),
                public_pii=catalog.public_display_values(),
            )
            active_facts = merge_structured_facts(self.facts, catalog_facts)
            direct_reply = (
                catalog.direct_reply if route.intents == ("faq",) else None
            )
            if direct_reply is not None:
                verdict = validate_output(
                    direct_reply,
                    active_facts,
                    masked_current.placeholders,
                    forbidden_raw=forbidden_raw,
                )
                return _aggregate(
                    accumulated,
                    direct_reply if verdict.ok else SAFE_OUTPUT_FALLBACK,
                    "catalog-local" if verdict.ok else "security-fallback",
                )
        owned_system = "\n\n".join(
            part
            for part in (self.system_prompt, route_metadata, catalog_block)
            if part
        )
        base_messages = (
            {"role": "system", "content": owned_system},
            *masked_context,
            {"role": "user", "content": masked_current.text},
        )
        validator_code: str | None = None
        initial_validator_code: str | None = None

        for attempt in range(1, 3):
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
                active_facts,
                masked_current.placeholders,
                forbidden_raw=forbidden_raw,
            )
            if verdict.ok:
                semantic = await self.output_validator.validate(
                    masked_input=masked_current.text,
                    masked_context=masked_context,
                    route_metadata=route_metadata,
                    candidate=answer.text,
                )
                if semantic.usage:
                    accumulated.append(_usage_only(semantic.usage))
                latest_usage = semantic.usage[-1] if semantic.usage else None
                logger.info(
                    "output_validator_decision attempt=%s action=%s source=%s "
                    "reason_code=%s model=%s total_tokens=%s",
                    attempt,
                    semantic.decision.action,
                    semantic.decision.source,
                    semantic.decision.reason_code,
                    latest_usage.model if latest_usage else "none",
                    sum(item.total_tokens for item in semantic.usage),
                )
                if semantic.decision.action == "allow":
                    try:
                        restored = session.restore_validated(
                            answer.text,
                            masked_current.placeholders,
                        )
                    except UnknownPlaceholder:
                        validator_code = "unknown_placeholder"
                    else:
                        return _aggregate(accumulated, restored, answer.model)
                else:
                    validator_code = semantic.decision.reason_code
            else:
                validator_code = verdict.code
            if initial_validator_code is None:
                initial_validator_code = validator_code

        fallback = SAFE_OUTPUT_FALLBACK
        if validator_code == initial_validator_code:
            fallback = {
                "medical_guarantee": MEDICAL_OUTPUT_FALLBACK,
                "invented_slot": SLOT_OUTPUT_FALLBACK,
            }.get(validator_code, SAFE_OUTPUT_FALLBACK)
        return _aggregate(
            accumulated,
            fallback,
            "security-fallback",
        )
