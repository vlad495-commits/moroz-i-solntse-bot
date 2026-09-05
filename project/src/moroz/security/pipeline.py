from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging

from moroz.messaging.router import (
    bound_untrusted_context,
    deterministic_route,
    route_message,
)
from moroz.security.guardrails import check_input
from moroz.security.context_compactor import ContextCompactor
from moroz.security.input_security import LLMInputSecurityClassifier
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    LLMUsage,
    LLMUnavailable,
    NonRetryableLLMError,
)
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
        context_compactor: ContextCompactor | None = None,
    ) -> None:
        self.gateway = gateway
        self.system_prompt = system_prompt
        self.facts = facts
        self.router = router
        self.input_security = input_security or LLMInputSecurityClassifier(
            gateway
        )
        self.output_validator = output_validator
        self.context_compactor = context_compactor

    async def respond(
        self,
        user_message: str,
        context: list[dict[str, str]],
        *,
        recent_message_count: int = 1,
        catalog=None,
        dispatch=None,
        booking_context=None,
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
        masked_history = [
            {
                "role": message["role"],
                "content": session.mask(message.get("content", "")).text,
            }
            for message in context
            if message.get("role") in {"user", "assistant"}
        ]
        masked_context = bound_untrusted_context(masked_history)
        masked_current = session.mask(user_message)
        if booking_context:
            masked_context.append({"role": "assistant", "content": session.mask(booking_context).text})
        forbidden_raw = session.raw_values()
        accumulated: list[LLMResponse] = []

        local_route = deterministic_route(masked_current.text)
        route_source = "deterministic" if local_route is not None else "fallback"
        needs_router = local_route is None and self.router is not None
        security_task = asyncio.create_task(
            self.input_security.classify(masked_current.text)
        )
        router_task = (
            asyncio.create_task(
                self.router.route(masked_current.text, masked_context)
            )
            if needs_router
            else None
        )
        try:
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
                await _cancel_and_drain(router_task)
                return _aggregate(
                    accumulated,
                    INPUT_BLOCK_REPLY,
                    (
                        "security-fallback"
                        if security_verdict.decision.source == "fallback"
                        else "security-llm"
                    ),
                )

            if router_task is not None:
                try:
                    router_verdict = await router_task
                except Exception:
                    local_route = route_message(masked_current.text)
                    route_source = "fallback"
                    logger.warning(
                        "router_decision_fallback reason_code=router_internal_error"
                    )
                else:
                    local_route = router_verdict.decision
                    route_source = router_verdict.source
                    if router_verdict.usage:
                        accumulated.append(_usage_only(router_verdict.usage))
                    if router_verdict.reason_code is not None:
                        logger.warning(
                            "router_decision_fallback reason_code=%s",
                            router_verdict.reason_code,
                        )
            route = local_route or route_message(masked_current.text)
        except asyncio.CancelledError:
            await _cancel_and_drain(security_task, router_task)
            raise

        if route_source == "fallback" and (dispatch is not None or self.router is not None):
            return _aggregate(accumulated,
                "Не удалось понять запрос. Попробуйте ещё раз или воспользуйтесь кнопками меню.",
                "router-fallback")
        logger.info("intent_decision route=%s source=%s action=%s confidence=%s",
                    route.route, route_source, route.action, _confidence_bucket(route.confidence))
        if dispatch is not None:
            if route.confidence < 0.6:
                return _aggregate(accumulated,
                    "Уточните, пожалуйста: посмотреть свободное время, свои записи или задать вопрос об услуге?",
                    "router-clarification")
            reply = await dispatch(route)
            if reply is not None:
                return _aggregate(accumulated, reply, "booking-local")
        elif self.router is not None and route.route in {"booking", "booking_management"}:
            return _aggregate(accumulated,
                "Запись внутри Telegram сейчас недоступна. Воспользуйтесь кнопками меню или напишите администратору.",
                "booking-unavailable")

        route_metadata = (
            f"ROUTE route={route.route}; "
            f"source={route_source}; "
            f"confidence={_confidence_bucket(route.confidence)}"
        )
        if route.route == "offtopic":
            return _aggregate(accumulated, OFFTOPIC_REPLY, "router-local")
        active_facts = self.facts
        catalog_block = ""
        if catalog is not None and route.route == "consultation":
            if callable(catalog):
                catalog = await catalog(route)
            catalog_block = catalog.data_block()
            extracted = extract_structured_facts(catalog_block)
            catalog_facts = StructuredFacts(
                prices=extracted.prices,
                public_contacts=frozenset(),
                slots=frozenset(),
                public_pii=catalog.public_display_values(),
            )
            active_facts = merge_structured_facts(self.facts, catalog_facts)
            direct_reply = catalog.direct_reply
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
        answer_context = masked_context
        if self.context_compactor is not None:
            compact = await self.context_compactor.compact(masked_history)
            answer_context = list(compact.messages)
            if compact.usage:
                accumulated.append(_usage_only(compact.usage))
            logger.info(
                "context_compactor_decision source=%s reason_code=%s "
                "input_messages=%s output_messages=%s total_tokens=%s",
                compact.source,
                compact.reason_code,
                len(masked_history),
                len(answer_context),
                sum(item.total_tokens for item in compact.usage),
            )
        base_messages = (
            {"role": "system", "content": owned_system},
            *answer_context,
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
                if self.output_validator is None:
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
                    semantic = await self.output_validator.validate(
                        masked_input=masked_current.text,
                        masked_context=answer_context,
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
