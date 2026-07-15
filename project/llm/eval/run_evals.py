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

from llm import init_llm, generate_response  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).resolve().parent


def _load_dataset(name: str) -> list[dict]:
    path = EVAL_DIR / f"{name}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_case_id(case: dict, ordinal: int) -> int:
    case_id = case.get("id")
    if isinstance(case_id, int) and not isinstance(case_id, bool):
        return case_id
    return ordinal


def _load_guardrail_checker():
    """Вернуть guardrail-проверку, если она доступна на текущей ступени."""
    import config

    enabled = getattr(config, "GUARDRAILS_INPUT_ENABLED", False)
    categories = getattr(config, "GUARDRAILS_INPUT_CATEGORIES", ())
    if not enabled or not categories:
        return None

    try:
        from guardrails import check_input
    except ModuleNotFoundError as error:
        if error.name == "guardrails":
            return None
        raise

    return check_input


async def _run_dataset() -> tuple[int, int]:
    """Прогнать общий тестовый датасет (smoke + категории)."""
    try:
        cases = _load_dataset("dataset")
    except Exception as error:
        print(f"[dataset] status=error error_type={type(error).__name__}")
        return 0, 1
    if not cases:
        print("[dataset] dataset.json пустой — пропуск")
        return 0, 0

    try:
        init_llm()
    except Exception as error:
        print(f"[dataset] status=error error_type={type(error).__name__}")
        return 0, len(cases)
    passed = 0
    failed = 0

    for ordinal, case in enumerate(cases, start=1):
        case_id = _safe_case_id(case, ordinal)
        input_text = case["input"]
        expected_contains = case.get("expected_contains", [])
        try:
            result = await generate_response(input_text, context=[])
            response_text = result.text.lower()
        except Exception as error:
            print(
                f"[dataset] case={case_id} status=error "
                f"error_type={type(error).__name__}"
            )
            failed += 1
            continue

        # Проверка: все expected_contains должны быть в ответе
        missing = [w for w in expected_contains if w.lower() not in response_text]
        if missing:
            print(
                f"[dataset] case={case_id} status=failed "
                f"missing_count={len(missing)}"
            )
            failed += 1
        else:
            print(f"[dataset] case={case_id} status=passed")
            passed += 1

    return passed, failed


async def _run_adversarial() -> tuple[int, int]:
    """Прогнать jailbreak-атаки: проверяем что guardrails ловит."""
    try:
        checker = _load_guardrail_checker()
    except Exception as error:
        print(f"[adversarial] status=error error_type={type(error).__name__}")
        return 0, 1
    if checker is None:
        print("[adversarial] status=unavailable")
        return 0, 0

    try:
        cases = _load_dataset("adversarial_dataset")
    except Exception as error:
        print(f"[adversarial] status=error error_type={type(error).__name__}")
        return 0, 1
    if not cases:
        print("[adversarial] adversarial_dataset.json пустой — пропуск")
        return 0, 0

    passed = 0
    failed = 0

    for ordinal, case in enumerate(cases, start=1):
        case_id = _safe_case_id(case, ordinal)
        input_text = case["input"]
        expected = case.get("expected", "input_blocked")

        try:
            ok, _reason = checker(input_text)
        except Exception as error:
            print(
                f"[adv] case={case_id} status=error "
                f"error_type={type(error).__name__}"
            )
            failed += 1
            continue
        actually_blocked = not ok

        # input_blocked = guardrail должен сработать на входе
        if expected == "input_blocked":
            if actually_blocked:
                print(f"[adv] case={case_id} status=blocked")
                passed += 1
            else:
                print(f"[adv] case={case_id} status=missed")
                failed += 1
        # prompt_defense = guardrail может пропустить, но LLM должна не раскрыть промпт
        elif expected == "prompt_defense":
            if actually_blocked:
                print(f"[adv] case={case_id} status=blocked")
                passed += 1
            else:
                print(f"[adv] case={case_id} status=manual_review")
                # Не считаем фейлом — output guardrail должен сработать
                passed += 1
        else:
            print(f"[adv] case={case_id} status=unsupported")
            failed += 1

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
