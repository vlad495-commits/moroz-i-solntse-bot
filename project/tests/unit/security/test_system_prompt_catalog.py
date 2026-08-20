import re
from pathlib import Path


def test_system_prompt_has_no_static_currency_prices_and_requires_catalog():
    prompt = Path("/app/llm/prompts/system.md").read_text(encoding="utf-8")

    assert re.search(r"\d[\d ]*\s*(?:руб|₽)", prompt, re.IGNORECASE) is None
    folded = prompt.casefold()
    assert "catalog_data" in folded
    assert "не угадывай" in folded
    assert "длительност" in folded
    assert "специалист" in folded


def test_system_prompt_keeps_stable_center_facts_without_catalog_prices():
    prompt = Path("/app/llm/prompts/system.md").read_text(encoding="utf-8")
    folded = prompt.casefold()

    assert "трудовые резервы, 33б" in folded
    assert 'трц "первый"' in folded
    assert "цокольный этаж" in folded
    assert all(
        direction in folded
        for direction in ("загар", "восстановление", "отдых", "уход за кожей")
    )
    assert "солярии часто начинают примерно от 5 минут" in folded
    assert "коллариуме — примерно от 7 минут" in folded
    assert "типу кожи" in folded
    assert "fresh день" in folded
    assert "криокапсула, водородотерапия, прессотерапия" in folded
    assert "антистресс за 1 неделю" in folded
    assert "3 процедуры криокапсулы" in folded
