# Pre-YCLIENTS Release Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Довести текущий Telegram Production V1 до одного проверенного commit-pinned staging-кандидата, провести полную ручную приёмку и подтвердить безопасный image-only rollback без YCLIENTS и production.

**Architecture:** Переиспользуем существующие `run_compact_eval_set`, общие eval-таблицы, Docker Compose, Git-bundle доставку без push, `project/ops/staging-runbook.md` и канонический human-QA чеклист. Постоянный release orchestrator не создаётся: одноразовый Compact runner живёт только в корневом ignored `tmp/`, а долговечное evidence записывается в дорожную карту и changelog.

**Tech Stack:** Python 3.12, Docker Compose, PostgreSQL 16, Alembic, OpenAI-compatible provider, PowerShell, POSIX shell/SSH, Telegram Web, FastAPI admin.

## Global Constraints

- Любой runtime, eval, migration и test запускается только через Docker; прямой `python bot.py` запрещён.
- Все временные runner, отчёты, screenshots, bundles и логи создаются только в корневом `tmp/`.
- `Дорожная карта.md` обновляется при старте и завершении каждой задачи; каждое действие сразу фиксируется в `changelog.md`.
- Каждый завершённый логический шаг получает отдельный локальный commit; push запрещён без отдельного запроса.
- Платный provider-вызов и любое изменение staging требуют отдельного явного разрешения владельца.
- Один Compact acceptance использует ровно `40` immutable synthetic cases, `COMPACT_MODEL=gpt-4.1-mini`, существующий provider key и существующий semantic judge; секреты, prompts, transcripts и raw provider payload не выводятся.
- Compact gate: `100%` critical, не менее `95%` total, `0` errors и независимая SQL-сверка тех же агрегатов.
- При провале платного suite повтор запрещён до root-cause анализа, test-first исправления, локальных regression gates и нового явного разрешения.
- YCLIENTS read/write, production, аккаунт заказчицы, TOTP/секреты, VK/Instagram/WhatsApp и кампании вне scope.
- Staging credentials читаются только из `/opt/moroz-staging/.env`; локальный `.env` не является источником staging admin credentials.

---

### Task 1: Compact acceptance preflight без provider-вызовов

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Read: `project/llm/eval/compact_dataset.json`
- Read: `project/migrations/versions/0017_llm_compact_evaluations.py`
- Read: `project/admin/eval_runner.py`

**Interfaces:**
- Consumes: existing immutable Compact dataset, migration `0017`, `run_compact_eval_set` and external `.env` provider configuration.
- Produces: fresh disposable PostgreSQL at `0017_llm_compact`, exactly `40` Compact cases, fresh admin/migrate images and a safe authorization handoff with no provider call.

- [x] **Step 1: Mark Task 1 active**

Replace the active roadmap wording with `Compact acceptance preflight`, keep the Compact checkbox open and append a timestamped changelog entry saying no provider call has started.

- [x] **Step 2: Verify local and Docker baseline**

Run from repository root:

```powershell
git status --short --branch
docker version --format '{{.Server.Version}}'
Set-Location project
$env:COMPOSE_PROJECT_NAME = 'moroz-preyclients-compact-preflight'
docker compose --env-file ../.env run --rm test pytest -q tests/unit/test_project_governance_docs.py
```

Expected: only the Task 1 roadmap/changelog edits are present, Docker server responds, governance tests pass.

- [x] **Step 3: Build exact migration and admin images**

```powershell
docker compose --env-file ../.env --profile migration build --no-cache migrate
docker compose --env-file ../.env build --no-cache admin
docker compose --env-file ../.env up -d postgres redis
docker compose --env-file ../.env run --rm migrate
docker compose --env-file ../.env run --rm --no-deps migrate alembic -c /app/alembic.ini current
docker compose --env-file ../.env run --rm --no-deps migrate alembic -c /app/alembic.ini heads
```

Expected: `0017_llm_compact (head)` is the only current/head revision.

- [x] **Step 4: Verify exact dataset and safe provider readiness**

```powershell
docker compose --env-file ../.env exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT suite, count(*), count(*) FILTER (WHERE critical) FROM eval_cases WHERE suite = ''compact'' GROUP BY suite"'
docker compose --env-file ../.env run --rm --no-deps -e COMPACT_MODEL=gpt-4.1-mini --entrypoint python admin -c "import eval_runner as e; assert e.COMPACT_MODEL == 'gpt-4.1-mini'; assert e.COMPACT_API_KEY; assert e.JUDGE_API_KEY; print('compact-preflight: model=gpt-4.1-mini key=set judge=set')"
```

