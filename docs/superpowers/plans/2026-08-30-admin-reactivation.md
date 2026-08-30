# Admin Reactivation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить owner-only вкладку «Реактивация» с отдельным marketing consent, настройками, детерминированным отбором трёх сегментов, внутренней очередью и журналом без реальных отправок.

**Architecture:** Additive migration создаёт четыре простые таблицы. Один admin repository выполняет SQL и транзакционно фиксирует кампанию с получателями; FastAPI router валидирует owner/CSRF и рендерит одну Jinja-страницу. Worker, scheduler, LLM и `outbound_messages` не меняются.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, asyncpg, PostgreSQL 16, Alembic, pytest; проверки только через Docker Compose.

## Global Constraints

- Marketing consent отсутствует по умолчанию; `processing_consents` не переносится.
- Consent хранит `channel`, `user_id`, `consent_version`, `granted_at`, состояние активности и `revoked_at`.
- Сегмент `regular` означает не менее двух завершённых визитов.
- LLM не выбирает получателей, скидку или оффер и в этой задаче не вызывается.
- Кампания не создаёт `scheduler_jobs`, `task_outbox` или `outbound_messages`.
- Staging, production, push и реальные сообщения запрещены.
- Известный baseline-блокер migration `0019` на Windows CRLF не исправляется этой веткой; focused Docker-команды временно монтируют Router dataset из Git с LF либо запускают тесты без Alembic там, где БД не нужна.

---

### Task 1: Additive schema and consent/settings repository

**Files:**
- Create: `project/migrations/versions/0020_admin_reactivation.py`
- Create: `project/admin/reactivation_database.py`
- Create: `project/tests/unit/admin/test_migration_0020.py`
- Create: `project/tests/integration/admin/test_reactivation_database.py`

**Interfaces:**
- Produces: `get_page_data()`, `save_settings(...)`, `set_marketing_consent(...)`, `create_campaign(...)`.
- Consumes: existing `admin.database.pool` lifecycle and `bookings.customer_id/status/scheduled_end_at`.

- [x] **Step 1: Write failing migration and repository tests**

Assert that migration `0020_admin_reactivation` follows `0019_router_v2`, creates `marketing_consents`, `reactivation_settings`, `reactivation_campaigns`, `reactivation_deliveries`, and downgrade removes only those tables. Integration tests must assert absence means no marketing consent, grant/revoke timestamps round-trip, settings round-trip, and `processing_consents` never creates eligibility.

- [x] **Step 2: Run RED in Docker**

```powershell
docker compose --env-file ../.env -p codex-admin-reactivation --profile test run --rm test pytest -q tests/unit/admin/test_migration_0020.py tests/integration/admin/test_reactivation_database.py
```

Expected: collection/import failure because migration and repository do not exist.

- [x] **Step 3: Implement minimum schema and repository**

Use one current-state row per `channel + user_id`; `set_marketing_consent` uses `INSERT ... ON CONFLICT DO UPDATE`, sets `granted_at=now(), revoked_at=NULL` for grant and preserves `granted_at` while setting `revoked_at=now()` for revoke. Seed singleton settings row `id=1` with conservative numeric defaults and empty offer/instruction. Do not seed any consent or campaign data.

- [x] **Step 4: Run GREEN in Docker and commit**

Expected: all Task 1 tests pass.

```powershell
git add project/migrations/versions/0020_admin_reactivation.py project/admin/reactivation_database.py project/tests/unit/admin/test_migration_0020.py project/tests/integration/admin/test_reactivation_database.py changelog.md
git commit -m "feat: добавлены данные реактивации и marketing consent"
```

### Task 2: Deterministic segments and internal campaign queue

**Files:**
- Modify: `project/admin/reactivation_database.py`
- Modify: `project/tests/integration/admin/test_reactivation_database.py`

**Interfaces:**
- Produces: `create_campaign(segment: str, created_by: UUID) -> UUID` and campaign/delivery aggregates in `get_page_data()`.
- Consumes: settings singleton and active rows from `marketing_consents`.

- [x] **Step 1: Add RED integration cases**

