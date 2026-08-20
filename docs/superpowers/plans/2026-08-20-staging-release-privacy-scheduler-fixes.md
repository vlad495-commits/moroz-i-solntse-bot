# Staging Release Privacy and Scheduler Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть privacy-блокеры draft PR №2 и сделать staging scheduler реально исполняемым и commit-pinned до merge и rollout.

**Architecture:** Customer deletion создаёт долговечный tombstone только по opaque YCLIENTS `external_id`, очищает локальную проекцию в той же транзакции, а projection replace фильтрует tombstones. Telegram sender повторно валидирует claimed outbound под тем же PostgreSQL customer advisory lock, который использует deletion, и удерживает lock на время provider call. Staging Compose и runbook включают отдельный immutable scheduler image с проверяемыми first-enable rollback semantics.

**Tech Stack:** Python 3.12, asyncpg, Alembic/SQLAlchemy, aiogram, PostgreSQL advisory locks, Docker Compose, pytest, Ruff 0.12.7.

## Global Constraints

- Любые YCLIENTS create/reschedule/cancel/delete и другие mutations запрещены; разрешены только существующие GET-only readers.
- Не выводить и не сохранять секреты, response bodies или персональные данные.
- Все Python-команды и тесты выполняются только через Docker Compose.
- Suppression хранит только `external_id text PRIMARY KEY` и `created_at timestamptz NOT NULL DEFAULT now()`.
- Customer deletion, suppression insert, projection delete и remnant checks выполняются в одной PostgreSQL-транзакции.
- Pre-send fence использует `pg_advisory_xact_lock(hashtextextended(customer_lock_subject(chat_id), 0))` и удерживает lock до завершения одного `telegram.send_message`.
- Scheduler image: `moroz-staging-scheduler:${STAGING_IMAGE_TAG}`; scheduler не получает Telegram, LLM или YCLIENTS credentials.
- Production, merge PR и staging rollout запрещены до полного Docker gate и повторного review `0 Critical / 0 Important`.
- Временные файлы создаются только в корневом `tmp/`; каждый завершённый логический шаг отражается в `Дорожная карта.md`, `changelog.md` и отдельном commit.

---

## File Map

- Create `project/migrations/versions/0012_yclients_projection_suppression.py`: additive suppression schema.
- Create `project/tests/unit/admin/test_migration_0012.py`: exact revision/table contract.
- Modify `project/tests/integration/test_migrations.py`: migration head and forward-schema compatibility.
- Modify `project/admin/customer_data_deletion.py`: collect external IDs, create tombstones, delete/remnant-check projection before owned booking rows disappear.
- Modify `project/src/moroz/booking/projection.py`: omit suppressed external IDs atomically during full replacement.
- Modify `project/tests/integration/admin/test_customer_data_deletion_postgres.py`: deletion, rollback and deletion→sync coverage.
- Modify `project/tests/integration/booking/test_yclients_projection.py`: suppressed/unrelated/lookup-failure replacement coverage.
- Modify `project/src/moroz/messaging/repository.py`: narrow async context manager for durable pre-send revalidation under customer lock.
- Modify `project/src/moroz/messaging/telegram.py`: construct/send only the durably revalidated row while the fence is held.
- Modify `project/tests/e2e/test_message_delivery.py`: both deletion/send race orders and existing delivery regressions.
- Modify `project/docker-compose.staging.yml`: enable and pin scheduler image without expanding its environment.
- Modify `project/ops/staging-runbook.md`: scheduler build, manifests, rollout, smoke and first-enable rollback.
- Modify `project/tests/unit/test_staging.py`: executable Compose/runbook contracts for scheduler image, env and rollback paths.
- Modify `project/src/moroz/notifications/handlers.py`: terminal no-op handler for the staging-only synthetic scheduler smoke.
- Modify `project/worker/main.py`: serialize the synthetic smoke with other system jobs.
- Modify `project/tests/unit/test_worker.py`: prove the synthetic job never reaches booking, notification or YCLIENTS dependencies.
- Modify `Дорожная карта.md` and `changelog.md`: live release-gate status and verification evidence.

---

### Task 1: Add the durable suppression schema

