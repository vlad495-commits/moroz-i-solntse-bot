from pathlib import Path

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


def test_admin_templates_do_not_escape_public_prefix():
    templates = PROJECT_ROOT / "admin" / "templates"

    for path in templates.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert 'href="/' not in text, path.name
        assert 'action="/' not in text, path.name
        assert 'src="/' not in text, path.name
        assert "fetch('/" not in text, path.name
        assert "EventSource('/" not in text, path.name
