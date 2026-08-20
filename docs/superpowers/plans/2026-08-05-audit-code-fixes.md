# Audit Code Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть девять подтверждённых code gaps последнего аудита без изменения системного LLM-промпта и без внешних мутаций.

**Architecture:** Сохраняем webhook + durable pipeline единственным Telegram runtime ingress. Админские prompt/log операции остаются файловыми, но получают предсказуемые Linux-права, атомарную замену и явные ошибки; backup и alert подключаются минимальными runtime-компонентами.

**Tech Stack:** Python 3.12, FastAPI, aiogram, Redis, PostgreSQL 16, Docker Compose, pytest, POSIX shell.

## Global Constraints

- Не изменять `project/llm/prompts/system.md` и не добавлять prompt-based улучшения.
- Не выполнять push, deploy, server pull или реальные staging/production действия.
- Для каждого production-изменения сначала получить ожидаемый RED, затем минимальный GREEN.
- Все команды проекта выполнять через Docker; временные файлы хранить только в корневом `tmp/`.
- Сохранять fail-closed безопасность, durable idempotency и существующие публичные контракты.

---

### Task 1: Admin logs authentication and explicit file-log state

**Files:** Modify `project/admin/logs_routes.py`, `project/admin/templates/logs.html`, `project/llm/bot.py`; create `project/llm/logging_config.py`; test `project/tests/e2e/admin/test_auth.py`, `project/tests/unit/test_runtime_logging_policy.py`.

**Interfaces:** `logs_tail(request, ...)` authenticates through `get_current_user`; `configure_logging()` installs stream/file handlers and emits `file_logging_unavailable error_type=<type>` when the file handler cannot be created; webhook imports and calls it.

- [ ] Add tests for unauthenticated `/logs/tail`, visible file-handler failure, webhook logging setup and `log_exists=false` during auto-refresh.
- [ ] Run focused tests in Docker; expect RED from missing auth/setup/refresh handling.
- [ ] Implement request authentication, shared logging setup, sanitized failure reporting and missing-file refresh copy.
- [ ] Re-run focused and existing admin/logging regressions; expect PASS.

### Task 2: Idempotent consent callbacks

**Files:** Modify `project/llm/webhook.py`; test `project/tests/e2e/test_privacy_gate.py`.

**Interfaces:** checkbox callback data encodes explicit target state (`on`/`off`); legacy values are one-way `on`. Replay performs no second state change or markup edit. Replay of `consent:done` after durable consent is a no-op.

- [ ] Add E2E tests replaying the same checkbox and done updates; assert one state transition, consent record and thanks outbound.
- [ ] Run both tests in Docker; expect RED from toggle semantics/deleted Redis state.
- [ ] Implement explicit-state callbacks and durable-consent short-circuit without pre-consent message text persistence.
- [ ] Re-run privacy/message-delivery regressions; expect PASS.

### Task 3: Writable and atomic prompt/log runtime files

**Files:** Modify `project/admin/Dockerfile`, `project/llm/Dockerfile`, `project/worker/Dockerfile`, `project/admin/prompt_routes.py`, `project/admin/templates/prompt_edit.html`, `project/llm/llm.py`, Compose files and deploy/staging runbooks; create `project/ops/prepare-runtime-dirs.sh`; test prompt/security/runbook suites.

**Interfaces:** app images use UID/GID `10001`; the preparation script owns only `llm/prompts` and `logs`; `_write_prompt()` fsyncs a sibling temp and calls `os.replace`; `_publish_reload()` returns false for exceptions/zero subscribers; `_load_prompt()` validates content/facts and replaces one `SecurityPipeline` reference only after success.

- [ ] Add RED tests for unmanaged bind permissions, direct non-atomic write, zero-subscriber success and partial/missing reload mutation.
- [ ] Run focused tests in Docker and record expected failures.
- [ ] Implement stable runtime identity/preparation, atomic replacement, explicit `reload_failed` UI and atomic fail-closed pipeline replacement.
- [ ] Re-run prompt, security, Compose and runbook regressions; expect PASS.

### Task 4: Restore user and automated daily backups

**Files:** Modify `project/ops/backup-postgres.sh`, `project/ops/restore-postgres.sh`, `project/docker-compose.prod.yml`, `project/ops/backup-runbook.md`; create `project/ops/run-backups.sh`; test ops suites.

**Interfaces:** restore database commands receive `--username "$POSTGRES_USER"`; backup supports explicit `POSTGRES_HOST`; production `backup` performs an immediate encrypted backup and repeats every `BACKUP_INTERVAL_SECONDS=86400`, exiting on failure for Compose restart.

- [ ] Add RED command-log assertions for restore username and a production daily-service contract.
- [ ] Run focused tests; expect missing flags/service failures.
- [ ] Add explicit user/host arguments, bounded loop, production service and runbook text.
- [ ] Re-run backup/restore/runbook tests; expect PASS, without claiming a real restore drill.

### Task 5: Runtime AlertRouter wiring

**Files:** Modify `project/worker/main.py`, `project/docker-compose.yml`; test `project/tests/unit/test_worker.py`, `project/tests/integration/test_alerts.py`.

**Interfaces:** `_with_task_alerts(handler, router)` emits a deduplicated sanitized technical alert for task exceptions and re-raises the original; alert failure never replaces it. Runtime builds the router only with `TECHNICAL_TELEGRAM_CHAT_ID`, with optional `BUSINESS_TELEGRAM_CHAT_ID`.

- [ ] Add RED tests for missing runtime wiring and original-error precedence.
- [ ] Run focused tests; expect RED.
- [ ] Wire the router to the worker handler and forward only explicit chat-id settings.
- [ ] Re-run worker/alerts/privacy logging regressions; expect PASS; human receipt remains external.

### Task 6: Disable unsafe polling ingress

**Files:** Modify `project/llm/Dockerfile`, `project/llm/bot.py`, `project/docker-compose.yml`; test Compose/runtime policy suites.

**Interfaces:** bot image always starts `uvicorn webhook:app`; health check only probes `/healthz`; legacy `bot.main()` fails before Telegram/storage/LLM I/O.

- [ ] Add RED tests rejecting Dockerfile/healthcheck polling branches and requiring early `bot.main()` failure.
- [ ] Run focused tests; expect RED.
- [ ] Remove the runtime selector and add an explicit fail-closed legacy guard.
- [ ] Re-run Compose/runtime regressions; expect PASS.

### Task 7: Manual QA plan and factual project status

**Files:** Modify `План ручного тестирования.md`, `Дорожная карта.md`, `changelog.md`.

**Interfaces:** the canonical plan separates automatic tests, human checks and LLM evals; staging/production, real integrations, restore drill, human alert receipt and eval gates stay unchecked.

- [ ] Replace the old 14-case-only plan with separate automatic/manual/eval matrices and evidence fields.
- [ ] Update only locally proven roadmap facts; keep external P0 gates open and correct stale implementation checkboxes only with evidence.
- [ ] Record factual changelog entries without claiming external gates passed.
- [ ] Run documentation contracts and `git diff --check`; expect PASS.

### Task 8: Full verification, independent review and commits

**Files:** Review every changed file; no new scope.

**Interfaces:** verification repeats the full Docker test matrix, adjusted for newly collected tests.

- [ ] Run focused gates, collect-only, then full unit/integration/contract/e2e matrix.
- [ ] Inspect `git diff --check`, status, prompt-file diff and changed-file scope; system prompt must be unchanged.
- [ ] Dispatch a read-only independent reviewer from base SHA; fix every Critical/Important finding and re-run affected tests.
- [ ] Create local logical commits only after fresh verification; do not push or deploy.
