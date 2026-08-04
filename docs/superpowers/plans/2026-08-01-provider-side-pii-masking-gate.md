# Provider-side PII Masking Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть provider-side PII masking gate воспроизводимой Docker-командой и зафиксировать доказательство, что фейковые PII не попадают в `LLMRequest`.

**Architecture:** Существующий `SecurityPipeline` и `PiiSession` остаются без изменений: текущий ввод и история маскируются перед вызовом gateway, а разрешённые placeholder восстанавливаются после validator. Работа исправляет только Windows-to-Linux путь в команде focused gate и обновляет живую документацию по фактически пройденной проверке.

**Tech Stack:** PowerShell, Docker Compose, pytest, Markdown, Git.

## Global Constraints

- Все Python-проверки запускаются только через Docker Compose.
- В тестах используются только вымышленные PII: `Анна Иванова`, `+7 999 123-45-67`, `old@example.ru`.
- Production-код не меняется, если существующий focused gate подтверждает требования.
- Значимые действия сразу фиксируются в `changelog.md`.

---

### Task 1: Сделать focused gate исполняемым

**Files:**
- Modify: `docs/guardrails-pii-provider-side-task.md`
- Test: `project/tests/unit/security/test_pipeline.py`

**Interfaces:**
- Consumes: Docker Compose service `test` с рабочей директорией `/workspace`.
- Produces: PowerShell-команда, передающая pytest Linux-пути `tests/unit/security` и `tests/unit/test_safe_logging.py`.

- [x] **Step 1: Воспроизвести ошибку исходной команды**

Run from `project/`:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests\unit\security tests\unit\test_safe_logging.py
```

Expected: exit `4`, `ERROR: file or directory not found: tests\unit\security`.

- [x] **Step 2: Проверить исправленную команду**

Run from `project/` with the task-file unit environment variables:

```powershell
docker compose --env-file ../.env run --rm --no-deps test pytest -q tests/unit/security tests/unit/test_safe_logging.py
```

Expected: `243 passed`.

- [x] **Step 3: Исправить task-файл**

Replace the pytest arguments with:

```powershell
pytest -q tests/unit/security tests/unit/test_safe_logging.py
```

- [x] **Step 4: Подтвердить provider payload assertion**

Verify `test_provider_sees_only_masked_current_input_and_history` asserts that `repr(CapturingGateway.requests)` does not contain the fake name, phone, or historical email and does contain the corresponding placeholders.

- [x] **Step 5: Проверить все критичные классы на provider boundary**

Run from `project/` with the task-file unit environment variables:

```powershell
docker compose --env-file ../.env run --rm --no-deps test pytest -q tests/e2e/test_security_pipeline.py::test_security_pipeline_masks_each_critical_pii_class
```

Expected: `6 passed` for phone/email/name/address/payment/medical.

### Task 2: Закрыть живую документацию

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: focused gate result `243 passed in 3.39s` and provider-boundary result `6 passed in 4.03s`.
- Produces: закрытый roadmap item и датированная запись с проверенным результатом.

- [x] **Step 1: Закрыть пункт дорожной карты**

Change the provider-side PII masking item from `[ ]` to `[x]` and record that current input/history are masked in captured `LLMRequest`, unknown placeholders are rejected, and the focused gate passed `243` tests.

- [x] **Step 2: Записать результат в changelog**

Append one `[2026-08-01 HH:MM]` entry with the original path failure, its root cause, the corrected command, `243 passed`, and the conclusion that production code did not require changes.

- [x] **Step 3: Проверить diff**

Run:

```powershell
git diff --check
git diff -- docs/guardrails-pii-provider-side-task.md 'Дорожная карта.md' changelog.md
```

Expected: `git diff --check` produces no output; the diff contains only the documented gate correction and completion evidence.

- [x] **Step 4: Commit**

```powershell
git add docs/guardrails-pii-provider-side-task.md docs/superpowers/plans/2026-08-01-provider-side-pii-masking-gate.md 'Дорожная карта.md' changelog.md
git commit -m "fix: закрыт provider-side pii masking gate"
```
