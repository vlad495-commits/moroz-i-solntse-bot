"""Fail-closed audit and Alembic stamp for the pre-Alembic schema."""

import argparse
import asyncio
import os
import sys

import asyncpg
from alembic import command
from alembic.config import Config


REVISION = "0001_existing_schema"

EXPECTED_COLUMNS = {
    "messages": [
        ("id", "bigint", True, "nextval('messages_id_seq'::regclass)"),
        ("chat_id", "bigint", True, None),
        ("user_id", "bigint", False, None),
        ("username", "character varying(255)", False, None),
        ("role", "character varying(16)", True, None),
        ("content", "text", True, None),
        ("created_at", "timestamp with time zone", True, "now()"),
        ("answered", "boolean", True, "false"),
    ],
    "token_usage": [
        ("id", "bigint", True, "nextval('token_usage_id_seq'::regclass)"),
        ("chat_id", "bigint", True, None),
        ("user_id", "bigint", False, None),
        ("prompt_tokens", "integer", True, "0"),
        ("completion_tokens", "integer", True, "0"),
        ("cached_tokens", "integer", True, "0"),
        ("total_tokens", "integer", True, "0"),
        ("model", "character varying(64)", True, None),
        ("created_at", "timestamp with time zone", True, "now()"),
    ],
    "prompt_versions": [
        ("id", "bigint", True, "nextval('prompt_versions_id_seq'::regclass)"),
        ("content", "text", True, None),
        ("author", "character varying(64)", False, None),
        ("comment", "text", False, None),
        ("created_at", "timestamp with time zone", True, "now()"),
    ],
    "eval_cases": [
        ("id", "bigint", True, "nextval('eval_cases_id_seq'::regclass)"),
        ("category", "character varying(64)", True, "'general'::character varying"),
        ("question", "text", True, None),
        ("expected_keywords", "text[]", True, "'{}'::text[]"),
        ("forbidden_keywords", "text[]", True, "'{}'::text[]"),
        ("expected_answer", "text", True, None),
        ("created_at", "timestamp with time zone", True, "now()"),
        ("updated_at", "timestamp with time zone", True, "now()"),
    ],
    "eval_runs": [
        ("id", "bigint", True, "nextval('eval_runs_id_seq'::regclass)"),
        ("started_at", "timestamp with time zone", True, "now()"),
        ("finished_at", "timestamp with time zone", False, None),
        ("total", "integer", True, "0"),
        ("passed", "integer", True, "0"),
        ("failed", "integer", True, "0"),
        ("status", "character varying(16)", True, "'running'::character varying"),
        ("judge_model", "character varying(64)", False, None),
        ("error_message", "text", False, None),
    ],
    "eval_results": [
        ("id", "bigint", True, "nextval('eval_results_id_seq'::regclass)"),
        ("run_id", "bigint", True, None),
        ("case_id", "bigint", False, None),
        ("question", "text", True, None),
        ("expected_answer", "text", True, None),
        ("actual_answer", "text", False, None),
        ("verdict", "character varying(32)", True, None),
        ("check_layer", "character varying(16)", False, None),
        ("score", "real", False, None),
        ("judge_reasoning", "text", False, None),
        ("duration_ms", "integer", False, None),
        ("error_message", "text", False, None),
        ("created_at", "timestamp with time zone", True, "now()"),
    ],
    "eval_case_reviews": [
        ("id", "bigint", True, "nextval('eval_case_reviews_id_seq'::regclass)"),
        ("case_id", "bigint", False, None),
        ("status", "character varying(32)", True, "'pending'::character varying"),
        ("reviewer", "character varying(64)", False, None),
        ("comment", "text", True, "''::text"),
        ("proposed_question", "text", False, None),
        ("proposed_answer", "text", False, None),
        ("category", "character varying(64)", False, None),
        ("created_at", "timestamp with time zone", True, "now()"),
        ("updated_at", "timestamp with time zone", True, "now()"),
    ],
}

