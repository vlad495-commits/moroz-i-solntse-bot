# Production Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить текущую тестовую FastAPI/Jinja2-панель в безопасную операционную админку собственницы и администраторов.

**Architecture:** Сохраняются FastAPI и серверные шаблоны. DB users, роли, TOTP, CSRF и аудит заменяют общий env-логин; отдельные routers читают shared repositories.

**Tech Stack:** FastAPI, Jinja2, asyncpg, argon2-cffi, pyotp, itsdangerous, pytest/httpx.

## Global Constraints

- Роли только `owner` и `admin`.
- TOTP обязателен; общих аккаунтов нет.
- Каждый просмотр ПД и значимое изменение аудируется.
- Платежи, маркетинг, голос и медицинские анкеты скрыты.
- Eval management остаётся внутренним техническим разделом.

---

### Task 1: DB users, password hash, TOTP and CSRF

**Files:** Create migration `0006_admin_users.py`; Create `project/admin/user_repository.py`, `security.py`; Modify `project/admin/auth.py`, `app.py`, templates `login.html`; Test `project/tests/e2e/admin/test_auth.py`.

- [ ] Test hashed login, missing/wrong TOTP, secure cookie flags, CSRF rejection and disabled user.
- [ ] Run red.
- [ ] Add `admin_users` and `admin_sessions`; implement:

```python
def authenticate(password_hash: str, password: str, totp_secret: str, code: str) -> bool:
    return password_hasher.verify(password_hash, password) and pyotp.TOTP(totp_secret).verify(code, valid_window=1)
```

Session cookie must be `HttpOnly`, `Secure` in production and `SameSite=Lax`; every POST validates per-session CSRF token.

- [ ] Run auth E2E; expect pass.
- [ ] Commit `feat: добавлены индивидуальные аккаунты TOTP и CSRF`.

### Task 2: RBAC and audit

**Files:** Create `project/admin/rbac.py`, `audit_repository.py`; Create migration `0007_admin_audit.py`; Test `project/tests/e2e/admin/test_rbac.py`.

- [ ] Test owner-only user/knowledge management, admin escalation access, admin denial for export/settings, PII view audit.
- [ ] Run red.
- [ ] Implement `require_permission(request, Permission.X)` and append-only `audit_events(actor_id, action, object_type, object_id, before, after, created_at)`.
- [ ] Run tests; expect 403 for denied access and audit rows for allowed sensitive actions.
- [ ] Commit `feat: добавлены RBAC и аудит админки`.

### Task 3: Dialogs, customer card, escalation and human mode UI

**Files:** Create `project/admin/customer_routes.py`, `escalation_routes.py`; Create templates `customer_detail.html`, `escalations.html`; Modify `base.html`; Test `project/tests/e2e/admin/test_operations_ui.py`.

- [ ] Test list/detail, take escalation, internal comment, enable/close human mode and permission checks.
- [ ] Run red.
- [ ] Implement thin routes calling shared repositories; POST actions require CSRF and record audit.
- [ ] Run tests; expect state changes persisted and visible.
- [ ] Commit `feat: добавлены карточка клиента и эскалации`.

### Task 4: Bookings, knowledge and version rollback

**Files:** Create `project/admin/booking_routes.py`, `knowledge_routes.py`, `knowledge_repository.py`; Create migration `0008_knowledge_versions.py`; Create templates `bookings.html`, `knowledge.html`; Modify `base.html`; Test `project/tests/e2e/admin/test_knowledge.py`.

- [ ] Test owner edit/version/rollback, admin read-only, current YCLIENTS status view and no direct local overwrite.
- [ ] Run red.
- [ ] Add `knowledge_items` and `knowledge_versions`; rollback creates a new version rather than deleting history.
- [ ] Run tests; expect version history and correct permissions.
- [ ] Commit `feat: добавлены записи знания и версии`.

### Task 5: Health, scheduler, errors and hidden deferred modules

**Files:** Create `project/admin/health_routes.py`, template `health.html`; Modify `project/admin/llm_status.py`, `base.html`; Test `project/tests/e2e/admin/test_health.py`.

- [ ] Test health summaries for Postgres/Redis/RabbitMQ/worker/scheduler/Telegram/YCLIENTS/LLM, stale heartbeat and no deferred nav links.
- [ ] Run red.
- [ ] Implement bounded-time health probes and display only operational metadata, never secrets.
- [ ] Run tests; expect degraded states rendered without crashing admin.
- [ ] Commit `feat: добавлен production health dashboard`.

### Task 6: Admin checkpoint

- [ ] Run all admin tests in Docker; expect pass.
- [ ] Manually verify owner/admin/TOTP/CSRF using test users.
- [ ] Run accessibility smoke for keyboard login/nav and labels.
- [ ] Update roadmap/changelog.
- [ ] Commit `docs: зафиксирован production admin checkpoint`.
