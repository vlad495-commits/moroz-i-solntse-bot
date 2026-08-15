# YCLIENTS Catalog Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать YCLIENTS единственным источником актуальных записываемых услуг, цен, длительности и мастеров для гибридных ответов Telegram-бота.

**Architecture:** Существующий scheduler/worker раз в UTC-час получает bounded read-only каталог по мастерам, валидирует его и атомарно заменяет одну PostgreSQL-проекцию. Message transaction читает текущий снимок, локальный matcher выбирает максимум пять услуг, а SecurityPipeline после input guard либо формирует точный ответ без LLM, либо добавляет безопасный catalog data block в обычный answer-вызов и расширяет price allowlist validator.

**Tech Stack:** Python 3.12, FastAPI worker runtime, asyncpg/PostgreSQL 16, Alembic/SQLAlchemy, RabbitMQ scheduler jobs, существующий stdlib YCLIENTS HTTP transport, pytest/pytest-asyncio, Docker Compose.

## Global Constraints

- Работать только в `codex/yclients-catalog-grounding` от `codex/admin-ops-rc@2b294aa`.
- Все Python/tests/migrations запускать только через Docker Compose.
- Не делать provider/YCLIENTS, staging или production вызовов; не делать push/deploy.
- Ровно одна новая таблица; без новой очереди, сервиса, контейнера, dependency, Redis-кэша, embeddings и отдельного LLM catalog-classifier.
- Catalog sync раз в UTC-час; snapshot разрешён до возраста 24 часов включительно.
- Только GET booking endpoints; raw provider body/произвольные поля не сохранять и не логировать.
- Input guard/medical/stop выполняются раньше любого детерминированного catalog reply.
- Message history, token usage, outbox и inbox completion остаются в существующей транзакции и idempotency order.
- После каждого логического шага обновлять `changelog.md`, после завершения — `Дорожная карта.md`.
- Каждый production change следует доказанному RED → минимальному GREEN.
- Внешний test env: `D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env`; его содержимое не читать, не копировать и не выводить.

---

### Task 1: Bounded provider catalog contract and schema

**Files:**
- Create: `project/migrations/versions/0011_yclients_service_catalog.py`
- Create: `project/src/moroz/booking/yclients_catalog.py`
- Create: `project/tests/unit/admin/test_migration_0011.py`
- Create: `project/tests/contract/booking/test_yclients_catalog.py`
- Modify: `project/tests/integration/test_migrations.py`
- Modify: `changelog.md`

**Interfaces:**
- Produces: `CatalogRecord`, `CatalogSnapshot`, `YclientsCatalogError`, `YclientsCatalogReader.read(now)`.
- `CatalogRecord` fields: `service_id: str`, `staff_id: str`, `service_name: str`, `category_name: str | None`, `staff_name: str`, `price_min: Decimal`, `price_max: Decimal`, `duration_minutes: int`.
- `CatalogSnapshot` fields: `records: tuple[CatalogRecord, ...]`, `synced_at: datetime`.

- [x] **Step 1: Write migration RED contracts**

Add assertions that migration head is `0011_yclients_service_catalog`, the new table has exactly the nine allowlisted columns from the spec, composite PK `(service_id, staff_id)`, numeric/duration CHECKs, and no table besides the expected schema set.

- [x] **Step 2: Write reader RED contracts**

Use a `FakeHttp` and exact response fixtures:

```python
staff = {"id": 10, "name": " Анна ", "bookable": True}
service = {
    "id": 20,
    "title": " Криотерапия ",
    "category": {"title": " Крио "},
    "price_min": "1230.00",
    "price_max": 1500,
    "seance_length": 180,
}
```

Assert exact GET order, `user_auth=False`, `without_seances=1`, one `book_services?...staff_id=10`, Decimal normalization, seconds→minutes, control stripping, stable sorting, empty snapshot acceptance, non-bookable staff exclusion and duplicate rejection.

Parameterize malformed IDs, bool/nan/infinite/negative/oversized prices, reversed range, non-minute or >24h duration, malformed category/text/envelope, >100 staff, >200 services/staff and >5 000 pairs. Each must raise only an allowlisted `YclientsCatalogError.code` without raw data.

