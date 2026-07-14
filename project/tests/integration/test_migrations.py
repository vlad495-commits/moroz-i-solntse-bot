import asyncpg
import pytest


pytestmark = pytest.mark.asyncio


async def test_alembic_creates_existing_schema(migrated_database_url):
    conn = await asyncpg.connect(migrated_database_url)
    try:
        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        indexes = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
    finally:
        await conn.close()

    assert {
        "messages",
        "token_usage",
        "prompt_versions",
        "eval_cases",
        "eval_runs",
        "eval_results",
        "eval_case_reviews",
    } <= tables
    assert {
        "idx_messages_chat_created",
        "idx_token_usage_chat_created",
        "idx_prompt_versions_created",
        "idx_eval_results_run",
        "idx_eval_case_reviews_case_id",
        "idx_eval_case_reviews_status",
    } <= indexes
