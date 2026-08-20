# Project Governance Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить корневую дорожную карту в единственный короткий пульт управления проектом, сохранить всю историю и однозначно разделить роли ТЗ, release plan, changelog, AGENTS, checklist, specs/plans и аудита бота Володи.

**Architecture:** Реорганизация выполняется отдельной документационной веткой только от зафиксированного post-release состояния. Старая дорожная карта перемещается в архив без потери данных, новая корневая версия содержит только актуальный status и `Now / Next / Later`, а автоматический pytest-контракт запрещает возврат конкурирующих текущих стадий.

**Tech Stack:** Markdown, Git, Python 3.12, pytest, Docker Compose.

## Global Constraints

- Runtime services, migrations, prompt, secrets и deployment scripts не изменяются. В test-only Compose profile разрешены только точечные read-only mounts документов, необходимых governance-контракту; корень репозитория и `.env` не монтируются.
- Текущий release candidate сначала проходит review, PR, merge и staging verification по `docs/superpowers/plans/2026-08-20-staging-release-privacy-scheduler-fixes.md`.
- Документационная ветка создаётся от exact post-release commit, а не от промежуточного candidate.
- `Дорожная карта.md` — единственный источник текущего статуса.
- Исторические факты не удаляются: прежняя дорожная карта сохраняется в `docs/archive/roadmap-history-through-2026-08-20.md`.
- Аудит Lucky Hair Studio остаётся референсом, а не источником продуктовых приоритетов.
- Все проверки запускаются только через Docker Compose.
- Каждый завершённый логический шаг обновляет `changelog.md` и получает отдельный commit.

---

## Execution prerequisite: закрыть текущий release checkpoint

Этот prerequisite выполняется существующим release-планом и не реализуется повторно в этой документационной ветке.

- [x] Провести независимый review локального release HEAD `e8efa47000b859e8bf700a291a539673f85c9800`.
- [x] Исправить findings и повторить затронутые Docker gates, если findings существуют.
- [x] Опубликовать пять локальных коммитов в PR №2 и сверить exact PR head.
- [x] Выполнить merge и commit-pinned staging rollout с сохранённым rollback.
- [x] Подтвердить migration `0012`, scheduler health/synthetic terminal job, privacy smoke и safe logs.
- [x] Выполнить read-only диагностику `GET /api/v1/records/{company_id}` с выводом только HTTP status.
- [x] Зафиксировать post-release commit/tag и фактический runtime status в changelog/roadmap.

Expected: существует один post-release commit, из которого можно честно построить новый текущий status.

---

### Task 1: Документный source-of-truth contract

**Files:**
- Create: `project/tests/unit/test_project_governance_docs.py`
- Modify: `project/docker-compose.yml` — только least-privilege read-only mounts test-profile.

**Interfaces:**
- Consumes: design `docs/superpowers/specs/2026-08-20-project-governance-design.md`.
- Produces: pytest-контракт обязательных разделов, role-banner, архива и статусов референса.

- [x] **Step 1: Write the failing governance tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_roadmap_is_the_only_current_status_source() -> None:
    roadmap = read("Дорожная карта.md")
    agents = read("AGENTS.md")
    assert "## Где мы сейчас" in roadmap
    assert "## Активная работа" in roadmap
    assert "## Блокеры" in roadmap
    assert "## Что нужно от владельца" in roadmap
    assert "## Дальше: Now / Next / Later" in roadmap
    assert "Текущая ступень" not in agents
    assert "единственный источник текущего статуса" in agents


def test_static_documents_have_one_role() -> None:
    for relative in (
        "ТЗ и архитектура.md",
        "План реализации.md",
        "changelog.md",
        "checklist.md",
    ):
        body = read(relative)
        assert "Роль документа" in body, relative
        assert "Дорожная карта.md" in body, relative


def test_history_and_governance_manual_exist() -> None:
    assert (ROOT / "docs/archive/roadmap-history-through-2026-08-20.md").is_file()
    manual = read("docs/project/Система управления проектом.md")
    assert "Один вопрос — один источник правды" in manual
    assert "Происхождение документов" in manual
    assert "Идея / референс" in manual


def test_volodya_audit_has_current_disposition() -> None:
    audit = read("docs/audits/Аудит решений бота Володи 2026-08-13.md")
    assert "Актуализация статусов" in audit
    assert "Реализовано или закрыто нашей реализацией" in audit
    assert "Остаётся в продуктовой очереди" in audit
    assert "Исключено или не переносится" in audit
