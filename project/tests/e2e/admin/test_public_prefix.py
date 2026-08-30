from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request


PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def request(path: str = "/", *, root_path: str = "/admin") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "root_path": root_path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
        }
    )


def test_admin_url_keeps_public_root_path():
    from paths import admin_url

    assert admin_url(request(), "/login") == "/admin/login"
    assert admin_url(request(), "/prompt/?saved=1") == "/admin/prompt/?saved=1"
    assert admin_url(request(root_path=""), "/login") == "/login"


@pytest.mark.asyncio
async def test_login_required_redirect_stays_under_admin_prefix():
    import app as admin_app
    from auth import _LoginRequired

    response = await admin_app._login_required_handler(request("/stats"), _LoginRequired())

    assert response.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_login_template_posts_and_loads_assets_under_admin_prefix():
    import app as admin_app

    response = await admin_app.login_page(request("/login"))
    body = response.body.decode("utf-8")

    assert 'action="/admin/login"' in body
    assert 'href="/admin/static/styles.css"' in body


@pytest.mark.parametrize(
    ("case", "expected_action"),
    (
        (None, "/admin/eval/cases"),
        (
            SimpleNamespace(
                id=7,
                category="faq",
                question="q",
                expected_answer="a",
                expected_keywords=[],
                forbidden_keywords=[],
            ),
            "/admin/eval/cases/7",
        ),
    ),
)
def test_eval_case_form_action_stays_under_admin_prefix(case, expected_action):
    import eval_routes

    response = eval_routes.templates.TemplateResponse(
        request("/eval/cases/new"),
        "eval_case_edit.html",
        {
            "user": SimpleNamespace(
                username="owner", role="owner", csrf_token="csrf"
            ),
            "case": case,
        },
    )

    assert f'action="{expected_action}"' in response.body.decode("utf-8")


def test_router_eval_navigation_stays_under_admin_prefix():
    import eval_routes

    response = eval_routes.templates.TemplateResponse(
        request("/eval/router/"),
        "eval_list.html",
        {
            "user": SimpleNamespace(
                username="owner", role="owner", csrf_token="csrf"
            ),
            "suite": "router_v2",
            "cases": [],
            "problem_cases": [],
            "runs": [],
        },
    )

    assert 'href="/admin/eval/router/"' in response.body.decode("utf-8")


def test_security_eval_navigation_stays_under_admin_prefix():
    import eval_routes

    response = eval_routes.templates.TemplateResponse(
        request("/eval/security/"),
        "eval_list.html",
        {
            "user": SimpleNamespace(
                username="owner", role="owner", csrf_token="csrf"
            ),
            "suite": "security",
            "cases": [],
            "problem_cases": [],
            "runs": [],
        },
    )

    assert 'href="/admin/eval/security/"' in response.body.decode("utf-8")


def test_admin_templates_do_not_escape_public_prefix():
    templates = PROJECT_ROOT / "admin" / "templates"

    for path in templates.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert 'href="/' not in text, path.name
        assert 'action="/' not in text, path.name
        assert 'src="/' not in text, path.name
        assert "fetch('/" not in text, path.name
        assert "EventSource('/" not in text, path.name
