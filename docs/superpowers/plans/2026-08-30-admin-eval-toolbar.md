# Admin Eval Toolbar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать панель действий Evaluations полноширинной, симметричной и адаптивной.

**Architecture:** Существующий шаблон получает отдельные классы для заголовка, информационного статуса и сетки действий. Стили изолируются префиксом `eval-`; backend, маршруты и JavaScript не меняются.

**Tech Stack:** Jinja2, HTML, CSS, pytest, Docker Compose.

## Global Constraints

- Не менять тексты действий, маршруты, CSRF, RBAC и runner.
- Не добавлять зависимости и JavaScript.
- Все проверки проекта запускать только через Docker.

---

### Task 1: Симметричная панель действий

**Files:**
- Modify: `project/admin/templates/eval_list.html`
- Modify: `project/admin/static/styles.css`
- Test: `project/tests/e2e/admin/test_eval_navigation.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: существующий Jinja-контекст `suite`, `cases`, `problem_cases`, `user`.
- Produces: классы `eval-heading`, `eval-toolbar`, `eval-status`, `eval-actions-grid`, `eval-action`.

- [x] **Step 1: Write the failing test**

```python
def test_eval_actions_use_balanced_toolbar_layout():
    body = render_eval_list("answer", "/eval/")
    styles = (PROJECT_ROOT / "admin" / "static" / "styles.css").read_text(encoding="utf-8")
    assert '<section class="eval-toolbar"' in body
    assert '<div class="eval-actions-grid">' in body
    assert 'type="checkbox"' not in body
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in styles
    assert "width: 100%;" in styles.split(".eval-action .btn {", 1)[1].split("}", 1)[0]
```

- [x] **Step 2: Run test to verify it fails**

Run: `docker compose --env-file ../.env run --rm test pytest tests/e2e/admin/test_eval_navigation.py::test_eval_actions_use_balanced_toolbar_layout -q`

Expected: `FAIL`, потому что новая панель и CSS-контракт ещё отсутствуют.

- [x] **Step 3: Write minimal implementation**

```html
<h1 class="eval-heading">…</h1>
<section class="eval-toolbar" aria-label="Действия набора">
    <div class="eval-status">С валидатором — недоступно в текущем раннере</div>
    <div class="eval-actions-grid">…существующие три действия…</div>
</section>
```

```css
.eval-actions-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.eval-action .btn { width: 100%; min-height: 48px; }
@media (max-width: 700px) { .eval-actions-grid { grid-template-columns: 1fr; } }
```

- [x] **Step 4: Run focused and regression tests**

Run: `docker compose --env-file ../.env run --rm test pytest tests/e2e/admin/test_eval_navigation.py -q`

Expected: все тесты файла проходят.

- [x] **Step 5: Verify project contracts and commit**

Run: `docker compose --env-file ../.env config --quiet`

Expected: exit code `0`.

```bash
git add project/admin/templates/eval_list.html project/admin/static/styles.css project/tests/e2e/admin/test_eval_navigation.py "Дорожная карта.md" changelog.md docs/superpowers
git commit -m "fix: выровнять панель действий evaluations"
```
