from __future__ import annotations

import json
from pathlib import Path

import pytest

from moroz.messaging.router import INTENTS, deterministic_route
from moroz.security.pii import PiiSession


DATASET = Path("/workspace/llm/eval/router_dataset.json")
QUALITY = {
    "simple",
    "context",
    "multi_intent",
    "conflict",
    "complaint",
    "handoff",
    "smalltalk",
    "offtopic",
    "other",
    "unknown",
    "prompt_safety",
}


def test_router_dataset_has_stable_unique_contract():
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    keys = [case["case_key"] for case in cases]
    categories = {case["category"] for case in cases}

    assert len(cases) == 20
    assert len(keys) == len(set(keys))
    assert QUALITY <= categories
    for case in cases:
        assert set(case) == {
            "case_key",
            "category",
            "input",
            "context",
            "expected_intents",
            "expected_clarification",
            "expected_source",
            "critical",
        }
        assert isinstance(case["input"], str) and case["input"].strip()
        assert all(
            message["role"] in {"user", "assistant"}
            for message in case["context"]
        )
        assert 1 <= len(case["expected_intents"]) <= 3
        assert set(case["expected_intents"]) <= set(INTENTS)
        assert case["expected_source"] in {"deterministic", "llm"}
        assert type(case["critical"]) is bool


@pytest.mark.parametrize(
    "case",
    json.loads(DATASET.read_text(encoding="utf-8")),
    ids=lambda case: case["case_key"],
)
def test_router_dataset_source_matches_runtime_deterministic_boundary(case):
    masked_input = PiiSession().mask(case["input"]).text
    decision = deterministic_route(masked_input)

    if case["expected_source"] == "llm":
        assert decision is None
    else:
        assert decision is not None
        assert set(decision.intents) == set(case["expected_intents"])
        assert (
            decision.requires_clarification
            == case["expected_clarification"]
        )