```

- [x] **Step 2: Run tests to verify RED**

Run:

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_project_governance_docs.py
```

Expected: FAIL because the new roadmap sections, archive, manual, banners and audit appendix do not yet exist.

- [x] **Step 3: Commit the RED contract**

```powershell
git add project/tests/unit/test_project_governance_docs.py changelog.md
git commit -m "test: зафиксировать единый источник статуса"
```

- [x] **Review fix: Make the contract part of the ordinary Docker suite**

Self-review after the first GREEN found that a manual whole-worktree bind mount was required. The test profile now mounts only the exact root documents and three documentation directories read-only; `.env`, runtime services and production Compose behavior remain unchanged.

---

### Task 2: Короткая дорожная карта и сохранённая история

**Files:**
- Move: `Дорожная карта.md` → `docs/archive/roadmap-history-through-2026-08-20.md`
- Create: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: exact post-release commit/tag and runtime evidence from prerequisite.
- Produces: root status dashboard with fixed headings required by Task 1.

- [x] **Step 1: Move the historical roadmap without rewriting it**

```powershell
New-Item -ItemType Directory -Force -Path docs/archive | Out-Null
git mv -- 'Дорожная карта.md' 'docs/archive/roadmap-history-through-2026-08-20.md'
```

- [x] **Step 2: Capture the exact post-release evidence**

Run:

```powershell
git rev-parse HEAD
git log -1 --format='%cI %s'
```

Copy the literal commit returned by the first command. Copy the exact test, review and staging evidence from the final release handoff/changelog entry. Do not write `latest`, a branch name, an unverified expected status or a placeholder into the roadmap.

- [x] **Step 3: Create the concise root roadmap**

Create `Дорожная карта.md` with this structure. For the four evidence bullets, insert the literal values captured in Step 2:

```markdown
# Дорожная карта — Moroz i Solntse Bot

> **Единственный источник текущего статуса проекта.** Цель продукта описана в `ТЗ и архитектура.md`, состав Telegram Production V1 — в `План реализации.md`, подробная история до 2026-08-20 сохранена в `docs/archive/roadmap-history-through-2026-08-20.md`.

## Где мы сейчас

- Продуктовый этап: Telegram Production V1 — завершение staging release.
- Проверенный commit/tag: точный post-release commit и immutable tag из release handoff.
- Staging: точный список проверенных runtime-компонентов и schema revision из release handoff.
- Последняя сверка: точное время финальной runtime-проверки в UTC+3.
- Полная многоканальная целевая система ещё не завершена: VK, Instagram, WhatsApp, cross-channel profile и кампании остаются следующими релизами.

## Активная работа

- Упорядочивание управления проектом по design `docs/superpowers/specs/2026-08-20-project-governance-design.md` и plan `docs/superpowers/plans/2026-08-20-project-governance-cleanup.md`.

## Доказано

- Точный результат полного Docker gate текущего release commit.
- Точный независимый review verdict текущего release commit.
- Точный staging smoke/runtime verdict после rollout.

## Блокеры

- Только фактически открытые blockers из финального release handoff; если их нет — `Нет`.

## Что нужно от владельца

- Только фактически необходимое следующее решение; если решения не требуется — `Ничего`.

## Дальше: Now / Next / Later

### Now

- Завершить документационную реорганизацию и проверить единый status source.

### Next

- Довести read-only центр записей: manual refresh и точная атрибуция источника.
- Добавить allowlisted technical trace одного запроса.
- Добавить версионированное управление знаниями.

### Later

- Постоянный профиль клиента и cross-channel identity.
- VK → Instagram → WhatsApp adapters через общий inbox/outbox.
- Реактивация и ручные кампании после consent/unsubscribe.

### Исключено

- Голосовые сообщения и STT.

## Источники и подробности

- Цель: `ТЗ и архитектура.md`.
- Telegram Production V1: `План реализации.md`.
- Управление проектом: `docs/project/Система управления проектом.md`.
- Референс Володи: `docs/audits/Аудит решений бота Володи 2026-08-13.md`.
- История: `changelog.md` и `docs/archive/roadmap-history-through-2026-08-20.md`.
```

- [x] **Step 4: Record the move immediately**

Append one timestamped line to `changelog.md` stating that the historical roadmap was preserved, the root roadmap became the only status source, and runtime/external systems were unchanged.

- [x] **Step 5: Run focused tests**

Run the Task 1 Docker command.

Expected: archive assertions pass; banner/manual/audit assertions still fail.

- [x] **Step 6: Commit**

