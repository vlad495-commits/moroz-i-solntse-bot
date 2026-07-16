import os
import subprocess

import asyncpg
import pytest

from conftest import RedactedDatabaseURL, disposable_database_url as database_fixture


pytestmark = pytest.mark.asyncio

CONFIG = "/workspace/alembic.ini"
CUTOVER = "/workspace/migrations/audit_existing_schema.py"

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


def run_alembic(database_url, *args):
    subprocess.run(
        ["alembic", "-c", CONFIG, *args],
        check=True,
        env={**os.environ, "DATABASE_URL": database_url},
    )


def run_alembic_result(database_url, *args):
    return subprocess.run(
        ["alembic", "-c", CONFIG, *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
    )


def run_cutover(database_url):
    return subprocess.run(
        ["python", CUTOVER, "--config", CONFIG],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
    )


async def make_unversioned_schema(database_url):
    run_alembic(database_url, "upgrade", "0001_existing_schema")
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("DROP TABLE alembic_version")
    finally:
        await conn.close()


async def snapshot_application_catalog(conn):
    tables = tuple(
        sorted(
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        )
    )
    columns = tuple(
        tuple(row.values())
        for row in await conn.fetch(
            """
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
            """
        )
    )
    constraints = tuple(
        tuple(row.values())
        for row in await conn.fetch(
            """
            SELECT c.relname, con.contype,
                   pg_get_constraintdef(con.oid, true)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
            ORDER BY c.relname, con.contype, con.conname
            """
        )
    )
    indexes = tuple(
        tuple(row.values())
        for row in await conn.fetch(
            """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """
        )
    )
    return tables, columns, constraints, indexes


async def test_database_url_repr_is_redacted():
    value = RedactedDatabaseURL("sensitive-value")

    assert repr(value) == "'<redacted-database-url>'"
    assert "sensitive-value" not in repr(value)


async def run_failing_database_fixture(monkeypatch, failures):
    errors = {
        name: error for name, error in failures.items()
    }

    class FailingAdmin:
        close_calls = 0
        operations = []

        async def execute(self, statement, *args):
            operation = (
                "create" if statement.startswith("CREATE DATABASE")
                else "terminate" if statement.startswith("SELECT pg_terminate_backend")
                else "drop" if statement.startswith("DROP DATABASE")
                else "other"
            )
            self.operations.append(operation)
            if operation in errors:
                raise errors[operation]

        async def close(self):
            self.close_calls += 1

    admin = FailingAdmin()

    async def connect(_url):
        return admin

    monkeypatch.setattr("conftest.asyncpg.connect", connect)
    fixture = database_fixture.__wrapped__()
    try:
        if "create" in errors:
            await anext(fixture)
        await anext(fixture)
        await fixture.aclose()
    except Exception as exc:
        return admin, exc
    raise AssertionError("fixture did not propagate the configured failure")


@pytest.mark.parametrize("operation", ["create", "drop"])
async def test_disposable_database_preserves_database_operation_error(
    monkeypatch, operation
):
    expected = ValueError(f"{operation} failed")

    admin, raised = await run_failing_database_fixture(
        monkeypatch, {operation: expected}
    )

    assert raised is expected
    assert type(raised) is ValueError
    assert str(raised) == f"{operation} failed"
    assert admin.close_calls == 1


@pytest.mark.parametrize("drop_also_fails", [False, True])
async def test_disposable_database_attempts_drop_but_preserves_terminate_error(
    monkeypatch, drop_also_fails
):
    terminate_error = RuntimeError("terminate failed")
    failures = {"terminate": terminate_error}
    if drop_also_fails:
        failures["drop"] = ValueError("drop failed")

    admin, raised = await run_failing_database_fixture(monkeypatch, failures)

    assert admin.operations == ["create", "terminate", "drop"]
    assert raised is terminate_error
    assert type(raised) is RuntimeError
    assert str(raised) == "terminate failed"
    assert admin.close_calls == 1


async def test_alembic_creates_exact_existing_schema(baseline_database_url):
    conn = await asyncpg.connect(baseline_database_url)
    try:
        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
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
    finally:
        await conn.close()

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

    assert tables == {*EXPECTED_COLUMNS, "alembic_version"}
    assert columns == EXPECTED_COLUMNS
    assert constraints == EXPECTED_CONSTRAINTS
    assert indexes == EXPECTED_INDEXES


async def test_messaging_migration_downgrade_preserves_baseline_schema(
    disposable_database_url,
):
    run_alembic(disposable_database_url, "upgrade", "0001_existing_schema")
    conn = await asyncpg.connect(disposable_database_url)
    try:
        baseline_catalog = await snapshot_application_catalog(conn)
    finally:
        await conn.close()

    new_tables = {
        "message_inbox",
        "outbound_messages",
        "processing_consents",
        "task_outbox",
    }
    try:
        run_alembic(disposable_database_url, "upgrade", "head")
        conn = await asyncpg.connect(disposable_database_url)
        try:
            head_catalog = await snapshot_application_catalog(conn)
        finally:
            await conn.close()

        assert set(head_catalog[0]) - set(baseline_catalog[0]) == new_tables

        run_alembic(
            disposable_database_url,
            "downgrade",
            "0001_existing_schema",
        )
        conn = await asyncpg.connect(disposable_database_url)
        try:
            downgraded_catalog = await snapshot_application_catalog(conn)
        finally:
            await conn.close()

        assert downgraded_catalog == baseline_catalog
        assert new_tables.isdisjoint(downgraded_catalog[0])
    finally:
        run_alembic(disposable_database_url, "upgrade", "head")

    conn = await asyncpg.connect(disposable_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == (
            "0004_pipeline_order_claim"
        )
    finally:
        await conn.close()


async def test_pipeline_order_migration_backfills_and_downgrades_cleanly(
    disposable_database_url,
):
    run_alembic(disposable_database_url, "upgrade", "0003_processing_consents")
    conn = await asyncpg.connect(disposable_database_url)
    try:
        await conn.execute(
            """
            INSERT INTO message_inbox
                (id, channel, external_message_id, chat_id, payload,
                 correlation_id)
            VALUES
                (gen_random_uuid(), 'telegram', 'old-1', '42', '{}',
                 gen_random_uuid()),
                (gen_random_uuid(), 'telegram', 'old-2', '42', '{}',
                 gen_random_uuid())
            """
        )
    finally:
        await conn.close()

    run_alembic(disposable_database_url, "upgrade", "head")
    conn = await asyncpg.connect(disposable_database_url)
    try:
        rows = await conn.fetch(
            "SELECT external_message_id, ingress_sequence "
            "FROM message_inbox ORDER BY ingress_sequence"
        )
        assert {row["external_message_id"] for row in rows} == {"old-1", "old-2"}
        assert rows[0]["ingress_sequence"] < rows[1]["ingress_sequence"]
        new_sequence = await conn.fetchval(
            """
            INSERT INTO message_inbox
                (id, channel, external_message_id, chat_id, payload,
                 correlation_id)
            VALUES
                (gen_random_uuid(), 'telegram', 'new-3', '42', '{}',
                 gen_random_uuid())
            RETURNING ingress_sequence
            """
        )
        assert new_sequence > rows[1]["ingress_sequence"]
        assert await conn.fetchval(
            """
            SELECT is_nullable = 'NO'
            FROM information_schema.columns
            WHERE table_name = 'message_inbox'
              AND column_name = 'ingress_sequence'
            """
        )
        assert await conn.fetchval(
            """
            SELECT count(*)
            FROM information_schema.columns
            WHERE table_name = 'outbound_messages'
              AND column_name = 'claimed_at'
            """
        ) == 1
    finally:
        await conn.close()

    run_alembic(disposable_database_url, "downgrade", "0003_processing_consents")
    conn = await asyncpg.connect(disposable_database_url)
    try:
        assert await conn.fetchval(
            """
            SELECT count(*)
            FROM information_schema.columns
            WHERE (table_name, column_name) IN (
                ('message_inbox', 'ingress_sequence'),
                ('outbound_messages', 'claimed_at')
            )
            """
        ) == 0
        assert await conn.fetchval("SELECT count(*) FROM message_inbox") == 3
    finally:
        await conn.close()

    run_alembic(disposable_database_url, "upgrade", "head")


async def test_cutover_audits_and_stamps_exact_unversioned_schema(
    disposable_database_url,
):
    await make_unversioned_schema(disposable_database_url)

    result = run_cutover(disposable_database_url)

    assert result.returncode == 0, result.stdout + result.stderr
    conn = await asyncpg.connect(disposable_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0001_existing_schema"
    finally:
        await conn.close()


async def test_cutover_is_idempotent_for_versioned_baseline(baseline_database_url):
    result = run_cutover(baseline_database_url)
    assert result.returncode == 0, result.stdout + result.stderr


async def test_baseline_downgrade_is_rejected_without_changing_schema_or_data(
    baseline_database_url,
):
    conn = await asyncpg.connect(baseline_database_url)
    try:
        message_id = await conn.fetchval(
            """
            INSERT INTO messages (chat_id, role, content)
            VALUES (7, 'user', 'preserve me')
            RETURNING id
            """
        )
        case_id = await conn.fetchval(
            """
            INSERT INTO eval_cases (question, expected_answer)
            VALUES ('preserve question', 'preserve answer')
            RETURNING id
            """
        )
        run_id = await conn.fetchval("INSERT INTO eval_runs DEFAULT VALUES RETURNING id")
        result_id = await conn.fetchval(
            """
            INSERT INTO eval_results
                (run_id, case_id, question, expected_answer, verdict)
            VALUES ($1, $2, 'preserve question', 'preserve answer', 'passed')
            RETURNING id
            """,
            run_id,
            case_id,
        )
        review_id = await conn.fetchval(
            "INSERT INTO eval_case_reviews (case_id) VALUES ($1) RETURNING id",
            case_id,
        )
        catalog_before = await snapshot_application_catalog(conn)
    finally:
        await conn.close()

    result = run_alembic_result(baseline_database_url, "downgrade", "base")

    assert result.returncode != 0
    conn = await asyncpg.connect(baseline_database_url)
    try:
        catalog_after = await snapshot_application_catalog(conn)
        assert catalog_after == catalog_before
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == (
            "0001_existing_schema"
        )
        assert await conn.fetchval(
            "SELECT content FROM messages WHERE id = $1", message_id
        ) == "preserve me"
        assert tuple(
            await conn.fetchrow(
                """
                SELECT er.id, er.run_id, er.case_id, ecr.id, ecr.case_id
                FROM eval_results er
                JOIN eval_case_reviews ecr ON ecr.case_id = er.case_id
                WHERE er.id = $1 AND ecr.id = $2
                """,
                result_id,
                review_id,
            )
        ) == (result_id, run_id, case_id, review_id, case_id)
    finally:
        await conn.close()


@pytest.mark.parametrize("drift", ["column", "index", "constraint"])
async def test_cutover_rejects_drift_in_already_stamped_schema(
    baseline_database_url, drift
):
    conn = await asyncpg.connect(baseline_database_url)
    try:
        if drift == "column":
            await conn.execute(
                "ALTER TABLE messages ALTER COLUMN username TYPE VARCHAR(128)"
            )
        elif drift == "index":
            await conn.execute("DROP INDEX idx_token_usage_chat_created")
        else:
            await conn.execute("ALTER TABLE messages DROP CONSTRAINT messages_role_check")
    finally:
        await conn.close()

    result = run_cutover(baseline_database_url)

    assert result.returncode != 0
    assert "Schema audit failed" in result.stderr
    conn = await asyncpg.connect(baseline_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == (
            "0001_existing_schema"
        )
    finally:
        await conn.close()


@pytest.mark.parametrize("version_state", ["empty", "unexpected"])
async def test_cutover_fails_closed_and_preserves_invalid_version_state(
    migrated_database_url, version_state
):
    conn = await asyncpg.connect(migrated_database_url)
    try:
        if version_state == "empty":
            await conn.execute("DELETE FROM alembic_version")
            expected = []
        else:
            await conn.execute(
                "UPDATE alembic_version SET version_num = 'unexpected_revision'"
            )
            expected = ["unexpected_revision"]
    finally:
        await conn.close()

    result = run_cutover(migrated_database_url)

    assert result.returncode != 0
    assert "Schema audit failed" in result.stderr
    conn = await asyncpg.connect(migrated_database_url)
    try:
        assert [
            row["version_num"]
            for row in await conn.fetch("SELECT version_num FROM alembic_version")
        ] == expected
    finally:
        await conn.close()


@pytest.mark.parametrize("schema_state", ["fresh", "partial", "mismatch"])
async def test_cutover_fails_closed_without_stamping(
    disposable_database_url, schema_state
):
    if schema_state != "fresh":
        await make_unversioned_schema(disposable_database_url)
        conn = await asyncpg.connect(disposable_database_url)
        try:
            if schema_state == "partial":
                await conn.execute("DROP TABLE token_usage")
            else:
                await conn.execute(
                    "ALTER TABLE messages ALTER COLUMN username TYPE VARCHAR(128)"
                )
        finally:
            await conn.close()

    result = run_cutover(disposable_database_url)

    assert result.returncode != 0
    assert "Schema audit failed" in result.stderr
    conn = await asyncpg.connect(disposable_database_url)
    try:
        assert await conn.fetchval("SELECT to_regclass('public.alembic_version')") is None
    finally:
        await conn.close()