- [x] **Step 3: Run Docker RED**

```powershell
Set-Location project
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-task1 run --build --rm test pytest -q tests/unit/admin/test_migration_0011.py tests/contract/booking/test_yclients_catalog.py tests/integration/test_migrations.py
```

Expected: FAIL because revision/module do not exist and head remains `0010`.

- [x] **Step 4: Implement migration and reader minimally**

Migration columns:

```python
sa.Column("service_id", sa.Text(), primary_key=True),
sa.Column("staff_id", sa.Text(), primary_key=True),
sa.Column("service_name", sa.Text(), nullable=False),
sa.Column("category_name", sa.Text()),
sa.Column("staff_name", sa.Text(), nullable=False),
sa.Column("price_min", sa.Numeric(10, 2), nullable=False),
sa.Column("price_max", sa.Numeric(10, 2), nullable=False),
sa.Column("duration_minutes", sa.Integer(), nullable=False),
sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
```

Reader algorithm:

```python
staff_items = _items(await _get("book_staff", (("without_seances", 1),)))
for staff in _bounded_bookable_staff(staff_items):
    services = _services(await _get("book_services", (("staff_id", staff.id),)))
    for service in _bounded_services(services):
        records.append(_record(service, staff))
return CatalogSnapshot(tuple(sorted(records, key=_record_key)), now)
```

Use `Decimal(str(value))`, `is_finite()`, exact two-decimal quantization, shared local safe-display helpers (no generic framework) and `YclientsTransportError` mapping. Never include response/body in exceptions.

- [x] **Step 5: Run Docker GREEN and migration proof**

Run the Task 1 selection, then:

```powershell
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-task1 run --rm migrate
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-task1 run --rm --entrypoint alembic migrate -c /app/alembic.ini current
```

Expected: all selected tests pass and `0011_yclients_service_catalog (head)`.

- [x] **Step 6: Update changelog, diff-check and commit**

Commit only Task 1 files:

```powershell
git diff --check
git add project/migrations/versions/0011_yclients_service_catalog.py project/src/moroz/booking/yclients_catalog.py project/tests/unit/admin/test_migration_0011.py project/tests/contract/booking/test_yclients_catalog.py project/tests/integration/test_migrations.py changelog.md
git commit -m "feat: читать каталог услуг YCLIENTS"
```

---

### Task 2: Atomic hourly projection

**Files:**
- Create: `project/src/moroz/booking/catalog.py`
- Create: `project/tests/unit/booking/test_catalog_sync.py`
- Create: `project/tests/integration/booking/test_catalog_projection.py`
- Modify: `project/src/moroz/notifications/handlers.py`
- Modify: `project/tests/e2e/notifications/test_reminders.py`
- Modify: `changelog.md`

**Interfaces:**
- Produces: `CATALOG_SYNC_KIND`, `CatalogRepository.serialized()`, `CatalogRepository.replace(connection, snapshot)`, `catalog_job(now)`, `CatalogSyncCoordinator.ensure_current(now)`, `CatalogSyncCoordinator.run(job)`.
- Consumes Task 1 `CatalogSnapshot` and `YclientsCatalogError`.

- [ ] **Step 1: Write sync/repository RED tests**

Prove UTC hour bucket:

```python
assert catalog_job(datetime(2026, 8, 15, 12, 59, tzinfo=UTC)).run_at == datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
assert catalog_job(now).idempotency_key == f"{CATALOG_SYNC_KIND}:2026-08-15T12:00:00+00:00"
```

Prove next-hour scheduling happens before reader error, busy lock skips without provider read, exact lock is released on body exception, atomic replace preserves the first literal snapshot after a trigger rejects the second insert, and stored JSON contains no raw fixture metadata.

- [ ] **Step 2: Write scheduler dispatch RED test**

Add `catalog_sync` argument to `handle_scheduler_job`; assert only `CATALOG_SYNC_KIND` dispatches to it and missing coordinator fails closed before booking lookup.

- [ ] **Step 3: Run Docker RED**

