# Staging-only Admin Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать нерабочие локальные `admin/admin` и оставить server-only staging `.env` единственным источником рабочего доступа к админке.

**Architecture:** Базовый Compose и локальный `.env` fail-closed оставляют bootstrap credentials пустыми. Auth принимает sessionless bootstrap-cookie только при полностью настроенных credentials и session secret длиной не меньше 32 символов. `AGENTS.md` фиксирует границу: staging credentials читаются только на staging-сервере и никогда не берутся из локального `.env`.

**Tech Stack:** Docker Compose, pytest, Markdown, ignored `.env`.

## Global Constraints

- Staging runtime и server-only `/opt/moroz-staging/.env` не изменять.
- Production и GitHub не затрагивать.
- Секреты не выводить в тесты, логи, Git или чат.

---

### Task 1: Fail-closed локальный bootstrap и единый источник правды

**Files:**
- Modify: `project/tests/unit/test_staging.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/.env.example`
- Modify: `AGENTS.md`
- Modify outside Git: `D:/AI_Projects/moroz_i_solntse/moroz-i-solntse-bot/.env`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Compose environment interpolation for `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
- Produces: empty local defaults; staging override remains mandatory in `docker-compose.staging.yml`.

- [x] **Step 1: Write the failing contract test**

Add a test that rejects `ADMIN_USERNAME: ${ADMIN_USERNAME:-admin}` and `ADMIN_PASSWORD: ${ADMIN_PASSWORD:-admin}`, requires empty base defaults, and keeps staging `:?set` requirements.

- [x] **Step 2: Verify RED in Docker**

Run focused `test_staging.py` in the project test container. Expected: the new contract fails on both legacy Compose defaults.

- [x] **Step 3: Implement the minimum fix**

Change base Compose defaults to empty values, reject bootstrap cookies when bootstrap configuration is absent or unsafe, empty the example/local login values, and add the staging-only source-of-truth rule to `AGENTS.md`.

- [x] **Step 4: Verify GREEN and staging invariants**

Run the focused Docker test, Compose render with local empty values, and read-only staging check proving its username remains non-empty and `admin_users=0` without printing a password.

- [x] **Step 5: Record and commit**

Update roadmap/changelog, run `git diff --check`, and create one local commit without push.
