import json
from pathlib import Path

import pytest

from eval import run_evals
from moroz.security.eval_catalog import evaluate_catalog_case


CATALOG_DATASET = Path("/workspace/llm/eval/catalog_dataset.json")


def test_catalog_eval_dataset_has_six_distinct_synthetic_scenarios():
    cases = json.loads(CATALOG_DATASET.read_text(encoding="utf-8"))

    assert [case["scenario"] for case in cases] == [
        "fresh_price",
        "missing_service",
        "ambiguous_name",
        "stale_catalog",
        "catalog_injection",
        "invented_price",
    ]
    assert len({case["id"] for case in cases}) == 6
    assert all(case["id"].startswith("catalog-") for case in cases)
    assert all("expected_contains" in case for case in cases)


@pytest.mark.asyncio
async def test_every_synthetic_catalog_case_executes_real_pipeline_contract():
    cases = json.loads(CATALOG_DATASET.read_text(encoding="utf-8"))

    results = [await evaluate_catalog_case(case) for case in cases]

    assert results == [True] * 6


@pytest.mark.asyncio
async def test_catalog_cli_batch_reports_dedicated_results(monkeypatch, capsys):
    cases = json.loads(CATALOG_DATASET.read_text(encoding="utf-8"))
    monkeypatch.setattr(run_evals, "_load_dataset", lambda name: cases)

    results = await run_evals._run_catalog()

    assert len(results) == 6
    assert all(result.passed for result in results)
    assert "[catalog] total=6 passed=6 failed=0 status=passed" in capsys.readouterr().out
