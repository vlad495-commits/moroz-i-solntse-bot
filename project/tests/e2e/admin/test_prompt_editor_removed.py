from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import app as admin_app
from tests.e2e.admin.test_public_prefix import request


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/prompt/"),
    ("GET", "/prompt/?tab=router"),
    ("GET", "/prompt/versions/1"),
    ("POST", "/prompt/save"),
    ("POST", "/prompt/rollback/1"),
])
async def test_prompt_editor_endpoints_are_removed(method, path):
    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app), base_url="http://test"
    ) as client:
        response = await client.request(method, path)
    assert response.status_code == 404


@pytest.mark.parametrize("role", ["owner", "admin", "operator"])
def test_prompt_editor_is_absent_from_navigation(role):
    response = admin_app.templates.TemplateResponse(
        request(), "base.html",
        {"user": SimpleNamespace(username="user", role=role)},
    )
    body = response.body.decode()
    assert "/prompt/" not in body
    assert "/eval/" in body
    if role == "owner":
        assert "/bot-control/" in body
