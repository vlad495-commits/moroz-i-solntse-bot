"""CRUD для eval-таблиц: тест-кейсы, прогоны, результаты."""

import logging
from datetime import datetime
from typing import Any

import database

logger = logging.getLogger(__name__)


# --- eval_cases ---

async def list_cases() -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, category, question, expected_keywords, forbidden_keywords,
                      expected_answer, created_at, updated_at
               FROM eval_cases ORDER BY id ASC"""
        )
    return [dict(r) for r in rows]


async def get_case(case_id: int) -> dict[str, Any] | None:
    if not database._pool:
        return None
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM eval_cases WHERE id = $1", case_id
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
               WHERE id = $1""",
            case_id, category, question, expected_keywords,
            forbidden_keywords, expected_answer,
        )


async def delete_case(case_id: int) -> None:
    async with database._pool.acquire() as conn:
        await conn.execute("DELETE FROM eval_cases WHERE id = $1", case_id)


# --- eval_runs ---

async def create_run(total: int, judge_model: str) -> int:
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO eval_runs (total, judge_model)
               VALUES ($1, $2) RETURNING id""",
            total, judge_model,
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


async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, started_at, finished_at, total, passed, failed,
                      status, judge_model
               FROM eval_runs ORDER BY started_at DESC LIMIT $1""",
            limit,
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
) -> int:
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO eval_results
               (run_id, case_id, question, expected_answer, actual_answer,
                verdict, check_layer, score, judge_reasoning, duration_ms, error_message)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
               RETURNING id""",
            run_id, case_id, question, expected_answer, actual_answer,
            verdict, check_layer, score, judge_reasoning, duration_ms, error_message,
        )
    return row["id"]


async def get_run_results(run_id: int) -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, case_id, question, expected_answer, actual_answer,
                      verdict, check_layer, score, judge_reasoning,
                      duration_ms, error_message, created_at
               FROM eval_results WHERE run_id = $1 ORDER BY id ASC""",
            run_id,
        )
    return [dict(r) for r in rows]


async def get_run_results_since(run_id: int, last_id: int) -> list[dict[str, Any]]:
    """Получить результаты прогона с id > last_id (для SSE-стрима)."""
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, case_id, question, verdict, check_layer, score
               FROM eval_results
               WHERE run_id = $1 AND id > $2
               ORDER BY id ASC""",
            run_id, last_id,
        )
    return [dict(r) for r in rows]
