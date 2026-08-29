from datetime import datetime, timezone

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


def request(path="/eval/compact/", *, method="GET", root_path="/admin"):
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


def case():
    return {
        "id": 170,
        "case_key": "compact-fact-retention-001",
        "category": "fact_retention",
        "question": "Compact context: fact_retention (31 messages)",
        "input_data": {
            "context": [{"role": "user", "content": "PRIVATE TRANSCRIPT"}],
            "expected_mode": "llm",
        },
        "expected_data": {
            "required_facts": ["Интересуется криокапсулой"],
            "forbidden_facts": ["Запись подтверждена"],
        },
        "critical": True,
    }


@pytest.mark.asyncio
async def test_compact_index_is_owner_only_before_case_reads(monkeypatch):
    await install_user(monkeypatch, "admin")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("compact cases must not be read")

    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)
    with pytest.raises(HTTPException) as denied:
        await eval_routes.compact_eval_index(request())
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_compact_index_is_read_only_root_path_safe_and_hides_transcript(
    monkeypatch,
):
    await install_user(monkeypatch)
    calls = []

    async def list_cases(suite):
        calls.append(("cases", suite)); return [case()]
    async def list_problem_cases(suite):
        calls.append(("problems", suite)); return [case()]
    async def list_runs(limit, suite):
        calls.append(("runs", limit, suite)); return []

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", list_problem_cases)
    monkeypatch.setattr(eval_routes.evdb, "list_runs", list_runs)
    response = await eval_routes.compact_eval_index(request())
    body = response.body.decode()

    assert calls == [
        ("cases", "compact"), ("problems", "compact"),
        ("runs", 10, "compact"),
    ]
    assert 'action="/admin/eval/compact/runs"' in body
    assert 'action="/admin/eval/compact/runs/problematic"' in body
    assert 'href="/admin/eval/compact/"' in body
    assert "Compact Evaluation" in body
    assert "compact-fact-retention-001" in body
    assert "mode: llm" in body and "messages: 1" in body
    assert "required: 1" in body and "forbidden: 1" in body
    assert "PRIVATE TRANSCRIPT" not in body
    assert "/eval/cases/new" not in body and "/delete" not in body


@pytest.mark.asyncio
async def test_compact_start_requires_csrf_and_uses_compact_suite(monkeypatch):
    await install_user(monkeypatch)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("must stop before reads")
    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)
    with pytest.raises(HTTPException) as denied:
        await eval_routes.compact_eval_run_start(
            request(method="POST"), csrf_token="wrong"
        )
    assert denied.value.status_code == 403

    cases = [{"id": 170}]
    captured = []
    async def list_cases(suite):
        assert suite == "compact"; return cases
    async def create_run(*args):
        captured.append(("create", args)); return 203
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
    monkeypatch.setattr(eval_routes.eval_runner, "run_compact_eval_set", run_set)
    monkeypatch.setattr(eval_routes, "_start_eval_task", start_task)
    monkeypatch.setattr(eval_routes, "record_audit", audit)
    response = await eval_routes.compact_eval_run_start(
        request(method="POST"), csrf_token="known-csrf"
    )

    assert captured[:3] == [
        ("create", (1, eval_routes.eval_runner.ROUTER_MODEL, "compact")),
        ("runner", 203, cases), ("task", 203),
    ]
    assert response.headers["location"] == "/admin/eval/runs/203"


@pytest.mark.asyncio
async def test_compact_problem_rerun_and_empty_redirect_are_suite_isolated(
    monkeypatch,
):
    await install_user(monkeypatch)
    captured = []
    cases = [{"id": 171}]
    async def problems(suite):
        captured.append(("problems", suite)); return cases
    async def create(*args):
        captured.append(("create", args)); return 204
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
    monkeypatch.setattr(eval_routes.eval_runner, "run_compact_eval_set", run_set)
    monkeypatch.setattr(eval_routes, "_start_eval_task", start)
    monkeypatch.setattr(eval_routes, "record_audit", audit)

    response = await eval_routes.compact_eval_problem_run_start(
        request(path="/eval/compact/runs/problematic", method="POST"),
        csrf_token="known-csrf",
    )
    assert captured[:4] == [
        ("problems", "compact"),
        ("create", (1, eval_routes.eval_runner.ROUTER_MODEL, "compact")),
        ("runner", 204, cases), ("task", 204),
    ]

    async def no_cases(_suite):
        return []
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", no_cases)
    empty = await eval_routes.compact_eval_problem_run_start(
        request(path="/eval/compact/runs/problematic", method="POST"),
        csrf_token="known-csrf",
    )
    assert empty.headers["location"] == (
        "/admin/eval/compact/?error=no_problem_cases"
    )


@pytest.mark.asyncio
async def test_compact_detail_and_stream_are_owner_only_and_show_safe_metadata(
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    run = {
        "id": 203, "suite": "compact", "started_at": now,
        "finished_at": now, "total": 1, "passed": 1, "failed": 0,
        "status": "finished", "judge_model": "compact-model",
        "error_message": None,
    }
    async def get_run(_id):
        return run
    async def results(_id):
        return [{
            "case_id": 170, "question": "Compact context: safe description",
            "verdict": "pass", "check_layer": "compact", "score": 0.96,
            "duration_ms": 2,
            "input_data": {
                "context": [{"role": "user", "content": "PRIVATE TRANSCRIPT"}],
                "expected_mode": "llm",
            },
            "expected_data": {
                "required_facts": ["safe fact"],
                "forbidden_facts": ["unsafe fact"],
            },
            "actual_data": {
                "source": "llm", "reason_code": "compacted",
                "input_message_count": 31, "output_message_count": 11,
                "tail_count": 10, "structural_ok": True,
                "semantic_ok": True,
            },
            "judge_reasoning": "matched", "error_message": None,
        }]
    monkeypatch.setattr(eval_routes.evdb, "get_run", get_run)
    monkeypatch.setattr(eval_routes.evdb, "get_run_results", results)
    await install_user(monkeypatch, "admin")
    with pytest.raises(HTTPException):
        await eval_routes.eval_run_detail(request("/eval/runs/203"), 203)
    with pytest.raises(HTTPException):
        await eval_routes.eval_run_stream(request("/eval/runs/203/stream"), 203)

    await install_user(monkeypatch)
    response = await eval_routes.eval_run_detail(request("/eval/runs/203"), 203)
    body = response.body.decode()
    assert 'href="/admin/eval/compact/"' in body
    assert "Compact Evaluation" in body
    assert "source: llm" in body
    assert "input messages: 31" in body
    assert "output messages: 11" in body
    assert "tail: 10" in body
    assert "structural: pass" in body and "semantic: pass" in body
    assert "PRIVATE TRANSCRIPT" not in body
    assert "UNTRUSTED_COMPACT_CONTEXT" not in body