Expected: `compact|40|28` and only safe presence markers; no key values.

- [x] **Step 5: Clean only the owned preflight namespace**

```powershell
docker compose --env-file ../.env down --volumes --remove-orphans
Remove-Item Env:COMPOSE_PROJECT_NAME
Set-Location ..
```

Verify with Docker labels that `moroz-preyclients-compact-preflight` has zero containers, volumes and networks. Do not prune global Docker resources.

- [x] **Step 6: Record evidence and commit**

Update roadmap/changelog with exact head, case counts and test results. Run `git diff --check`, then:

```powershell
git add -A
git commit -m "test: подготовлена приёмка Compact Evaluation"
```

- [x] **Step 7: Ask for the paid-run gate**

Ask the owner to authorize exactly one 40-case `gpt-4.1-mini` Compact Evaluation. Stop before any external provider call.

---

### Task 2: Paid Compact Evaluation and independent SQL verification

**Files:**
- Create temporarily: `tmp/run_compact_acceptance.py`
- Modify: `Дорожная карта.md`
- Modify: `docs/superpowers/plans/2026-08-26-llm-compact-context-and-compact-evaluation.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 1 preflight evidence and explicit owner authorization.
- Produces: one persisted Compact eval run plus safe aggregate evidence; closes the Compact runtime/evaluation pair only when the gate passes.

- [x] **Step 1: Record authorization before the call**

Append the exact approved model, case count and boundaries to changelog. Do not include price estimates as guarantees or reveal keys.

- [x] **Step 2: Create the ignored one-shot runner**

Use `apply_patch` to create `tmp/run_compact_acceptance.py` with this complete content:

```python
import asyncio
import json
import sys

sys.path.insert(0, "/app")

import database
import eval_database as evdb
import eval_runner


async def main() -> None:
    await database.init_db()
    try:
        cases = await evdb.list_cases("compact")
        if len(cases) != 40:
            raise RuntimeError("compact_case_count")
        critical_ids = {int(case["id"]) for case in cases if case["critical"]}
        if len(critical_ids) != 28:
            raise RuntimeError("compact_critical_count")
        run_id = await evdb.create_run(40, eval_runner.COMPACT_MODEL, "compact")
        print(json.dumps({"run_id": run_id, "model": eval_runner.COMPACT_MODEL}))
        await eval_runner.run_compact_eval_set(run_id, cases=cases)
        run = await evdb.get_run(run_id)
        results = await evdb.get_run_results(run_id)
        critical_failed = sum(
            result["case_id"] in critical_ids and result["verdict"] != "pass"
            for result in results
        )
        errors = sum(result["verdict"] == "error" for result in results)
        summary = {
            "run_id": run_id,
            "status": run["status"],
            "model": run["judge_model"],
            "total": run["total"],
            "passed": run["passed"],
            "failed": run["failed"],
            "results": len(results),
            "critical_total": len(critical_ids),
            "critical_failed": critical_failed,
            "errors": errors,
        }
        print(json.dumps(summary, sort_keys=True))
        if not (
            run["status"] == "finished"
            and run["total"] == 40
            and run["passed"] >= 38
            and len(results) == 40
            and critical_failed == 0
            and errors == 0
        ):
            raise RuntimeError("compact_acceptance_gate")
    finally:
        await database.close_db()


asyncio.run(main())
```

- [x] **Step 3: Start a clean acceptance database and run exactly once**

From `project/`:

```powershell
$env:COMPOSE_PROJECT_NAME = 'moroz-preyclients-compact-acceptance'
$repo = (Resolve-Path '..').Path
docker compose --env-file ../.env --profile migration build --no-cache migrate
docker compose --env-file ../.env build --no-cache admin
docker compose --env-file ../.env up -d postgres redis
docker compose --env-file ../.env run --rm migrate
docker compose --env-file ../.env run --rm --no-deps `
  -e COMPACT_MODEL=gpt-4.1-mini `
  --volume "${repo}/tmp/run_compact_acceptance.py:/tmp/run_compact_acceptance.py:ro" `
  --entrypoint python admin /tmp/run_compact_acceptance.py
```

Expected: exactly one safe summary for a single run. Never rerun this command automatically after failure.

- [x] **Step 4: Independently verify persisted aggregates**

Use the printed `run_id` as `$runId`:

