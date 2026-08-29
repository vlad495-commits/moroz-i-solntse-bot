from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from moroz.security.guardrails import check_input


DATASET = Path("/workspace/llm/eval/security_dataset.json")
CATEGORY_COUNTS = {
    "prompt_attack": 8,
    "obfuscation": 6,
    "third_party_pii": 6,
    "dangerous_content": 6,
    "context_poisoning": 4,
    "false_positive": 10,
}
V2_ALWAYS_LLM_KEYS = {f"security-fp-0{index}" for index in range(1, 7)}


def test_security_dataset_has_stable_unique_contract():
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    keys = [case["case_key"] for case in cases]

    assert len(cases) == 40
    assert len(keys) == len(set(keys))
    assert all(key.startswith("security-") for key in keys)
    assert Counter(case["category"] for case in cases) == CATEGORY_COUNTS
    for case in cases:
        assert set(case) == {
            "case_key", "category", "input", "context",
            "expected_action", "expected_source", "critical",
        }
        assert isinstance(case["input"], str) and case["input"].strip()
        assert all(
            set(message) == {"role", "content"}
            and message["role"] in {"user", "assistant"}
            and isinstance(message["content"], str)
            for message in case["context"]
        )
        assert case["expected_action"] in {"allow", "block"}
        assert case["expected_source"] in {"local", "llm"}
        assert type(case["critical"]) is bool


def test_security_dataset_is_synthetic_and_contains_no_secrets():
    raw = DATASET.read_text(encoding="utf-8").casefold()

    assert ".invalid" in raw
    assert "+7 000" in raw
    for forbidden in (
        "@gmail.com", "@mail.ru", "@yandex.ru", "sk-proj-", "sk-ant-",
        "109.71.246.167",
    ):
        assert forbidden not in raw


def test_security_dataset_covers_provider_quality_both_ways():
    llm_cases = [
        case
        for case in json.loads(DATASET.read_text(encoding="utf-8"))
        if case["expected_source"] == "llm"
    ]

    assert any(case["expected_action"] == "block" for case in llm_cases)
    assert any(case["expected_action"] == "allow" for case in llm_cases)


@pytest.mark.parametrize(
    "case",
    json.loads(DATASET.read_text(encoding="utf-8")),
    ids=lambda case: case["case_key"],
)
def test_security_dataset_source_matches_versioned_boundary(case):
    guard = check_input(case["input"], recent_message_count=0)
    expected_source = "local" if guard.action in {"block", "stop", "escalate"} else "llm"

    if case["case_key"] in V2_ALWAYS_LLM_KEYS:
        assert case["expected_source"] == "local"
        assert expected_source == "llm"
    else:
        assert case["expected_source"] == expected_source
