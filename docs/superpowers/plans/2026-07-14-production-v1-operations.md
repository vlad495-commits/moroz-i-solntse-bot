# Production Operations and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подготовить воспроизводимый production-деплой, мониторинг, алерты, backup/restore, нагрузочные проверки и финальный launch gate.

**Architecture:** Docker Compose на клиентском VPS, Caddy для TLS, healthchecks и ограниченные ресурсы. Скрипты backup/restore и runbooks лежат в Git без секретов; фактические секреты — только на сервере.

**Tech Stack:** Docker Compose, Caddy, PostgreSQL tools, Prometheus-compatible metrics, Telegram alerts, pytest, k6.

## Global Constraints

- Российский VPS, клиентский домен, TLS.
- Никаких секретов в Git/логах.
- Ручная поддержка 10:00–21:00; мониторинг 24/7.
- Backup считается рабочим только после restore drill.
- Production невозможен при любом открытом launch gate.

---

### Task 1: Production Compose, TLS and secret validation

**Files:** Create `project/docker-compose.prod.yml`, `project/ops/Caddyfile`, `project/ops/validate_env.py`, `.env.example`; Test `project/tests/unit/ops/test_validate_env.py`.

- [ ] Test rejection of default admin password, short session secret, missing webhook secret/YCLIENTS keys and HTTP public URL.
- [ ] Run red.
- [ ] Implement allowlisted production env validation and Caddy routes `/telegram/webhook` → bot, `/admin/*` → admin; do not expose stores/queues publicly.
- [ ] Run test and `docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet`; expect pass with test env.
- [ ] Commit `ops: добавлены production compose TLS и проверка секретов`.

### Task 2: Metrics and alert routing

**Files:** Create `project/src/moroz/common/metrics.py`, `project/src/moroz/common/alerts.py`; Create `project/admin/metrics_routes.py`; Test `project/tests/integration/test_alerts.py`.

- [ ] Test threshold alerts, dedup/cooldown, technical recipient and business-critical recipient routing.
- [ ] Run red.
- [ ] Implement counters/gauges for latency, errors, queue/DLQ age, integration health, token cost and unhandled requests; alerts use `alert:{code}:{subject}` dedupe keys.
- [ ] Run tests; expect one alert per cooldown and no PII in payload.
- [ ] Commit `ops: добавлены метрики и Telegram-алерты`.

### Task 3: Backup and restore drill

**Files:** Create `project/ops/backup-postgres.sh`, `restore-postgres.sh`, `verify-backup.sh`, `backup-runbook.md`; Test `project/tests/e2e/ops/test_restore.py`.

- [ ] Write test inserting a marker, creating backup, deleting test DB, restoring and finding marker.
- [ ] Run red because scripts are absent.
- [ ] Implement `pg_dump --format=custom`, encryption via server-provided key, checksum, retention 30 daily/12 monthly and restore into a new database before swap.
- [ ] Run restore drill; expect marker and Alembic head present.
- [ ] Commit `ops: добавлены проверяемые backup и restore`.

### Task 4: Smoke, load and failure tests

**Files:** Create `project/ops/smoke.ps1`, `project/ops/load/k6.js`, `project/tests/e2e/ops/test_degradation.py`; Modify `project/llm/eval/dataset.json` if final critical cases missing.

- [ ] Encode smoke for health/privacy/FAQ/mock booking/admin login; load for 30 inbound/min and 20 active chats; degradation for Redis/RabbitMQ/YCLIENTS/primary LLM outages.
- [ ] Run baseline and capture failures.
- [ ] Fix only failures within already approved behavior; no new features.
- [ ] Re-run: expect 100% critical evals, ≥95% total, no lost confirmed state, visible delay status, recovery after component restart.
- [ ] Commit `test: добавлены production smoke load и failure gates`.

### Task 5: Release, rollback and incident runbooks

**Files:** Create `project/ops/deploy-runbook.md`, `rollback-runbook.md`, `incident-runbook.md`, `launch-checklist.md`; Modify `Дорожная карта.md`, `changelog.md`.

- [ ] Document exact clone/pull, env placement, migrate, build, smoke, traffic switch and log commands.
- [ ] Document rollback to previous image and restore decision tree; forbid destructive downgrade without backup.
- [ ] Fill launch checklist with named evidence for YCLIENTS, legal texts, staff, TOTP, rotated secrets, TLS, evals, load, alerts and restore.
- [ ] Perform staging rehearsal; every command must exit 0 and be recorded.
- [ ] Commit `docs: добавлены production runbooks и launch gate`.

### Task 6: Final production acceptance

- [ ] Run full Docker pytest and eval suites; expect thresholds.
- [ ] Run smoke/load/degradation suite against staging.
- [ ] Restore latest encrypted backup into isolated DB and verify.
- [ ] Confirm all ten launch gates signed; otherwise stop at staging and list exact blockers.
- [ ] If all gates pass, deploy production, run smoke, record image digests and commit `release: принят Telegram production v1`; never push without explicit user request.