Run new unit/integration files plus `tests/e2e/notifications/test_reminders.py`; expect missing catalog sync symbols/dispatch.

- [ ] **Step 4: Implement minimal repository/coordinator**

Follow the existing booking projection structure, with separate lock and hourly delta:

```python
CATALOG_LOCK = "yclients_service_catalog:v1"
CATALOG_SYNC_KIND = "yclients_service_catalog_sync"

async def run(self, job):
    await self._scheduler.schedule(catalog_job(job.run_at + timedelta(hours=1)))
    async with self._repository.serialized() as connection:
        if connection is None:
            return JobResult.skipped("catalog_busy")
        snapshot = await self._reader.read(self._clock())
        await self._repository.replace(connection, snapshot)
    return JobResult.sent()
```

Use one `DELETE` + bounded `executemany` transaction and map asyncpg errors to `yclients_catalog_write`.

- [ ] **Step 5: Run Docker GREEN**

Expected: new sync/repository/handler selection passes; existing projection sync tests stay green.

- [ ] **Step 6: Changelog, diff-check and commit**

Commit message: `feat: обновлять каталог YCLIENTS каждый час`.

---

### Task 3: Fresh bounded lookup and deterministic matching

**Files:**
- Modify: `project/src/moroz/booking/catalog.py`
- Create: `project/tests/unit/booking/test_catalog_matching.py`
- Create: `project/tests/integration/booking/test_catalog_lookup.py`
- Modify: `changelog.md`

**Interfaces:**
- Produces immutable `CatalogVariant`, `CatalogService`, `CatalogGrounding` and `CatalogRepository.ground(connection, text, now)`.
- `CatalogGrounding` fields: `status: Literal['fresh','stale','missing']`, `services: tuple[CatalogService, ...]`, `simple_kind: Literal['price','duration','staff'] | None`, `ambiguous: bool`; bounded methods/properties: `direct_reply`, `fact_text()` and `data_block()`.

- [ ] **Step 1: Write matcher RED tests**

Fixtures must prove `ё→е`, casefold, punctuation, deterministic order, exact phrase priority, unique token overlap, service grouping, per-staff price/duration ranges, max five services, and fail-closed ties. Include unrelated text and adversarial display strings; no provider ID may appear in rendered public data.

Simple-kind detection is a small allowlist of Russian cues:

```python
PRICE_WORDS = {"цена", "цену", "цене", "ценой", "стоит", "стоимость", "прайс"}
DURATION_WORDS = {"длительность", "длится", "времени", "минут"}
STAFF_WORDS = {"мастер", "специалист", "кто", "сотрудник"}
COMPARISON_WORDS = {"сравни", "разница", "отличается", "лучше", "подобрать"}
```

Any comparison cue disables deterministic simple response.

- [ ] **Step 2: Write PostgreSQL freshness RED tests**

Seed catalog rows and scheduler jobs. Prove:

- exactly `now - 24h` is fresh;
- one microsecond older is stale;
- empty successful snapshot uses latest `finished_at` with `status='finished'`;
- newer failed/skipped/pending/claimed jobs do not become success;
- stale/missing returns no services even if old rows exist;
- fixed SQL binds kind/status values and selects only allowlisted columns.

- [ ] **Step 3: Run Docker RED**

Expected: missing grounding/query/matcher behavior.

- [ ] **Step 4: Implement minimal query and matcher**

One bounded SELECT loads the snapshot; freshness query uses `scheduler_jobs`. Build grouped services in Python. No SQL string interpolation, fuzzy library or extra table.

```python
if last_success is None:
    return CatalogGrounding("missing", (), kind, False)
if now - last_success > timedelta(hours=24):
    return CatalogGrounding("stale", (), kind, False)
return match_catalog(rows, text, kind)
```

Return clarification when top confidence ties; never choose based only on a common generic token such as `массаж`, `процедура`, `услуга`.

- [ ] **Step 5: Run Docker GREEN, changelog and commit**

Commit message: `feat: находить актуальную услугу без LLM`.

---

### Task 4: Security pipeline hybrid response and dynamic validator facts

