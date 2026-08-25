from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from moroz.security.validator import StructuredFacts, validate_output


DATASET = Path("/workspace/llm/eval/validator_dataset.json")
CATEGORY_COUNTS = {
    "valid_product_response": 16,
    "valid_boundary_response": 8,
    "valid_edge_format": 6,
    "non_russian": 6,
    "incomplete_or_empty": 6,
    "technical_artifact": 6,
    "unprofessional": 4,
    "unsafe_advice": 4,
    "product_rule": 4,
}
SEMANTIC_REASONS = {
    "safe",
    "non_russian",
    "incomplete",
    "technical_artifact",
    "unprofessional",
    "unsafe_advice",
    "product_rule",
}
EMPTY_FACTS = StructuredFacts(frozenset(), frozenset(), frozenset())


def _cases():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_validator_dataset_has_exact_stable_contract():
    cases = _cases()
    keys = [case["case_key"] for case in cases]

    assert len(cases) == 60
    assert len(keys) == len(set(keys))
    assert all(key.startswith("validator-") for key in keys)
    assert Counter(case["category"] for case in cases) == CATEGORY_COUNTS
    assert Counter(case["expected_action"] for case in cases) == {
        "allow": 30,
        "regenerate": 30,
    }
    assert sum(case["critical"] for case in cases) == 20
    for case in cases:
        assert set(case) == {
            "case_key",
            "category",
            "input",
            "context",
            "route_metadata",
            "candidate",
            "expected_action",
            "expected_source",
            "expected_reason_code",
            "critical",
        }
        assert isinstance(case["input"], str) and case["input"].strip()
        assert isinstance(case["candidate"], str)
        assert isinstance(case["route_metadata"], str)
        assert all(
            set(message) == {"role", "content"}
            and message["role"] in {"user", "assistant"}
            and isinstance(message["content"], str)
            for message in case["context"]
        )
        assert case["expected_action"] in {"allow", "regenerate"}
        assert case["expected_source"] in {"local", "llm"}
        assert isinstance(case["expected_reason_code"], str)
        assert type(case["critical"]) is bool


def test_validator_dataset_is_synthetic_and_contains_no_secrets():
    raw = DATASET.read_text(encoding="utf-8").casefold()

    assert ".invalid" in raw
    for forbidden in (
        "@gmail.com",
        "@mail.ru",
        "@yandex.ru",
        "sk-proj-",
        "sk-ant-",
        "109.71.246.167",
        "moroz_internal_canary_v1",
    ):
        assert forbidden not in raw


@pytest.mark.parametrize(
    "case",
    json.loads(DATASET.read_text(encoding="utf-8")),
    ids=lambda case: case["case_key"],
)
def test_validator_dataset_source_matches_runtime_boundary(case):
    local = validate_output(case["candidate"], EMPTY_FACTS, frozenset())
    expected_source = "local" if not local.ok else "llm"

    assert case["expected_source"] == expected_source
    if expected_source == "local":
        assert case["expected_action"] == "regenerate"
        assert case["expected_reason_code"] == local.code
    else:
        assert case["expected_reason_code"] in SEMANTIC_REASONS
        assert (case["expected_action"] == "allow") == (
            case["expected_reason_code"] == "safe"
        )


def test_validator_dataset_covers_context_and_false_positive_boundaries():
    cases = _cases()

    assert any(case["context"] for case in cases)
    allowed = [case["candidate"] for case in cases if case["expected_action"] == "allow"]
    joined = "\n".join(allowed).casefold()
    assert "cryo" in joined
    assert "специалист" in joined
    assert "администратор" in joined
