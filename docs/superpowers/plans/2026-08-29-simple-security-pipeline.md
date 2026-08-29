# Simple Security Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Упростить input-security до current-only `OK/BLOCK`, запустить Router параллельно и сделать semantic output-validator опциональным без ослабления локальных проверок.

**Architecture:** Существующие local guardrails и `PiiSession` остаются. Узкий classifier сам выполняет cheap → reserve → fail-open+alert; `SecurityPipeline` управляет двумя asyncio-задачами и не использует Router до разрешающего security verdict.

**Tech Stack:** Python 3.12, asyncio, aiogram worker, OpenAI/Anthropic adapters, Docker Compose, pytest.

## Global Constraints

- Только Docker для запуска тестов.
- Никаких новых сервисов, зависимостей, verdict cache или security state machine.
- Никаких raw ПД, provider payload или exception text в логах/alert.
- Не выполнять push, deploy, Telegram/YCLIENTS/production действия.

---

### Task 1: Current-only Security classifier

**Files:**
- Modify: `project/src/moroz/security/input_security.py`
- Test: `project/tests/unit/security/test_input_security.py`

- [ ] Заменить JSON/category tests на точные `OK/BLOCK`, current-only request и invalid-output fallback.
- [ ] Запустить тест и подтвердить ожидаемый RED.
- [ ] Реализовать минимальный cheap → reserve classifier с typed internal verdict и safe fail-open alert.
- [ ] Запустить unit-тест до GREEN.

### Task 2: Parallel Router gate

**Files:**
- Modify: `project/src/moroz/security/pipeline.py`
- Test: `project/tests/unit/security/test_pipeline.py`
- Test: `project/tests/e2e/test_security_pipeline.py`

- [ ] Переписать ordering-тесты: обе задачи стартуют, Router не используется до `OK`, `BLOCK` cancel+drain-ит Router.
- [ ] Добавить тест на отсутствие потерянной Router exception и отсутствие history в Security.
- [ ] Запустить тесты и подтвердить RED.
- [ ] Удалить `needs_input_security_review`/`has_context`, запускать Security всегда после local pass и маскирования.
- [ ] Запустить pipeline/e2e tests до GREEN.

### Task 3: Runtime и Evaluation wiring

**Files:**
- Modify: `project/llm/config.py`
- Modify: `project/llm/llm.py`
- Modify: `project/worker/main.py`
- Modify: `project/admin/eval_runner.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/.env.example`
- Test: `project/tests/unit/security/test_security_dataset.py`
- Test: `project/tests/unit/admin/test_security_eval_runner.py`
- Test: `project/tests/unit/test_worker.py`

- [ ] Добавить RED-тесты dedicated cheap provider, reserve, double failure alert и eval current-only contract.
- [ ] Добавить `SECURITY_*` provider tuple с fallback на Router provider и переиспользовать существующий `RESERVE_*`.
- [ ] Гарантировать safe critical event даже без Telegram alert transport.
- [ ] Выровнять runtime и Security Evaluation.
- [ ] Запустить затронутые тесты до GREEN.

### Task 4: Optional semantic output validator

**Files:**
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/llm/config.py`
- Modify: `project/llm/llm.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/.env.example`
- Test: `project/tests/unit/security/test_pipeline.py`
- Test: `project/tests/unit/security/test_validator.py`

- [ ] Добавить RED-тест: при выключенном semantic validator локальная canary/leak/PII validation остаётся обязательной.
- [ ] Добавить env-only `OUTPUT_VALIDATOR_ENABLED=false`; не добавлять Redis/runtime toggle.
- [ ] Сохранить существующий semantic retry flow при включённом флаге.
- [ ] Запустить output tests до GREEN.

### Task 5: Verification, docs and commit

- [ ] Запустить focused Docker suite для Security/Router/PII/output/worker/eval.
- [ ] Запустить полный Docker `pytest -q`.
- [ ] Выполнить `git diff --check`, privacy scan и review diff.
- [ ] Обновить `ТЗ и архитектура.md`, `Дорожная карта.md`, этот plan и `changelog.md` фактическими результатами.
- [ ] Создать локальный commit и записать exact SHA; push/deploy не выполнять.

