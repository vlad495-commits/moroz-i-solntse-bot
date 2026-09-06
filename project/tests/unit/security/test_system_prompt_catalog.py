import re
from pathlib import Path


def test_system_prompt_owns_prices_and_not_catalog_grounding():
    prompt = Path("/workspace/llm/prompts/system.md").read_text(encoding="utf-8")
    assert "Солярий — 42 ₽ за минуту." in prompt
    assert "Коллариум — 51 ₽ за минуту." in prompt
    assert "Коллагенарий — 42 ₽ за минуту." in prompt
    assert "UNTRUSTED_CATALOG_DATA" not in prompt
    assert "не перечисляй все варианты" in prompt
    assert "MOROZ_INTERNAL_CANARY_V1" in prompt
    assert len(re.findall(r"^## \d+\.", prompt, re.MULTILINE)) == 16


def test_system_prompt_keeps_center_facts_and_uncertain_program_boundaries():
    prompt = Path("/workspace/llm/prompts/system.md").read_text(encoding="utf-8")
    folded = prompt.casefold()
    assert "трудовые резервы, 33б" in folded
    assert 'трц "первый"' in folded
    assert "цокольный этаж" in folded
    assert "fresh день" in folded
    assert "3 криокапсулы, 3 прессотерапии" in folded
    assert "не предлагай неподтверждённые программы инициативно" in folded
    assert "не назначай число минут" in folded
    assert "не рассчитывай стоимость пакета сложением разовых цен" in folded