**Files:**
- Create: `project/migrations/versions/0012_yclients_projection_suppression.py`
- Create: `project/tests/unit/admin/test_migration_0012.py`
- Modify: `project/tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: Alembic head `0011_yclients_service_catalog`.
- Produces: PostgreSQL table `yclients_projection_suppressions(external_id text primary key, created_at timestamptz not null default now())` and revision `0012_yclients_projection_suppression`.

- [ ] **Step 1: Write the failing unit contract**

```python
def test_suppression_migration_contract():
    migration = load_migration()
    assert migration.revision == "0012_yclients_projection_suppression"
    assert migration.down_revision == "0011_yclients_service_catalog"
    source = MIGRATION.read_text(encoding="utf-8")
    assert '"yclients_projection_suppressions"' in source
    assert 'sa.Column("external_id", sa.Text(), primary_key=True)' in source
    assert 'sa.Column("created_at", sa.DateTime(timezone=True)' in source
    for forbidden in ("chat_id", "client_name", "phone", "booking_key", "service_names"):
        assert forbidden not in source
```

- [ ] **Step 2: Update migration integration expectations from `0011_yclients_service_catalog` to `0012_yclients_projection_suppression` and assert the exact two suppression columns**

```python
assert current_revision == "0012_yclients_projection_suppression"
assert columns["yclients_projection_suppressions"] == {
    "external_id",
    "created_at",
}
```

- [ ] **Step 3: Run RED in Docker**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_migration_0012.py tests/integration/test_migrations.py
```
Expected: FAIL because migration `0012` and its table do not exist.

- [ ] **Step 4: Add the minimal additive migration**

```python
"""Suppress rematerialization of locally deleted YCLIENTS projections."""

from alembic import op
import sqlalchemy as sa

revision = "0012_yclients_projection_suppression"
down_revision = "0011_yclients_service_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yclients_projection_suppressions",
        sa.Column("external_id", sa.Text(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("yclients_projection_suppressions")
```

- [ ] **Step 5: Run GREEN and commit**

Run the Step 3 command. Expected: PASS with migration head `0012` and forward-schema checks green.

```bash
git add project/migrations/versions/0012_yclients_projection_suppression.py project/tests/unit/admin/test_migration_0012.py project/tests/integration/test_migrations.py
git commit -m "feat: добавить suppression схему YCLIENTS-проекции"
```

---

### Task 2: Make deletion and projection replacement suppression-aware

**Files:**
- Modify: `project/admin/customer_data_deletion.py`
- Modify: `project/src/moroz/booking/projection.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `project/tests/integration/booking/test_yclients_projection.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: `yclients_projection_suppressions` from Task 1; existing `ProjectionRepository.replace(connection, snapshot) -> None` remains unchanged for callers.
- Produces: deletion count keys `yclients_projection_suppressions` and `yclients_booking_projection`; suppressed external IDs can never be materialized by `replace()`.

- [ ] **Step 1: Change the existing deletion integration assertion from preserving external ID `401` to deleting it and creating one metadata-only tombstone**

```python
assert await conn.fetchval(
    "SELECT count(*) FROM yclients_booking_projection WHERE external_id='401'"
) == 0
assert await conn.fetchval(
    "SELECT count(*) FROM yclients_projection_suppressions WHERE external_id='401'"
) == 1
stored = json.loads(
    await conn.fetchval(
        "SELECT to_jsonb(s)::text FROM yclients_projection_suppressions s "
        "WHERE external_id='401'"
    )
)
assert set(stored) == {"external_id", "created_at"}
```

- [ ] **Step 2: Add deletion→replacement coverage using a snapshot containing deleted `401` plus unrelated `402`**

```python
await repository.replace(connection, snapshot("401", "402"))
rows = await connection.fetch(
    "SELECT external_id FROM yclients_booking_projection ORDER BY external_id"
)
assert [row["external_id"] for row in rows] == ["402"]
```

- [ ] **Step 3: Add forced tombstone failure coverage**