```powershell
git add -- 'Дорожная карта.md' 'docs/archive/roadmap-history-through-2026-08-20.md' changelog.md
git commit -m "docs: сделать дорожную карту пультом проекта"
```

---

### Task 3: Роли корневых документов

**Files:**
- Modify: `AGENTS.md`
- Modify: `ТЗ и архитектура.md`
- Modify: `План реализации.md`
- Modify: `changelog.md`
- Modify: `checklist.md`

**Interfaces:**
- Consumes: root roadmap created in Task 2.
- Produces: one role per root document and no competing current stage in `AGENTS.md`.

- [x] **Step 1: Remove stale status from AGENTS**

Replace the current `Текущая ступень: 1` block with:

```markdown
> **Текущий статус проекта:** единственный источник — `Дорожная карта.md`.
> `AGENTS.md` содержит только правила работы и не определяет текущую ступень, релиз или состояние staging.
```

- [x] **Step 2: Add exact role banners**

Add after the title of each file:

`ТЗ и архитектура.md`:

```markdown
> **Роль документа:** целевые требования, бизнес-правила и архитектурные границы продукта. Текущий status и приоритеты находятся только в `Дорожная карта.md`.
```

`План реализации.md`:

```markdown
> **Роль документа:** состав и порядок Telegram Production V1 по восьми фазам. Текущий status проекта и дальнейший продуктовый backlog находятся только в `Дорожная карта.md`.
```

`changelog.md`:

```markdown
> **Роль документа:** append-only инженерный журнал значимых действий и доказательств. Это не backlog и не источник текущего статуса; актуальное состояние находится в `Дорожная карта.md`.
```

`checklist.md`:

```markdown
> **Роль документа:** исторический общий production-ready checklist стартового шаблона. Актуальные приоритеты находятся в `Дорожная карта.md`, release gates — в `План реализации.md` и профильных runbooks/checklists.
```

- [x] **Step 3: Run focused tests**

Expected: roadmap and role assertions pass; manual/audit assertions still fail.

- [x] **Step 4: Commit**

```powershell
git add AGENTS.md 'ТЗ и архитектура.md' 'План реализации.md' changelog.md checklist.md
git commit -m "docs: разделить роли проектных документов"
```

---

### Task 4: Руководство и происхождение документов

**Files:**
- Create: `docs/project/Система управления проектом.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: registry and rules from the approved design.
- Produces: reusable project-governance manual without current release status.

- [x] **Step 1: Create the manual**

The file must contain the following complete sections; expand each table row into normal Markdown without adding current release status:

```markdown
# Система управления проектом Moroz i Solntse Bot

## Один вопрос — один источник правды

| Вопрос | Источник |
|---|---|
| Где проект сейчас и что следующее? | `Дорожная карта.md` |
| Каким должен стать продукт? | `ТЗ и архитектура.md` |
| Что входит в Telegram Production V1? | `План реализации.md` |
| Что должна делать конкретная функция? | Соответствующая spec |
| Как её реализовать и проверить? | Соответствующий implementation plan |
| Почему принято значимое решение? | ADR или одобренная spec |
| Что фактически сделано? | Git, тесты и `changelog.md` |
| Что можно взять у референса? | Датированный аудит, затем решение владельца |
| Что реально развёрнуто? | Runtime evidence, отражённый в roadmap |

## Происхождение документов

- `AGENTS.md`, исходная дорожная карта, `changelog.md` и `checklist.md` созданы 2026-07-02 стартовым ступенчатым workflow LLM-проекта.
- ТЗ и отдельные архитектурные схемы появились 2026-07-07 и объединены 2026-07-14 в `ТЗ и архитектура.md` версии 0.4.
- Master plan Telegram Production V1 создан 2026-07-14 и перенесён в корень 2026-07-15 как `План реализации.md`.
- `docs/superpowers/specs/` и `docs/superpowers/plans/` введены 2026-07-14 для дизайна и исполнения отдельных задач.
- Аудит бота Володи выполнен 2026-08-13 по Lucky Hair Studio commit `5398f90` и перенесён в `docs/audits/` 2026-08-17.

## Как проходит работа

Идея / референс → ТЗ → ADR/spec → Roadmap → Release plan → Implementation plan → Код/тесты/review → Git/changelog → Runtime evidence → Roadmap.

## Правила обновления

### Новая идея

Записать источник и пользу. Если меняется продуктовая цель — обновить ТЗ или создать ADR/spec. После решения владельца поставить инициативу в `Now / Next / Later`. Подробную spec создавать только для выбранного `Now`.

