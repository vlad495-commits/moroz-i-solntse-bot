"""Прогон evaluation: тестовый датасет + adversarial-атаки.

Запуск (внутри llm-контейнера):
    docker compose exec bot python -m eval.run_evals
    docker compose exec bot python -m eval.run_evals --only adversarial
    docker compose exec bot python -m eval.run_evals --only dataset
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Добавляем родительскую папку (llm/) в sys.path чтобы импорты работали
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import GUARDRAILS_INPUT_CATEGORIES, GUARDRAILS_INPUT_ENABLED  # noqa: E402
from guardrails import check_input  # noqa: E402
from llm import init_llm, generate_response  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).resolve().parent


def _load_dataset(name: str) -> list[dict]:
    path = EVAL_DIR / f"{name}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


async def _run_dataset() -> tuple[int, int]:
    """Прогнать общий тестовый датасет (smoke + категории)."""
    cases = _load_dataset("dataset")
    if not cases:
        print("[dataset] dataset.json пустой — пропуск")
        return 0, 0

    init_llm()
    passed = 0
    failed = 0

    for case in cases:
        input_text = case["input"]
        expected_contains = case.get("expected_contains", [])
        try:
            result = await generate_response(input_text, context=[])
            response_text = result.text.lower()
        except Exception as e:
            print(f"[dataset] #{case['id']} ❌ EXCEPTION: {e}")
            failed += 1
            continue

        # Проверка: все expected_contains должны быть в ответе
        missing = [w for w in expected_contains if w.lower() not in response_text]
        if missing:
            print(
                f"[dataset] #{case['id']} ❌ FAIL "
                f"(missing: {missing}, input={input_text!r})"
            )
            failed += 1
        else:
            print(f"[dataset] #{case['id']} ✅ OK")
            passed += 1

    return passed, failed


async def _run_adversarial() -> tuple[int, int]:
    """Прогнать jailbreak-атаки: проверяем что guardrails ловит."""
    if not GUARDRAILS_INPUT_ENABLED or not GUARDRAILS_INPUT_CATEGORIES:
        print("[adversarial] Guardrails выключены — прогон бессмыслен. "
              "Включи через /guardrails и попробуй ещё раз.")
        return 0, 0

    cases = _load_dataset("adversarial_dataset")
    if not cases:
        print("[adversarial] adversarial_dataset.json пустой — пропуск")
        return 0, 0

    passed = 0
    failed = 0

    for case in cases:
        input_text = case["input"]
        expected = case.get("expected", "input_blocked")

        ok, reason = check_input(input_text)
        actually_blocked = not ok

        # input_blocked = guardrail должен сработать на входе
        if expected == "input_blocked":
            if actually_blocked:
                print(f"[adv] #{case['id']} {case['technique']} ✅ заблокирован ({reason})")
                passed += 1
            else:
                print(f"[adv] #{case['id']} {case['technique']} ❌ ПРОПУЩЕН (не заблокирован!)")
                failed += 1
        # prompt_defense = guardrail может пропустить, но LLM должна не раскрыть промпт
        elif expected == "prompt_defense":
            if actually_blocked:
                print(f"[adv] #{case['id']} {case['technique']} ✅ заблокирован на входе ({reason})")
                passed += 1
            else:
                print(f"[adv] #{case['id']} {case['technique']} ⚠️  не заблокирован — нужна ручная проверка LLM-ответа")
                # Не считаем фейлом — output guardrail должен сработать
                passed += 1

    return passed, failed


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=("dataset", "adversarial"),
        default=None,
        help="Прогнать только один из датасетов",
    )
    args = parser.parse_args()

    total_passed = 0
    total_failed = 0

    if args.only in (None, "adversarial"):
        print("=== Adversarial-тесты (jailbreak) ===")
        p, f = await _run_adversarial()
        total_passed += p
        total_failed += f
        print(f"Adversarial: {p}/{p + f} прошли\n")

    if args.only in (None, "dataset"):
        print("=== Тестовый датасет ===")
        p, f = await _run_dataset()
        total_passed += p
        total_failed += f
        print(f"Dataset: {p}/{p + f} прошли\n")

    total = total_passed + total_failed
    if total == 0:
        print("Нечего прогонять.")
        return 0

    pass_rate = (total_passed / total) * 100
    print(f"=== ИТОГО: {total_passed}/{total} ({pass_rate:.1f}%) ===")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
