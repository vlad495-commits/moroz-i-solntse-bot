# LLM Compact Context + Compact Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать masked bounded-window Compact Context и immutable 40-case Compact Evaluation в существующем runtime/admin eval-контуре.

**Architecture:** Новый typed `ContextCompactor` сжимает только старую часть последних 40 masked сообщений при пороге `>30`, возвращая strict JSON v1 summary + exact tail 10 или tail-only fallback. `SecurityPipeline` внедряет его после Input Security/Router перед answer/Validator; suite `compact` расширяет общие eval tables, runner, routes и templates без нового silo.

**Tech Stack:** Python 3.12, asyncio, dataclasses, OpenAI/Anthropic SDK adapter, FastAPI, Jinja2, asyncpg, Alembic, PostgreSQL 16, pytest, Docker Compose.

## Global Constraints

- Все Python/test/migration команды выполняются только через Docker Compose из `project/` с `--env-file ../.env`.
- TDD обязателен: новый production behavior появляется только после наблюдаемого RED.
- Runtime thresholds: `CONTEXT_MESSAGES_LIMIT=40`, `COMPACT_THRESHOLD=30`, `COMPACT_KEEP_RECENT=10`.
- Compactor provider получает только masked bounded `user`/`assistant` history; raw ПД, system/tool artifacts и provider response не логируются/не сохраняются.
- Summary contract: exact JSON v1 fields `facts`, `agreements`, `open_questions`, `constraints`, `conflicts`; invalid output даёт exact-tail fallback.
- Никаких persistent summary tables/Redis keys, новых dependencies, eval tables, push/deploy или paid real-provider calls без отдельного разрешения.
- После каждого логического шага обновлять `changelog.md`, `Дорожная карта.md` и делать локальный commit.

---

### Task 1: Typed ContextCompactor core

**Files:**
- Create: `project/src/moroz/security/context_compactor.py`
- Create: `project/tests/unit/security/test_context_compactor.py`

**Interfaces:**
- Consumes: `SDKProvider.complete(LLMRequest) -> LLMResponse`, `LLMUsage`.
- Produces: `CompactSummary`, `CompactResult`, `ContextCompactor.compact(masked_context)`.

- [x] **Step 1: Write parser/threshold/tail RED tests**

```python
@pytest.mark.asyncio
async def test_exact_threshold_does_not_call_provider():
    provider = ForbiddenProvider()
    result = await ContextCompactor(provider).compact(dialog(30))
    assert result.source == "unchanged"
    assert result.messages == tuple(dialog(30))

@pytest.mark.asyncio
async def test_long_context_returns_summary_and_exact_tail():
    provider = Provider(valid_summary_json())
    source = dialog(31)
    result = await ContextCompactor(provider).compact(source)
    assert result.source == "llm"
    assert result.messages[-10:] == tuple(source[-10:])
    assert result.messages[0]["content"].startswith("UNTRUSTED_COMPACT_CONTEXT_V1")
```

- [x] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_context_compactor.py`

Expected: collection/import failure because `moroz.security.context_compactor` does not exist.

- [x] **Step 3: Implement minimal strict component**

Implement frozen dataclasses, exact-key JSON parser with `parse_constant` rejection, item/count/length bounds, deterministic renderer, role/content filtering, message-boundary 24k serialization, threshold 30/tail 10, purpose `compact` request and usage propagation.

Core branch:

```python
async def compact(self, masked_context: list[dict[str, str]]) -> CompactResult:
    context = _valid_messages(masked_context)
    if len(context) <= self.threshold:
        return CompactResult(tuple(context), "unchanged", "below_threshold")
    tail = tuple(context[-self.keep_recent:])
    old = context[:-self.keep_recent]
    try:
        response = await self.provider.complete(
            LLMRequest(messages=_compact_request(old), purpose="compact")
        )
        summary = parse_compact_summary(response.text)
        return CompactResult((render_summary(summary), *tail), "llm", "compacted", response.usage)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        await self._alert(type(error).__name__)
        return CompactResult(tail, "fallback", _safe_reason(error))
```

- [x] **Step 4: Add focused RED cases, then GREEN one behavior at a time**

Cover exact keys/version, non-array/bool/constants, empty/oversized items, markdown wrapper, empty summary, latest conflict rendering, invalid roles/content, char bound, provider error, invalid output, alert failure, cancellation, usage and request-capture proving no raw values beyond supplied masked context.

- [x] **Step 5: Run GREEN and commit**

Run the focused Docker command from Step 2; expected all focused tests pass.

Commit: `feat: добавлен ContextCompactor`

---

### Task 2: Runtime configuration and pipeline integration

**Files:**
- Modify: `project/llm/config.py`
- Modify: `project/llm/llm.py`
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/worker/main.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/.env.example`
- Modify: `project/tests/unit/test_llm_providers.py`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `project/tests/unit/security/test_pipeline.py`
- Modify: `project/tests/integration/test_worker_usage_postgres.py`
- Modify: `project/tests/unit/test_worker.py`
- Modify: `project/tests/ops/verify_compose_db_fallback.ps1`

