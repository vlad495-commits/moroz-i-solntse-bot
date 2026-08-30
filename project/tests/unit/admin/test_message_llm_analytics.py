import pytest

import pricing


def test_usage_summary_sums_calls_tokens_and_each_models_price():
    assert hasattr(pricing, "summarize_usage_groups")
    result = pricing.summarize_usage_groups(
        [
            {
                "purpose": "router",
                "model": "gpt-4o-mini",
                "llm_calls": 2,
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "cached_tokens": 200,
                "total_tokens": 1100,
            },
            {
                "purpose": "answer",
                "model": "gpt-4.1",
                "llm_calls": 1,
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "cached_tokens": 100,
                "total_tokens": 550,
            },
        ]
    )

    assert result["llm_calls"] == 3
    assert result["prompt_tokens"] == 1500
    assert result["completion_tokens"] == 150
    assert result["cached_tokens"] == 300
    assert result["total_tokens"] == 1650
    assert result["cost_usd"] == pytest.approx(0.001445)
    assert result["savings_usd"] == pytest.approx(0.000165)
    assert [group["purpose"] for group in result["groups"]] == [
        "router",
        "answer",
    ]
    assert result["groups"][0]["cost_usd"] == pytest.approx(0.000195)
    assert result["groups"][1]["cost_usd"] == pytest.approx(0.00125)
