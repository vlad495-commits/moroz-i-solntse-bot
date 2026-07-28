import importlib
import json

import asyncpg
import pytest

from moroz.common.db import Database


audit_repository = importlib.import_module("audit_repository")


@pytest.mark.asyncio
async def test_record_audit_persists_jsonb_before_after(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    previous_pool = audit_repository.database._pool
    audit_repository.database._pool = database
    try:
        await audit_repository.record_audit(
            actor_id=None,
            action="test.audit_jsonb",
            object_type="test_object",
            object_id="object-1",
            before={"old": "значение"},
            after={"new": 2},
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
    finally:
        audit_repository.database._pool = previous_pool
        await database.close()

    conn = await asyncpg.connect(migrated_database_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT before, after
            FROM admin_audit_events
            WHERE action = 'test.audit_jsonb'
            """
        )
    finally:
        await conn.close()

    assert json.loads(row["before"]) == {"old": "значение"}
    assert json.loads(row["after"]) == {"new": 2}
