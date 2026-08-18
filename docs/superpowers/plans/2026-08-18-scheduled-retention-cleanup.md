# Scheduled Retention Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Восстановить ежедневное исполнение `DATA_RETENTION_DAYS` через существующий durable scheduler/worker-контур и атомарно удалять просроченные `messages` и `token_usage`.

**Architecture:** Один модуль `moroz.retention` владеет UTC-планированием и общим SQL-контрактом. Singleton worker ставит текущую daily job, scheduler публикует её обычным `scheduler_job`, а worker выполняет оба DELETE в одной транзакции и заранее обеспечивает следующую daily job. Legacy `llm/db.py::cleanup_old_records` делегирует тому же SQL-контракту.

**Tech Stack:** Python 3.12, asyncio, asyncpg, PostgreSQL, RabbitMQ, существующие `scheduler_jobs`/`SchedulerJobRepository`, pytest, Docker Compose.

## Global Constraints

- Проект запускается и тестируется только через Docker Compose.
- `DATA_RETENTION_DAYS` остаётся настраиваемым, default `1095`; значение `<= 0` отключает очистку.
- Автоматически удаляются только `messages` и `token_usage`.
- Не добавлять миграцию, контейнер, очередь, cron, dependency или provider-вызов.
- Job payload пустой; logs, alerts и `last_error_code` не содержат SQL, ПД, identifiers или message text.
- YCLIENTS, Redis, backups, inbox/outbox, consent, booking, scheduler, escalation и audit данные не изменяются.
- Каждый кодовый шаг выполняется TDD-циклом RED → GREEN и отдельным локальным commit; push запрещён без явного запроса.

---

### Task 1: Доменный retention-контракт и daily coordinator

**Files:**
- Create: `project/src/moroz/retention.py`
- Create: `project/tests/unit/test_retention.py`

**Interfaces:**
- Consumes: `PlannedSchedulerJob`, `SchedulerJob`, `JobResult`, `SchedulerJobRepository.schedule(...)`, `Database.acquire()`.
- Produces: `RETENTION_CLEANUP_KIND`, `RETENTION_ERROR_CODE`, `RetentionCleanupError`, `retention_job(now)`, `delete_expired_records(connection, retention_days)`, `RetentionCleanupCoordinator.ensure_current(now)`, `RetentionCleanupCoordinator.run(job)`.

- [x] **Step 1: Написать RED unit-контракт UTC bucket и disabled/next-job поведения**

Создать `project/tests/unit/test_retention.py`:

```python
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from moroz.notifications.models import JobResult
from moroz.retention import (
    RETENTION_CLEANUP_KIND,
    RetentionCleanupCoordinator,
    delete_expired_records,
    retention_job,
)


NOW = datetime(2026, 8, 18, 19, 22, tzinfo=UTC)


class Scheduler:
    def __init__(self):
        self.jobs = []

    async def schedule(self, job):
        self.jobs.append(job)
        return True


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Connection:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return Transaction()

    async def execute(self, query, retention_days):
        self.queries.append((query, retention_days))
        return "DELETE 2" if "messages" in query else "DELETE 3"


class Database:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def test_retention_job_uses_one_utc_day_bucket_without_payload():
    job = retention_job(NOW)

    assert job.kind == RETENTION_CLEANUP_KIND
    assert job.run_at == datetime(2026, 8, 18, tzinfo=UTC)
    assert job.payload == MappingProxyType({})
    assert job.idempotency_key == "retention_cleanup:2026-08-18"
    assert job.booking_key is None
    assert job.booking_starts_at is None


@pytest.mark.asyncio
async def test_positive_retention_schedules_current_and_next_day_and_cleans():
    scheduler = Scheduler()
    connection = Connection()
    coordinator = RetentionCleanupCoordinator(
        Database(connection), scheduler, retention_days=1095
    )

    await coordinator.ensure_current(NOW)
    result = await coordinator.run(retention_job(NOW))

    assert result == JobResult.sent()
    assert [job.idempotency_key for job in scheduler.jobs] == [
        "retention_cleanup:2026-08-18",
        "retention_cleanup:2026-08-19",
    ]
    assert [days for _, days in connection.queries] == [1095, 1095]


@pytest.mark.asyncio
async def test_disabled_retention_does_not_schedule_or_delete():
    scheduler = Scheduler()
    connection = Connection()
    coordinator = RetentionCleanupCoordinator(
        Database(connection), scheduler, retention_days=0
    )

    await coordinator.ensure_current(NOW)
    result = await coordinator.run(retention_job(NOW))

    assert result == JobResult.skipped("retention_disabled")
    assert scheduler.jobs == []
    assert connection.queries == []


@pytest.mark.asyncio
async def test_shared_delete_contract_returns_only_counts():
    connection = Connection()

    assert await delete_expired_records(connection, 1095) == {
        "messages": 2,
        "token_usage": 3,
    }
```