EXPECTED_CONSTRAINTS = {
    (table, "p", "PRIMARY KEY (id)") for table in EXPECTED_COLUMNS
} | {
    (
        "messages",
        "c",
        "CHECK (role::text = ANY (ARRAY['user'::character varying, "
        "'assistant'::character varying]::text[]))",
    ),
    (
        "eval_results",
        "f",
        "FOREIGN KEY (run_id) REFERENCES eval_runs(id) ON DELETE CASCADE",
    ),
    (
        "eval_results",
        "f",
        "FOREIGN KEY (case_id) REFERENCES eval_cases(id) ON DELETE SET NULL",
    ),
    (
        "eval_case_reviews",
        "f",
        "FOREIGN KEY (case_id) REFERENCES eval_cases(id) ON DELETE CASCADE",
    ),
}

EXPECTED_INDEXES = {
    (table, f"{table}_pkey", f"CREATE UNIQUE INDEX {table}_pkey ON public.{table} USING btree (id)")
    for table in EXPECTED_COLUMNS
} | {
    (
        "messages",
        "idx_messages_chat_created",
        "CREATE INDEX idx_messages_chat_created ON public.messages USING btree (chat_id, created_at DESC)",
    ),
    (
        "token_usage",
        "idx_token_usage_chat_created",
        "CREATE INDEX idx_token_usage_chat_created ON public.token_usage USING btree (chat_id, created_at DESC)",
    ),
    (
        "prompt_versions",
        "idx_prompt_versions_created",
        "CREATE INDEX idx_prompt_versions_created ON public.prompt_versions USING btree (created_at DESC)",
    ),
    (
        "eval_results",
        "idx_eval_results_run",
        "CREATE INDEX idx_eval_results_run ON public.eval_results USING btree (run_id, id)",
    ),
    (
        "eval_case_reviews",
        "idx_eval_case_reviews_case_id",
        "CREATE UNIQUE INDEX idx_eval_case_reviews_case_id ON public.eval_case_reviews "
        "USING btree (case_id) WHERE (case_id IS NOT NULL)",
    ),
    (
        "eval_case_reviews",
        "idx_eval_case_reviews_status",
        "CREATE INDEX idx_eval_case_reviews_status ON public.eval_case_reviews "
        "USING btree (status, updated_at DESC)",
    ),
}


class SchemaMismatch(Exception):
    pass


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    return (
        f"postgresql://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@postgres:5432/"
        f"{os.environ['POSTGRES_DB']}"
    )


async def audit_schema() -> bool:
    conn = await asyncpg.connect(database_url())
    try:
        version_table = await conn.fetchval(
            "SELECT to_regclass('public.alembic_version')"
        )
        if version_table:
            versions = await conn.fetch("SELECT version_num FROM alembic_version")
            if [row["version_num"] for row in versions] == [REVISION]:
                return False
            raise SchemaMismatch("alembic_version is not the baseline revision")

        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        if tables != set(EXPECTED_COLUMNS):
            raise SchemaMismatch("table set mismatch")

        column_rows = await conn.fetch(
            """
            SELECT c.relname AS table_name, a.attname AS column_name,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expr
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
              AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
            """,
            list(EXPECTED_COLUMNS),
        )
        columns = {}
        for row in column_rows:
            columns.setdefault(row["table_name"], []).append(
                (
                    row["column_name"],
                    row["data_type"],
                    row["attnotnull"],
                    row["default_expr"],
                )
            )
        if columns != EXPECTED_COLUMNS:
            raise SchemaMismatch("column contract mismatch")

        constraints = {
            (
                row["table_name"],
                row["contype"].decode(),
                row["definition"],
            )
            for row in await conn.fetch(
                """
                SELECT c.relname AS table_name, con.contype,
                       pg_get_constraintdef(con.oid, true) AS definition
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
                """,
                list(EXPECTED_COLUMNS),
            )
        }
        if constraints != EXPECTED_CONSTRAINTS:
            raise SchemaMismatch("constraint contract mismatch")

        indexes = {
            (row["tablename"], row["indexname"], row["indexdef"])
            for row in await conn.fetch(
                """
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = ANY($1::text[])
                """,
                list(EXPECTED_COLUMNS),
            )
        }
        if indexes != EXPECTED_INDEXES:
            raise SchemaMismatch("index contract mismatch")
        return True
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        needs_stamp = asyncio.run(audit_schema())
        if not needs_stamp:
            print(f"Database already stamped at {REVISION}")
            return 0
        command.stamp(Config(args.config), REVISION)
        print(f"Schema audit passed; stamped {REVISION}")
        return 0
    except SchemaMismatch as exc:
        print(f"Schema audit failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Cutover failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
