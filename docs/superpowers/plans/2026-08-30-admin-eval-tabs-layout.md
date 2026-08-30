# Admin Evaluations Tabs Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Оформить существующие подкладки Evaluations как полноширинную адаптивную сегментированную панель.

**Architecture:** Существующий семантический HTML и RBAC остаются без изменений. Один CSS-контракт фиксирует desktop grid, активный сегмент и mobile overflow; минимальная реализация меняет только существующие `.eval-tabs`/`.eval-tab` rules.

**Tech Stack:** Jinja2 HTML, native CSS, pytest, Docker Compose.

## Global Constraints

- Не менять маршруты, RBAC, подписи вкладок и данные.
- Не добавлять JavaScript или зависимости.
- Все проверки запускать только через Docker.

---

### Task 1: Полноширинная панель вкладок

**Files:**
- Modify: `project/tests/e2e/admin/test_eval_navigation.py`
- Modify: `project/admin/static/styles.css`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: существующие `.eval-tabs`, `.eval-tab`, `.eval-tab.active` из `eval_list.html`.
- Produces: пять равных desktop-сегментов, заметное active/focus состояние и horizontal overflow до 700 px.

- [ ] **Step 1: Добавить failing CSS-контракт**

```python
from pathlib import Path

PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_eval_tabs_use_full_width_segmented_layout():
    styles = (PROJECT_ROOT / "admin" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )
    tabs = styles.split(".eval-tabs {", 1)[1].split("}", 1)[0]
    assert "display: grid;" in tabs
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in tabs
    assert "width: 100%;" in tabs
    assert "background: var(--brand);" in styles.split(".eval-tab.active {", 1)[1]
    assert "@media (max-width: 700px) {\n    .eval-tabs {" in styles
```

- [ ] **Step 2: Подтвердить RED**

Run:

```bash
cd project
docker compose --env-file ../.env --profile test run --rm --no-deps --build test pytest tests/e2e/admin/test_eval_navigation.py::test_eval_tabs_use_full_width_segmented_layout -q
```

Expected: `1 failed`, потому что `.eval-tabs` пока использует `display: flex`.

- [ ] **Step 3: Внести минимальную CSS-правку**

Заменить существующие rules на grid-контейнер с `width: 100%`, светлой карточкой и равными колонками; активной вкладке задать `background: var(--brand)` и белый текст. В существующий `@media (max-width: 700px)` добавить flex overflow и `min-width: 150px` для вкладок.

- [ ] **Step 4: Подтвердить GREEN и регрессию**

Run:

```bash
cd project
docker compose --env-file ../.env --profile test run --rm --no-deps --build test pytest tests/e2e/admin/test_eval_navigation.py -q
docker compose --env-file ../.env --profile test run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache test python -m compileall -q /workspace
docker compose --env-file ../.env config --quiet
```

Expected: все navigation-тесты проходят; compileall и Compose завершаются с exit `0`.

- [ ] **Step 5: Обновить evidence и зафиксировать результат**

Отметить задачу выполненной в `Дорожная карта.md`, добавить RED/GREEN evidence в `changelog.md`, выполнить `git diff --check` и commit:

```bash
git add -A
git commit -m "style: оформить вкладки Evaluations"
```
