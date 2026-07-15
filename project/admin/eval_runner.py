"""Eval-runner: гоняет тест-кейсы через бот → проверка regex → при необходимости LLM-judge.

Архитектура:
- Admin сам инстанцирует AsyncOpenAI / AsyncAnthropic клиенты с теми же ключами.
- Системный промпт читается из volume `/app/prompts/system.md`.
- Bot-реплику получаем чистым LLM-вызовом (без guardrails и без буфера) —
  это специально, мы тестируем "ядро" бота.
- Двухступенчатая проверка: regex/keywords → если не прошёл → LLM-judge.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from openai import AsyncOpenAI

import eval_database as evdb

logger = logging.getLogger(__name__)

# --- Конфиг ---
LLM_API_KEY = os.getenv("LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "") or None
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")

RESERVE_API_KEY = os.getenv("RESERVE_API_KEY", "")
RESERVE_BASE_URL = os.getenv("RESERVE_BASE_URL", "") or None
RESERVE_MODEL = os.getenv("RESERVE_MODEL", "")

JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4.1-mini")
JUDGE_API_KEY = os.getenv("JUDGE_API_KEY", "") or LLM_API_KEY
JUDGE_BASE_URL = os.getenv("JUDGE_BASE_URL", "") or None
JUDGE_PASS_THRESHOLD = float(os.getenv("JUDGE_PASS_THRESHOLD", "0.8"))

LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2000"))

PROMPT_PATH = Path("/app/prompts/system.md")


def _detect_kind(model: str, base_url: str | None) -> str:
    """Тот же детектор как в llm.py — без зависимости от него."""
    if base_url:
        return "openai"
    if model.lower().startswith("claude") or "claude-" in model.lower():
        return "anthropic"
    return "openai"


def _create_client(api_key: str, base_url: str | None, kind: str):
    if kind == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=api_key)
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


_primary = None
_primary_kind: str = ""
_judge = None
_judge_kind: str = ""
_reserve = None
_reserve_kind: str = ""


def _init_clients() -> None:
    global _primary, _primary_kind, _judge, _judge_kind, _reserve, _reserve_kind

    if _primary is None and LLM_API_KEY:
        _primary_kind = _detect_kind(LLM_MODEL, LLM_BASE_URL)
        _primary = _create_client(LLM_API_KEY, LLM_BASE_URL, _primary_kind)

    if _judge is None and JUDGE_API_KEY:
        _judge_kind = _detect_kind(JUDGE_MODEL, JUDGE_BASE_URL)
        # Если ключ + base_url совпадают с основным — переиспользуем
        if (JUDGE_API_KEY == LLM_API_KEY and JUDGE_BASE_URL == LLM_BASE_URL
                and _judge_kind == _primary_kind):
            _judge = _primary
        else:
            _judge = _create_client(JUDGE_API_KEY, JUDGE_BASE_URL, _judge_kind)

    if _reserve is None and RESERVE_API_KEY and RESERVE_MODEL:
        try:
            _reserve_kind = _detect_kind(RESERVE_MODEL, RESERVE_BASE_URL)
            _reserve = _create_client(RESERVE_API_KEY, RESERVE_BASE_URL, _reserve_kind)
        except ImportError:
            logger.warning("Резервный клиент не создан (нет нужной библиотеки)")


def _read_system_prompt() -> str:
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text(encoding="utf-8").strip()
    return ""


# --- Слой 1: regex/keywords проверка ---

def _matches_keyword(text: str, kw: str) -> bool:
    """Проверить вхождение ключевого слова. Префикс 'r:' = regex, иначе substring."""
    text_lower = text.lower()
    if kw.startswith("r:"):
        try:
            return bool(re.search(kw[2:], text, re.IGNORECASE))
        except re.error:
            logger.warning("Невалидный regex: %s", kw)
            return False
    return kw.lower() in text_lower


def keyword_check(
    actual: str,
    expected_keywords: list[str],
    forbidden_keywords: list[str],
) -> tuple[bool, str]:
    """Возвращает (прошёл_ли, причина).

    Проходит если:
    - Все expected_keywords найдены в actual
    - Ни один forbidden_keyword не найден в actual
    """
    missing = [kw for kw in expected_keywords if not _matches_keyword(actual, kw)]
    if missing:
        return False, f"Не найдены ключевые слова: {missing}"

    found_forbidden = [kw for kw in forbidden_keywords if _matches_keyword(actual, kw)]
    if found_forbidden:
        return False, f"Найдены запрещённые слова: {found_forbidden}"

    return True, ""


# --- Слой 2: LLM-judge ---

JUDGE_PROMPT_TEMPLATE = """Ты эксперт-оценщик ответов AI-ассистента. Сравни эталонный ответ с фактическим ответом ассистента и оцени насколько фактический ответ соответствует эталонному ПО СМЫСЛУ (а не дословно).