- [x] **Step 2: Запустить RED unit gate**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_retention.py
```

Expected: FAIL на `ModuleNotFoundError: No module named 'moroz.retention'`.

- [x] **Step 3: Реализовать минимальный `moroz.retention`**

Создать `project/src/moroz/retention.py`:

```python
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from moroz.notifications.models import JobResult, PlannedSchedulerJob


RETENTION_CLEANUP_KIND = "retention_cleanup"
RETENTION_ERROR_CODE = "retention_cleanup_failed"


class RetentionCleanupError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(RETENTION_ERROR_CODE)
        self.code = RETENTION_ERROR_CODE


def _delete_count(command_tag: str) -> int:
    parts = command_tag.split()
    if len(parts) != 2 or parts[0] != "DELETE" or not parts[1].isdigit():
        raise RetentionCleanupError()
    return int(parts[1])


def retention_job(now: datetime) -> PlannedSchedulerJob:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    bucket = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=RETENTION_CLEANUP_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{RETENTION_CLEANUP_KIND}:{bucket.date().isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


async def delete_expired_records(connection, retention_days: int) -> dict[str, int]:
    if retention_days <= 0:
        return {}
    messages = await connection.execute(
        "DELETE FROM messages "
        "WHERE created_at < now() - make_interval(days => $1)",
        retention_days,
    )
    token_usage = await connection.execute(
        "DELETE FROM token_usage "
        "WHERE created_at < now() - make_interval(days => $1)",
        retention_days,
    )
    return {
        "messages": _delete_count(messages),
        "token_usage": _delete_count(token_usage),
    }


class RetentionCleanupCoordinator:
    def __init__(self, database, scheduler, *, retention_days: int) -> None:
        self._database = database
        self._scheduler = scheduler
        self._retention_days = retention_days

    async def ensure_current(self, now: datetime) -> None:
        if self._retention_days > 0:
            await self._scheduler.schedule(retention_job(now))

    async def run(self, job) -> JobResult:
        if self._retention_days <= 0:
            return JobResult.skipped("retention_disabled")
        await self._scheduler.schedule(retention_job(job.run_at + timedelta(days=1)))
        try:
            async with self._database.acquire() as connection:
                async with connection.transaction():
                    await delete_expired_records(connection, self._retention_days)
        except RetentionCleanupError:
            raise
        except Exception as error:
            raise RetentionCleanupError() from error
        return JobResult.sent()
```

- [x] **Step 4: Запустить GREEN unit gate**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_retention.py
```

Expected: `4 passed`.

- [x] **Step 5: Зафиксировать Task 1**

```powershell
git add project/src/moroz/retention.py project/tests/unit/test_retention.py
git commit -m "feat: добавить daily retention coordinator"
```

---

### Task 2: PostgreSQL atomicity и единый legacy SQL-контракт

**Files:**
- Create: `project/tests/integration/test_retention_postgres.py`
- Modify: `project/llm/db.py:119`
- Test: `project/tests/unit/test_retention.py`

**Interfaces:**
- Consumes: `delete_expired_records(connection, retention_days)` и `RetentionCleanupCoordinator` из Task 1.
- Produces: доказанный PostgreSQL-контракт old/fresh/control/rollback и legacy delegation без второго набора DELETE.

- [x] **Step 1: Написать RED legacy-delegation contract и PostgreSQL integration tests**

В `project/tests/unit/test_retention.py` добавить:

```python
from pathlib import Path


def test_legacy_cleanup_delegates_to_shared_retention_contract():
    source = Path("/workspace/llm/db.py").read_text(encoding="utf-8")

    assert "from moroz.retention import delete_expired_records" in source
    assert "return await delete_expired_records(conn, DATA_RETENTION_DAYS)" in source
    assert 'f"DELETE FROM {table}' not in source
```

Создать `project/tests/integration/test_retention_postgres.py` с fixture `Database(migrated_database_url, min_size=1, max_size=1)` и двумя тестами:

```python
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.retention import RetentionCleanupCoordinator, RetentionCleanupError, retention_job


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 18, 19, 22, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


class Scheduler:
    async def schedule(self, _job):
        return True


async def _seed(connection, chat_id, created_at):
    await connection.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, created_at) "
        "VALUES ($1, $1, 'user', 'retention-test', $2)",
        chat_id,
        created_at,
    )
    await connection.execute(
        "INSERT INTO token_usage "
        "(chat_id, user_id, prompt_tokens, completion_tokens, cached_tokens, "
        "total_tokens, model, created_at) "
        "VALUES ($1, $1, 1, 1, 0, 2, 'retention-test', $2)",
        chat_id,
        created_at,
    )


async def test_cleanup_deletes_expired_and_preserves_fresh_rows(database):
    async with database.acquire() as connection:
        database_now = await connection.fetchval("SELECT now()")
        await _seed(connection, 8101, database_now - timedelta(days=1096))
        await _seed(connection, 8102, database_now - timedelta(days=1094))
        await connection.execute("SET timezone = 'UTC'")
        coordinator = RetentionCleanupCoordinator(
            database, Scheduler(), retention_days=1095
        )
        await coordinator.run(retention_job(NOW))
        rows = await connection.fetch(
            "SELECT chat_id, 'messages' AS source FROM messages "
            "WHERE chat_id IN (8101, 8102) UNION ALL "
            "SELECT chat_id, 'token_usage' AS source FROM token_usage "
            "WHERE chat_id IN (8101, 8102) ORDER BY source, chat_id"
        )

    assert [(row['chat_id'], row['source']) for row in rows] == [
        (8102, 'messages'),
        (8102, 'token_usage'),
    ]


async def test_second_delete_failure_rolls_back_first_delete(database):
    async with database.acquire() as connection:
        database_now = await connection.fetchval("SELECT now()")
        await _seed(connection, 8201, database_now - timedelta(days=1096))
        await connection.execute(
            "CREATE FUNCTION retention_test_fail() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced'; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER retention_test_fail BEFORE DELETE ON token_usage "
            "FOR EACH STATEMENT EXECUTE FUNCTION retention_test_fail()"
        )
    coordinator = RetentionCleanupCoordinator(database, Scheduler(), retention_days=1095)

    with pytest.raises(RetentionCleanupError, match='^retention_cleanup_failed$'):
        await coordinator.run(retention_job(NOW))

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM messages WHERE chat_id = 8201"
        ) == 1
```

- [x] **Step 2: Запустить RED integration gate**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_retention.py tests/integration/test_retention_postgres.py
```

Expected: PostgreSQL tests могут пройти, но общий Task 2 RED gate ниже падает на `test_legacy_cleanup_delegates_to_shared_retention_contract`, потому что `llm/db.py` ещё содержит собственный SQL-цикл.

- [x] **Step 3: Перевести legacy wrapper на общий DELETE-контракт**

В `project/llm/db.py` добавить:

```python
from moroz.retention import delete_expired_records
```

Тело database-ветки `cleanup_old_records` заменить на:

```python
    try:
        async with _pool.acquire() as conn:
            async with conn.transaction():
                return await delete_expired_records(conn, DATA_RETENTION_DAYS)
    except Exception as error:
        logger.error("db_cleanup_failed error_type=%s", type(error).__name__)
        return {}
```

Обновить docstring: удалить ссылку на отсутствующий `_cleanup_loop`; указать, что production schedule принадлежит `moroz.retention.RetentionCleanupCoordinator`.

- [x] **Step 4: Довести PostgreSQL тесты до GREEN и проверить общий контракт**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_retention.py tests/integration/test_retention_postgres.py
```

Expected: все тесты в двух файлах PASS; rollback test подтверждает сохранение `messages` после ошибки `token_usage`.

- [x] **Step 5: Зафиксировать Task 2**

```powershell
git add project/llm/db.py project/tests/integration/test_retention_postgres.py project/tests/unit/test_retention.py
git commit -m "test: доказать атомарную retention-очистку"
```

---

### Task 3: Scheduler handler и безопасный worker routing

**Files:**
- Modify: `project/src/moroz/notifications/handlers.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/e2e/notifications/test_reminders.py`
- Modify: `project/tests/unit/test_worker.py`

**Interfaces:**
- Consumes: `RETENTION_CLEANUP_KIND`, `RETENTION_ERROR_CODE`, `RetentionCleanupError`, coordinator из Task 1.
- Produces: routing `scheduler_job -> retention_cleanup.run(job)` без booking/YCLIENTS dependencies и allowlisted failure code.

- [x] **Step 1: Написать RED handler/worker tests**

В `project/tests/e2e/notifications/test_reminders.py` добавить imports и тест:

```python
from unittest.mock import AsyncMock

from moroz.retention import RETENTION_CLEANUP_KIND


async def test_retention_job_routes_without_booking_dependencies():
    job = SchedulerJob(
        id=uuid4(),
        kind=RETENTION_CLEANUP_KIND,
        run_at=datetime(2026, 8, 18, tzinfo=UTC),
        payload=MappingProxyType({}),
        idempotency_key="retention_cleanup:2026-08-18",
        attempts=0,
        booking_key=None,
        booking_starts_at=None,
    )
    retention = AsyncMock()
    retention.run.return_value = JobResult.sent()

    result = await handle_scheduler_job(
        job,
        booking_port=None,
        outbox=None,
        retention_cleanup=retention,
    )

    assert result == JobResult.sent()
    retention.run.assert_awaited_once_with(job)
```

В `project/tests/unit/test_worker.py` добавить отдельный тест:

```python
@pytest.mark.asyncio
async def test_retention_scheduler_job_needs_only_repository_and_coordinator():
    job_id = uuid4()
    completed = []
    job = SchedulerJob(
        id=job_id,
        kind=RETENTION_CLEANUP_KIND,
        run_at=datetime(2026, 8, 18, tzinfo=UTC),
        payload={},
        idempotency_key="retention_cleanup:2026-08-18",
        attempts=0,
        booking_key=None,
        booking_starts_at=None,
    )

    class SchedulerRepository:
        async def get_claimed(self, requested):
            assert requested == job_id
            return job

        async def complete(self, received, result):
            completed.append((received.id, result))

    retention = SimpleNamespace(run=AsyncMock(return_value=JobResult.sent()))
    handler = worker_main.MessageTaskHandler(
        object(),
        object(),
        object(),
        scheduler_repository=SchedulerRepository(),
        retention_cleanup=retention,
    )

    await handler.handle(
        QueueTask(
            kind="scheduler_job",
            payload={"job_id": str(job_id)},
            idempotency_key=f"scheduler_job:{job_id}",
        )
    )

    retention.run.assert_awaited_once_with(job)
    assert completed == [(job_id, JobResult.sent())]
```

Добавить failure test: `RetentionCleanupError()` приводит к
`record_failure(..., error_code="retention_cleanup_failed", terminal=False)`.

- [x] **Step 2: Запустить RED routing gate**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/e2e/notifications/test_reminders.py tests/unit/test_worker.py -k "retention"
```

Expected: FAIL из-за отсутствующих imports/parameter/routing branch.

- [x] **Step 3: Подключить retention к общему scheduler handler**

В `handle_scheduler_job(...)` добавить keyword `retention_cleanup=None` и первую ветку:

```python
    if job.kind == RETENTION_CLEANUP_KIND:
        if retention_cleanup is None:
            raise RuntimeError("retention cleanup is not configured")
        return await retention_cleanup.run(job)
```

Импортировать `RETENTION_CLEANUP_KIND` из `moroz.retention`.

- [x] **Step 4: Подключить системную ветку в `MessageTaskHandler`**

В constructor сохранить `retention_cleanup`; в набор job, не требующих booking
lookup/lock, добавить `RETENTION_CLEANUP_KIND`. Во все вызовы
`self._scheduler_handler(...)` передать `retention_cleanup=self._retention_cleanup`.

В `_scheduler_error_code` добавить до generic fallback:

```python
    if isinstance(error, RetentionCleanupError):
        return RETENTION_ERROR_CODE
```

Обновить существующие exact kwargs assertions в `test_worker.py`, добавив
`"retention_cleanup": None`.

- [x] **Step 5: Запустить GREEN и соседний scheduler regression**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/e2e/notifications/test_reminders.py tests/unit/test_worker.py tests/integration/notifications/test_jobs.py
```

Expected: PASS без изменения booking/projection/catalog поведения.

- [x] **Step 6: Зафиксировать Task 3**

```powershell
git add project/src/moroz/notifications/handlers.py project/worker/main.py project/tests/e2e/notifications/test_reminders.py project/tests/unit/test_worker.py
git commit -m "feat: маршрутизировать retention через worker"
```

---

### Task 4: Worker startup и Compose allowlist

**Files:**
- Modify: `project/worker/main.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `project/tests/unit/test_migration_profile.py`

**Interfaces:**
- Consumes: `RetentionCleanupCoordinator(database, scheduler, retention_days=...)`.
- Produces: startup guarantee текущей UTC job и явная передача `DATA_RETENTION_DAYS` только bot/worker runtime.

- [ ] **Step 1: Написать RED startup/config tests**

В `test_worker.py` расширить startup-order test fake coordinator-ом, который
фиксирует `retention_ensure_current` до `queue_connect`/`supervise`.

В `test_migration_profile.py` добавить к exact worker environment:

```python
"DATA_RETENTION_DAYS": "${DATA_RETENTION_DAYS:-1095}",
```

И отдельный allowlist assertion:

```python
def test_retention_setting_is_limited_to_runtime_owners():
    services = compose_services()

    assert "DATA_RETENTION_DAYS" in services["bot"]["environment"]
    assert "DATA_RETENTION_DAYS" in services["worker"]["environment"]
    for name in ("test", "migrate", "cutover", "scheduler", "admin"):
        assert "DATA_RETENTION_DAYS" not in services[name].get("environment", {})
```

- [ ] **Step 2: Запустить RED startup/config gate**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_worker.py tests/unit/test_migration_profile.py -k "retention or configured_worker"
```

Expected: FAIL, потому что worker ещё не получает setting и не обеспечивает job.

- [ ] **Step 3: Создать coordinator при старте singleton worker**

В `project/worker/main.py` импортировать `DATA_RETENTION_DAYS` из текущего
`config.py`, а также `RetentionCleanupCoordinator`. После database connect и
singleton lock создать coordinator с существующим `SchedulerJobRepository`:

```python
        scheduler_repository = SchedulerJobRepository(database)
        retention_cleanup = RetentionCleanupCoordinator(
            database,
            scheduler_repository,
            retention_days=DATA_RETENTION_DAYS,
        )
        await retention_cleanup.ensure_current(datetime.now(UTC))
```

Передать тот же `scheduler_repository` и `retention_cleanup` в
`MessageTaskHandler`. Не создавать второй repository внутри этого runtime graph.

- [ ] **Step 4: Добавить Compose allowlist**

В `worker.environment` базового `project/docker-compose.yml` добавить:

```yaml
DATA_RETENTION_DAYS: ${DATA_RETENTION_DAYS:-1095}
```

Scheduler, test, migrate, cutover и admin не изменять.

- [ ] **Step 5: Запустить GREEN startup/config gate и config render**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_worker.py tests/unit/test_migration_profile.py -k "retention or configured_worker"
docker compose --env-file ../.env config --quiet
```

Expected: pytest PASS; Compose config exit `0`.

- [ ] **Step 6: Зафиксировать Task 4**

```powershell
git add project/worker/main.py project/docker-compose.yml project/tests/unit/test_worker.py project/tests/unit/test_migration_profile.py
git commit -m "feat: запускать retention ежедневно"
```

---

### Task 5: Полная проверка, документация и закрытие checkpoint

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Verify: `docs/superpowers/specs/2026-08-18-scheduled-retention-cleanup-design.md`
- Verify: all files changed in Tasks 1–4.

**Interfaces:**
- Consumes: завершённые Tasks 1–4 и фактический вывод Docker gates.
- Produces: проверенную локальную поставку и честный roadmap status без staging/production claims.

- [ ] **Step 1: Запустить focused retention gate**

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_retention.py tests/integration/test_retention_postgres.py tests/e2e/notifications/test_reminders.py tests/unit/test_worker.py tests/integration/notifications/test_jobs.py tests/unit/test_migration_profile.py
```

Expected: все собранные тесты PASS, failures `0`.

- [ ] **Step 2: Запустить privacy/scheduler regression**

```powershell
cd project
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/e2e/notifications tests/integration/notifications
```

Expected: PASS, failures `0`; customer deletion и scheduler jobs не регрессировали.

- [ ] **Step 3: Запустить полный Docker suite**

```powershell
cd project
docker compose --env-file ../.env run --rm test pytest -q
```

Expected: exit `0`, failures `0`, skips перечислить явно, если они есть.

- [ ] **Step 4: Запустить compile/config/diff gates**

```powershell
cd project
docker compose --env-file ../.env run --rm test python -m compileall -q /workspace
docker compose --env-file ../.env config --quiet
cd ..
git diff --check
git status --short
```

Expected: первые три команды exit `0`; status содержит только ожидаемые файлы retention-поставки до commit.

- [ ] **Step 5: Обновить живые документы только фактическими результатами**

В `Дорожная карта.md` отметить parent retention task и implementation subtask
`[x]`, записать точные focused/regression/full counts и явно указать, что
staging/production не изменялись.

В `changelog.md` добавить строку UTC+3 с реализованным потоком, точными Docker
результатами, compile/config/diff gates и отсутствием внешних действий.

- [ ] **Step 6: Проверить staged scope и зафиксировать завершение**

```powershell
git add project/src/moroz/retention.py project/llm/db.py project/src/moroz/notifications/handlers.py project/worker/main.py project/docker-compose.yml project/tests/unit/test_retention.py project/tests/integration/test_retention_postgres.py project/tests/e2e/notifications/test_reminders.py project/tests/unit/test_worker.py project/tests/unit/test_migration_profile.py 'Дорожная карта.md' changelog.md
git diff --cached --check
git diff --cached --stat
git commit -m "feat: завершить плановую retention-очистку"
git status --short
```

Expected: commit создан; финальный `git status --short` пуст либо показывает
только заранее существовавшие пользовательские изменения, перечисленные перед
началом реализации.

---

## Self-Review Mapping

- Default `1095` / disabled `<=0`: Tasks 1 and 4.
- Один daily UTC job и restart idempotency: Tasks 1 and 4.
- Только `messages`/`token_usage`: Tasks 1 and 2.
- PostgreSQL atomicity: Task 2.
- Existing scheduler/RabbitMQ/worker pipeline: Tasks 3 and 4.
- Safe fixed error code and empty payload: Tasks 1 and 3.
- No new service/migration/dependency: enforced by all tasks and final staged scope.
- Legacy SQL deduplication: Task 2.
- Focused, privacy/scheduler regression, full suite and docs evidence: Task 5.
