# Customer-owned YCLIENTS Production App Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Browser actions that save settings, create credentials or change permissions require action-time confirmation.

**Goal:** Создать контролируемое заказчиком бесплатное непубличное YCLIENTS-приложение, безопасно подключить его к staging, доказать read-only sync и один согласованный booking lifecycle, затем допустить credentials к production release gate.

**Architecture:** Текущий single-tenant worker использует статические `YCLIENTS_PARTNER_TOKEN`, `YCLIENTS_USER_TOKEN` и `YCLIENTS_COMPANY_ID`. Customer-owned приложение предоставляет системного пользователя с минимальными правами только к одному филиалу; staging первым выполняет bounded acceptance, а production получает credentials только после успешного read-only, synthetic lifecycle и release gates.

**Tech Stack:** YCLIENTS developer cabinet/API v2, Docker Compose, existing `YclientsConfig`, `YclientsAdapter`, projection scheduler, `yclients-smoke`, server-only `.env`, PostgreSQL projection and safe-log tooling.

## Global Constraints

- Design source: `docs/superpowers/specs/2026-08-20-customer-owned-yclients-app-design.md`.
- Текущее приложение разработчика остаётся dev/test; его tokens не копируются в production.
- Production-приложение принадлежит заказчику, тип `Непубличное`, монетизация `Бесплатное`.
- Пароль, TOTP, Partner Token, User Token, test phone, raw provider body и ПД запрещены в Git, чатах, документации, screenshots, changelog и command output.
- Любое изменение в кабинете YCLIENTS выполняется только после action-time подтверждения заказчика/владельца.
- До отдельного разрешения допустимы только GET/read-only вызовы.
- Mutation smoke создаёт ровно одну синтетическую запись и не читает, не изменяет и не удаляет чужие записи.
- Blind retry после неопределённого POST/PUT/DELETE запрещён; такой результат требует ручной проверки.
- Docker-only; прямой запуск Python на хосте запрещён.
- Production rollout запрещён до закрытия YCLIENTS, owner TOTP, full Docker, review, staging acceptance и rollback gates.
- Если onboarding выявит кодовый разрыв, внешний rollout останавливается; исправление выполняется отдельным TDD-планом, а не ad hoc на сервере.

## Files and systems

- Read: `project/src/moroz/booking/yclients_http.py` — production config boundary.
- Read: `project/src/moroz/booking/yclients.py` — availability and protected lifecycle adapter.
- Read: `project/src/moroz/booking/yclients_records.py` — records-list projection reader.
- Read: `project/src/moroz/booking/yclients_sandbox_smoke.py` — consented lifecycle smoke.
- Read: `project/src/moroz/booking/projection.py` — scheduler projection contract.
- Modify throughout execution: `Дорожная карта.md`, `changelog.md` — status and append-only evidence.
- External: customer YCLIENTS developer cabinet and one production branch.
- External secret store: `/opt/moroz-staging/.env`, later the production server-only env.
- Server rollback artifact: timestamped directory under `/opt/moroz-staging/backups/`; never Git.
- Runtime source changes: none expected. Any required source change becomes a separate reviewed plan.

---

### Task 1: Freeze prerequisites and local baseline

**Consumes:** approved customer-owned app design and current merged Telegram Production V1.

**Produces:** verified code baseline, named human participants, safe test inputs and a no-mutation starting point.

- [ ] **Step 1: Verify repository identity and clean task scope**

Run from repository root:

```powershell
git rev-parse HEAD
git status --short --branch
git log -3 --oneline
```

Expected: exact intended main commit; only approved onboarding progress files may be modified. Do not push.

- [ ] **Step 2: Run the focused Docker baseline**

Run from `project/`:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/contract/booking `
  tests/unit/booking/test_yclients_sandbox_smoke.py `
  tests/integration/booking/test_yclients_projection.py `
  tests/unit/test_worker.py
```

Expected: exit `0`; no external YCLIENTS call because tests use fake/local transports.

- [ ] **Step 3: Confirm the customer session inputs without secrets**

Record only these non-secret facts in the roadmap/changelog:

- customer representative who can manage YCLIENTS users/rights;
- exact target branch name and numeric `company_id` presence, without client data;
- one test service and employee selected by the customer;
- two future test windows where a synthetic booking cannot disrupt operations;
- explicit owner responsible for the final mutation consent.

Do not record login, phone, tokens or TOTP.

- [ ] **Step 4: Commit the baseline checkpoint**

```powershell
git add -- 'Дорожная карта.md' changelog.md
git commit -m "docs: начать подключение YCLIENTS заказчика"
```

---

### Task 2: Create the customer-owned private application

**Consumes:** customer session from Task 1.

**Produces:** one free private application controlled by the customer; no credentials leave the approved secret path.

- [ ] **Step 1: Customer signs in and opens the developer cabinet**

