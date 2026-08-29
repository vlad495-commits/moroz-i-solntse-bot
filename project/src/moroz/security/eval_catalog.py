from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from moroz.booking.catalog import (
    CatalogGrounding,
    CatalogService,
    CatalogVariant,
)
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


def build_synthetic_catalog(data: Mapping[str, object]) -> CatalogGrounding:
    services = tuple(
        CatalogService(
            service_id=str(service["service_id"]),
            service_name=str(service["service_name"]),
            category_name=(
                str(service["category_name"])
                if service.get("category_name") is not None
                else None
            ),
            variants=tuple(
                CatalogVariant(
                    staff_id=str(variant["staff_id"]),
                    staff_name=str(variant["staff_name"]),
                    price_min=Decimal(str(variant["price_min"])),
                    price_max=Decimal(str(variant["price_max"])),
                    duration_minutes=int(variant["duration_minutes"]),
                )
                for variant in service.get("variants", [])
            ),
        )
        for service in data.get("services", [])
    )
    return CatalogGrounding(
        status=str(data["status"]),
        services=services,
        simple_kind=(
            str(data["simple_kind"])
            if data.get("simple_kind") is not None
            else None
        ),
        ambiguous=bool(data.get("ambiguous", False)),
    )


async def evaluate_catalog_case(case: Mapping[str, object]) -> bool:
    """Исполнить synthetic catalog case через настоящий security pipeline."""
    catalog_data = case.get("catalog")
    if not isinstance(catalog_data, Mapping):
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
            "",
            extract_structured_facts(""),
        ).respond(
            str(case["question"]),
            [],
            recent_message_count=1,
            catalog=build_synthetic_catalog(catalog_data),
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