```python
await conn.execute(
    "CREATE FUNCTION reject_suppression() RETURNS trigger AS $$ "
    "BEGIN RAISE EXCEPTION 'forced suppression failure'; END; $$ LANGUAGE plpgsql; "
    "CREATE TRIGGER reject_suppression BEFORE INSERT ON "
    "yclients_projection_suppressions FOR EACH ROW EXECUTE FUNCTION reject_suppression();"
)
with pytest.raises(CustomerDataDeletionError, match="customer data deletion failed"):
    await delete_customer_data(...)
assert await conn.fetchval("SELECT count(*) FROM bookings WHERE customer_id='42'") == 1
assert await conn.fetchval("SELECT count(*) FROM yclients_booking_projection WHERE external_id='401'") == 1
```

- [ ] **Step 4: Add projection lookup failure coverage proving the old snapshot remains intact and the public code is fixed**

```python
await connection.execute("DROP TABLE yclients_projection_suppressions")
with pytest.raises(YclientsProjectionError, match="^yclients_projection_write$"):
    await repository.replace(connection, replacement)
assert await projection_rows(database) == original_rows
```

- [ ] **Step 5: Run RED in Docker**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/integration/admin/test_customer_data_deletion_postgres.py tests/integration/booking/test_yclients_projection.py
```
Expected: FAIL because deletion preserves projection and replacement ignores tombstones.

- [ ] **Step 6: Collect provider IDs before booking deletion, then insert tombstones, delete matching projection rows, and verify exact coverage inside the existing transaction**

```python
external_ids = [
    row["external_id"]
    for row in await conn.fetch(
        "SELECT external_id FROM bookings "
        "WHERE customer_id = $1 AND external_id IS NOT NULL",
        chat,
    )
]
if external_ids:
    await conn.executemany(
        "INSERT INTO yclients_projection_suppressions (external_id) VALUES ($1) "
        "ON CONFLICT (external_id) DO NOTHING",
        [(value,) for value in external_ids],
    )
await _delete(
    conn,
    counts,
    "yclients_booking_projection",
    "DELETE FROM yclients_booking_projection WHERE external_id = ANY($1::text[])",
    external_ids,
)
suppression_count = await conn.fetchval(
    "SELECT count(*) FROM yclients_projection_suppressions "
    "WHERE external_id = ANY($1::text[])",
    external_ids,
)
projection_count = await conn.fetchval(
    "SELECT count(*) FROM yclients_booking_projection "
    "WHERE external_id = ANY($1::text[])",
    external_ids,
)
if suppression_count != len(set(external_ids)) or projection_count:
    raise CustomerDataDeletionError("customer data deletion failed")
```

- [ ] **Step 7: Filter suppressed records inside `ProjectionRepository.replace()` before deleting the old snapshot**

```python
async with connection.transaction():
    suppressed = set(
        await connection.fetch(
            "SELECT external_id FROM yclients_projection_suppressions "
            "WHERE external_id = ANY($1::text[])",
            [record.external_id for record in snapshot.records],
        )
    )
    rows = [row for row in rows if row[0] not in {item["external_id"] for item in suppressed}]
    await connection.execute("DELETE FROM yclients_booking_projection")
    if rows:
        await connection.executemany(INSERT_PROJECTION_SQL, rows)
```

- [ ] **Step 8: Run GREEN, update docs and commit**

Run the Step 5 command. Expected: PASS; suppressed `401` stays absent after a full sync, `402` remains visible, forced errors roll back atomically.

Update the roadmap gate with `suppression implemented; focused Docker gate green` and append a timestamped changelog entry without IDs, secrets or PII.

```bash
git add project/admin/customer_data_deletion.py project/src/moroz/booking/projection.py project/tests/integration/admin/test_customer_data_deletion_postgres.py project/tests/integration/booking/test_yclients_projection.py 'Дорожная карта.md' changelog.md
git commit -m "fix: не восстанавливать удалённую YCLIENTS-проекцию"
```

---

### Task 3: Fence claimed Telegram delivery against customer deletion

**Files:**
- Modify: `project/src/moroz/messaging/repository.py`
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: `project/tests/e2e/test_message_delivery.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: `customer_lock_subject(chat_id: str) -> str`, claimed `OutboundMessage`, statuses `sending/sent/delivery_unknown`.
- Produces: `MessageRepository.fence_claimed_outbound(outbound: OutboundMessage) -> AsyncContextManager[OutboundMessage | None]`; yielded row is reloaded from PostgreSQL and the transaction lock remains held until context exit.

