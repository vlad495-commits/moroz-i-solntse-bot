# Production V1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать совместимый production-фундамент: общий пакет, Docker-тесты, управляемые миграции, RabbitMQ и отдельные worker/scheduler процессы без поломки текущего бота и админки.

**Architecture:** Существующие entrypoints остаются на месте, но получают общий пакет `project/src/moroz`. Docker build context поднимается до `project/`, чтобы все процессы использовали один код; таблицы переносятся из runtime DDL в Alembic.

**Tech Stack:** Python 3.12, asyncpg, Alembic, aio-pika, PostgreSQL 16, Redis 7, RabbitMQ, Docker Compose, pytest.

## Global Constraints

- Выполнять только через Docker Compose.
- Не переносить бизнес-сценарии в этой фазе.
- Переименовать Compose-сервис `llm` в `bot`, сохранив каталог/entrypoint `project/llm`, текущий URL админки и поведение обоих приложений.
- Runtime не выполняет `CREATE TABLE` или `ALTER TABLE`.
- Не добавлять ORM и SPA.

---

### Task 1: Docker test service и общий импортируемый пакет

**Files:**
- Create: `project/src/moroz/__init__.py`
- Create: `project/src/moroz/common/config.py`
- Create: `project/tests/unit/common/test_config.py`
- Create: `project/requirements-dev.txt`
- Create: `project/Dockerfile.test`
- Modify: `project/docker-compose.yml`
- Modify: `project/llm/Dockerfile`
- Modify: `project/admin/Dockerfile`

**Interfaces:**
- Produces: `Settings.from_env(env: Mapping[str, str]) -> Settings`
- Produces: Docker service `test` under profile `test`.

- [x] **Step 1: Write the failing config test**

```python
from moroz.common.config import Settings


def test_settings_build_database_url_from_postgres_parts():
    settings = Settings.from_env({
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "moroz",
    })
    assert settings.database_url == "postgresql://app:secret@postgres:5432/moroz"
    assert settings.rabbitmq_url == "amqp://guest:guest@rabbitmq:5672/"
```

- [x] **Step 2: Run test to verify red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/common/test_config.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'moroz'`.

- [x] **Step 3: Add minimal package, settings and test image**

```python
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    redis_url: str
    rabbitmq_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        database_url = env.get("DATABASE_URL", "")
        if not database_url:
            database_url = (
                f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
                f"@postgres:5432/{env['POSTGRES_DB']}"
            )
        return cls(
            database_url=database_url,
            redis_url=env.get("REDIS_URL", "redis://redis:6379/0"),
            rabbitmq_url=env.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/"),
        )
```

`Dockerfile.test` copies `src`, `tests`, both production requirements files and `requirements-dev.txt`; set `PYTHONPATH=/app/src:/app/llm:/app/admin`. Change production Docker build contexts to `.` with `dockerfile: llm/Dockerfile` and `admin/Dockerfile`; each Dockerfile copies its entrypoint directory plus `src/`. Rename the Compose service `llm` to `bot` without moving `project/llm` or changing the bot entrypoint.

- [x] **Step 4: Run config test and Compose validation**

Run: `docker compose --env-file ../.env --profile test build test && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/common/test_config.py -q && docker compose --env-file ../.env config --quiet`

Expected: `1 passed`; Compose exits 0.

- [x] **Step 5: Commit**

```bash
git add project/src project/tests project/requirements-dev.txt project/Dockerfile.test project/docker-compose.yml project/llm/Dockerfile project/admin/Dockerfile
git commit -m "build: добавлен общий пакет и Docker-тесты"
```

### Task 2: Alembic baseline вместо runtime DDL

**Files:**
- Create: `project/alembic.ini`
- Create: `project/migrations/env.py`
- Create: `project/migrations/versions/0001_existing_schema.py`
- Create: `project/tests/integration/conftest.py`
- Create: `project/tests/integration/test_migrations.py`
- Modify: `project/requirements-dev.txt`
- Modify: `project/llm/db.py:14-123`
- Modify: `project/admin/database.py:24-101`
- Modify: `project/docker-compose.yml`

**Interfaces:**
- Produces: service `migrate` running `alembic upgrade head`.
- Preserves: `init_db()` only creates asyncpg pool.

- [x] **Step 1: Write migration smoke test**

```python
import asyncpg