**Files:**
- Modify: `project/src/moroz/security/validator.py`
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/llm/llm.py`
- Modify: `project/tests/unit/security/test_validator.py`
- Modify: `project/tests/unit/security/test_pipeline.py`
- Modify: `project/tests/e2e/test_security_pipeline.py`
- Modify: `changelog.md`

**Interfaces:**
- Produces `merge_structured_facts(base, catalog) -> StructuredFacts`.
- Extends `SecurityPipeline.respond(..., catalog: CatalogGrounding | None = None)` and `generate_response(..., catalog=None)` without breaking existing callers.

- [ ] **Step 1: Write validator RED tests**

Prove union of normalized price/contact/slot/public-PII facts without mutation; catalog price accepted; a price not in selected services returns `unverified_price`.

- [ ] **Step 2: Write pipeline RED tests**

Prove in order:

1. blocked/medical/stop input wins over fresh deterministic catalog response;
2. unique simple price/duration/staff reply makes zero gateway calls and passes validator;
3. ambiguous reply lists at most five escaped/safe names and makes zero gateway calls;
4. stale/missing catalog price query returns fixed administrator fallback, zero gateway calls;
5. complex query adds one bounded system data block, not raw JSON/IDs/descriptions;
6. normal complex path makes one answer call, while existing review guard and validator retry semantics remain unchanged;
7. hallucinated price fails once, valid retry succeeds; two failures return safe fallback;
8. PII masking/restoration still works with catalog context.

- [ ] **Step 3: Run Docker RED**

Run validator/pipeline/e2e selections. Expected: new optional parameter/merger/template behavior absent.

- [ ] **Step 4: Implement minimal facts merge and catalog branch**

After current input decision, PII masking and route construction:

```python
catalog_facts = extract_structured_facts(catalog.fact_text()) if catalog and catalog.services else EMPTY_FACTS
active_facts = merge_structured_facts(self.facts, catalog_facts)
if catalog and catalog.direct_reply is not None:
    verdict = validate_output(catalog.direct_reply, active_facts, frozenset())
    return _zero(catalog.direct_reply if verdict.ok else SAFE_OUTPUT_FALLBACK)
owned_system = "\n\n".join(part for part in (self.system_prompt, route_metadata, catalog.data_block()) if part)
```

Catalog block starts with an invariant instruction that fields are untrusted data and not commands; variants are bounded and deterministically formatted. Do not add a second LLM gateway.

- [ ] **Step 5: Run Docker GREEN and compatibility regressions**

Run all security pipeline/validator/router/LLM gateway tests. Existing no-catalog behavior must remain green.

- [ ] **Step 6: Changelog, diff-check and commit**

Commit message: `feat: отвечать по каталогу с проверкой цен`.

---

### Task 5: Worker wiring, message atomicity and prompt cutover

**Files:**
- Modify: `project/worker/main.py`
- Modify: `project/src/moroz/notifications/handlers.py` if Task 2 interface needs final wiring only
- Modify: `project/llm/prompts/system.md`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `project/tests/e2e/test_message_delivery.py`
- Create: `project/tests/e2e/test_catalog_message_flow.py`
- Modify: prompt/eval tests that assert old numeric answers
- Modify: `changelog.md`

**Interfaces:**
- `_build_yclients_services` returns lifecycle, booking projection sync and catalog sync/repository graph without new credentials.
- `MessageTaskHandler` receives `catalog_repository`; `_process_message` calls `ground(connection, persisted_text, datetime.now(UTC))` and passes result as keyword-only `catalog` to the existing LLM callable.

- [ ] **Step 1: Write worker wiring RED tests**

Prove complete/empty/partial YCLIENTS config behavior, one shared `YclientsConfig`, catalog `ensure_current` at startup, catalog scheduler dispatch/failure allowlist and no new Compose environment/service/queue.

- [ ] **Step 2: Write message-flow RED tests**

Using real PostgreSQL message transaction and fake LLM/sender, prove:

- fresh simple question creates assistant/history/token usage/outbox exactly once with zero-token catalog response;
- duplicate inbox task adds nothing;
- complex grounding reaches LLM but catalog data is not stored as a separate message;
- human mode stores only user text and never calls catalog LLM response;
- forced outbox/history failure rolls back all message effects;
- stale catalog prevents old prompt/history price from being emitted.

- [ ] **Step 3: Write prompt cutover RED test**

Assert the authoritative prompt section contains no currency amount pattern (`руб`, `₽`, numeric price phrases) and includes the rule to use only catalog data for price/duration/staff. Update eval fixtures to expect safe clarification/fallback when no catalog is supplied instead of legacy numeric prices.

- [ ] **Step 4: Run Docker RED**

Expected: missing worker graph/grounding argument and legacy prompt prices.

- [ ] **Step 5: Implement minimal runtime wiring**

Build catalog reader/repository/coordinator beside existing booking projection using the same config and scheduler repository. Extend only the `CATALOG_SYNC_KIND` scheduler branch and error allowlist. Keep other booking/lifecycle paths unchanged.

Remove numeric pricing/course/deposit examples from the system prompt, preserving non-price service explanations and explicitly routing unsupported financial entities to administrator clarification.

- [ ] **Step 6: Run focused Docker GREEN**

Run worker, notifications, message delivery, catalog flow, prompt/admin prompt tests, eval schema tests, security, booking projection and migration selections.

- [ ] **Step 7: Changelog, diff-check and commit**

Commit message: `feat: подключить каталог к сообщениям бота`.

---

### Task 6: Scope audit, documentation and final verification

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-08-15-yclients-catalog-grounding.md` checkboxes/evidence
- Create ignored report: `.superpowers/sdd/yclients-catalog-grounding-report.md`

