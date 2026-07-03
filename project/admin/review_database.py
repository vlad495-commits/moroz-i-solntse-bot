"""Database helpers for client review of eval cases."""

from typing import Any

import database


STATUSES = {
    "pending": "На проверке",
    "ok": "Ок",
    "needs_edit": "Нужно поправить",
    "delete": "Убрать кейс",
    "new_case": "Новый кейс",
}


def normalize_status(status: str) -> str:
    status = (status or "pending").strip()
    return status if status in STATUSES else "pending"


async def list_review_cases(status: str = "all") -> list[dict[str, Any]]:
    if not database._pool:
        return []

    status = (status or "all").strip()
    where_sql = ""
    args: list[Any] = []
    if status == "pending":
        where_sql = "WHERE COALESCE(r.status, 'pending') = 'pending'"
    elif status in {"ok", "needs_edit", "delete"}:
        where_sql = "WHERE r.status = $1"
        args.append(status)

    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                c.id, c.category, c.question, c.expected_answer,
                c.expected_keywords, c.forbidden_keywords,
                COALESCE(r.status, 'pending') AS review_status,
                r.comment AS review_comment,
                r.proposed_answer,
                r.reviewer,
                r.updated_at AS review_updated_at
            FROM eval_cases c
            LEFT JOIN eval_case_reviews r ON r.case_id = c.id
            {where_sql}
            ORDER BY c.id ASC
            """,
            *args,
        )
    return [dict(r) for r in rows]


async def list_suggestions() -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, reviewer, comment, proposed_question,
                   proposed_answer, category, created_at, updated_at
            FROM eval_case_reviews
            WHERE case_id IS NULL
            ORDER BY updated_at DESC, id DESC
            """
        )
    return [dict(r) for r in rows]


async def get_review_counts() -> dict[str, int]:
    counts = {"all": 0, "pending": 0, "ok": 0, "needs_edit": 0, "delete": 0, "new_case": 0}
    if not database._pool:
        return counts

    async with database._pool.acquire() as conn:
        case_rows = await conn.fetch(
            """
            SELECT COALESCE(r.status, 'pending') AS status, COUNT(*) AS n
            FROM eval_cases c
            LEFT JOIN eval_case_reviews r ON r.case_id = c.id
            GROUP BY COALESCE(r.status, 'pending')
            """
        )
        suggestion_count = await conn.fetchval(
            "SELECT COUNT(*) FROM eval_case_reviews WHERE case_id IS NULL"
        )

    for row in case_rows:
        row_status = row["status"]
        if row_status in counts:
            counts[row_status] = row["n"]
        counts["all"] += row["n"]
    counts["new_case"] = suggestion_count or 0
    return counts


async def save_case_review(
    case_id: int,
    status: str,
    comment: str,
    proposed_answer: str,
    reviewer: str,
) -> None:
    status = normalize_status(status)
    comment = (comment or "").strip()
    proposed_answer = (proposed_answer or "").strip()

    async with database._pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "SELECT id FROM eval_case_reviews WHERE case_id = $1",
            case_id,
        )
        if existing_id:
            await conn.execute(
                """
                UPDATE eval_case_reviews
                SET status = $2, comment = $3, proposed_answer = $4,
                    reviewer = $5, updated_at = NOW()
                WHERE id = $1
                """,
                existing_id, status, comment, proposed_answer, reviewer,
            )
            return

        await conn.execute(
            """
            INSERT INTO eval_case_reviews
                (case_id, status, comment, proposed_answer, reviewer)
            VALUES ($1, $2, $3, $4, $5)
            """,
            case_id, status, comment, proposed_answer, reviewer,
        )


async def create_suggestion(
    question: str,
    expected_answer: str,
    comment: str,
    reviewer: str,
) -> None:
    async with database._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO eval_case_reviews
                (case_id, status, reviewer, comment, proposed_question,
                 proposed_answer, category)
            VALUES (NULL, 'new_case', $1, $2, $3, $4, $5)
            """,
            reviewer,
            (comment or "").strip(),
            (question or "").strip(),
            (expected_answer or "").strip(),
            "general",
        )


async def delete_suggestion(suggestion_id: int) -> None:
    if not database._pool:
        return
    async with database._pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM eval_case_reviews WHERE id = $1 AND case_id IS NULL",
            suggestion_id,
        )
