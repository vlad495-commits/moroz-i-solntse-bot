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
