"""Append-only admin audit writes."""

from __future__ import annotations

from typing import Any

import database


async def record_audit(
    *,
    actor_id: int | None,
    action: str,
    object_type: str,
    object_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    if not database._pool:
        return
    async with database._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_audit_events (
                actor_id, action, object_type, object_id,
                before, after, ip_address, user_agent
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            actor_id,
            action,
            object_type,
            object_id,
            before,
            after,
            ip_address,
            user_agent,
        )