The customer performs authentication and TOTP personally. Technical assistance may navigate only after explicit approval; credentials are never typed into chat or stored by the project.

- [ ] **Step 2: Create the production application**

Set and save:

- application name: `Moroz i Solntse Telegram Bot — Production`;
- application type: `Непубличное`;
- monetization type: `Бесплатное`;
- category: `Онлайн-запись`.

Do not configure paid trial, marketplace publication, multi-branch onboarding, iframe registration or user-data transfer.

- [ ] **Step 3: Verify ownership and isolation read-only**

Confirm visibly, without copying token values:

- application appears in the customer-controlled developer cabinet;
- type is private and monetization is free;
- application ID and User Token fields exist;
- current developer-owned dev/test application is unchanged.

- [ ] **Step 4: Record safe evidence**

Changelog may contain only `customer-owned app created`, private/free booleans and the date. Application IDs, tokens, user names and screenshots are excluded.

---

### Task 3: Configure least-privilege access and the booking marker

**Consumes:** private production application from Task 2.

**Produces:** system user rights sufficient for current endpoints and one exact `moroz_booking_key` field.

- [ ] **Step 1: Select only the approved permission allowlist**

Enable:

- `Журнал записи` → `Неограниченный доступ к истории расписания и записей`, `Перенос записи`;
- `Форма записи` → `Доступ к данным клиентов`, `Доступ к созданию новых клиентов в записи`, `Просмотр дополнительных полей записи`, `Создавать записи`, `Изменять записи`, `Изменять дополнительные поля записи`, `Изменять комментарий`, `Изменять состав услуг`, `Удалять записи`;
- `Настройки` → read access to `Услуги` and `Сотрудники`;
- `Раздел «Обзор»` → `Просматривать список записей`.

Keep client database export, phone-list visibility, finance, payments, loyalty, warehouse, messaging, users and system administration disabled.

- [ ] **Step 2: Save permissions only after action-time confirmation**

Before clicking `Сохранить`, show the final permission summary to the customer. After saving, re-open the page and verify the same selected set.

- [ ] **Step 3: Create the exact additional record field**

In the customer branch create:

- code: `moroz_booking_key`;
- type: text;
- editable by the application system user;
- hidden from normal customer-facing flows where YCLIENTS allows it.

Do not repurpose an existing field with another code or business meaning.

- [ ] **Step 4: Stop on any permission mismatch**

If YCLIENTS uses different labels or refuses a required setting, capture only the label and HTTP/status class, update changelog, and stop. Do not broaden rights speculatively.

---

### Task 4: Transfer secrets to staging with exact rollback

**Consumes:** Partner Token, User Token and `company_id` from the customer-owned app; customer-approved test service/name/phone exist only in the secure execution channel.

**Produces:** staging worker bound to customer YCLIENTS plus a restorable prior env snapshot.

- [ ] **Step 1: Read-only staging preflight**

Using the deploy workflow, verify exact `/opt/moroz-staging`, current commit/tag, app image IDs, schema head, health and owner/mode of `/opt/moroz-staging/.env`. Output only safe booleans/counts.

- [ ] **Step 2: Create a protected env backup**

Create one timestamped backup directory inside `/opt/moroz-staging/backups/`, copy only the current `.env`, retain original owner/mode, and record only backup path and SHA-256. Never print file contents.

- [ ] **Step 3: Replace the exact YCLIENTS keys atomically**

Through suppressed non-echoing input, replace only:

```text
YCLIENTS_PARTNER_TOKEN
YCLIENTS_USER_TOKEN
YCLIENTS_COMPANY_ID
YCLIENTS_BASE_URL
YCLIENTS_TIMEZONE
YCLIENTS_TIMEOUT_SECONDS
YCLIENTS_TEST_SERVICE_ID
YCLIENTS_TEST_NAME
YCLIENTS_TEST_PHONE
```

Keep `YCLIENTS_SANDBOX_CONSENT` unset. Verify presence/non-empty state, numeric `company_id`, URL host, timezone and file permissions without values.

- [ ] **Step 4: Recreate only the worker for read-only acceptance**

Use the already pinned staging image and canonical Compose project. Recreate `worker`; do not rebuild, migrate, restart stores or start `yclients-smoke`. Verify worker health and fresh safe-log counters.

- [ ] **Step 5: Prove env rollback before external reads**

Document the exact command sequence that restores the backup `.env`, preserves owner/mode and recreates only worker. Do not execute rollback unless acceptance fails or a rehearsal is explicitly authorized.

---

### Task 5: Run the read-only YCLIENTS acceptance gate

**Consumes:** staging worker with customer-owned credentials and no mutation consent.

**Produces:** redacted proof of permissions, catalog, records-list, projection and scheduler.

- [ ] **Step 1: Probe authorization and catalog with GET only**

Call only official read endpoints for user permissions, services, staff, bookable dates/times, additional record fields and records-list. Output only HTTP status, counts, true/false permission flags and exact `moroz_booking_key` match count; suppress response bodies, IDs, names, phones and tokens.

