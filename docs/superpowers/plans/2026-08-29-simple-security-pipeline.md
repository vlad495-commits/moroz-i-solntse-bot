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

- [x] Заменить JSON/category tests на точные `OK/BLOCK`, current-only request и invalid-output fallback.
- [x] Запустить тест и подтвердить ожидаемый RED.
- [x] Реализовать минимальный cheap → reserve classifier с typed internal verdict и safe fail-open alert.
- [x] Запустить unit-тест до GREEN.

### Task 2: Parallel Router gate

**Files:**
- Modify: `project/src/moroz/security/pipeline.py`
- Test: `project/tests/unit/security/test_pipeline.py`
- Test: `project/tests/e2e/test_security_pipeline.py`

- [x] Переписать ordering-тесты: обе задачи стартуют, Router не используется до `OK`, `BLOCK` cancel+drain-ит Router.
- [x] Добавить тест на отсутствие потерянной Router exception и отсутствие history в Security.
- [x] Запустить тесты и подтвердить RED.
- [x] Удалить `needs_input_security_review`/`has_context`, запускать Security всегда после local pass и маскирования.
- [x] Запустить pipeline/e2e tests до GREEN.

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

- [x] Добавить RED-тесты dedicated cheap provider, reserve, double failure alert и eval current-only contract.
- [x] Добавить `SECURITY_*` provider tuple с fallback на Router provider и переиспользовать существующий `RESERVE_*`.
- [x] Гарантировать safe critical event даже без Telegram alert transport.
- [x] Выровнять runtime и Security Evaluation.
- [x] Запустить затронутые тесты до GREEN.

### Task 4: Optional semantic output validator

**Files:**
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/llm/config.py`
- Modify: `project/llm/llm.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/.env.example`
- Test: `project/tests/unit/security/test_pipeline.py`
- Test: `project/tests/unit/security/test_validator.py`

- [x] Добавить RED-тест: при выключенном semantic validator локальная canary/leak/PII validation остаётся обязательной.
- [x] Добавить env-only `OUTPUT_VALIDATOR_ENABLED=false`; не добавлять Redis/runtime toggle.
- [x] Сохранить существующий semantic retry flow при включённом флаге.
- [x] Запустить output tests до GREEN.

### Task 5: Verification, docs and commit

- [x] Запустить focused Docker suite для Security/Router/PII/output/worker/eval: `569 passed in 12.32s`.
- [x] Запустить migration cycle: `30 passed in 101.46s`, head `0018_simple_security`.
- [x] Запустить полный Docker `pytest -q`: `1629 passed / 24 failed`; `19` failures относятся к отсутствующим root visual assets в project build context, все `5` patch-related contracts исправлены; post-fix regression `10 passed`.
- [x] Выполнить `git diff --check`, privacy scan и review diff: clean, self-review сохранил immutable 0015 и вынес delta только в 0018.
- [x] Обновить `ТЗ и архитектура.md`, `Дорожная карта.md`, этот plan и `changelog.md` фактическими результатами.
- [x] Создать локальный commit и записать exact SHA в итоговой передаче; push/deploy не выполнять.
