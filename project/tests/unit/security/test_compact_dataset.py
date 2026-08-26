from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


DATASET = Path("/workspace/llm/eval/compact_dataset.json")
CATEGORY_COUNTS = {
    "threshold_boundary": 6,
    "fact_retention": 8,
    "agreement_retention": 6,
    "open_question_constraint": 6,
    "conflicting_updates": 6,
    "no_hallucination": 4,
    "privacy_and_injection": 4,
}
CRITICAL_COUNTS = {
    "threshold_boundary": 2,
    "fact_retention": 4,
    "agreement_retention": 4,
    "open_question_constraint": 4,
    "conflicting_updates": 6,
    "no_hallucination": 4,
    "privacy_and_injection": 4,
}


def _cases() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_compact_dataset_has_exact_stable_contract():
    cases = _cases()
    keys = [case["case_key"] for case in cases]

    assert len(cases) == 40
    assert len(keys) == len(set(keys))
    assert all(key.startswith("compact-") for key in keys)
    assert Counter(case["category"] for case in cases) == CATEGORY_COUNTS
    assert Counter(
        case["category"] for case in cases if case["critical"]
    ) == CRITICAL_COUNTS
    assert sum(case["critical"] for case in cases) == 28

    for case in cases:
        assert set(case) == {
            "case_key",
            "category",
            "context",
            "expected_mode",
            "required_facts",
            "forbidden_facts",
            "critical",
        }
        assert case["expected_mode"] in {"unchanged", "llm"}
        assert type(case["critical"]) is bool
        assert case["context"]
        assert all(
            set(message) == {"role", "content"}
            and message["role"] in {"user", "assistant"}
            and isinstance(message["content"], str)
            and message["content"].strip()
            for message in case["context"]
        )
        assert all(
            isinstance(fact, str) and fact.strip()
            for fact in case["required_facts"] + case["forbidden_facts"]
        )


def test_compact_dataset_pins_30_31_boundary_and_quality_context_sizes():
    cases = _cases()
    boundary = [case for case in cases if case["category"] == "threshold_boundary"]

    assert Counter(len(case["context"]) for case in boundary) == {30: 3, 31: 3}
    assert all(
        case["expected_mode"] == ("unchanged" if len(case["context"]) == 30 else "llm")
        for case in boundary
    )
    assert all(len(case["context"]) == 31 for case in cases if case not in boundary)
    assert all(case["expected_mode"] == "llm" for case in cases if case not in boundary)


def test_compact_dataset_is_synthetic_and_contains_no_secrets_or_raw_pii():
    raw = DATASET.read_text(encoding="utf-8").casefold()

    assert "example.invalid" in raw
    assert "<phone>" in raw
    assert "<email>" in raw
    for forbidden in (
        "@gmail.com",
        "@mail.ru",
        "@yandex.ru",
        "sk-proj-",
        "sk-ant-",
        "109.71.246.167",
        "moroz_internal_canary_v1",
        "+7 9",
        "+79",
        "system prompt",
    ):
        assert forbidden not in raw


def test_compact_dataset_covers_retention_conflicts_and_hallucinations():
    cases = _cases()

    assert all(case["required_facts"] for case in cases if case["category"] in {
        "fact_retention",
        "agreement_retention",
        "open_question_constraint",
        "conflicting_updates",
    })
    assert all(case["forbidden_facts"] for case in cases if case["category"] in {
        "conflicting_updates",
        "no_hallucination",
        "privacy_and_injection",
    })

