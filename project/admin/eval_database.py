"""CRUD для eval-таблиц: тест-кейсы, прогоны, результаты."""

import json
import logging
from typing import Any

import database

logger = logging.getLogger(__name__)


def _decode_json_fields(row, *fields: str) -> dict[str, Any]:
    item = dict(row)
    for field in fields:
        if isinstance(item.get(field), str):
            item[field] = json.loads(item[field])
    return item


# --- eval_cases ---

async def list_cases(suite: str = "answer") -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, suite, case_key, category, question,
                      expected_keywords, forbidden_keywords, expected_answer,
                      input_data, expected_data, critical, created_at, updated_at
               FROM eval_cases WHERE suite = $1 ORDER BY id ASC""",
            suite,
        )
    return [_decode_json_fields(r, "input_data", "expected_data") for r in rows]


async def list_problem_cases(suite: str = "answer") -> list[dict[str, Any]]:
    """Return cases whose latest eval result is fail/error."""
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """WITH latest_results AS (
                   SELECT DISTINCT ON (result.case_id)
                          result.case_id, result.verdict, result.run_id,
                          result.created_at, result.id
                   FROM eval_results result
                   JOIN eval_runs run ON run.id = result.run_id
                   WHERE result.case_id IS NOT NULL AND run.suite = $1
                   ORDER BY result.case_id, result.created_at DESC, result.id DESC
               )
               SELECT c.id, c.suite, c.case_key, c.category, c.question,
                      c.expected_keywords, c.forbidden_keywords,
                      c.expected_answer, c.input_data, c.expected_data,
                      c.critical, c.created_at, c.updated_at
               FROM eval_cases c
               JOIN latest_results lr ON lr.case_id = c.id
               WHERE c.suite = $1 AND lr.verdict <> 'pass'
               ORDER BY c.id ASC""",
            suite,
        )
    return [_decode_json_fields(r, "input_data", "expected_data") for r in rows]


async def get_case(case_id: int) -> dict[str, Any] | None:
    if not database._pool:
        return None
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM eval_cases WHERE id = $1 AND suite = 'answer'",
            case_id,
        )
    return dict(row) if row else None


async def create_case(
    category: str,
    question: str,
    expected_keywords: list[str],
    forbidden_keywords: list[str],
    expected_answer: str,
) -> int:
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO eval_cases
               (category, question, expected_keywords, forbidden_keywords, expected_answer)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id""",
            category, question, expected_keywords, forbidden_keywords, expected_answer,
        )
    return row["id"]


async def update_case(
    case_id: int,
    category: str,
    question: str,
    expected_keywords: list[str],
    forbidden_keywords: list[str],
    expected_answer: str,
) -> None:
    async with database._pool.acquire() as conn:
        await conn.execute(
            """UPDATE eval_cases
               SET category = $2, question = $3,
                   expected_keywords = $4, forbidden_keywords = $5,
                   expected_answer = $6, updated_at = NOW()
               WHERE id = $1 AND suite = 'answer'""",
            case_id, category, question, expected_keywords,
            forbidden_keywords, expected_answer,
        )


async def delete_case(case_id: int) -> None:
    async with database._pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM eval_cases WHERE id = $1 AND suite = 'answer'",
            case_id,
        )


# --- eval_runs ---

async def create_run(
    total: int,
    judge_model: str,
    suite: str = "answer",
) -> int:
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO eval_runs (total, judge_model, suite)
               VALUES ($1, $2, $3) RETURNING id""",
            total,
            judge_model,
            suite,
        )
    return row["id"]


async def update_run_progress(run_id: int, passed: int, failed: int) -> None:
    async with database._pool.acquire() as conn:
        await conn.execute(
            "UPDATE eval_runs SET passed = $2, failed = $3 WHERE id = $1",
            run_id, passed, failed,
        )


async def finish_run(
    run_id: int,
    passed: int,
    failed: int,
    status: str = "finished",
    error_message: str | None = None,
) -> None:
    async with database._pool.acquire() as conn:
        await conn.execute(
            """UPDATE eval_runs
               SET passed = $2, failed = $3, status = $4,
                   error_message = $5, finished_at = NOW()
               WHERE id = $1""",
            run_id, passed, failed, status, error_message,
        )


async def get_run(run_id: int) -> dict[str, Any] | None:
    if not database._pool:
        return None
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM eval_runs WHERE id = $1", run_id)
    return dict(row) if row else None


async def list_runs(
    limit: int = 50,
    suite: str = "answer",
) -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, suite, started_at, finished_at, total, passed, failed,
                      status, judge_model
               FROM eval_runs WHERE suite = $2
               ORDER BY started_at DESC LIMIT $1""",
            limit,
            suite,
        )
    return [dict(r) for r in rows]


# --- eval_results ---

async def save_result(
    run_id: int,
    case_id: int | None,
    question: str,
    expected_answer: str,
    actual_answer: str | None,
    verdict: str,
    check_layer: str | None,
    score: float | None,
    judge_reasoning: str | None,
    duration_ms: int,
    error_message: str | None = None,
    actual_data: dict[str, Any] | None = None,
) -> int:
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO eval_results
               (run_id, case_id, question, expected_answer, actual_answer,
                verdict, check_layer, score, judge_reasoning, duration_ms,
                error_message, actual_data)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                       $12::jsonb)
               RETURNING id""",
            run_id, case_id, question, expected_answer, actual_answer,
            verdict, check_layer, score, judge_reasoning, duration_ms,
            error_message,
            json.dumps(
                actual_data or {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    return row["id"]


async def get_run_results(run_id: int) -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT result.id, result.case_id, result.question,
                      result.expected_answer, result.actual_answer,
                      result.verdict, result.check_layer, result.score,
                      result.judge_reasoning, result.duration_ms,
                      result.error_message, result.actual_data,
                      cases.input_data, cases.expected_data, result.created_at
               FROM eval_results result
               JOIN eval_runs runs ON runs.id = result.run_id
               LEFT JOIN eval_cases cases
                 ON cases.id = result.case_id AND cases.suite = runs.suite
               WHERE result.run_id = $1 ORDER BY result.id ASC""",
            run_id,
        )
    return [
        _decode_json_fields(r, "input_data", "expected_data", "actual_data")
        for r in rows
    ]


async def get_run_results_since(run_id: int, last_id: int) -> list[dict[str, Any]]:
    """Получить результаты прогона с id > last_id (для SSE-стрима)."""
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT result.id, result.case_id, result.question,
                      result.verdict, result.check_layer, result.score,
                      result.error_message, result.actual_data,
                      cases.expected_data
               FROM eval_results result
               JOIN eval_runs runs ON runs.id = result.run_id
               LEFT JOIN eval_cases cases
                 ON cases.id = result.case_id AND cases.suite = runs.suite
               WHERE result.run_id = $1 AND result.id > $2
               ORDER BY result.id ASC""",
            run_id, last_id,
        )
    return [_decode_json_fields(r, "expected_data", "actual_data") for r in rows]