Create synthetic rows only inside disposable PostgreSQL fixtures. Assert: `after_visit` uses completed visit age, `sleeping` uses latest completed visit age, `regular` requires `count(*) >= 2`, inactive/revoked/no consent never gets a delivery, and queuing creates only `reactivation_campaigns` plus `reactivation_deliveries(status='queued')`.

- [x] **Step 2: Implement one transaction and three allowlisted SQL predicates**

Reject unknown segments before SQL. Snapshot discount, offer and LLM instruction into the campaign. Insert selected `channel/user_id` rows with a unique `(campaign_id, channel, user_id)` constraint and update recipient counters. Do not import messaging, scheduler or LLM modules.

- [x] **Step 3: Run focused GREEN and commit**

```powershell
docker compose --env-file ../.env -p codex-admin-reactivation --profile test run --rm test pytest -q tests/integration/admin/test_reactivation_database.py
git add project/admin/reactivation_database.py project/tests/integration/admin/test_reactivation_database.py changelog.md
git commit -m "feat: добавлена внутренняя очередь реактивации"
```

### Task 3: Owner-only admin tab

**Files:**
- Create: `project/admin/reactivation_routes.py`
- Create: `project/admin/templates/reactivation.html`
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/static/styles.css`
- Create: `project/tests/unit/admin/test_reactivation_routes.py`

**Interfaces:**
- Routes: `GET /reactivation/`, `POST /reactivation/settings`, `POST /reactivation/consent`, `POST /reactivation/campaigns`.
- Every POST consumes existing `csrf_token`; all routes require role `owner`.

- [x] **Step 1: Add RED route/template tests**

Assert owner access, non-owner 403, CSRF failure, validated numeric bounds, consent grant/revoke, campaign creation redirect, navigation label and active state, Russian table headers, and absence of any send/start button.

- [x] **Step 2: Implement router and single template**

Use native HTML number inputs and textareas. The page contains settings, consent recording, campaign form, queue and journal. Status text explicitly says «Отправка не подключена».

- [x] **Step 3: Run focused GREEN and commit**

```powershell
docker compose --env-file ../.env -p codex-admin-reactivation --profile test run --rm test pytest -q tests/unit/admin/test_reactivation_routes.py tests/unit/test_database_modules.py
git add project/admin/reactivation_routes.py project/admin/templates/reactivation.html project/admin/app.py project/admin/templates/base.html project/admin/static/styles.css project/tests/unit/admin/test_reactivation_routes.py changelog.md
git commit -m "feat: добавлена вкладка реактивации"
```

### Task 4: Privacy deletion, regression and closure

**Files:**
- Modify: `project/admin/customer_data_deletion.py`
- Modify: `project/tests/unit/admin/test_customer_data_deletion.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Customer deletion removes matching `marketing_consents` and `reactivation_deliveries`; campaign aggregates remain non-identifying journal data.

- [x] **Step 1: Add RED deletion assertions**

Seed one consent and delivery for the deleted Telegram customer and unrelated control rows. Assert only target identifiers disappear and deletion verification counts reach zero.

- [x] **Step 2: Implement the two targeted deletes and verification queries**

Keep the existing customer advisory lock and transaction. Do not delete whole campaigns or settings.

- [x] **Step 3: Run Docker gates**

Run focused reactivation/admin/privacy tests, migration upgrade/downgrade/upgrade on the disposable DB, Compose config, compileall, `git diff --check`, and a full suite if the known Router LF baseline can be mounted safely without changing tracked files. Confirm `outbound_messages`, `task_outbox` and `scheduler_jobs` remain empty in reactivation tests.

- [x] **Step 4: Update roadmap/changelog and commit**

```powershell
git add project/admin/customer_data_deletion.py project/tests Дорожная\ карта.md changelog.md
git commit -m "test: закрыта безопасность реактивации"
```

- [x] **Step 5: Review common integration conflicts**

Compare branch against `main` and report expected conflicts in `project/admin/app.py`, `project/admin/templates/base.html`, migration revision `0020`, `Дорожная карта.md` and `changelog.md`. Do not merge or push.
