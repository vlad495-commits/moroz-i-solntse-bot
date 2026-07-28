"""Admin user/session database access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import database
from security import new_csrf_token


async def count_admin_users() -> int:
    if not database._pool:
        return 0
    async with database._pool.acquire() as conn:
        value = await conn.fetchval("SELECT COUNT(*) FROM admin_users")
    return int(value or 0)


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    if not database._pool:
        return None
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, username, role, password_hash, totp_secret, enabled
            FROM admin_users
            WHERE username = $1
            """,
            username,
        )
    return dict(row) if row else None


async def create_session(user_id: int, session_id: str, ttl_seconds: int) -> str:
    csrf_token = new_csrf_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    if database._pool:
        async with database._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admin_sessions (id, user_id, csrf_token, expires_at)
                VALUES ($1, $2, $3, $4)
                """,
                session_id,
                user_id,
                csrf_token,
                expires_at,
            )
    return csrf_token