- [ ] **Step 1: Add a test where a claimed row is deleted before the fence and assert `SKIPPED` with zero Telegram calls**

```python
claimed = await repository.claim_outbound_delivery(outbound_id)
await connection.execute("DELETE FROM outbound_messages WHERE id=$1", outbound_id)
result = await deliver_claimed_outbound(telegram, repository, claimed)
assert result == DeliveryResult.SKIPPED
assert telegram.sent_messages == []
```

- [ ] **Step 2: Add two event-controlled concurrency tests**

```python
# Send wins: FakeTelegram signals entered, deletion task must remain pending
# until the provider release event is set; after deletion returns no further send occurs.
assert not deletion_task.done()
release_send.set()
assert await send_task == DeliveryResult.SENT
assert (await deletion_task).status == "deleted"

# Deletion wins: hold the customer lock in deletion, start sender, finish deletion,
# then sender rechecks the missing row and skips without calling Telegram.
assert (await deletion_task).status == "deleted"
assert await send_task == DeliveryResult.SKIPPED
assert telegram.sent_messages == []
```

- [ ] **Step 3: Run RED in Docker**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_message_delivery.py -k "deletion or post_send or ordered or delivery_unknown"
```
Expected: the new deletion race tests FAIL because sender trusts its in-memory claim.

- [ ] **Step 4: Add the repository async context manager**

```python
@asynccontextmanager
async def fence_claimed_outbound(self, outbound: OutboundMessage):
    async with self._database.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                customer_lock_subject(outbound.chat_id),
            )
            row = await connection.fetchrow(
                "SELECT id, channel, chat_id, text, delivery_options, idempotency_key "
                "FROM outbound_messages WHERE id=$1 AND channel=$2 AND chat_id=$3 "
                "AND status='sending'",
                outbound.id,
                outbound.channel,
                outbound.chat_id,
            )
            yield None if row is None else _outbound_from_row(row)
```

Refactor the existing claim conversion into `_outbound_from_row(row) -> OutboundMessage` so both paths use identical JSON parsing; do not add a second lock service.

- [ ] **Step 5: Send only the fenced row while its context is active**

```python
async with repository.fence_claimed_outbound(outbound) as current:
    if current is None:
        return DeliveryResult.SKIPPED
    sent_message = await telegram.send_message(**_send_arguments(current))
```

Keep existing cancellation, timeout, release and post-send completion behavior. Provider errors still update status only after the fence context exits, preventing a nested acquire deadlock.

- [ ] **Step 6: Run GREEN and commit**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_message_delivery.py tests/integration/admin/test_customer_data_deletion_postgres.py
```
Expected: PASS including both lock orderings, ordered delivery, network timeout, cancellation and admin-reply completion.

```bash
git add project/src/moroz/messaging/repository.py project/src/moroz/messaging/telegram.py project/tests/e2e/test_message_delivery.py changelog.md
git commit -m "fix: сериализовать Telegram send с удалением клиента"
```

---

### Task 4: Enable a commit-pinned scheduler in the staging release contract

**Files:**
- Modify: `project/docker-compose.staging.yml`
- Modify: `project/ops/staging-runbook.md`
- Modify: `project/tests/unit/test_staging.py`
- Modify: `project/src/moroz/notifications/handlers.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: base Compose scheduler command/environment/dependencies and `project/scheduler/Dockerfile`.
- Produces: staging image `moroz-staging-scheduler:${STAGING_IMAGE_TAG}`, manifest state `scheduler absent` or `scheduler <immutable-image-id>`, exact-image rollout/restore verification and a synthetic terminal scheduler-job smoke.

- [ ] **Step 1: Replace the disabled-scheduler test with immutable-image and secret-allowlist assertions**

```python
assert "profiles" not in override["scheduler"]
assert override["scheduler"]["image"] == (
    "moroz-staging-scheduler:${STAGING_IMAGE_TAG:?set STAGING_IMAGE_TAG}"
)
assert set(merged["scheduler"]["environment"]) <= {
    "DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "RABBITMQ_URL", "RABBITMQ_USER", "RABBITMQ_PASSWORD",
}
for forbidden in ("TELEGRAM", "LLM", "OPENAI", "YCLIENTS"):
    assert forbidden not in repr(merged["scheduler"]["environment"])
