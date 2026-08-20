# LLM Reference Adaptation Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрепить постоянный обязательный контракт использования Lucky Hair Studio как референса для Router, Input Security, Validator, Compact Context и их evaluation suites.

**Architecture:** Один постоянный документ в `docs/project/` хранит процедуру и границы адаптации. `Дорожная карта.md` делает его обязательным входным gate четырёх этапов, а система управления проектом регистрирует роль документа и связывает его с общим reference workflow.

**Tech Stack:** Markdown, Git, PowerShell read-only contract checks.

## Global Constraints

- Использовать только Lucky Hair Studio commit `5398f909829f5db1b5052087f5a826c2bbcd5244`, а не текущее грязное рабочее дерево референса.
- Не изменять runtime-код, datasets, `.env`, staging, production или внешние системы.
- Не превращать референс в автоматический backlog и не копировать его архитектуру механически.
- Сохранить наши единые security pipeline и admin eval-runner.
- Зафиксировать изменения в `changelog.md` и отдельном локальном commit; push не выполнять.

---

### Task 1: Создать постоянный контракт и связать его с документами-владельцами

**Files:**
- Create: `docs/project/Контракт адаптации LLM-референса Володи.md`
- Modify: `Дорожная карта.md:68-73`
- Modify: `Дорожная карта.md:99-106`
- Modify: `docs/project/Система управления проектом.md:32-40`
- Modify: `docs/project/Система управления проектом.md:83-89`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-08-20-llm-reference-adaptation-contract-design.md`.
- Produces: обязательный входной gate, на который ссылаются четыре LLM-этапа дорожной карты.

- [x] **Step 1: Создать постоянный контракт**

Перенести из утверждённой спецификации в постоянный документ:

- exact path и commit референса;
- карту файлов Router/Security/Validator/Compact;
- обязательную таблицу `решение → наше состояние → пробел → решение → причина → проверка`;
- запреты на механическое копирование, второй security-контур и параллельные eval-подсистемы;
- последовательность `Router → Input Security → Validator → Compact Context`;
- общий gate runtime + evaluation.

- [x] **Step 2: Сделать контракт обязательным в дорожной карте**

Перед четырьмя LLM-парами добавить абзац:

```markdown
> **Обязательный входной gate для четырёх LLM-этапов:** до design и реализации выполнить `docs/project/Контракт адаптации LLM-референса Володи.md`: изучить Lucky Hair Studio на commit `5398f90`, составить таблицу «берём / адаптируем / отклоняем» и встроить выбранные решения в наши единые security pipeline и eval-runner. Runtime-компонент и его Evaluation закрываются только одной парой.
```

В разделе источников заменить краткую расплывчатую строку на прямую ссылку на контракт.

- [x] **Step 3: Зарегистрировать владельца правила**

Добавить постоянный контракт в таблицу документов системы управления проектом и уточнить reference workflow:

```markdown
Для Router, Input Security, Validator, Compact Context и их evaluations действует дополнительный обязательный gate: `docs/project/Контракт адаптации LLM-референса Володи.md`. До design исполнитель изучает зафиксированный commit референса и документирует решения «взять / адаптировать / отклонить».
```

- [x] **Step 4: Записать действие в changelog**

Запись должна назвать созданный контракт, commit референса, четыре пары компонентов, изменённые документы-владельцы и отсутствие runtime/external изменений.

- [x] **Step 5: Проверить документальные контракты**

Run:

```powershell
rg -n "Контракт адаптации LLM-референса Володи|5398f90|берём / адаптируем / отклоняем|Router → Input Security → Validator → Compact Context" `
  'docs/project/Контракт адаптации LLM-референса Володи.md' `
  'Дорожная карта.md' `
  'docs/project/Система управления проектом.md'
rg -n "TBD|TODO|PLACEHOLDER|\?\?\?" `
  'docs/project/Контракт адаптации LLM-референса Володи.md' `
  'Дорожная карта.md' `
  'docs/project/Система управления проектом.md'
git diff --check
```

Expected: первый поиск подтверждает контракт, commit, decision table и порядок этапов; второй не находит placeholders; `git diff --check` завершается exit `0`.

- [x] **Step 6: Сделать локальный commit**

```powershell
git add -- 'docs/project/Контракт адаптации LLM-референса Володи.md' 'Дорожная карта.md' 'docs/project/Система управления проектом.md' 'changelog.md' 'docs/superpowers/plans/2026-08-20-llm-reference-adaptation-contract.md'
git commit -m "docs: закрепить адаптацию LLM референса"
```

Expected: commit создан, `git status --short` пуст; push не выполняется.
