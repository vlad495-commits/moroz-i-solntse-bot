# Admin Evaluations Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Объединить существующие evaluation-наборы под одной ссылкой бокового меню и переключать их горизонтальными подкладками без изменения API, данных и раннеров.

**Architecture:** Существующие GET/POST routes и suite-specific handlers остаются без изменений. Один Jinja-шаблон строит подкладки по текущему `suite`, а CSS оформляет активную ссылку синей нижней линией; специализированные Git-managed наборы сохраняют read-only режим.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTML/CSS, pytest, Docker Compose.

## Global Constraints

- Работать только в `codex/admin-evaluations-navigation`.
- Все проверки запускать только через Docker Compose.
- Не менять schema, dataset JSON, API, POST routes и eval runner.
- Сохранить legacy URLs `/eval/`, `/eval/router/`, `/eval/security/`, `/eval/validator/`, `/eval/compact/`.
- Не добавлять «Стирание ПД»: suite и route отсутствуют.
- Не выполнять push, merge, staging или production actions.

---

### Task 1: Контракты единой навигации

**Files:**
- Create: `project/tests/e2e/admin/test_eval_navigation.py`

**Interfaces:**
- Consumes: существующий `eval_routes.templates.TemplateResponse` и контекст `suite`, `cases`, `problem_cases`, `runs`.
- Produces: проверяемый HTML-контракт классов `eval-tabs`, `eval-tab active`, единственной sidebar-ссылки и таблицы истории.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, timezone

import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


def render_eval_list(suite: str, path: str, *, with_run: bool = False) -> str:
    request = Request({
        "type": "http", "method": "GET", "scheme": "https",
        "path": path, "root_path": "/admin", "headers": [],
        "query_string": b"", "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
    })
    runs = [{
        "id": 41, "started_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
        "status": "finished", "passed": 3, "failed": 1, "total": 4,
        "judge_model": "judge-test",
    }] if with_run else []
    response = eval_routes.templates.TemplateResponse(
        request,
        "eval_list.html",
        {
            "user": AuthenticatedUser(7, "owner", "owner", "csrf", "session"),
            "suite": suite,
            "cases": [],
            "problem_cases": [],
            "runs": runs,
        },
    )
    return response.body.decode("utf-8")


@pytest.mark.parametrize(
    ("suite", "path", "active_label"),
    [
        ("answer", "/eval/", "Основная LLM"),
        ("validator", "/eval/validator/", "Валидатор"),
        ("router_v2", "/eval/router/", "Роутер"),
        ("security", "/eval/security/", "Input Security"),
        ("compact", "/eval/compact/", "Сжатие контекста"),
    ],
)
def test_eval_template_renders_shared_tabs_and_active_suite(suite, path, active_label):
    body = render_eval_list(suite, path)
    assert '<nav class="eval-tabs"' in body
    assert f'class="eval-tab active" href="/admin{path}">{active_label}</a>' in body
    assert "Стирание ПД" not in body

def test_eval_sidebar_has_only_one_evaluations_link():
    body = render_eval_list("answer", "/eval/")
    sidebar = body.split('<nav class="sidebar">', 1)[1].split('</nav>', 1)[0]
    assert sidebar.count('/eval/') == 1
    assert "Router Evaluation" not in sidebar

def test_eval_history_keeps_required_columns():
    body = render_eval_list("answer", "/eval/", with_run=True)
    for label in ("ID", "Дата запуска", "Статус", "Прошло / всего", "Pass rate", "Валидатор", "Judge-модель"):
        assert label in body
    assert "#41" in body and "75%" in body and "judge-test" in body
```

- [ ] **Step 2: Run RED**

Run:

```powershell
docker compose --env-file ../../../.env run --rm test pytest tests/e2e/admin/test_eval_navigation.py -q
```

Expected: FAIL because `eval-tabs`, the active underline contract and consolidated sidebar do not exist.

- [ ] **Step 3: Commit RED tests**

```powershell
git add project/tests/e2e/admin/test_eval_navigation.py
git commit -m "test: добавлен контракт навигации evaluations"
```

### Task 2: Минимальная UI-перестройка

**Files:**
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/templates/eval_list.html`
- Modify: `project/admin/static/styles.css`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: текущий `suite`, legacy URL, `cases`, `problem_cases`, `runs`.
- Produces: одна sidebar-ссылка, пять suite links, активная подкладка, единая таблица последних прогонов.

- [ ] **Step 1: Implement the minimal template and CSS change**

```jinja2
{% set eval_tabs = [('answer', 'Основная LLM', '/eval/'), ('validator', 'Валидатор', '/eval/validator/'), ('router_v2', 'Роутер', '/eval/router/'), ('security', 'Input Security', '/eval/security/'), ('compact', 'Сжатие контекста', '/eval/compact/')] %}
<nav class="eval-tabs" aria-label="Наборы evaluations">
  {% for key, label, href in eval_tabs %}
  <a class="eval-tab {% if suite == key or (key == 'router_v2' and suite == 'router') %}active{% endif %}" href="{{ request.scope.get('root_path', '') }}{{ href }}">{{ label }}</a>
  {% endfor %}
</nav>
```

Remove the four specialized anchors from `base.html` and add CSS-only underline styling. Keep existing run/case form actions. For Git-managed component suites render «Новый кейс» disabled with an explicit tooltip; for `answer` keep the working existing link. Show the requested validator option on `answer` as unavailable in the current runner, rather than posting an ignored value.

- [ ] **Step 2: Run GREEN navigation tests**

```powershell
docker compose --env-file ../../../.env run --rm test pytest tests/e2e/admin/test_eval_navigation.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run focused regression**

```powershell
docker compose --env-file ../../../.env run --rm test pytest tests/e2e/admin/test_eval_navigation.py tests/e2e/admin/test_router_eval_routes.py tests/e2e/admin/test_security_eval_routes.py tests/e2e/admin/test_validator_eval_routes.py tests/e2e/admin/test_compact_eval_routes.py tests/e2e/admin/test_public_prefix.py -q
```

Expected: all focused admin evaluation tests pass.

- [ ] **Step 4: Record evidence and commit**

Update the roadmap task as complete and append Docker evidence to `changelog.md`, then:

```powershell
git add project/admin/templates/base.html project/admin/templates/eval_list.html project/admin/static/styles.css project/tests/e2e/admin/test_eval_navigation.py "Дорожная карта.md" changelog.md
git commit -m "feat: объединена навигация evaluations"
```

- [ ] **Step 5: Verify clean diff**

```powershell
git diff --check HEAD~1 HEAD
git status --short
```

Expected: no whitespace errors and clean worktree.