```

- [ ] **Step 2: Add runbook tests for build/manifests/rollout/log scan and both rollback paths**

```python
assert "for service in bot worker scheduler admin migrate" in runbook
assert "moroz-staging-scheduler:${STAGING_IMAGE_TAG}" in runbook
assert "scheduler absent" in previous_capture
assert "rm -sf scheduler" in absent_rollback
assert "scheduler" in verify_runtime_ids
assert "synthetic" in scheduler_smoke.lower()
assert "terminal" in scheduler_smoke.lower()
```

- [ ] **Step 3: Run RED in Docker**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/test_staging.py -k "scheduler or rollback or image"
```
Expected: FAIL because staging disables scheduler and the runbook omits its image/state.

- [ ] **Step 4: Pin and enable the staging scheduler without overriding base environment**

```yaml
  scheduler:
    image: "moroz-staging-scheduler:${STAGING_IMAGE_TAG:?set STAGING_IMAGE_TAG}"
```

- [ ] **Step 5: Extend the runbook with exact first-enable semantics**

Use these literal manifest records:

```sh
if docker inspect moroz-staging-scheduler-1 >/dev/null 2>&1; then
  scheduler_image_id="$(docker inspect -f '{{.Image}}' moroz-staging-scheduler-1)"
  printf 'scheduler %s\n' "$scheduler_image_id" >> "$rollback_dir/previous-image-ids"
else
  printf 'scheduler absent\n' >> "$rollback_dir/previous-image-ids"
fi
```

Candidate build and manifest:

```sh
docker build -f scheduler/Dockerfile -t "moroz-staging-scheduler:${STAGING_IMAGE_TAG}" .
scheduler_id="$(docker image inspect -f '{{.Id}}' "moroz-staging-scheduler:${STAGING_IMAGE_TAG}")"
printf 'scheduler %s\n' "$scheduler_id" >> "$rollback_dir/candidate-image-ids"
```

Rollback branch:

```sh
previous_scheduler="$(awk '$1=="scheduler"{print $2}' "$rollback_dir/previous-image-ids")"
if test "$previous_scheduler" = absent; then
  docker compose --env-file ../.env -p moroz-staging -f docker-compose.yml -f docker-compose.staging.yml rm -sf scheduler
  ! docker inspect moroz-staging-scheduler-1 >/dev/null 2>&1
else
  test -n "$previous_scheduler"
  docker image tag "$previous_scheduler" "moroz-staging-scheduler:${STAGING_PREVIOUS_IMAGE_TAG}"
fi
```

Rollout and candidate restore must use `up -d --no-build --wait --wait-timeout 120 bot worker scheduler admin`, then compare `moroz-staging-scheduler-1` `.Image` with the candidate manifest.

- [ ] **Step 6: Document a bounded synthetic smoke that never calls YCLIENTS or sends client notifications**

Add the fixed system kind and terminal no-op before any booking lookup:

```python
STAGING_SCHEDULER_SMOKE_KIND = "staging_scheduler_smoke"

async def handle_scheduler_job(job: SchedulerJob, **dependencies) -> JobResult:
    if job.kind == STAGING_SCHEDULER_SMOKE_KIND:
        return JobResult.skipped("staging_scheduler_smoke")
    # existing routing follows unchanged
```

Include `STAGING_SCHEDULER_SMOKE_KIND` in `MessageTaskHandler`'s serialized system-kind set. Add a unit test with `booking_port`, `outbox`, lifecycle, projection, catalog and retention dependencies as mocks, then assert the job completes as `JobResult.skipped("staging_scheduler_smoke")` and every dependency has zero calls.

```sql
INSERT INTO scheduler_jobs (id, kind, run_at, payload, idempotency_key, status)
VALUES (gen_random_uuid(), 'staging_scheduler_smoke', now(), '{}'::jsonb,
        'staging_scheduler_smoke:' || gen_random_uuid()::text, 'pending')
RETURNING id;
```

The smoke command must poll only this returned UUID until status is `skipped` with reason `staging_scheduler_smoke`, fail on timeout, and remove no customer rows. It must also assert that `booking_key` and `booking_starts_at` remain null.

