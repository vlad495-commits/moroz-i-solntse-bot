from datetime import datetime, timezone

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import eval_routes
from auth import AuthenticatedUser


def request(
    path: str = "/eval/router/",
    *,
    method: str = "GET",
    root_path: str = "/admin",
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "root_path": root_path,
            "headers": [(b"user-agent", b"pytest")],
            "query_string": b"",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
        }
    )


def user(role="owner") -> AuthenticatedUser:
    return AuthenticatedUser(
        id=7,
        username=role,
        role=role,
        csrf_token="known-csrf",
        session_id="session-id",
    )


async def install_user(monkeypatch, role="owner"):
    async def current_user(_request):
        return user(role)

    monkeypatch.setattr(eval_routes, "get_current_user", current_user)


@pytest.mark.asyncio
async def test_router_index_is_owner_only_before_case_reads(monkeypatch):
    await install_user(monkeypatch, "admin")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Router cases must not be read for non-owner")

    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)

    with pytest.raises(HTTPException) as denied:
        await eval_routes.router_eval_index(request())

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_router_start_rejects_csrf_before_case_reads(monkeypatch):
    await install_user(monkeypatch)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Router cases must not be read before CSRF passes")

    monkeypatch.setattr(eval_routes.evdb, "list_cases", forbidden)

    with pytest.raises(HTTPException) as denied:
        await eval_routes.router_eval_run_start(
            request(method="POST"),
            csrf_token="wrong-csrf",
        )

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_router_index_uses_router_only_counts_and_read_only_root_urls(
    monkeypatch,
):
    await install_user(monkeypatch)
    calls = []
    case = {
        "id": 41,
        "case_key": "router-v2-case-001",
        "category": "context",
        "question": "Синтетический вопрос",
        "expected_data": {"route": "booking"},
        "critical": True,
    }

    async def list_cases(suite):
        calls.append(("cases", suite))
        return [case]

    async def list_problem_cases(suite):
        calls.append(("problems", suite))
        return [case]

    async def list_runs(limit, suite):
        calls.append(("runs", limit, suite))
        return []

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "list_problem_cases", list_problem_cases)
    monkeypatch.setattr(eval_routes.evdb, "list_runs", list_runs)

    response = await eval_routes.router_eval_index(request())
    body = response.body.decode("utf-8")

    assert calls == [
        ("cases", "router_v2"),
        ("problems", "router_v2"),
        ("runs", 10, "router_v2"),
    ]
    assert 'action="/admin/eval/router/runs"' in body
    assert 'action="/admin/eval/router/runs/problematic"' in body
    assert "router-v2-case-001" in body
    assert "booking" in body
    assert "/eval/cases/new" not in body
    assert "/delete" not in body


@pytest.mark.asyncio
async def test_router_run_start_reuses_supervision_and_audits(monkeypatch):
    await install_user(monkeypatch)
    cases = [{"id": 41}]
    created = []
    started = []
    audited = []

    async def list_cases(suite):
        assert suite == "router_v2"
        return cases

    async def create_run(*args, **kwargs):
        created.append((args, kwargs))
        return 91

    async def idle():
        return None

    def run_router_eval_set(run_id, *, cases):
        started.append(("runner", run_id, cases))
        return idle()

    def start_task(run_id, coroutine):
        started.append(("task", run_id))
        coroutine.close()

    async def audit(**kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "create_run", create_run)
    monkeypatch.setattr(
        eval_routes.eval_runner,
        "run_router_eval_set",
        run_router_eval_set,
    )
    monkeypatch.setattr(eval_routes, "_start_eval_task", start_task)
    monkeypatch.setattr(eval_routes, "record_audit", audit)

    response = await eval_routes.router_eval_run_start(
        request(method="POST"),
        csrf_token="known-csrf",
    )

    assert created == [
        ((1, eval_routes.eval_runner.ROUTER_MODEL, "router_v2"), {})
    ]
    assert started == [
        ("runner", 91, cases),
        ("task", 91),
    ]
    assert audited[0]["action"] == "eval.router_run_start"
    assert audited[0]["after"] == {"total": 1, "suite": "router_v2"}
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/eval/runs/91"


