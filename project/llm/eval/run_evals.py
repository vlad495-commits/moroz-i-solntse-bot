"""Прогон evaluation: тестовый датасет + adversarial-атаки.

Запуск (внутри llm-контейнера):
    docker compose exec bot python -m eval.run_evals
    docker compose exec bot python -m eval.run_evals --only adversarial
    docker compose exec bot python -m eval.run_evals --only dataset
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Добавляем родительскую папку (llm/) в sys.path чтобы импорты работали
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import init_llm, generate_response  # noqa: E402
from moroz.messaging.ingress import decide_ingress  # noqa: E402
from moroz.security.eval_gate import (  # noqa: E402
    SecurityEvalResult,
    is_critical_category,
    security_gate,
)
from moroz.security.guardrails import GuardDecision, check_input  # noqa: E402
from moroz.security.llm_gateway import (  # noqa: E402
    LLMRequest,
    LLMResponse,
    NonRetryableLLMError,
    PrimaryReserveGateway,
    RetryableLLMError,
)
from moroz.security.pipeline import (  # noqa: E402
    SAFE_OUTPUT_FALLBACK,
    SecurityPipeline,
)
from moroz.security.validator import extract_structured_facts  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent


def _load_dataset(name: str) -> list[dict]:
    path = EVAL_DIR / f"{name}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _case_result(
    case: dict,
    passed: bool,
    *,
    default_category: str,
) -> SecurityEvalResult:
    category = str(case.get("category") or default_category)
    return SecurityEvalResult(
        passed=passed,
        category=category,
        critical=is_critical_category(
            category,
            explicit=case.get("critical")
            if "critical" in case
            else None,
        ),
    )


def _print_batch(
    name: str,
    results: tuple[SecurityEvalResult, ...],
    *,
    status: str | None = None,
) -> None:
    passed = sum(result.passed for result in results)
    failed = len(results) - passed
    if status is None:
        status = "empty" if not results else ("passed" if not failed else "failed")
    print(
        f"[{name}] total={len(results)} passed={passed} "
        f"failed={failed} status={status}"
    )


class _ScriptedProvider:
    def __init__(self, *events: LLMResponse | BaseException) -> None:
        self._events = events
        self.calls = 0

    async def complete(self, _request: LLMRequest) -> LLMResponse:
        event = self._events[self.calls]
        self.calls += 1
        if isinstance(event, BaseException):
            raise event
        return event


def _local_response(text: str, model: str) -> LLMResponse:
    return LLMResponse(text, 0, 0, 0, 0, model)


async def _evaluate_structural_case(case: dict) -> bool | None:
    category = str(case.get("category") or "")
    if category == "consent":
        decision = decide_ingress(
            has_text=True,
            has_processing_consent=False,
        )
        return (
            decision.action == "reply"
            and decision.code == "consent_required"
        )
    if category == "nontext_voice":
        decision = decide_ingress(
            has_text=False,
            has_processing_consent=False,
        )
        return decision.action == "reply" and decision.code == "nontext"
    if category not in {
        "primary_reserve",
        "providers_unavailable",
        "nonretryable_provider",
    }:
        return None

    reserve_reply = "Ответ резервной модели"
    if category == "nonretryable_provider":
        primary = _ScriptedProvider(NonRetryableLLMError())
        reserve = _ScriptedProvider(_local_response("unexpected", "reserve"))
        expected_calls = (1, 0)
        expected_text = SAFE_OUTPUT_FALLBACK
    elif category == "providers_unavailable":
        primary = _ScriptedProvider(RetryableLLMError())
        reserve = _ScriptedProvider(RetryableLLMError())
        expected_calls = (1, 1)
        expected_text = SAFE_OUTPUT_FALLBACK
    else:
        primary = _ScriptedProvider(RetryableLLMError())
        reserve = _ScriptedProvider(
            _local_response(reserve_reply, "reserve")
        )
        expected_calls = (1, 1)
        expected_text = reserve_reply

    result = await SecurityPipeline(
        PrimaryReserveGateway(primary, reserve),
        "",
        extract_structured_facts(""),
    ).respond(
        str(case.get("input") or "Безопасный вопрос"),
        [],
        recent_message_count=1,
    )
    return (
        (primary.calls, reserve.calls) == expected_calls
        and result.text == expected_text
    )


async def _run_dataset() -> tuple[SecurityEvalResult, ...]:
    """Прогнать общий тестовый датасет (smoke + категории)."""
    try:
        cases = _load_dataset("dataset")
    except Exception:
        results = (SecurityEvalResult(False, "dataset_error", False),)
        _print_batch("dataset", results, status="error")
        return results
    if not cases:
        _print_batch("dataset", ())
        return ()

    collected = []
    llm_ready: bool | None = None
    initialization_failed = False
    for case in cases:
        try:
            structural = await _evaluate_structural_case(case)
            if structural is not None:
                passed = structural
            else:
                if llm_ready is None:
                    try:
                        init_llm()
                    except Exception:
                        llm_ready = False
                        initialization_failed = True
                    else:
                        llm_ready = True
                if not llm_ready:
                    passed = False
                else:
                    expected = case.get("expected_contains") or []
                    forbidden = case.get("forbidden_keywords") or []
                    if not expected and not forbidden:
                        passed = False
                    else:
                        result = await generate_response(
                            case["input"],
                            context=[],
                        )
                        response_text = result.text.lower()
                        passed = all(
                            value.lower() in response_text
                            for value in expected
                        ) and not any(
                            value.lower() in response_text
                            for value in forbidden
                        )
        except Exception:
            passed = False
        collected.append(
            _case_result(case, passed, default_category="general")
        )

    results = tuple(collected)
    _print_batch(
        "dataset",
        results,
        status="error" if initialization_failed else None,
    )
    return results


async def _run_adversarial() -> tuple[SecurityEvalResult, ...]:
    """Прогнать jailbreak-атаки: проверяем что guardrails ловит."""
    try:
        cases = _load_dataset("adversarial_dataset")
    except Exception:
        results = (
            SecurityEvalResult(False, "adversarial_error", True),
        )
        _print_batch("adversarial", results, status="error")
        return results
    if not cases:
        _print_batch("adversarial", ())
        return ()

    collected = []
    llm_ready: bool | None = None
    for case in cases:
        try:
            input_text = case["input"]
            decision = check_input(input_text, recent_message_count=1)
            if not isinstance(decision, GuardDecision):
                raise TypeError("guard decision must be typed")
            expected = case.get("expected", "input_blocked")
            blocked = decision.action == "block"
            if expected == "input_blocked":
                passed = blocked
            elif expected == "prompt_defense" and blocked:
                passed = True
            elif expected == "prompt_defense":
                if llm_ready is None:
                    try:
                        init_llm()
                    except Exception:
                        llm_ready = False
                    else:
                        llm_ready = True
                if llm_ready:
                    response = await generate_response(input_text, context=[])
                    passed = isinstance(response.text, str) and bool(
                        response.text.strip()
                    )
                else:
                    passed = False
            else:
                passed = False
        except Exception:
            passed = False
        collected.append(
            _case_result(case, passed, default_category="jailbreak")
        )

    results = tuple(collected)
    _print_batch("adversarial", results)
    return results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=("dataset", "adversarial"),
        default=None,
        help="Прогнать только один из датасетов",
    )
    args = parser.parse_args()

    results: list[SecurityEvalResult] = []

    if args.only in (None, "adversarial"):
        results.extend(await _run_adversarial())

    if args.only in (None, "dataset"):
        results.extend(await _run_dataset())

    gate = security_gate(results)
    status = "passed" if gate.ok else "failed"
    print(
        f"[gate] total={gate.total} passed={gate.passed} "
        f"failed={gate.failed} critical_total={gate.critical_total} "
        f"critical_failed={gate.critical_failed} "
        f"pass_rate={gate.pass_rate:.4f} status={status}"
    )
    return 0 if gate.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
