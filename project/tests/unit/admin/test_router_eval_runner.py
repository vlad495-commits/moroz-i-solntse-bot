import logging

import pytest

import eval_runner
from moroz.messaging.router import RouteDecision, RouterVerdict


class CapturingRouter:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    async def route(self, text, context):
        self.calls.append((text, context))
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def router_case(**overrides):
    value = {
        "id": 7,
        "suite": "router_v2",
        "case_key": "router-v2-test-001",
        "category": "context",
        "question": "А сколько это?",
        "input_data": {"input": "А сколько это?", "context": []},
        "expected_data": {"route": "consultation"},
        "critical": False,
    }
    value.update(overrides)
    return value


def test_router_case_diff_compares_only_single_route():
    expected = {"route": "booking"}
    actual = RouteDecision("booking", 0.83)
    assert eval_runner.router_case_diff(expected, actual) == (True, "matched")
    assert eval_runner.router_case_diff(
        expected, RouteDecision("consultation", 0.9)
    ) == (False, "route_mismatch")


@pytest.mark.asyncio
async def test_quality_case_masks_pii_and_never_calls_answer_or_judge(monkeypatch):
    router = CapturingRouter(
        RouterVerdict(RouteDecision("consultation", 0.9), (), source="llm")
    )
    case = router_case(
        question="Мой телефон +7 900 111-22-33, а сколько это?",
        input_data={
            "input": "Мой телефон +7 900 111-22-33, а сколько это?",
            "context": [
                {"role": "assistant", "content": "Рассказываю про криотерапию"},
                {"role": "user", "content": "test@example.invalid"},
            ],
        },
    )
    saved = {}

    async def save_result(*args, **kwargs):
        saved.update({"args": args, "kwargs": kwargs})
        return 501

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("answer LLM and judge must not run")

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    monkeypatch.setattr(eval_runner, "_generate_bot_response", forbidden)
    monkeypatch.setattr(eval_runner, "llm_judge", forbidden)

    result = await eval_runner.run_router_case(case, 12, router=router)

    assert "+7 900 111-22-33" not in repr(router.calls)
    assert "test@example.invalid" not in repr(router.calls)
    assert result == {
        "id": 501,
        "case_id": 7,
        "verdict": "pass",
        "check_layer": "router",
    }
    assert saved["kwargs"]["actual_data"]["route"] == "consultation"
    assert saved["kwargs"]["actual_data"]["source"] == "llm"
    assert saved["kwargs"]["judge_reasoning"] == "matched"


@pytest.mark.asyncio
async def test_deterministic_case_never_calls_llm_router(monkeypatch):
    router = CapturingRouter(AssertionError("LLM router must stay unused"))
    case = router_case(
        question="Хочу записаться",
        input_data={"input": "Хочу записаться", "context": []},
        expected_data={"route": "booking"},
    )
    saved = {}

    async def save_result(*_args, **kwargs):
        saved.update(kwargs)
        return 502

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    result = await eval_runner.run_router_case(case, 13, router=router)

    assert result["verdict"] == "pass"
    assert router.calls == []
    assert saved["actual_data"]["source"] == "deterministic"


@pytest.mark.asyncio
async def test_router_eval_set_is_sequential_and_uses_existing_gate(monkeypatch):
    events = []
    finished = []
    cases = [
        router_case(id=index, critical=index == 2, passes=index == 1)
        for index in (1, 2)
    ]

    async def run_case(case, run_id, *, router):
        events.append(("case", case["id"], run_id, router))
        return {
            "id": case["id"],
            "case_id": case["id"],
            "verdict": "pass" if case["passes"] else "fail",
            "check_layer": "router",
        }

    async def progress(run_id, passed, failed):
        events.append(("progress", run_id, passed, failed))

    async def finish(*args, **kwargs):
        finished.append((args, kwargs))

    fake_router = object()
    monkeypatch.setattr(eval_runner, "run_router_case", run_case)
    monkeypatch.setattr(eval_runner.evdb, "update_run_progress", progress)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish)

    await eval_runner.run_router_eval_set(15, cases=cases, router=fake_router)

    assert events == [
        ("case", 1, 15, fake_router),
        ("progress", 15, 1, 0),
        ("case", 2, 15, fake_router),
        ("progress", 15, 1, 1),
    ]
    assert finished == [((15, 1, 1), {"status": "failed"})]


@pytest.mark.asyncio
async def test_router_eval_set_loads_only_v2_cases(monkeypatch):
    loaded = []

    async def list_cases(suite):
        loaded.append(suite)
        return []

    async def finish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(eval_runner.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish)

    await eval_runner.run_router_eval_set(17, router=object())

    assert loaded == ["router_v2"]


@pytest.mark.asyncio
async def test_router_errors_store_and_log_only_error_type(monkeypatch, caplog):
    sentinel = "https://secret:password@provider.invalid raw-provider-response"
    router = CapturingRouter(RuntimeError(sentinel))
    saved = {}

    async def save_result(*_args, **kwargs):
        saved.update(kwargs)
        return 503

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        result = await eval_runner.run_router_case(
            router_case(question="private-question-sentinel"),
            16,
            router=router,
        )

    assert result["verdict"] == "error"
    assert saved["error_message"] == "RuntimeError"
    assert saved["actual_data"] == {}
    assert sentinel not in caplog.text
    assert sentinel not in repr(saved)
    assert "private-question-sentinel" not in caplog.text
