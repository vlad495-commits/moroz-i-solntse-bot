from __future__ import annotations

from collections.abc import Mapping, Sequence

from moroz.security.llm_gateway import LLMRequest, LLMResponse
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts


class _ScriptedProvider:
    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = tuple(responses)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.purpose == "security":
            return LLMResponse("OK", 0, 0, 0, 0, "catalog-eval-security")
        text = self._responses[self.calls]
        self.calls += 1
        return LLMResponse(text, 0, 0, 0, 0, "catalog-eval")


async def evaluate_catalog_case(case: Mapping[str, object]) -> bool:
    """Проверить ручную консультацию; историческое имя batch сохранено для CLI."""
    prompt = case.get("system_prompt")
    if not isinstance(prompt, str):
        return False
    responses = case.get("provider_responses", [])
    if not isinstance(responses, list) or not all(
        isinstance(item, str) for item in responses
    ):
        return False

    provider = _ScriptedProvider(responses)
    try:
        result = await SecurityPipeline(
            provider,
            prompt,
            extract_structured_facts(prompt),
        ).respond(
            str(case["question"]),
            [],
            recent_message_count=1,
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return False

    text = result.text.casefold()
    expected = case.get("expected_contains", [])
    forbidden = case.get("forbidden_keywords", [])
    if not isinstance(expected, list) or not isinstance(forbidden, list):
        return False
    return all(str(value).casefold() in text for value in expected) and not any(
        str(value).casefold() in text for value in forbidden
    )