```powershell
$runId = 1
docker compose --env-file ../.env exec -T postgres sh -lc "psql -U \"`$POSTGRES_USER\" -d \"`$POSTGRES_DB\" -v run_id=$runId -Atc \"SELECT r.id, r.status, r.judge_model, r.total, r.passed, r.failed, count(er.id), count(*) FILTER (WHERE c.critical), count(*) FILTER (WHERE c.critical AND er.verdict <> 'pass'), count(*) FILTER (WHERE er.verdict = 'error') FROM eval_runs r JOIN eval_results er ON er.run_id=r.id JOIN eval_cases c ON c.id=er.case_id AND c.suite=r.suite WHERE r.id=:'run_id'::bigint AND r.suite='compact' GROUP BY r.id\""
```

Expected: the database independently reports `finished`, `40` total/results, at least `38` passed, `28` critical, `0` critical failed and `0` errors.

- [x] **Step 5: Branch on the evidence**

If green, mark both Compact checkboxes complete and move roadmap active state to final local candidate gates. If red, keep them open, record only safe category/result metadata, invoke systematic-debugging and do not start another paid run.

- [x] **Step 6: Clean exact temporary resources and commit evidence**

Use `apply_patch` to delete only `tmp/run_compact_acceptance.py`; bring down only `moroz-preyclients-compact-acceptance` with volumes and verify zero owned leftovers. Run `git diff --check`, then:

```powershell
git add -A
git commit -m "eval: пройдена приёмка Compact Evaluation"
```

Use a failure-specific commit message if the gate did not pass; never mark the roadmap item complete on failure.

---

### Task 3: Fresh final local candidate gates and review

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify conditionally: only files required by accepted review/test findings

**Interfaces:**
- Consumes: green Compact acceptance commit.
- Produces: one exact clean candidate commit with fresh full Docker, migration, static/privacy and review evidence.

- [x] **Step 1: Mark final local gate active and capture exact commit**

Require a clean tracked tree, record `git rev-parse HEAD`, and set an owned namespace `moroz-preyclients-final-local`.

- [x] **Step 2: Run focused release regression**

From `project/` run the Compact focused list already fixed in `docs/superpowers/plans/2026-08-26-llm-compact-context-and-compact-evaluation.md`, plus:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/test_project_governance_docs.py `
  tests/integration/test_migrations.py `
  tests/unit/test_staging.py `
  tests/e2e/ops/test_runbooks.py
```

Expected: all selected tests pass.

- [x] **Step 3: Run fresh no-cache full Docker suite**

```powershell
$repo = (Resolve-Path '..').Path
docker compose --env-file ../.env --profile test build --no-cache test
docker compose --env-file ../.env --profile test run --rm `
  --volume "${repo}/docs/architecture:/docs/architecture:ro" `
  --volume "${repo}/moroz-i-solntse-full-architecture.html:/moroz-i-solntse-full-architecture.html:ro" `
  test pytest -q
```

Expected: zero failures/errors/skips not explicitly documented.

- [x] **Step 4: Run migration and static/privacy gates**

```powershell
docker compose --env-file ../.env --profile migration build --no-cache migrate
docker compose --env-file ../.env up -d postgres
docker compose --env-file ../.env run --rm migrate
docker compose --env-file ../.env run --rm --no-deps migrate alembic -c /app/alembic.ini current
docker compose --env-file ../.env run --rm --no-deps migrate alembic -c /app/alembic.ini heads
docker compose --env-file ../.env config --quiet
docker compose --env-file ../.env run --rm --no-deps --entrypoint python test -m compileall -q /workspace
```

Run the existing secret/PII scanners referenced by the Compact plan. Expected: one head `0017_llm_compact`, all exits `0`, no forbidden additions.

- [x] **Step 5: Review the full post-staging delta**

Invoke `requesting-code-review` against `220d03e5880f3645586c63090766671a3e8e9eaa..HEAD`. Review correctness, privacy, migration compatibility, provider ordering, eval isolation, Compose allowlists and rollback compatibility. Accepted findings use systematic-debugging and TDD; speculative refactors are rejected.

- [x] **Step 6: Repeat affected and full gates after any fix**

Do not reuse pre-fix output. If code changes, create `codex/pre-yclients-release-fixes` in an isolated worktree before implementation, then merge only after a fresh green verification.

- [x] **Step 7: Record candidate and commit**

Update roadmap/changelog with exact commit and evidence, clean the owned namespace, verify zero leftovers and commit:

