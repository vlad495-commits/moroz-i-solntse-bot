# Staging Admin Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Включить существующую админку за staging HTTPS, развернуть актуальный
`main` и остановиться после согласованного technical smoke.

**Architecture:** Staging overlay собирает commit-tagged admin без отдельного
host-порта, а существующий Caddy публикует его под `/admin/`. До реального
upgrade предыдущие bot/worker images проверяются с candidate head в полностью
изолированном Compose project.

**Tech Stack:** Docker Compose, Caddy, FastAPI, PostgreSQL 16, Alembic, Redis,
RabbitMQ, pytest.

## Global Constraints

- Production и не-staging контейнеры не изменяются.
- Scheduler, YCLIENTS smoke, уведомления и дополнительные каналы выключены.
- Секреты остаются только в `/opt/moroz-staging/.env` и не печатаются.
- Код доставляется через Git, runtime запускается только через Docker.
- Любой blocker останавливает rollout без несвязанных исправлений.
- После зелёного technical smoke работа останавливается.

---

### Task 1: Staging admin и HTTPS contract

**Files:**
- Modify: `project/tests/unit/test_staging.py`
- Modify: `project/docker-compose.staging.yml`
- Modify: `project/ops/staging/Caddyfile`

**Interfaces:**
- Consumes: base service `admin` и его `/login` healthcheck.
- Produces: commit-tagged staging admin, доступный только через `/admin/`.

- [ ] **Step 1: Write failing tests**

Добавить точные assertions:

```python
assert services["admin"]["image"] == (
    "moroz-staging-admin:${STAGING_IMAGE_TAG:?set STAGING_IMAGE_TAG}"
)
assert services["admin"]["ports"] == []
assert services["admin"]["environment"] == {
    "ADMIN_ROOT_PATH": "/admin",
    "ADMIN_COOKIE_SECURE": "true",
    "ADMIN_USERNAME": "${ADMIN_USERNAME:?set ADMIN_USERNAME}",
    "ADMIN_PASSWORD": "${ADMIN_PASSWORD:?set ADMIN_PASSWORD}",
    "ADMIN_SESSION_SECRET": "${ADMIN_SESSION_SECRET:?set ADMIN_SESSION_SECRET}",
}
assert services["scheduler"]["profiles"] == ["disabled-in-staging"]
```

Отдельно потребовать redirect `/admin`, `handle_path /admin/*`,
`reverse_proxy admin:8080` и `depends_on.admin.condition == service_healthy`.

- [ ] **Step 2: Run RED**

Запустить `project/tests/unit/test_staging.py` в Compose test container.

Expected: focused failures, потому что staging отключает admin и Caddy не имеет
маршрута `/admin`.

- [ ] **Step 3: Implement minimum**

Задать admin image и пять environment fields выше, очистить inherited ports,
убрать только admin disabled profile. Scheduler оставить disabled. Добавить
production-compatible Caddy redirect/proxy и healthy dependency.

- [ ] **Step 4: Run GREEN**

Повторить focused staging tests. Expected: все selected tests проходят.

### Task 2: Repeatable safety gates

**Files:**
- Modify: `project/tests/unit/test_staging.py`
- Modify: `project/ops/staging-runbook.md`

**Interfaces:**
- Produces: повторяемые compatibility, health privacy и stop gates.

- [ ] **Step 1: Write failing runbook tests**

Потребовать в runbook:

```python
assert "moroz-staging-compat-" in text
assert "STAGING_PREVIOUS_IMAGE_TAG" in text
assert '{"status":"ok"}' in text
assert "/admin/login" in text
```

Ordering assertion ставит compatibility до реального `alembic upgrade head`;
stop boundary идёт сразу после technical smoke.

- [ ] **Step 2: Run RED**

Запустить focused runbook tests. Expected: failures на отсутствующих новых gates.

- [ ] **Step 3: Add minimum runbook commands**

Описать exact isolated project `moroz-staging-compat-${candidate_tag}`:

1. поднять отдельные PostgreSQL, Redis и RabbitMQ;
2. применить candidate migration head;
3. override bot/worker на `STAGING_PREVIOUS_IMAGE_TAG`;
4. использовать незанятый loopback bot port;
5. потребовать healthy bot и worker;
6. удалить только exact compatibility project и его volumes.

Добавить проверки exact public health body, admin login/redirect, webhook `403`,
unrelated `404`, webhook status и safe log scan. После них — явный stop.

- [ ] **Step 4: Run GREEN**

Повторить focused staging tests. Expected: все selected tests проходят.

### Task 3: Local quality gate и publication

**Files:**
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

- [ ] **Step 1: Run focused evidence**

Повторить staging и health endpoint tests в Docker.

- [ ] **Step 2: Run full quality gate**

Запустить полный Docker pytest, compileall, staging Compose render, Caddy
validate, Ruff `E9,F` и `git diff --check`. Expected: zero failures и exit `0`.

- [ ] **Step 3: Record and commit**

Записать safe evidence в changelog/roadmap и закоммитить минимальную реализацию
в `main`.

- [ ] **Step 4: Push**

Fetch, clean-main и fast-forward checks, затем push `main` в `origin/main`.
Локальный и remote commit IDs обязаны совпасть.

### Task 4: Staging pre-rollout и compatibility

**Files:** только защищённое состояние `/opt/moroz-staging`.

- [ ] **Step 1: Reconfirm rollback**

Проверить previous commit `b5ce49d`, images `yclients-7e2ec278ed7`, checksum
encrypted dump, env backup, disk, Docker и ownership.

- [ ] **Step 2: Fetch and build**

Fetch `origin/main`; остановиться, если переход не fast-forward. Не меняя running
services, собрать immutable candidate bot/worker/migrate/admin и просканировать
image metadata на секреты.

- [ ] **Step 3: Prepare credentials**

Сгенерировать отсутствующие non-default admin username/password/session secret
прямо в mode-600 staging `.env`, не печатая значения.

- [ ] **Step 4: Prove compatibility**

Выполнить isolated compatibility project из Task 2. Если previous bot или worker
не healthy с candidate head, остановиться до реальной миграции.

### Task 5: Rollout и technical smoke

**Files:** только существующий staging checkout.

- [ ] **Step 1: Update and migrate**

Fast-forward checkout на опубликованный `main`, поднять stores, выполнить
`alembic upgrade head` и подтвердить `alembic current == head`.

- [ ] **Step 2: Start required services**

Поднять только bot, worker и admin, проверить Caddy config и запустить staging
ingress. Scheduler и YCLIENTS profiles не включать.

- [ ] **Step 3: Configure webhook**

Проверить dedicated bot identity, установить HTTPS webhook без удаления pending
updates и потребовать чистый final status.

- [ ] **Step 4: Agreed technical smoke**

Проверить required containers, loopback health, exact public
`{"status":"ok"}`, admin login/redirect, TLS, webhook `403`, unrelated `404`,
Alembic head, webhook status и safe log aggregates.

- [ ] **Step 5: Stop**

Не запускать live canary, ручные сценарии, recovery drills, исправления или
дополнительные тесты. Подготовить итоговый отчёт и rollback-команды.
