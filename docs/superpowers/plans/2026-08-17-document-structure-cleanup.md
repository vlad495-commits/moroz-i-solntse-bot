# Document Structure Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разложить пользовательские документы по понятным папкам, сохранив полную HTML-схему также в корне репозитория.

**Architecture:** HTML-схемы станут каноническими файлами `docs/architecture/`; корневая `moroz-i-solntse-full-architecture.html` останется точной копией основной схемы для быстрого открытия. Ручные проверки и аудиты получат отдельные тематические каталоги, а тесты будут искать HTML по новым каноническим путям.

**Tech Stack:** Git, Markdown, Python pytest, Docker Compose.

## Global Constraints

- Не менять содержимое трёх HTML-схем и не обращаться к staging, production, YCLIENTS или Telegram.
- Все проектные проверки запускать только через Docker с отдельным Compose project name и удалить только этот контур после проверки.
- Не переписывать исторические записи `changelog.md` и старые планы только из-за переноса файлов.
- Обновить `Дорожная карта.md` и `changelog.md`; сделать локальный логический коммит без push.

---

### Task 1: Проверяемый контракт новых путей HTML

**Files:**
- Modify: `project/tests/unit/test_message_path_visual.py`
- Modify: `project/tests/unit/test_architecture_visual.py`
- Modify: `project/tests/unit/test_full_project_architecture_visual.py`
- Test: те же три файла, запущенные через Compose profile `test`

**Interfaces:**
- Consumes: переменные `MESSAGE_PATH_HTML_PATH`, `ARCHITECTURE_HTML_PATH`, `FULL_ARCHITECTURE_HTML_PATH`.
- Produces: defaults в `docs/architecture/` для всех трёх visual contracts.

- [ ] **Step 1: Изменить expected paths в тестах на `docs/architecture/`.**

```python
REPO_ROOT / "docs" / "architecture" / "production-v1-architecture.html"
```

- [ ] **Step 2: Запустить RED с новыми путями до переноса файлов.**

Run: `docker compose --env-file ../.env -p moroz-doc-cleanup-red --profile test run --rm ... pytest tests/unit/test_message_path_visual.py tests/unit/test_architecture_visual.py tests/unit/test_full_project_architecture_visual.py -q`

Expected: FAIL с `FileNotFoundError` для ещё не созданных путей `docs/architecture/*.html`.

### Task 2: Минимальный перенос и актуальные ссылки

**Files:**
- Move: `docs/*.html` → `docs/architecture/`
- Move: корневые ручные QA-файлы → `docs/qa/manual/`
- Move: два тематических audit-файла → `docs/audits/`
- Modify: `docs/superpowers/specs/2026-08-01-full-project-architecture-reference-style-design.md`
- Modify: `docs/superpowers/plans/2026-08-01-provider-side-pii-masking-gate.md`
- Modify: `docs/superpowers/plans/2026-08-06-admin-ops-release-candidate.md`

**Interfaces:**
- Consumes: RED-контракт Task 1.
- Produces: `docs/architecture/`, `docs/qa/manual/`, `docs/audits/` без loose пользовательских файлов в `docs/`; корневая HTML-копия остаётся байт-в-байт равной канонической.

- [ ] **Step 1: Перенести файлы командой `git mv` без изменения их содержимого.**

```text
docs/message-processing-path.html → docs/architecture/message-processing-path.html
docs/moroz-i-solntse-full-architecture.html → docs/architecture/moroz-i-solntse-full-architecture.html
docs/production-v1-architecture.html → docs/architecture/production-v1-architecture.html
```

- [ ] **Step 2: Обновить только актуальные прямые ссылки на перенесённые документы.**

- [ ] **Step 3: Выполнить Docker GREEN и проверить, что корневая копия полной схемы совпадает с канонической.**

Expected: все visual/document tests PASS.

### Task 3: Документация, финальная проверка и commit

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: результат Docker GREEN.
- Produces: зафиксированный перенос, чистый diff и локальный коммит.

- [ ] **Step 1: Отметить задачу в дорожной карте и внести факт переноса/проверки в changelog.**

- [ ] **Step 2: Выполнить финальные проверки.**

Run: `git diff --check` и полный scoped Docker pytest visual/document gate.

Expected: exit code `0` и `0` failed.

- [ ] **Step 3: Закоммитить логический шаг.**

```bash
git add -A
git commit -m "docs: упорядочена проектная документация"
```
