# Reactivation Safe Audience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Автоматически сохранять отдельный Telegram marketing consent, присваивать клиенту один сегмент и показывать неизменяемый список черновика до внутренней очереди.

**Architecture:** Existing Telegram consent callback writes marketing consent in the same PostgreSQL transaction as processing consent. Repository SQL assigns one segment by priority and materializes recipient IDs as `draft` deliveries; queue confirmation only revalidates those IDs and can remove, never add. UI exposes the draft list and has no create-and-queue shortcut.

**Tech Stack:** Python 3.12, FastAPI, aiogram, asyncpg, PostgreSQL, Alembic, Jinja2, pytest, Docker Compose.

## Global Constraints

- Existing clients remain marketing-opted-out unless an explicit ads checkbox or audited admin action exists.
- Segment priority is `sleeping` → `regular` → `after_visit`.
- Queue confirmation cannot add a recipient absent from the draft.
- No Scheduler, LLM, outbound message, worker, staging, production or push changes.
- Tests run only through Docker.

---

### Task 1: Persist Telegram marketing consent

**Files:**
- Modify: `project/src/moroz/security/consent.py`
- Modify: `project/llm/webhook.py`
- Modify: `project/tests/e2e/test_privacy_gate.py`

**Interfaces:**
- Produces: `ConsentService.grant_marketing_consent(channel, user_id, consent_version, connection=...)`.
- Consumes: existing checked Redis state and the callback transaction.

- [ ] Add a failing E2E case: checked `pii + ads` followed by `done` creates one active `marketing_consents` row with channel `telegram`, user `7`, version `v1`; `pii` alone creates none.
- [ ] Run the two cases in Docker and verify the ads case fails because the row is absent.
- [ ] Add `MARKETING_CONSENT_VERSION = "v1"`, one upsert method, and call it inside the existing consent transaction only when `"ads" in checked`.
- [ ] Run the focused privacy cases in Docker; commit `feat: marketing consent сохраняется из Telegram`.

### Task 2: Exclusive segments and materialized draft

**Files:**
- Create: `project/migrations/versions/0021_reactivation_draft.py`
- Modify: `project/admin/reactivation_database.py`
- Modify: `project/tests/unit/admin/test_migration_0020.py`
- Modify: `project/tests/integration/admin/test_reactivation_database.py`

**Interfaces:**
- Produces: draft delivery status and `create_campaign(...)->UUID` that materializes recipient IDs.
- Consumes: booking aggregates and active marketing consent.

- [ ] Add failing integration cases proving one customer receives exactly one priority segment and draft creation inserts `reactivation_deliveries(status='draft')`.
- [ ] Add a failing case proving queue confirmation does not add a newly eligible customer and changes revoked/human-mode draft rows to `skipped`.
- [ ] Run RED in Docker.
- [ ] Add migration `0021` that replaces the delivery status check with `('draft','queued','skipped','sent','error')`.
- [ ] Replace segment predicates with one aggregate `CASE`; create campaign and draft rows in one transaction; queue only revalidates stored draft rows.
- [ ] Run focused migration/repository GREEN; commit `feat: добавлен безопасный черновик аудитории`.

### Task 3: Preview-first admin UI and closure

**Files:**
- Modify: `project/admin/reactivation_routes.py`
- Modify: `project/admin/reactivation_database.py`
- Modify: `project/admin/templates/reactivation.html`
- Modify: `project/tests/unit/admin/test_reactivation_routes.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- UI creates only a draft; journal displays recipient ID, available username, visit count, last visit, consent date and status.
- Queue POST confirms an existing draft only.

- [ ] Add failing route/template tests requiring one `Создать и проверить список` action and forbidding `Создать и поставить в очередь`.
- [ ] Add repository projection fields through joins to bookings, messages and consent without new stored PII.
- [ ] Simplify campaign POST to draft-only and render the exact recipient preview plus skipped reason.
- [ ] Run Docker route, repository, privacy, migration and no-outbound gates; run Compose config, Docker compileall and `git diff --check`.
- [ ] Mark roadmap complete, log exact evidence and commit `feat: добавлен предпросмотр аудитории реактивации`.
- [ ] Report navigation and migration conflicts; keep branch/worktree, do not merge or push.
