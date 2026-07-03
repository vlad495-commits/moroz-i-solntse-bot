"""CRUD для таблицы prompt_versions (история системного промпта)."""

from typing import Any

import database


async def list_versions(limit: int = 50) -> list[dict[str, Any]]:
    if not database._pool:
        return []
    async with database._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, author, comment, created_at
            FROM prompt_versions
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def get_version(version_id: int) -> dict[str, Any] | None:
    if not database._pool:
        return None
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, content, author, comment, created_at "
            "FROM prompt_versions WHERE id = $1",
            version_id,
        )
    return dict(row) if row else None


async def get_latest_version() -> dict[str, Any] | None:
    if not database._pool:
        return None
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, content, author, comment, created_at "
            "FROM prompt_versions ORDER BY created_at DESC LIMIT 1"
        )
    return dict(row) if row else None


async def create_version(
    content: str,
    author: str | None,
    comment: str | None,
) -> int:
    if not database._pool:
        raise RuntimeError("DB pool не инициализирован")
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO prompt_versions (content, author, comment)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            content, author, comment,
        )
    return int(row["id"])