async def test_alembic_creates_existing_tables(migrated_database_url):
    conn = await asyncpg.connect(migrated_database_url)
    names = set(await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
    await conn.close()
    assert {"messages", "token_usage", "prompt_versions", "eval_cases", "eval_runs", "eval_results"} <= {r["tablename"] for r in names}
```

- [x] **Step 2: Verify red on a clean test database**

The integration fixture creates a uniquely named disposable PostgreSQL database, runs `alembic upgrade head` against its overridden `DATABASE_URL`, yields that URL, and always drops the database during cleanup.

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/test_migrations.py -q`

Expected: FAIL because Alembic configuration/revision does not exist.

- [x] **Step 3: Add baseline and remove DDL from startup**

```python
def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger()),
        sa.Column("username", sa.String(255)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("answered", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
```

Repeat explicit table definitions for the five existing tables and their indexes. `init_db()` in both apps must only call `asyncpg.create_pool`. Add `migrate` as an explicit one-shot Compose service; do not make application startup mutate schema.

Pin compatible `alembic`, `SQLAlchemy` and `pytest-asyncio` versions in `requirements-dev.txt`; SQLAlchemy is used only by Alembic migration infrastructure, not as an application ORM.

- [x] **Step 4: Run upgrade, downgrade on disposable DB, upgrade and tests**

Run: `docker compose --env-file ../.env --profile migration run --rm migrate && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/test_migrations.py -q`

Expected: the normal migration exits 0; the integration fixture independently performs upgrade on a disposable database, the test passes, and cleanup removes that database. Also verify downgrade/upgrade on another disposable database before committing.

- [x] **Step 5: Commit**

```bash
git add project/alembic.ini project/migrations project/tests/integration project/requirements-dev.txt project/llm/db.py project/admin/database.py project/docker-compose.yml
git commit -m "db: добавлен Alembic baseline"
```

### Task 3: Общий asyncpg pool и структурированный correlation_id

**Files:**
- Create: `project/src/moroz/common/db.py`
- Create: `project/src/moroz/common/observability.py`
- Create: `project/tests/unit/common/test_observability.py`
- Create: `project/tests/integration/test_database.py`
- Modify: `project/llm/db.py`
- Modify: `project/admin/database.py`

**Interfaces:**
- Produces: `Database(database_url)`, `Database.connect()`, `Database.close()`, `Database.acquire()`.
- Produces: `new_correlation_id() -> UUID` and `log_event(logger, event, correlation_id, **fields)`.

- [x] **Step 1: Write failing observability test**

```python
import json
from moroz.common.observability import event_payload, new_correlation_id


def test_event_payload_contains_stable_correlation_id():
    cid = new_correlation_id()
    payload = event_payload("message.accepted", cid, chat_id="42")
    assert json.loads(payload) == {
        "event": "message.accepted",
        "correlation_id": str(cid),
        "chat_id": "42",
    }
```

```python
from moroz.common.db import Database


async def test_database_connect_acquire_and_close(migrated_database_url):
    database = Database(migrated_database_url)
    await database.connect()
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT 1") == 1
    await database.close()
```

- [x] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/common/test_observability.py tests/integration/test_database.py -q`

Expected: FAIL because the shared observability/database modules are absent.

- [x] **Step 3: Implement minimal shared helpers**

```python
def event_payload(event: str, correlation_id: UUID, **fields: object) -> str:
    return json.dumps(
        {"event": event, "correlation_id": str(correlation_id), **fields},
        ensure_ascii=False,
        sort_keys=True,
    )
```

Wrap asyncpg pool creation in `Database`; keep existing query functions as compatibility wrappers using this shared pool. Do not refactor queries unrelated to the phase.

- [x] **Step 4: Run unit tests and safe bot/admin image smoke**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/common tests/integration/test_database.py -q && docker compose --env-file ../.env build bot admin && docker compose --env-file ../.env run --rm --no-deps bot python -m compileall -q /app && docker compose --env-file ../.env run --rm --no-deps admin python -m compileall -q /app && docker compose --env-file ../.env config --quiet`

Expected: tests pass; bot/admin images build and compile/import smoke succeeds without starting Telegram polling. Do not run a second bot instance against a token that may already be active on the test server.

- [x] **Step 5: Commit**

```bash
git add project/src/moroz/common project/tests/unit/common project/tests/integration/test_database.py project/llm/db.py project/admin/database.py
git commit -m "refactor: добавлены общие БД и observability helpers"
```

### Task 4: RabbitMQ, worker и scheduler skeleton

**Files:**
- Create: `project/src/moroz/common/queue.py`
- Create: `project/worker/main.py`
- Create: `project/worker/Dockerfile`
- Create: `project/scheduler/main.py`
- Create: `project/scheduler/Dockerfile`
- Create: `project/tests/integration/test_queue.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/llm/requirements.txt`

**Interfaces:**
- Produces: frozen/shallow-immutable `QueueTask(kind, payload, idempotency_key)` envelope with intentionally mutable JSON payload, JSON round-trip and `QueuePort.publish(task)`.
- Produces: `RabbitQueue.connect()`, `close()`, `publish(task)`, `consume_one(handler)` and long-running `consume(handler)` with manual ack.
- Uses: durable direct exchange/queue `tasks`, retry header `x-retry-count`, direct DLX `tasks.dlx` and queue `tasks.dlq` retained for 30 days.

- [x] **Step 1: Write failing round-trip test**

```python
async def test_queue_round_trip(rabbit_queue):
    received = []

    async def handle(task):
        received.append(task)

    await rabbit_queue.publish(QueueTask(kind="ping", payload={"value": 7}, idempotency_key="ping:7"))
    await rabbit_queue.consume_one(handle)
    assert received[0].payload == {"value": 7}
```

Add a second integration case: a handler that always raises is called once initially plus exactly three retries; after the third retry the persistent message is routed to `tasks.dlq` with the original `message_id`/idempotency key and is no longer present in `tasks`.

- [x] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/test_queue.py -q`

Expected: FAIL because RabbitMQ service and adapter are absent.

- [x] **Step 3: Add robust queue and containers**

```python
class RabbitQueue(QueuePort):
    async def publish(self, task: QueueTask) -> None:
        message = aio_pika.Message(
            body=task.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=task.idempotency_key,
            headers={"x-retry-count": 0},
        )
        await self.exchange.publish(message, routing_key="tasks")
```

`connect()` creates and reuses one robust connection/channel with publisher confirms, QoS 4, durable direct exchange `tasks`, durable queue `tasks`, durable direct exchange `tasks.dlx` and durable queue `tasks.dlq`; `close()` releases them. Bind `tasks` with routing key `tasks`; bind the DLQ with `tasks.dlq` and set its message TTL to `2592000000` ms (30 days).

On successful handler completion, ack manually. On failure, republish with incremented `x-retry-count` and ack the original only after publisher confirm; allow three retries after the initial delivery. After retry count 3, publish to the DLX preserving persistent delivery and `message_id`, then ack the original. If republish itself fails, do not silently lose the original message. Worker connects once and consumes continuously. Scheduler skeleton is a long-running process: it logs periodic heartbeat, handles graceful shutdown and stays healthy until stopped. Add a pinned RabbitMQ 4 management-alpine service with healthcheck plus worker/scheduler services.

- [x] **Step 4: Run queue test and container health**

Run: `docker compose --env-file ../.env up -d rabbitmq && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/test_queue.py -q && docker compose --env-file ../.env up -d worker scheduler && docker compose --env-file ../.env ps`

Expected: queue round-trip and retry/DLQ tests pass without a competing worker; then rabbitmq, worker and scheduler are running/healthy.

- [x] **Step 5: Commit**

```bash
git add project/src/moroz/common/queue.py project/worker project/scheduler project/tests/integration/test_queue.py project/docker-compose.yml project/llm/requirements.txt
git commit -m "feat: добавлены RabbitMQ worker и scheduler"
```

### Task 5: Foundation regression gate

**Files:**
- Create: `project/.dockerignore`
- Create: `project/migrate/Dockerfile`
- Create: `project/migrate/requirements.txt`
- Modify: `project/docker-compose.yml`
- Modify: `project/Dockerfile.test`
- Modify: `project/tests/integration/conftest.py`
- Modify: `project/tests/integration/test_migrations.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Produces: подтвержденный checkpoint «foundation complete».

- [x] **Step 1: Close deferred Foundation review notes test-first**

Add dedicated cutover regressions for an empty `alembic_version` table and an unexpected revision; both must fail closed without changing the version state. Harden the disposable database fixture so the admin connection closes in an outer `finally`, even if terminate/drop fails. Add `project/.dockerignore` so Docker never receives `.env*`, `data/`, `logs/`, `tmp/`, caches or bytecode. Replace the test-image/read-only-bind migration runtime with one immutable, non-root migration image shared by `migrate` and `cutover`. Remove the unused scheduler copy from `Dockerfile.test`.

- [x] **Step 2: Run the complete Docker suite**

Run: `docker compose --env-file ../.env --profile test build test && docker compose --env-file ../.env --profile test run --rm test pytest -q`

Expected: all tests pass.

- [x] **Step 3: Validate migration and safe Compose state**

Use an isolated `COMPOSE_PROJECT_NAME` plus shell-only generated PostgreSQL, Redis and RabbitMQ test credentials; never print or persist their values. Run normal/test/migration `docker compose --env-file ../.env config --quiet`, then `docker compose --env-file ../.env --profile migration run --rm migrate`. Build the bot image and run compile/import smoke without starting Telegram polling. Start only `postgres redis rabbitmq admin worker scheduler` and inspect `docker compose --env-file ../.env ps`.

Also run the sanitized Compose regression with `DATABASE_URL` absent/empty: PostgreSQL parts come from the external env file, while RabbitMQ credentials remain shell-only. `DATABASE_URL` stays the preferred override; Compose never constructs a database URI. Rendered profile allowlists are exact: `test` receives `DATABASE_URL`, the three PostgreSQL parts and `RABBITMQ_URL`; `migrate`/`cutover` receive only `DATABASE_URL` and the three PostgreSQL parts.

Expected: exit 0; the immutable migration image upgrades successfully through both URL override and PostgreSQL-parts fallback; admin/worker/scheduler and stores are healthy; bot image compiles/imports but no Telegram polling process starts. A live Telegram E2E remains a launch gate until a separate test token exists.

- [x] **Step 4: Inspect fresh logs and production boundaries**

Run: `docker compose --env-file ../.env logs --since=5m admin worker scheduler`

Expected: no traceback; schema is not created by runtime; scheduler heartbeat is present. Verify production images run as non-root, runtime DDL matches are zero, tracked `.env` files are zero, hardcoded credentials are zero, build contexts exclude sensitive/runtime data, and isolated containers/volumes/networks are removed after the gate.

- [x] **Step 5: Record result**

Mark foundation tasks complete in `Дорожная карта.md`; add commands and results to `changelog.md`.

- [x] **Step 6: Commit**

```bash
git add "Дорожная карта.md" changelog.md
git commit -m "docs: зафиксирован production foundation checkpoint"
```

### Task 6: Whole-branch review fixes

**Files:**
- Modify: `project/migrations/versions/0001_existing_schema.py`
- Modify: `project/migrations/audit_existing_schema.py`
- Modify: `project/migrations/env.py`
- Create: `project/migrations/database_url.py`
- Modify: `project/src/moroz/common/config.py`
- Modify: `project/llm/config.py`
- Modify: `project/admin/database.py`
- Modify: `project/src/moroz/common/queue.py`
- Modify: `project/worker/main.py`
- Modify: `project/llm/cache.py`
- Modify: `project/admin/eval_runner.py`
- Create: `project/worker/requirements.txt`
- Modify: `project/worker/Dockerfile`
- Modify: `project/scheduler/main.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/tests/ops/verify_compose_db_fallback.ps1`
- Create: `project/tests/unit/test_safe_logging.py`
- Modify: Foundation unit/integration/ops tests and operator documentation.

**Interfaces and safety contracts:**
- Baseline downgrade is fail-closed and cannot drop historical tables.
- Cutover audits the full catalog even when `alembic_version` is already at the baseline revision.
- PostgreSQL fallback DSNs percent-encode reserved characters in user/password/database parts; explicit `DATABASE_URL` remains unchanged and preferred.
- Worker, Redis and PostgreSQL receive only their required environment variables; worker depends only on aio-pika and reads only `RABBITMQ_URL`.
- Rabbit consumer tracks the actual aio-pika callback tasks, propagates fatal delivery errors, stops new deliveries before shutdown, drains in-flight work to a bounded timeout and gives cancellation its own one-second bound. A callback that ignores cancellation is detached safely; Docker `stop_grace_period: 30s` remains the final process-level bound. Worker readiness is published synchronously with the consumer lifecycle; scheduler health reflects a fresh heartbeat.
- Retry attempts use increasing delays before the first real producer/handler is introduced. Tests inject zero/fake delays; production defaults remain non-zero and increasing.
- Redis, primary/reserve LLM, judge, eval-case and eval-run failures never log connection/provider URLs, raw exception text or raw judge/question/answer content. Diagnostics are limited to fixed events, safe numeric IDs, exception type and content length; persisted eval `error_message` stores only the exception type.

- [x] **Step 1: Add meaningful RED regressions for all branch-review findings**

Cover destructive downgrade, stamped-schema drift, reserved-character PostgreSQL password, runtime env allowlists, worker dependency boundary, callback fatal propagation, bounded in-flight drain/cancel, consumer readiness, scheduler heartbeat freshness and increasing retry delays. Update the canonical gate regression to force a fresh test-image build.

- [x] **Step 2: Implement migration, DSN and least-privilege fixes**

Use only standard-library URL encoding in shared runtime code and a tiny migration-local helper so the migration image stays minimal. Make baseline downgrade raise without DDL. Audit both stamped and unstamped schemas before returning success/stamping. Remove runtime store/worker `env_file` entries and use explicit allowlists.

- [x] **Step 3: Implement queue supervision, drain, health and backoff**

Keep at-least-once semantics: stop intake, wait for actual in-flight callback tasks up to a bounded timeout, cancel remaining callbacks, wait at most one additional second and close the connection so unacked messages are redelivered. Never wait forever for a callback that suppresses cancellation; detach it with safe result retrieval and rely on the 30-second Docker stop grace as the final process bound. Any fatal callback error must end `consume()` and the worker process. Publish worker readiness synchronously after broker registration and remove it synchronously when consumer readiness clears; scheduler health must fail when its heartbeat becomes stale.

- [x] **Step 4: Close branch-review Minor notes**

Make the Docker gate build the test image, correct the first AGENTS command working directory, split the Alembic roadmap checkpoint, give worker a minimal requirements file, document `QueueTask` as a frozen envelope with shallow/mutable JSON payload semantics, and redact legacy Redis/judge error logs without adding a logging framework.

- [x] **Step 5: Run safe full regression gate and independent re-review**

Run focused RED/GREEN plus the complete Docker suite with an isolated Compose project, no bot polling, shell-only test credentials, migration/cutover checks, Rabbit callback/backoff tests, runtime health/graceful shutdown, image/env/dependency/scans and cleanup `0/0/0`. Then request whole-branch re-review from merge base before marking Foundation complete again.