Вопрос пользователя:
{question}

Эталонный ответ:
{expected}

Фактический ответ ассистента:
{actual}

Оцени по шагам:
1. Покрывает ли фактический ответ ключевую информацию из эталонного?
2. Нет ли в фактическом ответе фактических ошибок или противоречий с эталоном?
3. Допускается перефразирование, синонимы, разный порядок изложения — это НЕ ошибка.
4. Допускается если фактический ответ короче или длиннее эталонного — это НЕ ошибка.

Поставь итоговый score от 0.0 до 1.0:
- 1.0 = ответ полностью соответствует эталону по смыслу
- 0.7-0.99 = ответ верный по смыслу, есть небольшие упущения
- 0.4-0.69 = ответ частично соответствует, есть пропуски ключевой информации
- 0.0-0.39 = ответ неверный, противоречит эталону или не отвечает на вопрос

Верни СТРОГО валидный JSON без markdown-обёртки:
{{"score": 0.85, "reasoning": "Краткое обоснование 1-2 предложения"}}
"""


async def _invoke_llm(client, kind: str, model: str, messages: list[dict]) -> str:
    """Универсальный вызов LLM. Возвращает строку с ответом."""
    if kind == "anthropic":
        # Извлекаем system отдельно, конвертируем формат
        system = ""
        msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                msgs.append(m)
        resp = await client.messages.create(
            model=model,
            max_tokens=LLM_MAX_TOKENS,
            system=system,
            messages=msgs,
            temperature=LLM_TEMPERATURE,
        )
        return "\n".join(b.text for b in resp.content if b.type == "text")

    # OpenAI-совместимый
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
    )
    return resp.choices[0].message.content or ""


async def llm_judge(question: str, expected: str, actual: str) -> tuple[float, str]:
    """LLM-as-judge: вернуть (score 0.0-1.0, reasoning)."""
    if not _judge:
        raise RuntimeError("Judge-клиент не инициализирован (JUDGE_API_KEY/LLM_API_KEY пусты)")

    prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=question, expected=expected, actual=actual
    )
    messages = [{"role": "user", "content": prompt}]

    # Для OpenAI-совместимых judge-моделей используем response_format,
    # для Anthropic — просим JSON в промпте (без структурированного формата).
    if _judge_kind == "openai":
        response = await _judge.chat.completions.create(
            model=JUDGE_MODEL,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
    else:
        content = await _invoke_llm(_judge, _judge_kind, JUDGE_MODEL, messages)

    try:
        data = json.loads(content)
        score = float(data.get("score", 0.0))
        reasoning = str(data.get("reasoning", "")).strip()
        return max(0.0, min(1.0, score)), reasoning
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(
            "judge_invalid_json content_length=%s error_type=%s",
            len(content),
            type(e).__name__,
        )
        return 0.0, "Judge parse error"


# --- Прогон одного кейса ---

async def _generate_bot_response(question: str, system_prompt: str) -> str:
    """Сгенерировать ответ бота на вопрос. Чистый LLM-вызов с системным промптом."""
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": question})

    try:
        return await _invoke_llm(_primary, _primary_kind, LLM_MODEL, messages)
    except Exception as e:
        # Fallback на резервный провайдер если основной упал
        if _reserve:
            logger.warning("Основной LLM упал, fallback на резервный: %s", e)
            try:
                return await _invoke_llm(_reserve, _reserve_kind, RESERVE_MODEL, messages)
            except Exception:
                logger.exception("Резервный fallback тоже упал")
        raise


async def run_case(case: dict, run_id: int) -> dict:
    """Прогнать один тест-кейс. Записать результат в БД. Вернуть запись результата."""
    started = time.time()
    system_prompt = _read_system_prompt()

    actual_answer = ""
    verdict = "fail"
    check_layer: str | None = None
    score: float | None = None
    reasoning: str | None = None
    error_message: str | None = None

    try:
        # 1. Получаем фактический ответ бота
        actual_answer = await _generate_bot_response(case["question"], system_prompt)

        # 2. Слой 1: regex/keywords
        keywords = list(case.get("expected_keywords") or [])
        forbidden = list(case.get("forbidden_keywords") or [])

        if keywords or forbidden:
            ok, reason = keyword_check(actual_answer, keywords, forbidden)
            if ok:
                verdict = "pass"
                check_layer = "regex"
                reasoning = "Все ключевые слова найдены"
            else:
                # Слой 1 не прошёл — пробуем judge
                check_layer = "judge"
                reasoning_part_1 = reason
                if case.get("expected_answer"):
                    score, judge_reason = await llm_judge(
                        case["question"],
                        case["expected_answer"],
                        actual_answer,
                    )
                    reasoning = f"{reasoning_part_1}. Judge: {judge_reason}"
                    verdict = "pass" if score >= JUDGE_PASS_THRESHOLD else "fail"
                else:
                    verdict = "fail"
                    reasoning = reasoning_part_1
        else:
            # Нет keywords — сразу judge
            check_layer = "judge"
            if case.get("expected_answer"):
                score, judge_reason = await llm_judge(
                    case["question"],
                    case["expected_answer"],
                    actual_answer,
                )
                reasoning = judge_reason
                verdict = "pass" if score >= JUDGE_PASS_THRESHOLD else "fail"
            else:
                verdict = "fail"
                reasoning = "Нет expected_answer — нечего сравнивать"

    except Exception as e:
        logger.exception("Ошибка в кейсе #%s", case.get("id"))
        error_message = str(e)
        verdict = "error"

    duration_ms = int((time.time() - started) * 1000)

    result_id = await evdb.save_result(
        run_id=run_id,
        case_id=case.get("id"),
        question=case["question"],
        expected_answer=case.get("expected_answer", ""),
        actual_answer=actual_answer,
        verdict=verdict,
        check_layer=check_layer,
        score=score,
        judge_reasoning=reasoning,
        duration_ms=duration_ms,
        error_message=error_message,
    )

    return {
        "id": result_id,
        "case_id": case.get("id"),
        "verdict": verdict,
        "check_layer": check_layer,
        "score": score,
    }


# --- Главный прогон ---

async def run_eval_set(run_id: int, cases: list[dict] | None = None) -> None:
    """Прогнать все кейсы. Идёт последовательно, чтобы прогресс-бар был стабилен."""
    _init_clients()

    if cases is None:
        cases = await evdb.list_cases()
    total = len(cases)

    if total == 0:
        await evdb.finish_run(run_id, 0, 0, status="finished")
        return

    passed = 0
    failed = 0

    try:
        for case in cases:
            res = await run_case(case, run_id)
            if res["verdict"] == "pass":
                passed += 1
            else:
                failed += 1
            await evdb.update_run_progress(run_id, passed, failed)

        await evdb.finish_run(run_id, passed, failed, status="finished")
    except Exception as e:
        logger.exception("Прогон #%s упал", run_id)
        await evdb.finish_run(
            run_id, passed, failed, status="error", error_message=str(e)
        )