@pytest.mark.asyncio
async def test_router_problem_rerun_uses_only_router_problem_cases(monkeypatch):
    await install_user(monkeypatch)
    cases = [{"id": 52}]
    captured = []

    async def list_problem_cases(suite):
        captured.append(("problems", suite))
        return cases

    async def create_run(*args):
        captured.append(("create", args))
        return 92

    async def idle():
        return None

    def run_router_eval_set(run_id, *, cases):
        captured.append(("runner", run_id, cases))
        return idle()

    def start_task(run_id, coroutine):
        captured.append(("task", run_id))
        coroutine.close()

    async def audit(**kwargs):
        captured.append(("audit", kwargs["after"]))

    monkeypatch.setattr(
        eval_routes.evdb, "list_problem_cases", list_problem_cases
    )
    monkeypatch.setattr(eval_routes.evdb, "create_run", create_run)
    monkeypatch.setattr(
        eval_routes.eval_runner,
        "run_router_eval_set",
        run_router_eval_set,
    )
    monkeypatch.setattr(eval_routes, "_start_eval_task", start_task)
    monkeypatch.setattr(eval_routes, "record_audit", audit)

    response = await eval_routes.router_eval_problem_run_start(
        request(path="/eval/router/runs/problematic", method="POST"),
        csrf_token="known-csrf",
    )

    assert captured[:4] == [
        ("problems", "router_v2"),
        ("create", (1, eval_routes.eval_runner.ROUTER_MODEL, "router_v2")),
        ("runner", 92, cases),
        ("task", 92),
    ]
    assert response.headers["location"] == "/admin/eval/runs/92"


@pytest.mark.asyncio
async def test_router_detail_requires_owner_and_renders_structured_payload(
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    run = {
        "id": 91,
        "suite": "router_v2",
        "started_at": now,
        "finished_at": now,
        "total": 1,
        "passed": 1,
        "failed": 0,
        "status": "finished",
        "judge_model": "router-model",
        "error_message": None,
    }

    async def get_run(_run_id):
        return run

    async def get_results(_run_id):
        return [
            {
                "case_id": 41,
                "question": "Синтетический вопрос",
                "verdict": "pass",
                "check_layer": "router",
                "score": None,
                "duration_ms": 3,
                "expected_data": {"route": "consultation"},
                "actual_data": {
                    "route": "consultation",
                    "source": "llm",
                    "confidence": 0.9,
                    "reason_code": None,
                },
                "error_message": None,
            }
        ]

    monkeypatch.setattr(eval_routes.evdb, "get_run", get_run)
    monkeypatch.setattr(eval_routes.evdb, "get_run_results", get_results)
    await install_user(monkeypatch, "admin")
    with pytest.raises(HTTPException) as denied:
        await eval_routes.eval_run_detail(
            request(path="/eval/runs/91"),
            91,
        )
    assert denied.value.status_code == 403

    await install_user(monkeypatch, "owner")
    response = await eval_routes.eval_run_detail(
        request(path="/eval/runs/91"),
        91,
    )
    body = response.body.decode("utf-8")
    assert 'href="/admin/eval/router/"' in body
    assert "Ожидалось" in body
    assert "Фактически" in body
    assert "consultation" in body
    assert "0.9" in body


@pytest.mark.asyncio
async def test_router_stream_requires_owner_before_streaming(monkeypatch):
    await install_user(monkeypatch, "admin")

    async def get_run(_run_id):
        return {"id": 91, "suite": "router"}

    monkeypatch.setattr(eval_routes.evdb, "get_run", get_run)

    with pytest.raises(HTTPException) as denied:
        await eval_routes.eval_run_stream(
            request(path="/eval/runs/91/stream"),
            91,
        )

    assert denied.value.status_code == 403


def test_historical_router_detail_still_renders_v1_contract():
    now = datetime.now(timezone.utc)
    response = eval_routes.templates.TemplateResponse(
        request(path="/eval/runs/90"),
        "eval_run_detail.html",
        {
            "user": user(),
            "run": {
                "id": 90,
                "suite": "router",
                "started_at": now,
                "finished_at": now,
                "total": 1,
                "passed": 1,
                "failed": 0,
                "status": "finished",
                "judge_model": "legacy-router",
                "error_message": None,
            },
            "results": [
                {
                    "case_id": 1,
                    "question": "legacy",
                    "verdict": "pass",
                    "check_layer": "router",
                    "score": None,
                    "duration_ms": 1,
                    "expected_data": {
                        "intents": ["faq"],
                        "requires_clarification": False,
                        "source": "llm",
                    },
                    "actual_data": {
                        "intents": ["faq"],
                        "requires_clarification": False,
                        "source": "llm",
                        "confidence": 0.9,
                    },
                    "judge_reasoning": "matched",
                }
            ],
        },
    )

    body = response.body.decode("utf-8")
    assert 'href="/admin/eval/router/"' in body
    assert "intents: faq" in body
    assert "clarification: нет" in body


def test_answer_template_keeps_crud_and_answer_urls():
    response = eval_routes.templates.TemplateResponse(
        request(path="/eval/"),
        "eval_list.html",
        {
            "user": user(),
            "suite": "answer",
            "cases": [
                {
                    "id": 1,
                    "category": "faq",
                    "question": "q",
                    "expected_answer": "a",
                }
            ],
            "problem_cases": [],
            "runs": [],
        },
    )
    body = response.body.decode("utf-8")

    assert 'action="/admin/eval/runs"' in body
    assert 'href="/admin/eval/cases/new"' in body
    assert 'href="/admin/eval/cases/1"' in body
    assert 'action="/admin/eval/cases/1/delete"' in body
