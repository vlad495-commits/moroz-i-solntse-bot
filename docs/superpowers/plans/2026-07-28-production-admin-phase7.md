# Production Admin Phase 7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first production-safe admin foundation on top of the existing FastAPI/Jinja2 admin container.

**Architecture:** Keep the current admin service and server-rendered templates. Add one schema migration, small security/repository modules, and update existing routes to use DB-backed sessions, TOTP, CSRF, RBAC and audit.

**Tech Stack:** Python 3.12, FastAPI 0.136.1, Starlette 1.0.0, Jinja2 3.1.6, asyncpg 0.31.0, PostgreSQL 16, Alembic 1.18.5, Docker Compose, pytest.

## Global Constraints

- Start from `main` commit `5e57f2f7ae88e81b2c6fbe92c8c67171ec643e8f`.
- Current migration head is `0008_yclients_lifecycle`; the next migration is `0009_production_admin.py`.
- Work only locally through Docker Compose.
- Do not run staging, production, provider, YCLIENTS, or Telegram mutations.
- Do not push, merge, or create PRs without a separate explicit request.
- Reuse the existing FastAPI/Jinja2 admin stack; do not add a SPA, service, ORM, queue, or new dependency.
- Roles are exactly `owner` and `admin`.
- TOTP and CSRF are required for DB-backed admin sessions.
- Sensitive PII reads and meaningful admin writes are audited.

---

## File Structure

- Create `project/migrations/versions/0009_production_admin.py` for admin users, sessions and audit events.
- Create `project/admin/security.py` for PBKDF2 password hashes, TOTP and CSRF helpers.
- Create `project/admin/user_repository.py` for admin users and sessions.
- Create `project/admin/audit_repository.py` for append-only audit events.
- Create `project/admin/rbac.py` for small permission checks.
- Modify `project/admin/auth.py` to authenticate DB users, keep a no-user bootstrap fallback, and return an authenticated user object.
- Modify `project/admin/app.py` and existing POST routes to validate CSRF and pass the richer user object to templates.
- Modify `project/admin/templates/login.html` and `project/admin/templates/base.html` for TOTP, CSRF and role display.
- Add focused tests under `project/tests/unit/admin/` and `project/tests/e2e/admin/`.
- Update `Дорожная карта.md` and `changelog.md`.

## Task 1: Security Helpers And Migration

**Files:**
- Create: `project/admin/security.py`
- Create: `project/migrations/versions/0009_production_admin.py`
- Test: `project/tests/unit/admin/test_security.py`
- Test: `project/tests/unit/admin/test_migration_0009.py`

**Interfaces:**
- Produces: `hash_password(password: str, *, salt: bytes | None = None) -> str`
- Produces: `verify_password(encoded_hash: str, password: str) -> bool`
- Produces: `verify_totp(secret: str, code: str, *, now: int | None = None, window: int = 1) -> bool`
- Produces: `new_csrf_token() -> str`
- Produces: Alembic revision id `0009_production_admin`, down revision `0008_yclients_lifecycle`

- [ ] **Step 1: Write failing tests**

```python
def test_password_hash_verifies_and_rejects_wrong_password():
    encoded = security.hash_password("correct horse")
    assert security.verify_password(encoded, "correct horse")
    assert not security.verify_password(encoded, "wrong")


def test_totp_accepts_current_code_and_rejects_wrong_code():
    secret = "JBSWY3DPEHPK3PXP"
    code = security._totp_code(secret, 59)
    assert security.verify_totp(secret, code, now=59, window=0)
    assert not security.verify_totp(secret, "000000", now=59, window=0)


def test_admin_migration_follows_0008():
    assert migration.revision == "0009_production_admin"
    assert migration.down_revision == "0008_yclients_lifecycle"
```

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest project/tests/unit/admin/test_security.py project/tests/unit/admin/test_migration_0009.py -q`

Expected: fail because modules/migration do not exist.

- [ ] **Step 3: Implement minimal helpers and migration**

Use `hashlib.pbkdf2_hmac`, `hmac.compare_digest`, `base64.b32decode`, `struct.pack`, `secrets.token_urlsafe`, and Alembic `op.create_table`.

- [ ] **Step 4: Run green**

Run the same Docker pytest command.

- [ ] **Step 5: Commit**

Commit: `feat: add production admin security schema`

## Task 2: DB Users, Sessions And Login

**Files:**
- Create: `project/admin/user_repository.py`
- Modify: `project/admin/auth.py`
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/login.html`
- Test: `project/tests/e2e/admin/test_auth.py`

