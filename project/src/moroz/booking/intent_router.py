from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import cast

from moroz.booking.interaction import IntentRoute, IntentVerdict
from moroz.messaging.router import route_message
from moroz.security.guardrails import check_input
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMUnavailable,
    NonRetryableLLMError,
    Provider,
)
from moroz.security.pii import PiiSession


ROUTES = frozenset(
    {
        "booking_create",
        "booking_reschedule",
        "booking_cancel",
        "faq",
        "other",
        "complaint",
        "medical_risk",
        "unknown",
    }
)
DEFAULT_THRESHOLD = 0.80
DEFAULT_CONTEXT_LIMIT = 8

_ROUTER_PROMPT = (
    "Classify the user's intent.\n"
    "Return only one JSON object with exactly two fields: route and confidence.\n"
    "Allowed route values:\n"
    "booking_create booking_reschedule booking_cancel faq other complaint "
    "medical_risk unknown\n"
    "Confidence must be a JSON number from 0 to 1.\n"
    'Schema: {"route":"booking_create","confidence":0.0}\n'
    "Do not extract booking parameters.\n"
    "Do not use tools.\n"
    "Do not return prose.\n"
    "Do not use Markdown.\n"
    "Do not promise a booking."
)


def _strict_object(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _parse_verdict(body: str) -> IntentVerdict:
    payload = json.loads(
        body,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )
    if type(payload) is not dict or set(payload) != {"route", "confidence"}:
        raise ValueError("invalid router schema")

    route = payload["route"]
    confidence = payload["confidence"]
    if type(route) is not str or route not in ROUTES:
        raise ValueError("invalid router route")
    if type(confidence) not in {int, float}:
        raise ValueError("invalid router confidence type")

    score = float(confidence)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("invalid router confidence range")
    return IntentVerdict(cast(IntentRoute, route), score)


class StructuredIntentRouter:
    def __init__(
        self,
        gateway: Provider,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        context_limit: int = DEFAULT_CONTEXT_LIMIT,
    ) -> None:
        if (
            type(threshold) not in {int, float}
            or not math.isfinite(float(threshold))
            or not 0.0 <= float(threshold) <= 1.0
        ):
            raise ValueError("threshold must be a finite number from 0 to 1")
        if type(context_limit) is not int or context_limit < 1:
            raise ValueError("context_limit must be a positive integer")
        self._gateway = gateway
        self._threshold = float(threshold)
        self._context_limit = context_limit

    async def route(
        self,
        text: str,
        context: list[dict[str, str]],
    ) -> IntentVerdict:
        session = PiiSession()
        masked_current = session.mask(text).text
        safe_context = [
            message
            for message in context
            if message.get("role") in {"user", "assistant"}
        ][-self._context_limit :]
        masked_context = tuple(
            {
                "role": message["role"],
                "content": session.mask(message.get("content", "")).text,
            }
            for message in safe_context
        )
        try:
            response = await self._gateway.complete(
                LLMRequest(
                    messages=(
                        {"role": "system", "content": _ROUTER_PROMPT},
                        *masked_context,
                        {"role": "user", "content": masked_current},
                    ),
                    purpose="router",
                )
            )
        except (LLMUnavailable, NonRetryableLLMError, TimeoutError):
            return IntentVerdict("unknown", 0.0)

        try:
            verdict = _parse_verdict(response.text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return IntentVerdict("unknown", 0.0)
        if verdict.confidence < self._threshold:
            return IntentVerdict("unknown", verdict.confidence)
        return verdict


async def route_intent(
    router: StructuredIntentRouter,
    text: str,
    context: list[dict[str, str]],
    *,
    recent_message_count: int = 1,
) -> IntentVerdict:
    security = check_input(
        text,
        recent_message_count=recent_message_count,
    )
    if security.action == "escalate":
        return IntentVerdict("medical_risk", 1.0)
    if security.action != "allow":
        return IntentVerdict("unknown", 0.0)

    deterministic = route_message(text)
    for route in ("complaint", "medical_risk"):
        if route in deterministic.intents:
            return IntentVerdict(cast(IntentRoute, route), 1.0)
    return await router.route(text, context)