**Interfaces:**
- Consumes the complete implementation.
- Produces merge-ready local branch evidence; no merge/push/deploy.

- [ ] **Step 1: Review exact branch diff against spec**

Check all changed files and grep for forbidden additions: provider calls in message/admin paths, new Compose service/queue/dependency, raw provider logging, prices left in prompt, unbounded lists, direct deterministic reply before guard, live credentials in tests.

- [ ] **Step 2: Run canonical affected Docker gate**

Include all new tests plus migrations, booking projection/reconciliation, scheduler/notifications, worker, message delivery, router, security pipeline/validator, prompt admin/reload, architecture/Compose and privacy/deletion suites. Record exact count/time/exit.

- [ ] **Step 3: Run migration and static proofs**

```powershell
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-final run --rm migrate
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-final run --rm --entrypoint alembic migrate -c /app/alembic.ini current
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-final run --rm test python -m compileall -q src llm worker admin
git diff --check
```

Expected: `0011_yclients_service_catalog (head)`, compile exit 0, diff-check empty.

- [ ] **Step 4: Run fresh full Docker suite**

```powershell
docker compose --env-file 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env' -p yclients-catalog-final run --build --rm test pytest -q
```

Capture complete stdout/stderr and exit code under ignored root `tmp/`.

- [ ] **Step 5: Perform fresh correctness/security and Ponytail review passes**

The correctness pass must report Critical/Important/Minor findings and Ready status against spec/plan. The separate Ponytail pass checks deletable abstractions/dependencies/flexibility. Fix every Critical/Important and justified Minor via fresh RED/GREEN before proceeding.

- [ ] **Step 6: Close docs and commit**

Mark roadmap complete only after fresh gates/review. Append exact commands/counts/head/cleanup and no-provider/no-push statement to changelog/report. Mark plan checkboxes complete and commit:

```powershell
git add 'Дорожная карта.md' changelog.md docs/superpowers/plans/2026-08-15-yclients-catalog-grounding.md
git commit -m "docs: завершить grounding по каталогу YCLIENTS"
```

- [ ] **Step 7: Verify cleanup and final HEAD**

Resolve the exact Compose project resources by label before `down -v`; remove only `yclients-catalog-*` namespaces after validating their paths/names. Confirm `0` matching containers/volumes/networks, clean tracked worktree, ancestry from `2b294aa`, no remote branch contains HEAD, and report the exact final SHA. Do not merge until the user asks.
