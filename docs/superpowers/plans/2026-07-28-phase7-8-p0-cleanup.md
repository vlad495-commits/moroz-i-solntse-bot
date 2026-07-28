# Phase 7–8 P0 Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven local P0 cleanup findings without expanding Phase 7–8 product scope.

**Architecture:** Keep Caddy prefix stripping and make FastAPI/Jinja/browser URLs root-path aware. Enforce secure admin cookies through the existing production validator and switch rollback through explicit Compose image variables.

**Tech Stack:** Python 3.12, FastAPI, Starlette, Jinja2, Caddy, Docker Compose, pytest.

## Global Constraints

- Start from acceptance commit `c7a34e8` on local branch `codex/phase7-8-readiness`.
- Use Docker for every project test and runtime validation.
- Do not add services or dependencies.
- Do not mutate staging, production, providers, YCLIENTS, or Telegram.
- Do not merge or push.
- Update `Дорожная карта.md`, `План реализации.md`, affected phase plans, and `changelog.md`.

## Task 1: Public `/admin` prefix and secure-cookie contract

**Files:**
- Modify: `project/admin/app.py`
- Modify: `project/admin/*_routes.py`
- Modify: `project/admin/templates/*.html`
- Modify: `project/docker-compose.prod.yml`
- Modify: `project/ops/validate_env.py`
- Test: `project/tests/e2e/admin/test_public_prefix.py`
- Test: `project/tests/unit/ops/test_validate_env.py`

- [ ] Add tests asserting `/admin` redirects and rendered links/forms/assets/browser requests.
- [ ] Run the focused Docker tests and confirm RED on current absolute URLs.
- [ ] Configure `ADMIN_ROOT_PATH=/admin`, build URLs from request root path, and pass `ADMIN_COOKIE_SECURE`.
- [ ] Require the exact production value `true` in the validator.
- [ ] Re-run the focused Docker tests and confirm GREEN.
- [ ] Commit as one logical admin ingress/security fix.

## Task 2: Executable image rollback

**Files:**
- Modify: `project/docker-compose.yml`
- Modify: `project/ops/rollback-runbook.md`
- Test: `project/tests/e2e/ops/test_runbooks.py`

- [ ] Add a failing test for explicit bot/worker/scheduler/admin image variables and the `--no-build` rollback command.
- [ ] Run the focused Docker test and confirm RED.
- [ ] Add the four existing-service image variables and exact immutable-image override command.
- [ ] Re-run the focused Docker test and production Compose render with previous-image overrides.
- [ ] Commit as one logical rollback fix.

## Task 3: Honest readiness documentation and cleanup

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `План реализации.md`
- Modify: `docs/superpowers/plans/2026-07-28-production-admin-phase7.md`
- Modify: `docs/superpowers/plans/2026-07-14-production-v1-operations.md`
- Modify: `changelog.md`

- [ ] Mark Phase 7–8 local-ready and production launch blocked.
- [ ] State the actual Production Admin boundary and list missing health, booking, knowledge, and escalation capabilities.
- [ ] Remove trailing blank-line/whitespace findings and run `git diff --check`.
- [ ] Commit the documentation truth pass.

## Task 4: Completion gates and review

- [ ] Run targeted admin/ops Docker gates.
- [ ] Run production Compose `config --quiet`.
- [ ] Run full Docker pytest if feasible.
- [ ] Run `git diff --check`.
- [ ] Remove only the Docker namespace created by this task and prove zero leftover containers, volumes, networks, and task-tagged images.
- [ ] Request independent review against all seven findings and fix any Critical/Important issue before reporting.
