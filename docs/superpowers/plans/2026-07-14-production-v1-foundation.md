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

Run: `docker compose --profile test run --rm test pytest tests/unit/common/test_config.py -q`

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

Run: `docker compose --profile test build test && docker compose --profile test run --rm test pytest tests/unit/common/test_config.py -q && docker compose config --quiet`

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

Run: `docker compose --profile test run --rm test pytest tests/integration/test_migrations.py -q`

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

Run: `docker compose run --rm migrate && docker compose --profile test run --rm test pytest tests/integration/test_migrations.py -q`

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

- [ ] **Step 1: Write failing observability test**

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

- [ ] **Step 2: Run red**

Run: `docker compose --profile test run --rm test pytest tests/unit/common/test_observability.py tests/integration/test_database.py -q`

Expected: FAIL because the shared observability/database modules are absent.

- [ ] **Step 3: Implement minimal shared helpers**

```python
def event_payload(event: str, correlation_id: UUID, **fields: object) -> str:
    return json.dumps(
        {"event": event, "correlation_id": str(correlation_id), **fields},
        ensure_ascii=False,
        sort_keys=True,
    )
```

Wrap asyncpg pool creation in `Database`; keep existing query functions as compatibility wrappers using this shared pool. Do not refactor queries unrelated to the phase.

- [ ] **Step 4: Run unit tests and safe bot/admin image smoke**

Run: `docker compose --profile test run --rm test pytest tests/unit/common tests/integration/test_database.py -q && docker compose build bot admin && docker compose run --rm --no-deps bot python -m compileall -q /app && docker compose run --rm --no-deps admin python -m compileall -q /app && docker compose config --quiet`

Expected: tests pass; bot/admin images build and compile/import smoke succeeds without starting Telegram polling. Do not run a second bot instance against a token that may already be active on the test server.

- [ ] **Step 5: Commit**

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
- Produces: `RabbitQueue(QueuePort).publish(task: QueueTask) -> None`.
- Produces: `consume(handler: Callable[[QueueTask], Awaitable[None]]) -> None` with manual ack.

- [ ] **Step 1: Write failing round-trip test**

```python
async def test_queue_round_trip(rabbit_queue):
    received = []
    await rabbit_queue.publish(QueueTask(kind="ping", payload={"value": 7}, idempotency_key="ping:7"))
    await rabbit_queue.consume_one(lambda task: received.append(task))
    assert received[0].payload == {"value": 7}
```

- [ ] **Step 2: Run red**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_queue.py -q`

Expected: FAIL because RabbitMQ service and adapter are absent.

- [ ] **Step 3: Add robust queue and containers**

```python
class RabbitQueue(QueuePort):
    async def publish(self, task: QueueTask) -> None:
        connection = await aio_pika.connect_robust(self.settings.rabbitmq_url)
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=4)
        message = aio_pika.Message(
            body=task.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=task.idempotency_key,
        )
        await channel.default_exchange.publish(message, routing_key="tasks")
```

Declare durable `tasks` exchange/queue and DLQ. Worker consumes with `message.process(requeue=False)` only around a successful handler; failures follow bounded retry headers then DLQ. Scheduler skeleton is a long-running process: it logs periodic heartbeat, handles graceful shutdown and stays healthy until stopped. Add RabbitMQ healthcheck and worker/scheduler services.

- [ ] **Step 4: Run queue test and container health**

Run: `docker compose up -d rabbitmq worker scheduler && docker compose --profile test run --rm test pytest tests/integration/test_queue.py -q && docker compose ps`

Expected: test passes; rabbitmq, worker, scheduler are running/healthy.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/common/queue.py project/worker project/scheduler project/tests/integration/test_queue.py project/docker-compose.yml project/llm/requirements.txt
git commit -m "feat: добавлены RabbitMQ worker и scheduler"
```

### Task 5: Foundation regression gate

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Produces: подтвержденный checkpoint «foundation complete».

- [ ] **Step 1: Run the complete Docker suite**

Run: `docker compose --profile test run --rm test pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Validate migration and Compose state**

Run: `docker compose run --rm migrate && docker compose config --quiet && docker compose up -d --build && docker compose ps`

Expected: exit 0; bot/admin/worker/scheduler and stores are running.

- [ ] **Step 3: Inspect fresh logs**

Run: `docker compose logs --since=5m bot admin worker scheduler`

Expected: no traceback; schema is not created by runtime.

- [ ] **Step 4: Record result**

Mark foundation tasks complete in `Дорожная карта.md`; add commands and results to `changelog.md`.

- [ ] **Step 5: Commit**

```bash
git add "Дорожная карта.md" changelog.md
git commit -m "docs: зафиксирован production foundation checkpoint"
```
