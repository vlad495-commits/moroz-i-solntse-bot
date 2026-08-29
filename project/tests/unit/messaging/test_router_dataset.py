from __future__ import annotations

import json
from pathlib import Path

import pytest

from moroz.messaging.router import ROUTES, deterministic_route
from moroz.security.pii import PiiSession


DATASET = Path("/workspace/llm/eval/router_dataset_v2.json")
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
def test_router_v2_source_matches_runtime_deterministic_boundary(case):
    masked_input = PiiSession().mask(case["input"]).text
    decision = deterministic_route(masked_input)

    if case["expected_source"] == "llm":
        assert decision is None
    else:
        assert decision is not None
        assert decision.route == case["expected_route"]
