from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def render_eval_list(
    suite: str, path: str, *, with_run: bool = False, role: str = "owner"
) -> str:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "root_path": "/admin",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
        }
    )
    runs = (
        [
            {
                "id": 41,
                "started_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
                "status": "finished",
                "passed": 3,
                "failed": 1,
                "total": 4,
                "judge_model": "judge-test",
            }
        ]
        if with_run
        else []
    )
    response = eval_routes.templates.TemplateResponse(
        request,
        "eval_list.html",
        {
            "user": AuthenticatedUser(7, role, role, "csrf", "session"),
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
def test_eval_template_renders_shared_tabs_and_active_suite(
    suite, path, active_label
):
    body = render_eval_list(suite, path)

    assert '<nav class="eval-tabs"' in body
    assert (
        f'class="eval-tab active" href="/admin{path}">{active_label}</a>'
        in body
    )
    assert body.count('<a class="eval-tab') == 5
    assert "Стирание ПД" not in body


def test_eval_tabs_use_full_width_segmented_layout():
    styles = (PROJECT_ROOT / "admin" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )
    tabs = styles.split(".eval-tabs {", 1)[1].split("}", 1)[0]

    assert "display: grid;" in tabs
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in tabs
    assert "width: 100%;" in tabs
    assert "background: var(--brand);" in styles.split(
        ".eval-tab.active {", 1
    )[1]
    assert "@media (max-width: 700px) {\n    .eval-tabs {" in styles


def test_eval_actions_use_balanced_toolbar_layout():
    body = render_eval_list("answer", "/eval/")
    styles = (PROJECT_ROOT / "admin" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert '<h1 class="eval-heading">' in body
    assert '<section class="eval-toolbar"' in body
    assert '<div class="eval-actions-grid">' in body
    assert body.count('class="eval-action"') == 2
    assert 'class="btn eval-action"' in body
    assert 'type="checkbox"' not in body
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in styles
    action_button = styles.split(".eval-action .btn,", 1)[1].split("}", 1)[0]
    assert "width: 100%;" in action_button
    assert "min-height: 48px;" in action_button
    assert "@media (max-width: 700px)" in styles


def test_eval_sidebar_has_only_one_evaluations_link():
    body = render_eval_list("answer", "/eval/")
    sidebar = body.split('<nav class="sidebar">', 1)[1].split("</nav>", 1)[0]

    assert sidebar.count('href="/admin/eval/"') == 1
    assert "Router Evaluation" not in sidebar
    assert "Validator Evaluation" not in sidebar
    assert "Compact Evaluation" not in sidebar
    assert "Input Security" not in sidebar


def test_non_owner_sees_only_accessible_answer_tab():
    body = render_eval_list("answer", "/eval/", role="admin")

    assert body.count('<a class="eval-tab') == 1
    assert 'href="/admin/eval/">Основная LLM</a>' in body
    assert 'href="/admin/eval/validator/"' not in body


def test_eval_history_keeps_required_columns_and_values():
    body = render_eval_list("answer", "/eval/", with_run=True)

    for label in (
        "ID",
        "Дата запуска",
        "Статус",
        "Прошло / всего",
        "Pass rate",
        "Валидатор",
        "Judge-модель",
    ):
        assert label in body
    assert "#41" in body
    assert "75%" in body
    assert "judge-test" in body


def test_eval_actions_are_honest_about_existing_capabilities():
    answer = render_eval_list("answer", "/eval/")
    validator = render_eval_list("validator", "/eval/validator/")

    assert 'href="/admin/eval/cases/new"' in answer
    assert "С валидатором" in answer
    assert "недоступно в текущем раннере" in answer
    assert 'action="/admin/eval/validator/runs"' in validator
    assert "+ Новый кейс" in validator
    assert "Набор версионируется в Git" in validator
    assert 'href="/admin/eval/cases/new"' not in validator
