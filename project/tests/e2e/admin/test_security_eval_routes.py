from datetime import datetime, timezone

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


def request(path="/eval/security/", *, method="GET", root_path="/admin"):
    return Request({
        "type": "http", "method": method, "scheme": "https", "path": path,
        "root_path": root_path, "headers": [(b"user-agent", b"pytest")],
        "query_string": b"", "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
    })


def user(role="owner"):
    return AuthenticatedUser(7, role, role, "known-csrf", "session-id")


async def install_user(monkeypatch, role="owner"):
    async def current_user(_request):
        return user(role)
    monkeypatch.setattr(eval_routes, "get_current_user", current_user)


@pytest.mark.asyncio
async def test_security_index_is_owner_only_before_case_reads(monkeypatch):
    await install_user(monkeypatch, "admin")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("security cases must not be read")

    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)
    with pytest.raises(HTTPException) as denied:
        await eval_routes.security_eval_index(request())
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_security_index_is_read_only_and_root_path_safe(monkeypatch):
    await install_user(monkeypatch)
    calls = []
    case = {
        "id": 70, "case_key": "security-test-001", "category": "prompt_attack",
        "question": "synthetic", "expected_data": {"action": "block", "source": "llm"},
        "critical": True,
    }

    async def list_cases(suite):
        calls.append(("cases", suite)); return [case]
    async def list_problem_cases(suite):
        calls.append(("problems", suite)); return [case]
    async def list_runs(limit, suite):
        calls.append(("runs", limit, suite)); return []

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", list_problem_cases)
    monkeypatch.setattr(eval_routes.evdb, "list_runs", list_runs)
    response = await eval_routes.security_eval_index(request())
    body = response.body.decode()

    assert calls == [("cases", "security"), ("problems", "security"), ("runs", 10, "security")]
    assert 'action="/admin/eval/security/runs"' in body
    assert 'action="/admin/eval/security/runs/problematic"' in body
    assert "security-test-001" in body and "block" in body and "llm" in body
    assert "/eval/cases/new" not in body and "/delete" not in body


@pytest.mark.asyncio
async def test_security_start_requires_csrf_and_uses_security_suite(monkeypatch):
    await install_user(monkeypatch)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("must stop before reads")
    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)
    with pytest.raises(HTTPException) as denied:
        await eval_routes.security_eval_run_start(
            request(method="POST"), csrf_token="wrong"
        )
    assert denied.value.status_code == 403

    cases = [{"id": 70}]
    captured = []
    async def list_cases(suite):
        assert suite == "security"; return cases
    async def create_run(*args):
        captured.append(("create", args)); return 93
    async def idle(): return None
    def run_set(run_id, *, cases):
        captured.append(("runner", run_id, cases)); return idle()
    def start_task(run_id, coroutine):
        captured.append(("task", run_id)); coroutine.close()
    async def audit(**kwargs): captured.append(("audit", kwargs["after"]))

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "create_run", create_run)
    monkeypatch.setattr(eval_routes.eval_runner, "run_security_eval_set", run_set)
    monkeypatch.setattr(eval_routes, "_start_eval_task", start_task)
    monkeypatch.setattr(eval_routes, "record_audit", audit)
    response = await eval_routes.security_eval_run_start(
        request(method="POST"), csrf_token="known-csrf"
    )

    assert captured[:3] == [
        ("create", (1, eval_routes.eval_runner.SECURITY_MODEL, "security")),
        ("runner", 93, cases), ("task", 93),
    ]
    assert response.headers["location"] == "/admin/eval/runs/93"


@pytest.mark.asyncio
async def test_security_problem_rerun_is_suite_isolated(monkeypatch):
    await install_user(monkeypatch)
    captured = []
    cases = [{"id": 71}]
    async def problems(suite): captured.append(("problems", suite)); return cases
    async def create(*args): captured.append(("create", args)); return 94
    async def idle(): return None
    def run_set(run_id, *, cases): captured.append(("runner", run_id, cases)); return idle()
    def start(run_id, coroutine): captured.append(("task", run_id)); coroutine.close()
    async def audit(**_kwargs): return None
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", problems)
    monkeypatch.setattr(eval_routes.evdb, "create_run", create)
    monkeypatch.setattr(eval_routes.eval_runner, "run_security_eval_set", run_set)
    monkeypatch.setattr(eval_routes, "_start_eval_task", start)
    monkeypatch.setattr(eval_routes, "record_audit", audit)

    response = await eval_routes.security_eval_problem_run_start(
        request(path="/eval/security/runs/problematic", method="POST"),
        csrf_token="known-csrf",
    )
    assert captured[:4] == [
        ("problems", "security"),
        ("create", (1, eval_routes.eval_runner.SECURITY_MODEL, "security")),
        ("runner", 94, cases), ("task", 94),
    ]
    assert response.headers["location"] == "/admin/eval/runs/94"


@pytest.mark.asyncio
async def test_security_detail_and_stream_require_owner(monkeypatch):
    now = datetime.now(timezone.utc)
    run = {
        "id": 93, "suite": "security", "started_at": now, "finished_at": now,
        "total": 1, "passed": 1, "failed": 0, "status": "finished",
        "judge_model": "security-model", "error_message": None,
    }
    async def get_run(_id): return run
    async def results(_id): return [{
        "case_id": 70, "question": "synthetic", "verdict": "pass",
        "check_layer": "security", "score": None, "duration_ms": 2,
        "expected_data": {"action": "block", "source": "llm"},
        "actual_data": {"action": "block", "source": "llm", "reason_code": "prompt_attack"},
        "judge_reasoning": "matched", "error_message": None,
    }]
    monkeypatch.setattr(eval_routes.evdb, "get_run", get_run)
    monkeypatch.setattr(eval_routes.evdb, "get_run_results", results)
    await install_user(monkeypatch, "admin")
    with pytest.raises(HTTPException):
        await eval_routes.eval_run_detail(request("/eval/runs/93"), 93)
    with pytest.raises(HTTPException):
        await eval_routes.eval_run_stream(request("/eval/runs/93/stream"), 93)

    await install_user(monkeypatch)
    response = await eval_routes.eval_run_detail(request("/eval/runs/93"), 93)
    body = response.body.decode()
    assert 'href="/admin/eval/security/"' in body
    assert "Input Security Evaluation" in body
    assert "prompt_attack" in body