**Interfaces:**
- Consumes: Task 1 `ContextCompactor`, `CompactResult`.
- Produces: config validated at import, injected compactor, compact usage aggregation and 40-message worker query.

- [x] **Step 1: Write configuration RED tests**

Assert Compose/default config exports `CONTEXT_MESSAGES_LIMIT=40`, `COMPACT_THRESHOLD=30`, `COMPACT_KEEP_RECENT=10`, compact provider variables, and rejects `KEEP_RECENT > THRESHOLD` or `THRESHOLD >= CONTEXT_MESSAGES_LIMIT`.

- [x] **Step 2: Verify RED through Docker**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/test_migration_profile.py tests/unit/test_llm_providers.py`

Expected: assertions show old context limit/missing compact config and dependency.

- [x] **Step 3: Implement minimal config/provider wiring**

Add config values defaulted from Router, validate the three limits in one helper, create one compact `SDKProvider`, inject `ContextCompactor` into `SecurityPipeline`, preserve it during prompt reload, and add Compose/env example variables for worker/admin/bot where their imports require them.

- [x] **Step 4: Write pipeline RED tests**

Tests must prove:

```python
assert security.calls[0].context == original_recent_masked
assert router.calls[0].context == original_recent_masked
assert answer_request.messages[1:-1] == compact_result.messages
assert validator.calls[0].masked_context == list(compact_result.messages)
assert [u.purpose for u in result.usage].count("compact") == 1
```

Also prove local block/offtopic/direct catalog branches never call compactor and cancellation propagates.

- [x] **Step 5: Verify pipeline RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_pipeline.py`

Expected: missing `context_compactor` injection/calls.

- [x] **Step 6: Implement pipeline/worker GREEN**

Mask full 40-message window first; derive existing bounded recent context for Security/Router; compact only immediately before provider answer; aggregate compact usage; pass compacted context to answer and semantic Validator. Change worker query limit default to 40 without adding summary persistence.

- [x] **Step 7: Add worker usage RED/GREEN and regression**

Assert SQL receives limit 40, current buffered input is not duplicated, and `token_usage.purpose='compact'` is stored with answer/validator usages.

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/test_llm_providers.py `
  tests/unit/test_migration_profile.py `
  tests/unit/security/test_context_compactor.py `
  tests/unit/security/test_pipeline.py `
  tests/integration/test_worker_usage_postgres.py
```

Expected: all selected tests pass.

- [x] **Step 8: Update changelog/roadmap and commit**

Commit: `feat: Compact Context встроен в runtime`

---

### Task 3: Immutable Compact dataset and migration 0017

**Files:**
- Create: `project/llm/eval/compact_dataset.json`
- Create: `project/migrations/versions/0017_llm_compact_evaluations.py`
- Create: `project/tests/unit/security/test_compact_dataset.py`
- Create: `project/tests/unit/admin/test_migration_0017.py`
- Modify: `project/migrate/Dockerfile`

**Interfaces:**
- Produces: 40 `suite="compact"` cases in common schema; checksum-pinned LF dataset.

- [x] **Step 1: Write dataset/migration RED contracts**

Assert exact total/category/critical counts `40/28`, unique keys, `30/31` boundary cases, allowed exact fields, synthetic-only privacy rules, checksum load, down revision `0016_llm_validator`, no new tables, suite-only downgrade and migrate image copy.

- [x] **Step 2: Verify RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_compact_dataset.py tests/unit/admin/test_migration_0017.py`

Expected: missing dataset/migration.

- [x] **Step 3: Add minimal 40-case dataset and migration**

Use the design counts. Quality contexts are padded to 31 messages with neutral synthetic turns; `input_data` stores context/mode, `expected_data` stores required/forbidden facts. Migration normalizes CRLF before SHA-256 and bulk inserts into `eval_cases` only.

- [x] **Step 4: Run GREEN, Alembic head check, commit**

Run focused tests plus:

```powershell
docker compose --env-file ../.env run --rm migrate alembic -c /app/alembic.ini heads
```

Expected: single `0017_llm_compact (head)`.

Commit: `eval: добавлен dataset Compact Evaluation`

---

### Task 4: Compact eval runner

**Files:**
- Modify: `project/admin/eval_runner.py`
- Create: `project/tests/unit/admin/test_compact_eval_runner.py`
- Modify: `project/tests/unit/admin/test_router_eval_database.py`

**Interfaces:**
- Produces: `_build_context_compactor`, `run_compact_case`, `run_compact_eval_set`, safe `actual_data`.

- [x] **Step 1: Write runner RED tests**

