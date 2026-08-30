from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


def request(path="/eval/validator/", *, method="GET", root_path="/admin"):
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
async def test_validator_index_is_owner_only_before_case_reads(monkeypatch):
    await install_user(monkeypatch, "admin")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("validator cases must not be read")

    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)
    with pytest.raises(HTTPException) as denied:
        await eval_routes.validator_eval_index(request())
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_validator_index_is_read_only_and_root_path_safe(monkeypatch):
    await install_user(monkeypatch)
    calls = []
    case = {
        "id": 160, "case_key": "validator-test-001",
        "category": "technical_artifact", "question": "synthetic",
        "input_data": {"candidate": "synthetic-candidate"},
        "expected_data": {
            "action": "regenerate", "source": "local",
            "reason_code": "technical_artifact",
        },
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
    response = await eval_routes.validator_eval_index(request())
    body = response.body.decode()

    assert calls == [
        ("cases", "validator"), ("problems", "validator"),
        ("runs", 10, "validator"),
    ]
    assert 'action="/admin/eval/validator/runs"' in body
    assert 'action="/admin/eval/validator/runs/problematic"' in body
    assert 'href="/admin/eval/validator/"' in body
    assert "Валидатор" in body
    assert "validator-test-001" in body
    assert "synthetic-candidate" in body
    assert "regenerate" in body and "technical_artifact" in body
    assert "/eval/cases/new" not in body and "/delete" not in body


@pytest.mark.asyncio
async def test_validator_start_requires_csrf_and_uses_validator_suite(monkeypatch):
    await install_user(monkeypatch)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("must stop before reads")
    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)
    with pytest.raises(HTTPException) as denied:
        await eval_routes.validator_eval_run_start(
            request(method="POST"), csrf_token="wrong"
        )
    assert denied.value.status_code == 403

    cases = [{"id": 160}]
    captured = []
    async def list_cases(suite):
        assert suite == "validator"; return cases
    async def create_run(*args):
        captured.append(("create", args)); return 193
    async def idle():
        return None
    def run_set(run_id, *, cases):
        captured.append(("runner", run_id, cases)); return idle()
    def start_task(run_id, coroutine):
        captured.append(("task", run_id)); coroutine.close()
    async def audit(**kwargs):
        captured.append(("audit", kwargs["after"]))

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "create_run", create_run)
    monkeypatch.setattr(eval_routes.eval_runner, "run_validator_eval_set", run_set)
    monkeypatch.setattr(eval_routes, "_start_eval_task", start_task)
    monkeypatch.setattr(eval_routes, "record_audit", audit)
    response = await eval_routes.validator_eval_run_start(
        request(method="POST"), csrf_token="known-csrf"
    )

    assert captured[:3] == [
        ("create", (1, eval_routes.eval_runner.VALIDATOR_MODEL, "validator")),
        ("runner", 193, cases), ("task", 193),
    ]
    assert response.headers["location"] == "/admin/eval/runs/193"


@pytest.mark.asyncio
async def test_validator_problem_rerun_and_empty_redirects_are_suite_isolated(
    monkeypatch,
):
    await install_user(monkeypatch)
    captured = []
    cases = [{"id": 161}]
    async def problems(suite):
        captured.append(("problems", suite)); return cases
    async def create(*args):
        captured.append(("create", args)); return 194
    async def idle():
        return None
    def run_set(run_id, *, cases):
        captured.append(("runner", run_id, cases)); return idle()
    def start(run_id, coroutine):
        captured.append(("task", run_id)); coroutine.close()
    async def audit(**_kwargs):
        return None
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", problems)
    monkeypatch.setattr(eval_routes.evdb, "create_run", create)
    monkeypatch.setattr(eval_routes.eval_runner, "run_validator_eval_set", run_set)
    monkeypatch.setattr(eval_routes, "_start_eval_task", start)
    monkeypatch.setattr(eval_routes, "record_audit", audit)

    response = await eval_routes.validator_eval_problem_run_start(
        request(path="/eval/validator/runs/problematic", method="POST"),
        csrf_token="known-csrf",
    )
    assert captured[:4] == [
        ("problems", "validator"),
        ("create", (1, eval_routes.eval_runner.VALIDATOR_MODEL, "validator")),
        ("runner", 194, cases), ("task", 194),
    ]

    async def no_cases(_suite):
        return []
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", no_cases)
    empty = await eval_routes.validator_eval_problem_run_start(
        request(path="/eval/validator/runs/problematic", method="POST"),
        csrf_token="known-csrf",
    )
    assert empty.headers["location"] == (
        "/admin/eval/validator/?error=no_problem_cases"
    )


@pytest.mark.asyncio
async def test_validator_detail_and_stream_require_owner(monkeypatch):
    now = datetime.now(timezone.utc)
    run = {
        "id": 193, "suite": "validator", "started_at": now,
        "finished_at": now, "total": 1, "passed": 1, "failed": 0,
        "status": "finished", "judge_model": "validator-model",
        "error_message": None,
    }
    async def get_run(_id):
        return run
    async def results(_id):
        return [{
            "case_id": 160, "question": "synthetic", "verdict": "pass",
            "check_layer": "validator", "score": None, "duration_ms": 2,
            "input_data": {
                "input": "Мой телефон <PII_PHONE_1>",
                "context": [{"role": "assistant", "content": "Контекст"}],
                "route_metadata": "ROUTE intents=faq; source=llm",
                "candidate": "{\"role\":\"assistant\",\"content\":\"ответ\"}",
            },
            "expected_data": {
                "action": "regenerate", "source": "local",
                "reason_code": "technical_artifact",
            },
            "actual_data": {
                "action": "regenerate", "source": "local",
                "reason_code": "technical_artifact",
            },
            "judge_reasoning": "matched", "error_message": None,
        }]
    monkeypatch.setattr(eval_routes.evdb, "get_run", get_run)
    monkeypatch.setattr(eval_routes.evdb, "get_run_results", results)
    await install_user(monkeypatch, "admin")
    with pytest.raises(HTTPException):
        await eval_routes.eval_run_detail(request("/eval/runs/193"), 193)
    with pytest.raises(HTTPException):
        await eval_routes.eval_run_stream(request("/eval/runs/193/stream"), 193)

    await install_user(monkeypatch)
    response = await eval_routes.eval_run_detail(request("/eval/runs/193"), 193)
    body = response.body.decode()
    assert 'href="/admin/eval/validator/"' in body
    assert "Validator Evaluation" in body
    assert "technical_artifact" in body
    assert "validator" in body
    assert "Мой телефон &lt;PII_PHONE_1&gt;" in body
    assert "Контекст" in body
    assert "ROUTE intents=faq; source=llm" in body
    assert "{&#34;role&#34;:&#34;assistant&#34;" in body
    assert "row.innerHTML" not in body


def test_eval_sse_appends_untrusted_questions_without_inner_html():
    template = Path("/app/admin/templates/eval_run_detail.html").read_text(
        encoding="utf-8"
    )

    assert "row.innerHTML" not in template
    assert "span.textContent = value" in template