### Начало задачи

Зафиксировать exact base branch/commit, создать изолированную ветку/worktree, связать roadmap с одной active spec/plan и явно ограничить изменяемые файлы и среды.

### Завершение задачи

Получить тестовые и review evidence, сделать логический commit, добавить значимую запись в changelog и обновить roadmap. Не объявлять deployed до runtime evidence.

### Релиз

Зафиксировать candidate commit/tag, пройти полный gate и review, сохранить rollback, выполнить rollout и runtime-проверку. Только после этого обновить roadmap как `развёрнуто`.

## Референсы

External reference → audit → owner decision → roadmap. Референс не становится требованием автоматически.

## Защита от повторения путаницы

- не хранить текущую стадию в AGENTS;
- не записывать будущие задачи в changelog;
- не считать локальный commit развёрнутым;
- не создавать spec для Later;
- не вести второй status source в Notion/Jira без отказа от корневой roadmap;
- при противоречии исправлять документ-владелец вопроса.
```

Expand bracketed sections with the complete approved content; no placeholders remain in the created file.

- [x] **Step 2: Log and run focused tests**

Expected: only audit disposition test remains failing.

- [x] **Step 3: Commit**

```powershell
git add 'docs/project/Система управления проектом.md' changelog.md
git commit -m "docs: описать систему управления проектом"
```

---

### Task 5: Актуализация референса Володи

**Files:**
- Modify: `docs/audits/Аудит решений бота Володи 2026-08-13.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: historical audit and post-release factual status.
- Produces: current disposition without rewriting the dated audit conclusions.

- [x] **Step 1: Append, do not rewrite, the current disposition**

Add `## Актуализация статусов после аудита` with three subsections:

- `### Реализовано или закрыто нашей реализацией` — consent, deletion, handoff/reply, customer event journal, read-only bookings/reconciliation/catalog, buffer/inbox/outbox, LLM security, scheduler/operations;
- `### Остаётся в продуктовой очереди` — technical trace, permanent profile/identity, manual refresh/attribution, versioned knowledge, specialized eval views, campaigns, VK/Instagram/WhatsApp;
- `### Исключено или не переносится` — voice/STT, internal calendar instead of YCLIENTS, direct admin provider calls, shared env file, monolithic worker, system prompt rewrite from catalog.

Finish with:

```text
Новый кандидат из референса проходит только путь: audit → решение владельца → roadmap → spec → plan → implementation.
```

- [x] **Step 2: Run full focused governance test**

Expected: `4 passed`.

- [x] **Step 3: Commit**

```powershell
git add 'docs/audits/Аудит решений бота Володи 2026-08-13.md' changelog.md
git commit -m "docs: актуализировать решения референса Володи"
```

---

### Task 6: Финальная проверка и закрытие

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-08-20-project-governance-cleanup.md`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: verified documentation-only delivery and one current status source.

- [x] **Step 1: Check scope**

```powershell
git diff --name-only <POST_RELEASE_BASE>..HEAD
git diff --check <POST_RELEASE_BASE>..HEAD
```

Expected: only the explicitly listed documentation files and `project/tests/unit/test_project_governance_docs.py`; no runtime, migration, Compose, prompt, secret or deployment file.

- [x] **Step 2: Run Docker verification**

```powershell
Set-Location project
docker compose --env-file ../.env run --build --rm test pytest -q tests/unit/test_project_governance_docs.py tests/unit/test_full_project_architecture_visual.py
```

Expected: all tests pass.

- [x] **Step 3: Verify links and forbidden competing status**

```powershell
rg -n "Текущая ступень|единственный источник текущего статуса|Роль документа" AGENTS.md 'Дорожная карта.md' 'ТЗ и архитектура.md' 'План реализации.md' changelog.md checklist.md
```

Expected: `Текущая ступень` absent from AGENTS; the root roadmap is the only current status source; role banners exist in four static documents.

- [x] **Step 4: Close the plan and roadmap**

Mark every completed checkbox in this plan, mark governance cleanup completed in the root roadmap, set `Что нужно от владельца` to the exact remaining decision or `Ничего`, and append exact verification evidence to changelog.

- [x] **Step 5: Commit**

```powershell
git add 'Дорожная карта.md' changelog.md 'docs/superpowers/plans/2026-08-20-project-governance-cleanup.md'
git commit -m "docs: завершить упорядочивание проекта"
```

- [x] **Step 6: Final clean-state proof**

```powershell
git status --short
git log -6 --oneline
```

Expected: clean working tree and a readable sequence of governance commits.