- [ ] **Step 7: Run GREEN, render Compose, update docs and commit**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/test_staging.py tests/unit/test_documented_compose_commands.py tests/unit/test_worker.py
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.staging.yml config --quiet
```
Expected: PASS; rendered scheduler has an immutable image, no disabled profile and no Telegram/LLM/YCLIENTS environment.

```bash
git add project/docker-compose.staging.yml project/ops/staging-runbook.md project/tests/unit/test_staging.py project/src/moroz/notifications/handlers.py project/worker/main.py project/tests/unit/test_worker.py 'Дорожная карта.md' changelog.md
git commit -m "ops: включить commit-pinned scheduler на staging"
```

---

### Task 5: Run the release gate and update draft PR №2

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: reproducible test evidence and a reviewable pushed PR branch; does not merge or deploy.

- [ ] **Step 1: Run focused privacy, messaging, migration and staging suites**

```bash
docker compose --env-file ../.env run --rm test pytest -q \
  tests/unit/admin/test_migration_0012.py \
  tests/integration/test_migrations.py \
  tests/integration/booking/test_yclients_projection.py \
  tests/integration/admin/test_customer_data_deletion_postgres.py \
  tests/e2e/test_message_delivery.py \
  tests/unit/test_staging.py
```
Expected: all tests PASS with no skips in the new contracts.

- [ ] **Step 2: Run the full Docker suite with the repository's canonical read-only HTML mounts**

```bash
docker compose --env-file ../.env run --rm \
  -v ../moroz-i-solntse-full-architecture.html:/workspace/moroz-i-solntse-full-architecture.html:ro \
  -v ../docs/architecture/moroz-i-solntse-full-architecture.html:/workspace/docs/architecture/moroz-i-solntse-full-architecture.html:ro \
  test pytest -q
```
Expected: full suite PASS; record the exact count and duration.

- [ ] **Step 3: Run static and Compose gates in Docker**

```bash
docker compose --env-file ../.env run --rm test python -m compileall -q /workspace
docker compose --env-file ../.env run --rm test sh -lc "pip install --no-deps ruff==0.12.7 >/dev/null && ruff check --select E9,F /workspace"
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.staging.yml config --quiet
git diff --check origin/main...HEAD
```
Expected: all four commands exit 0; Ruff prints `All checks passed!`.

- [ ] **Step 4: Self-review against the approved design**

Check every design criterion explicitly: suppressed data does not return; both send/delete orders are deterministic; scheduler previous `absent` and previous image paths are tested; no secret/environment expansion; no YCLIENTS mutation; no production changes.

- [ ] **Step 5: Request an independent re-review of `origin/main...HEAD`**

Expected verdict: `0 Critical / 0 Important`. Any Critical or Important finding returns to RED/GREEN before proceeding.

- [ ] **Step 6: Record evidence, commit and push the already-authorized PR branch**

```bash
git add 'Дорожная карта.md' changelog.md
git commit -m "docs: подтвердить privacy gate staging release"
git push origin codex/staging-update-2026-08-18
gh pr view 2 --json isDraft,mergeable,url,headRefOid
```
Expected: PR №2 remains draft and mergeable. Do not merge yet.

- [ ] **Step 7: Only after the user confirms the green report, mark PR ready, merge, then follow the deploy skill for staging-only rollout and read-only YCLIENTS diagnosis**

The rollout must use `/opt/moroz-staging/.env`, exact merged commit images and the updated rollback manifest. It must not copy local credentials, touch production, or issue YCLIENTS mutations. The diagnostic may emit only allowlisted endpoint/auth/permission/status evidence, never response bodies or PII.

---

## Self-Review Result

- Spec coverage: durable suppression, atomic rollback/remnant checks, both outbound race orders, scheduler image/environment/first-enable rollback/smoke and final review gate are each mapped to a task.
- Placeholder scan: no deferred implementation markers or unspecified error handling remains.
- Type consistency: the only new runtime API is `fence_claimed_outbound(outbound: OutboundMessage)`, and both repository and sender tasks use that exact name and yield type.
- Scope: no production action, external permission change or YCLIENTS mutation is included.
