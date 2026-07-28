# Admin Session Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production admin requests validate DB-backed sessions server-side instead of trusting only the signed cookie payload.

**Architecture:** Keep the existing FastAPI/Jinja2 admin and `0009_production_admin` tables. Add minimal repository functions for active-session lookup, last-seen update, and session deletion; make auth resolve the signed cookie through Postgres when `sid` is present.

**Tech Stack:** Python 3.12, FastAPI, asyncpg pool through existing `database._pool`, itsdangerous signed cookie, Docker-only pytest.

## Global Constraints

- No new services, dependencies, providers, or external mutations.
- Start from existing Phase 7 migration `0009_production_admin`; do not add migration `0010` for this code-only fix.
- Keep bootstrap fallback fail-closed on default `admin/admin` and default `ADMIN_SESSION_SECRET`.
- Run tests only through Docker Compose.
- Update `Дорожная карта.md` and `changelog.md`.

---

### Task 1: Repository Session Contract

**Files:**
- Modify: `project/admin/user_repository.py`
- Test: `project/tests/unit/admin/test_auth.py`

**Interfaces:**
- Produces: `get_active_session(session_id: str) -> dict[str, Any] | None`
- Produces: `delete_session(session_id: str) -> None`

- [ ] Write failing tests for valid session, expired session, disabled user, and deleted/missing session.
- [ ] Run focused Docker RED: `pytest tests/unit/admin/test_auth.py -q`.
- [ ] Implement `get_active_session` with an `admin_sessions` + `admin_users` join, `expires_at > now()`, `enabled = TRUE`, and `last_seen_at = now()`.
- [ ] Implement `delete_session`.
- [ ] Run focused Docker GREEN.

### Task 2: Async Auth And Routes

**Files:**
- Modify: `project/admin/auth.py`
- Modify: `project/admin/app.py`
- Modify: `project/admin/bot_control_routes.py`
- Modify: `project/admin/eval_routes.py`
- Modify: `project/admin/logs_routes.py`
- Modify: `project/admin/prompt_routes.py`
- Modify: `project/admin/review_routes.py`
- Test: `project/tests/e2e/admin/test_auth.py`

**Interfaces:**
- Consumes: `user_repository.get_active_session`
- Consumes: `user_repository.delete_session`
- Produces: `async def get_current_user(request: Request) -> AuthenticatedUser`

- [ ] Write failing route/auth tests showing stale `sid` is rejected and logout deletes the DB session.
- [ ] Run focused Docker RED.
- [ ] Change `get_current_user` to async and validate `sid` through the repository.
- [ ] Update admin routes to `await get_current_user(request)`.
- [ ] Update logout to delete the current `sid` when present.
- [ ] Run focused Docker GREEN.

### Task 3: Verification And Docs

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: passed focused gates from Tasks 1-2.

- [ ] Run `git diff --check`.
- [ ] Run focused admin/session Docker gates.
- [ ] Attempt full Docker suite if Docker/RabbitMQ is healthy.
- [ ] Record exact verification output and any infrastructure limitation in roadmap/changelog.
- [ ] Commit one local logical fix.