Cover masked request, threshold structural pass, exact tail, semantic judge pass/fail, hallucination/forbidden fact, exception→error, safe result metadata, gate `100% critical + >=95%`, progress/finalize/cancel and suite-filtered problem rerun.

- [x] **Step 2: Verify RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_compact_eval_runner.py`

Expected: missing runner functions.

- [x] **Step 3: Implement production-component runner**

Instantiate the same `ContextCompactor` settings as runtime. Structural check is authoritative; semantic judge receives only masked source/summary/required/forbidden data and returns strict score/reasoning. Persist only source/reason/counts/dimensions, never transcript/full summary.

- [x] **Step 4: Run GREEN and common eval regression**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/admin/test_compact_eval_runner.py `
  tests/unit/admin/test_router_eval_runner.py `
  tests/unit/admin/test_security_eval_runner.py `
  tests/unit/admin/test_validator_eval_runner.py `
  tests/unit/admin/test_router_eval_database.py `
  tests/unit/test_eval_privacy.py
```

Expected: all selected tests pass.

- [x] **Step 5: Update changelog and commit**

Commit: `eval: Compact runner встроен в общий контур`

---

### Task 5: Owner-only Compact Evaluation UI

**Files:**
- Modify: `project/admin/eval_routes.py`
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/templates/eval_list.html`
- Modify: `project/admin/templates/eval_run_detail.html`
- Create: `project/tests/e2e/admin/test_compact_eval_routes.py`

**Interfaces:**
- Produces: `/eval/compact/`, full/problem POST routes, common detail/SSE projection.

- [ ] **Step 1: Write route/template RED tests**

Prove owner-only GET, CSRF POST, root-path URLs, read-only 40-case list, safe expected/actual metadata, no create/edit/delete, full run, problem rerun and common detail/SSE labels.

- [ ] **Step 2: Verify RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/e2e/admin/test_compact_eval_routes.py`

Expected: route 404/missing template branches.

- [ ] **Step 3: Implement minimal shared UI branches**

Mirror Router/Security/Validator route lifecycle with `suite="compact"`; reuse `eval_list.html` and `eval_run_detail.html`, adding only compact-specific labels and safe fields. Do not create a new template or editable controls.

- [ ] **Step 4: Run GREEN and admin regression**

Run all four component route suites and `tests/e2e/admin/test_public_prefix.py`; expected all pass.

- [ ] **Step 5: Update changelog/roadmap and commit**

Commit: `feat: Compact Evaluation добавлен в админку`

---

### Task 6: Integrated verification, review and acceptance handoff

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify only if assertions require it: architecture HTML tests/docs.

- [ ] **Step 1: Run focused Docker gate**

Run one fresh focused gate:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/security/test_context_compactor.py `
  tests/unit/security/test_compact_dataset.py `
  tests/unit/security/test_pipeline.py `
  tests/unit/test_llm_providers.py `
  tests/unit/test_migration_profile.py `
  tests/integration/test_worker_usage_postgres.py `
  tests/unit/admin/test_migration_0017.py `
  tests/unit/admin/test_compact_eval_runner.py `
  tests/e2e/admin/test_compact_eval_routes.py `
  tests/unit/admin/test_router_eval_runner.py `
  tests/unit/admin/test_security_eval_runner.py `
  tests/unit/admin/test_validator_eval_runner.py `
  tests/e2e/admin/test_router_eval_routes.py `
  tests/e2e/admin/test_security_eval_routes.py `
  tests/e2e/admin/test_validator_eval_routes.py `
  tests/unit/test_eval_privacy.py
```

Record exact pass/fail/duration.

- [ ] **Step 2: Run migration upgrade/downgrade/upgrade in disposable namespace**

Confirm one head, 40 compact cases, suite-only downgrade and restoration without changing other suites. Clean only the exact owned Compose namespace and verify zero leftovers.

- [ ] **Step 3: Run full Docker suite**

Use the established full-suite command with all required read-only architecture path overrides. Record exact output and cleanup.

- [ ] **Step 4: Invoke requesting-code-review and fix findings test-first**

Review the whole feature diff against the design/plan. Every accepted finding gets RED→GREEN verification; rerun affected gates.

- [ ] **Step 5: Run final fresh focused/full verification**

No completion claim before current output proves zero failures, single Alembic head and clean owned Docker resources.

- [ ] **Step 6: Update docs and commit local candidate**

Record actual tests/review/limitations. Keep roadmap checkbox open until explicit paid real-provider Compact Evaluation passes.

Commit: `feat: завершена локальная реализация Compact Context`

- [ ] **Step 7: Ask separate permission for paid real-provider acceptance**

Do not call external provider until the owner explicitly approves model/cost. After approval, run the immutable 40-case suite, independently verify SQL aggregates, fix failures test-first if needed, and only then mark the pair complete.
