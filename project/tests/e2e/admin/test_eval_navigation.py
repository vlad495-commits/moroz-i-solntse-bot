from datetime import datetime, timezone

import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


def render_eval_list(suite: str, path: str, *, with_run: bool = False) -> str:
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
def test_eval_template_renders_shared_tabs_and_active_suite(
    suite, path, active_label
):
    body = render_eval_list(suite, path)

    assert '<nav class="eval-tabs"' in body
    assert (
        f'class="eval-tab active" href="/admin{path}">{active_label}</a>'
        in body
    )
    assert body.count('class="eval-tab') == 5
    assert "Стирание ПД" not in body


def test_eval_sidebar_has_only_one_evaluations_link():
    body = render_eval_list("answer", "/eval/")
    sidebar = body.split('<nav class="sidebar">', 1)[1].split("</nav>", 1)[0]

    assert sidebar.count('href="/admin/eval/"') == 1
    assert "Router Evaluation" not in sidebar
    assert "Validator Evaluation" not in sidebar
    assert "Compact Evaluation" not in sidebar
    assert "Input Security" not in sidebar


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