**Interfaces:**
- Consumes: Task 1 security helpers and migration tables.
- Produces: `AuthenticatedUser(id: int | None, username: str, role: str, csrf_token: str | None)`
- Produces: async `authenticate_db_user(username: str, password: str, totp_code: str) -> AuthenticatedUser | None`
- Produces: async `create_db_session(user_id: int, ttl_seconds: int) -> tuple[str, str]`
- Produces: `get_current_user(request: Request) -> AuthenticatedUser`

- [ ] **Step 1: Write failing e2e tests**

Cover successful DB login with TOTP, wrong TOTP rejection, disabled user rejection, signed cookie creation and env fallback only when there are no DB users.

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest project/tests/e2e/admin/test_auth.py -q`

Expected: fail because DB-backed auth is not implemented.

- [ ] **Step 3: Implement minimal login**

Keep `/login` and `/logout`. Add `totp_code: str = Form("")` to login submit. Store the opaque `session_id` in the existing cookie name.

- [ ] **Step 4: Run green**

Run the same Docker pytest command.

- [ ] **Step 5: Commit**

Commit: `feat: add db backed admin login`

## Task 3: CSRF, RBAC And Audit

**Files:**
- Create: `project/admin/rbac.py`
- Create: `project/admin/audit_repository.py`
- Modify: `project/admin/prompt_routes.py`
- Modify: `project/admin/bot_control_routes.py`
- Modify: `project/admin/eval_routes.py`
- Modify: `project/admin/review_routes.py`
- Modify: `project/admin/app.py`
- Modify: relevant templates with hidden `csrf_token`
- Test: `project/tests/e2e/admin/test_csrf_rbac_audit.py`

**Interfaces:**
- Consumes: `AuthenticatedUser`
- Produces: `require_role(user: AuthenticatedUser, allowed: set[str]) -> None`
- Produces: `validate_csrf(request: Request, token: str) -> None`
- Produces: `record_audit(actor_id: int | None, action: str, object_type: str, object_id: str | None, before: dict | None, after: dict | None, request: Request) -> None`

- [ ] **Step 1: Write failing tests**

Cover POST without CSRF returns 403, owner-only write succeeds, admin write is denied where required, and chat detail read writes an audit event.

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest project/tests/e2e/admin/test_csrf_rbac_audit.py -q`

- [ ] **Step 3: Implement minimal checks**

Add a small shared form helper and call it only from existing POST routes. Audit chat detail reads and bot/prompt/eval/review write actions.

- [ ] **Step 4: Run green**

Run the same Docker pytest command.

- [ ] **Step 5: Commit**

Commit: `feat: add admin csrf rbac and audit`

## Task 4: Documentation, Roadmap And Completion Gate

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-07-14-production-v1-admin.md` if needed to point to the updated `0009` plan.

- [ ] **Step 1: Update roadmap and changelog**

Mark Phase 7 started and record implemented tasks with UTC+3 timestamp.

- [ ] **Step 2: Run focused admin gate**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest project/tests/unit/admin project/tests/e2e/admin -q`

- [ ] **Step 3: Run migration/profile gate**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest project/tests/integration/test_migrations.py project/tests/unit/test_migration_profile.py -q`

- [ ] **Step 4: Run full Docker suite**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest -q`

- [ ] **Step 5: Request independent review**

Review the diff from `5e57f2f7ae88e81b2c6fbe92c8c67171ec643e8f` to current `HEAD`, fix Critical/Important findings, and repeat focused verification.

- [ ] **Step 6: Commit checkpoint**

Commit: `docs: record production admin phase 7 checkpoint`

