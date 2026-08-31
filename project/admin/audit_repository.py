"""Append-only admin audit writes."""

from __future__ import annotations

import json
from typing import Any

import database


def request_ip_address(request: object) -> str | None:
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def request_user_agent(request: object) -> str | None:
    headers = getattr(request, "headers", None)
    if not headers:
        return None
    get_header = getattr(headers, "get", None)
    if not get_header:
        return None
    return get_header("user-agent")


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
        await record_audit_in_transaction(
            conn,
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            before=before,
            after=after,
            ip_address=ip_address,
            user_agent=user_agent,
        )


async def record_audit_in_transaction(
    connection,
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
    await connection.execute(
        """
        INSERT INTO admin_audit_events (
            actor_id, action, object_type, object_id,
            before, after, ip_address, user_agent
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8)
        """,
        actor_id,
        action,
        object_type,
        object_id,
        json.dumps(before, ensure_ascii=False) if before is not None else None,
        json.dumps(after, ensure_ascii=False) if after is not None else None,
        ip_address,
        user_agent,
    )