Expected: every required endpoint returns `200`; `Просматривать список записей` is effective; one exact field exists.

- [ ] **Step 2: Trigger projection through the existing scheduler path**

Allow the existing worker/scheduler contract to enqueue and execute one projection sync. Do not call a mutation endpoint and do not insert provider fixtures.

- [ ] **Step 3: Verify projection safely**

Query PostgreSQL for projection row count, latest sync timestamp, terminal scheduler state and safe failure code. Do not output projected client/staff/service values.

Expected: sync terminal-success, freshness advances, scheduler schedules the next ten-minute job, and no secret/PII log counter increases.

- [ ] **Step 4: Handle failure fail-closed**

For `401/403`, restore the previous env/worker state if the customer binding caused runtime degradation, then fix only the proven missing permission or credential. For malformed/transport/provider errors, keep the gate open and do not proceed to mutation smoke.

- [ ] **Step 5: Commit the read-only evidence**

Update roadmap/changelog with statuses/counts only, then commit:

```powershell
git add -- 'Дорожная карта.md' changelog.md
git commit -m "test: подтвердить YCLIENTS заказчика read-only"
```

---

### Task 6: Run one explicitly consented lifecycle smoke

**Consumes:** fully green Task 5, customer-selected service/staff/windows and fresh explicit mutation consent.

**Produces:** one create/get/reschedule/get/cancel proof with zero active synthetic matches.

- [ ] **Step 1: Obtain action-time consent**

Immediately before starting, confirm the exact branch, test service, test identity and that one synthetic record will be created, moved and cancelled. Without this confirmation, stop.

- [ ] **Step 2: Set mutation consent only for this process**

Pass `YCLIENTS_SANDBOX_CONSENT=yes` as a process-only override to the one `yclients-smoke` run. Do not persist it in `.env`.

- [ ] **Step 3: Execute the existing Docker smoke once**

From the exact deployed source/image context run the equivalent canonical command:

```powershell
docker compose --env-file ../.env --profile yclients-smoke run --rm `
  -e YCLIENTS_SANDBOX_CONSENT=yes yclients-smoke
```

Expected safe summary: services read, two future slots, create/get/reschedule/get/cancel confirmed, `matches=1`, `active_matches=0`, `success=true`, `manual_review_required=false`. Raw provider records and identifiers remain suppressed.

- [ ] **Step 4: Never auto-repeat an uncertain mutation**

If output reports transport uncertainty, unknown outcome, malformed success or manual review, do not rerun and do not guess a DELETE target. Ask the customer to inspect the narrow agreed time window and resolve the synthetic record manually.

- [ ] **Step 5: Verify no active synthetic remnant**

Perform only exact `moroz_booking_key` read-only reconciliation from the smoke. Expected: one historical match and zero active matches.

- [ ] **Step 6: Record and commit safe evidence**

```powershell
git add -- 'Дорожная карта.md' changelog.md
git commit -m "test: подтвердить lifecycle YCLIENTS заказчика"
```

---

### Task 7: Final release gate and production handoff

**Consumes:** Tasks 1–6 green, owner/admin TOTP login proven, remaining non-YCLIENTS release tasks complete.

**Produces:** reviewed production candidate with restorable credentials and customer offboarding control.

- [ ] **Step 1: Run the fresh full Docker release suite**

Use the canonical test-profile with documented read-only mounts. Expected: all tests pass, compile/config gates exit `0`, `git diff --check` clean.

- [ ] **Step 2: Request independent review**

Review exact diff and operational evidence for Critical/Important issues, secret/PII disclosure, permission creep, blind mutation retry and rollback gaps. Fix any finding before continuing.

- [ ] **Step 3: Rehearse staging app/env rollback**

Run candidate → previous → candidate for application images and separately prove the protected env restore procedure without DB downgrade. Final state must be the reviewed candidate with customer-owned credentials and all services healthy.

- [ ] **Step 4: Copy credentials to production through the secure channel**

Create the production server-only backup first, replace only the approved YCLIENTS keys, verify presence/mode without values, and deploy the exact reviewed immutable image/tag. No production smoke may create another synthetic booking.

- [ ] **Step 5: Run production read-only smoke**

Verify health, HTTPS/webhook, permissions, projection freshness, scheduler terminal state and safe logs. Do not repeat lifecycle mutations in production.

- [ ] **Step 6: Document rotation and offboarding**

The customer can revoke the production app. Technical handoff records how to rotate both tokens, restore prior server env, disable YCLIENTS operations and keep FAQ/admin handoff working. No secret values enter the handoff document.

- [ ] **Step 7: Mark release complete only after every gate is evidenced**

Update `Дорожная карта.md`, `changelog.md` and the final launch checklist. Merge/push/deploy actions require their normal authorization and must reference the exact commit/tag.

