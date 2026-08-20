# Eval Catalog and Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать локальные эвалы честными для catalog runtime, закрыть adversarial bypass и устранить постоянные содержательные провалы.

**Architecture:** Существующий `SecurityPipeline` остаётся единственной точкой ответа. Eval-runner получает optional synthetic catalog; structural policies выносятся в общий детерминированный evaluator; guardrails усиливаются небольшим набором privilege/secret patterns; prompt хранит только постоянные факты.

**Tech Stack:** Python 3.12, pytest, FastAPI admin runner, Docker Compose, PostgreSQL, OpenAI-compatible judge.

## Global Constraints

- Все проектные команды выполняются только через Docker Compose с отдельным project name.
- `dataset.json` не перезаписывается, `adversarial_dataset.json` не изменяется.
- YCLIENTS, Telegram, staging, production, deploy и push запрещены.
- TDD RED обязателен до каждого production change.
- `Дорожная карта.md` и `changelog.md` обновляются по завершении логических шагов.

---

### Task 1: Catalog и structural eval paths

**Files:**
- Create: `project/src/moroz/security/eval_structural.py`
- Create: `project/llm/eval/catalog_dataset.json`
- Modify: `project/admin/eval_runner.py`
- Modify: `project/llm/eval/run_evals.py`
- Test: `project/tests/unit/test_eval_privacy.py`
- Test: `project/tests/unit/security/test_eval_catalog.py`

**Interfaces:**
- `evaluate_structural_case(case: Mapping[str, object]) -> Awaitable[bool | None]`.
- `_generate_bot_response(question, system_prompt, catalog=None) -> str`.
- `run_case(case, run_id, *, catalog=None) -> dict`.

- [x] Добавить RED, доказывающий передачу `CatalogGrounding` в pipeline и отсутствие primary/judge calls для structural cases.
- [x] Запустить Docker RED и подтвердить ожидаемые failures.
- [x] Вынести существующую structural-логику в общий модуль и добавить optional catalog arguments без новой абстракции.
- [x] Добавить шесть synthetic cases отдельным JSON-массивом, сохранив существующие datasets.
- [x] Запустить focused GREEN и расширенные eval/security regressions.
- [x] Сделать локальный логический коммит.

### Task 2: Universal adversarial guard

**Files:**
- Modify: `project/src/moroz/security/guardrails.py`
- Modify: `project/tests/unit/security/test_guardrails.py`

**Interfaces:**
- `check_input(text, recent_message_count=1)` возвращает `GuardDecision("block", "prompt_injection")` для всех 20 universal attacks.

- [x] Добавить RED, загружающий неизменённый `adversarial_dataset.json` и требующий local block для каждого кейса.
- [x] Добавить negative RED/compatibility cases для обычных вопросов о правилах, адресе и услугах.
- [x] Запустить Docker RED и подтвердить 20 local bypass (прежние 2 PASS обеспечивались внешним prompt-defense, а не локальным guard).
- [x] Добавить минимальные privilege-context и protected-target patterns.
- [x] Запустить focused GREEN, pipeline и adversarial CLI.
- [x] Сделать локальный логический коммит.

### Task 3: Постоянные факты и полезные safe replies

**Files:**
- Modify: `project/llm/prompts/system.md`
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/tests/unit/security/test_pipeline.py`
- Modify: `project/tests/unit/security/test_system_prompt_catalog.py`

**Interfaces:**
- Prompt содержит адрес, направления выбора, безопасный старт загара и состав программ без динамических цен.
- Prompt-leak и medical replies объясняют границу и дают безопасный следующий шаг.
- Неподтверждённый slot не объявляется свободным и направляет к проверке доступности.

- [x] Добавить RED для постоянных prompt facts и трёх safe replies.
- [x] Запустить Docker RED и подтвердить конкретные отсутствующие факты/формулировки.
- [x] Внести минимальные prompt/constants изменения без hardcoded catalog prices.
- [x] Запустить focused GREEN и связанные prompt/pipeline regressions.
- [x] Сделать локальный логический коммит.

### Task 4: Fresh eval и закрытие отчёта

**Files:**
- Modify: `project/llm/eval/local_2026-08-17_report.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Отчёт отдельно показывает judge, catalog, structural и adversarial results.

- [x] Поднять точный временный Compose namespace и применить migrations.
- [x] Выполнить admin judge-run на существующих 69 cases.
- [x] Выполнить dedicated synthetic catalog eval и adversarial CLI.
- [x] Запустить релевантный Docker pytest gate и `git diff --check`.
- [x] Обновить отчёт, roadmap и changelog реальными результатами без маскировки FAIL.
- [x] Проверить секреты, сделать локальный коммит и удалить только exact temporary namespace.
