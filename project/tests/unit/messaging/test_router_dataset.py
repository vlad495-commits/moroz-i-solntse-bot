from __future__ import annotations

import json
from pathlib import Path

import pytest

from moroz.messaging.router import ROUTES, route_message
from moroz.security.pii import PiiSession


DATASET = Path("/workspace/llm/eval/router_dataset_v2.json")
DATASET_V3 = Path("/workspace/llm/eval/router_dataset_v3.json")
CATEGORIES = {
    "consultation",
    "booking",
    "booking_management",
    "escalation",
    "smalltalk",
    "offtopic",
    "other",
    "prompt_safety",
    "pii",
}


def _cases() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_router_v3_dataset_has_bounded_structured_coverage():
    cases = json.loads(DATASET_V3.read_text(encoding="utf-8"))
    keys = [case["case_key"] for case in cases]
    categories = {case["category"] for case in cases}

    assert len(cases) >= 25
    name_case = next(case for case in cases if case["case_key"] == "router-v3-context-name")
    assert name_case["expected"]["action"] == "continue"
    assert len(keys) == len(set(keys))
    assert all(key.startswith("router-v3-") for key in keys)
    assert {
        "time_window",
        "multi_intent",
        "correction",
        "negation",
        "ownership",
        "context",
        "injection",
        "invalid_output",
    } <= categories
    for case in cases:
        assert set(case) == {
            "case_key", "category", "input", "context", "expected", "critical"
        }
        assert set(case["expected"]) == {
            "route", "action", "topics", "services", "date",
            "time_from", "time_to", "staff", "choice",
        }
        assert len(case["expected"]["services"]) <= 3
        assert type(case["critical"]) is bool


def test_router_v2_dataset_has_stable_unique_contract():
    cases = _cases()
    keys = [case["case_key"] for case in cases]

    assert len(cases) == 24
    assert sum(case["critical"] for case in cases) == 16
    assert len(keys) == len(set(keys))
    assert all(key.startswith("router-v2-") for key in keys)
    assert {case["category"] for case in cases} == CATEGORIES
    for case in cases:
        assert set(case) == {
            "case_key",
            "category",
            "input",
            "context",
            "expected_route",
            "expected_source",
            "critical",
        }
        assert isinstance(case["input"], str) and case["input"].strip()
        assert all(
            set(message) == {"role", "content"}
            and message["role"] in {"user", "assistant"}
            and isinstance(message["content"], str)
            for message in case["context"]
        )
        assert case["expected_route"] in ROUTES
        assert case["expected_source"] in {"deterministic", "llm"}
        assert type(case["critical"]) is bool


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["case_key"])
def test_historical_router_cases_have_no_local_intent_classification(case):
    masked_input = PiiSession().mask(case["input"]).text
    decision = route_message(masked_input)

    # v2 is an immutable migration seed. Its historical source is not today's
    # routing policy; the expected semantic destination remains applicable.
    assert decision.route == "consultation"
    assert decision.confidence == 0.0