```powershell
git add -A
git commit -m "test: подтверждён pre-YCLIENTS release candidate"
```

---

### Task 4: Commit-pinned staging rollout without push

**Files:**
- Create temporarily: `tmp/pre-yclients-release-$short.bundle`, where `$short` is computed by `git rev-parse --short=12 HEAD`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Follow: `project/ops/staging-runbook.md`

**Interfaces:**
- Consumes: Task 3 exact candidate and explicit owner authorization to modify staging.
- Produces: the same commit running on staging with schema `0017_llm_compact`, protected rollback state and exact image manifests.

- [x] **Step 1: Ask for staging mutation authorization**

Present the exact candidate SHA, local gate results, expected affected services and explicit exclusions. Stop until approved.

Owner authorization received on 2026-08-28 for exact candidate `d18f67e40e0751ef04f0455e00bd778ffa57365c`. Later local status-only commits are excluded from the deployment bundle.

- [x] **Step 2: Invoke the deploy skill and perform read-only inventory**

Verify SSH identity, clean `/opt/moroz-staging` checkout, `/opt/moroz-staging/.env` mode/owner without values, current tag/schema, running health, free space, current webhook and non-staging containers. Any drift is a blocker.

- [ ] **Step 3: Create and verify a Git bundle without push**

From repository root:

```powershell
$sha = git rev-parse HEAD
$short = git rev-parse --short=12 HEAD
$bundle = "tmp/pre-yclients-release-$short.bundle"
git bundle create $bundle HEAD
git bundle verify $bundle
Get-FileHash -Algorithm SHA256 $bundle
```

Transfer quietly through the established protected SSH/SCP channel, recheck SHA-256 and `git bundle verify` on the VPS, then checkout the exact SHA. Do not print credentials or raw `.env`.

- [ ] **Step 4: Execute staging runbook sections 1–8 exactly**

Follow fail-closed prerequisites, inventory, protected secrets, rollback capture, config/build/image evidence, backward compatibility, persistent image pin, migration, apps/health/HTTPS and webhook lifecycle. Use `/opt/moroz-staging/.env`; do not run YCLIENTS smoke or production Compose.

Expected: exact candidate images for bot/worker/scheduler/admin/migrate, schema `0017_llm_compact`, healthy runtime, HTTPS/webhook safe checks and preserved rollback manifests.

- [ ] **Step 5: Run immediate safe-log scan**

Use runbook section 12 over logs since rollout start. Expected allowlisted counters are zero; do not paste raw logs into tracked files or chat.

- [ ] **Step 6: Record rollout evidence and commit**

Record only SHA, image IDs/digests, schema, health booleans, webhook booleans, aggregate log counters and rollback directory identifier. Delete only the owned local bundle after verified delivery, update roadmap/changelog and commit:

```powershell
git add -A
git commit -m "deploy: обновлён pre-YCLIENTS staging candidate"
```

---

### Task 5: Full manual acceptance of Telegram and admin

**Files:**
- Create ignored: `tmp/manual-test-$stamp/Отчет по тестированию бота.md`, where `$stamp = Get-Date -Format 'yyyyMMdd-HHmm'`
- Create ignored: screenshots in the same computed `tmp/manual-test-$stamp/` directory
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Follow: `docs/qa/manual/Ручное тестирование человеком.md`

**Interfaces:**
- Consumes: exact Task 4 staging candidate.
- Produces: evidence-backed statuses for all 36 scenarios, admin/log reconciliation and a release verdict.

- [ ] **Step 1: Invoke manual-qa-bot-testing and browser control skills**

Use Full run type because Router, Input Security, Validator and Compact Context changed after the last staging baseline.

- [ ] **Step 2: Confirm safe starting state**

Record Moscow start time and candidate SHA; verify admin URL, bot unpaused, webhook/container health and absence of pre-run fresh errors. Use only synthetic data.

- [ ] **Step 3: Execute all 36 scenarios**

Follow the canonical checklist exactly: first impression; services/prices; booking honesty; medical boundaries; prompt security; fake PII; buffering/context; non-text/long input; stop and pause/resume. Prefer Telegram Web; use synthetic webhook only where the real surface cannot reliably send payloads.

- [ ] **Step 4: Verify admin and logs**

Confirm dialog/message order, stats deltas, pause audit, component Evaluation pages and no fresh `Traceback`, `Exception`, `ERROR` or `CRITICAL` after run start. Leave the bot unpaused.

- [ ] **Step 5: Capture human-only gaps honestly**

If a second Telegram account, microphone, sticker picker or subjective owner sign-off is unavailable, mark it `Не проверено`; never convert technical evidence into human approval. Ask the owner only for the exact remaining manual action.

- [ ] **Step 6: Write report and commit durable evidence**

Save screenshots/report only under root `tmp/`. Update roadmap/changelog with aggregate statuses and blockers, then commit tracked docs:

```powershell
git add -A
git commit -m "test: проведена полная приёмка staging-бота"
```

---

### Task 6: Conditional defect fix-loop

**Files:**
- Modify conditionally: exact production/test files implicated by reproduced defects
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 5 defects with reproducible steps and evidence.
- Produces: test-first root-cause fixes and a clean targeted re-acceptance; produces no code change when no defect is confirmed.

- [ ] **Step 1: Classify findings**

Critical/Important defects block release. Nuances are tracked with an explicit workaround and do not trigger speculative code changes.

- [ ] **Step 2: Reproduce one defect at a time**

Invoke systematic-debugging, trace the shared root cause and create the smallest failing automated test. Before code edits create an isolated `codex/pre-yclients-release-fixes` worktree using `using-git-worktrees`.

- [ ] **Step 3: Implement minimal TDD fix**

Invoke test-driven-development and ponytail. Fix the shared root cause with no new dependency or abstraction unless existing code cannot express the correction.

- [ ] **Step 4: Verify and commit each logical fix**

Run RED → GREEN, related regression set and `git diff --check`; update roadmap/changelog and commit with a concrete root-cause message such as `fix: сохранён контекст после сжатия истории`.

- [ ] **Step 5: Repeat Tasks 3–5 only where invalidated**

Any code change requires a fresh full local gate and new commit-pinned staging image. Repeat affected manual scenarios; repeat the full manual suite only if the change affects shared pipeline behavior.

- [ ] **Step 6: Close the conditional task**

If no confirmed defects exist, record `0 blocking defects` and mark the roadmap item complete without creating production code.

---

### Task 7: Final staging acceptance and rollback rehearsal

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Follow: `project/ops/staging-runbook.md`

**Interfaces:**
- Consumes: clean manually accepted candidate and protected Task 4 rollback state.
- Produces: final health/smoke/log/rollback evidence with candidate restored; closes autonomous pre-YCLIENTS staging work.

- [ ] **Step 1: Freeze exact final candidate**

Require clean server checkout, exact candidate SHA/tag, schema `0017_llm_compact`, healthy services and no unresolved Critical/Important QA defects.

- [ ] **Step 2: Run bounded staging acceptance**

Execute runbook technical smoke, scheduler synthetic `skipped` job, Telegram webhook/live canary, worker recovery, Redis recovery and safe-log scan. Do not call YCLIENTS or send real client notifications.

- [ ] **Step 3: Execute image-only rollback**

Run section 13 unchanged with the protected previous/candidate manifests. Confirm `candidate → previous → candidate`, healthy services and webhook status at both checkpoints; never downgrade the database.

- [ ] **Step 4: Verify restored candidate**

Confirm candidate image IDs, schema, health, HTTPS, webhook, scheduler safe result and log counters. The trap/restore path must leave the candidate running even if previous verification fails.

- [ ] **Step 5: Update roadmap and changelog**

Mark all pre-YCLIENTS Now items complete only if the exact evidence is green. Keep YCLIENTS and production sections open and unchanged.

- [ ] **Step 6: Run final documentation gate and commit**

```powershell
Set-Location project
docker compose --env-file ../.env run --rm test pytest -q tests/unit/test_project_governance_docs.py
Set-Location ..
git diff --check
git add -A
git commit -m "release: принят автономный pre-YCLIENTS staging candidate"
```

Invoke `verification-before-completion` before the completion claim. Do not push or start YCLIENTS/production work.

---

## Self-Review Result

- Spec coverage: Compact authorization/acceptance, local gates/review, bundle-based staging rollout, full human QA, conditional fix-loop and rollback rehearsal each map to one task.
- Scope: no new permanent runtime component, dependency, table, provider workflow or release framework is introduced.
- Safety: paid calls and staging mutations have explicit stop gates; YCLIENTS, production, push and secrets remain excluded.
- Failure behavior: paid suite is never retried automatically; test/review/QA failures keep roadmap gates open and route through root-cause analysis.
- Placeholder scan: clean; runtime identifiers are computed from the exact candidate at execution time.
