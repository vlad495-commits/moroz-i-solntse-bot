import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import eval_routes
import eval_runner


def _load_run_evals_module():
    stubs = {
        "config": SimpleNamespace(
            GUARDRAILS_INPUT_CATEGORIES=["configured"],
            GUARDRAILS_INPUT_ENABLED=True,
        ),
        "guardrails": SimpleNamespace(check_input=lambda _text: (True, "")),
        "llm": SimpleNamespace(init_llm=lambda: None, generate_response=None),
    }
    originals = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "run_evals_under_test", Path("/app/llm/eval/run_evals.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


run_evals = _load_run_evals_module()


class EvalInitError(RuntimeError):
    pass


class EvalFinalizeError(RuntimeError):
    pass


class EvalBackgroundError(RuntimeError):
    pass


class CliProviderError(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_run_eval_set_catches_init_failure_and_persists_only_type(
    monkeypatch, caplog
):
    sentinel = "https://user:password@provider init-user-sentinel"
    finished = []

    def fail_init():
        raise EvalInitError(sentinel)

    async def finish_run(*args, **kwargs):
        finished.append((args, kwargs))

    monkeypatch.setattr(eval_runner, "_init_clients", fail_init)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish_run)

    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        await eval_runner.run_eval_set(51, cases=[])

    assert finished == [
        ((51, 0, 0), {"status": "error", "error_message": "EvalInitError"})
    ]
    assert "eval_run_failed run_id=51 error_type=EvalInitError" in caplog.text
    assert sentinel not in caplog.text
    assert sentinel not in repr(finished)


@pytest.mark.asyncio
async def test_run_eval_set_recovers_from_success_finalization_failure(
    monkeypatch, caplog
):
    sentinel = "https://user:password@provider finalize-user-sentinel"
    calls = []

    async def finish_run(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise EvalFinalizeError(sentinel)

    monkeypatch.setattr(eval_runner, "_init_clients", lambda: None)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish_run)

    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        await eval_runner.run_eval_set(52, cases=[])

    assert calls == [
        ((52, 0, 0), {"status": "finished"}),
        (
            (52, 0, 0),
            {"status": "error", "error_message": "EvalFinalizeError"},
        ),
    ]
    assert "eval_run_failed run_id=52 error_type=EvalFinalizeError" in caplog.text
    assert sentinel not in caplog.text
    assert sentinel not in repr(calls)


@pytest.mark.asyncio
async def test_eval_route_owns_and_retrieves_background_task(monkeypatch, caplog):
    sentinel = "https://user:password@provider background-user-sentinel"
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    exception_contexts = []
    loop.set_exception_handler(lambda _loop, context: exception_contexts.append(context))

    monkeypatch.setattr(eval_routes, "get_current_user", lambda _request: "admin")

    async def list_cases():
        return [{"id": 1}]

    async def create_run(**_kwargs):
        return 53

    async def fail_run(_run_id):
        await release.wait()
        raise EvalBackgroundError(sentinel)

    monkeypatch.setattr(eval_routes.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_routes.evdb, "create_run", create_run)
    monkeypatch.setattr(eval_routes.eval_runner, "run_eval_set", fail_run)
    before = asyncio.all_tasks()

    try:
        with caplog.at_level(logging.ERROR, logger=eval_routes.logger.name):
            response = await eval_routes.eval_run_start(object(), object())
            created = tuple(asyncio.all_tasks() - before)
            assert len(created) == 1
            task = created[0]
            owned_tasks = getattr(eval_routes, "_eval_tasks", None)
            was_owned = owned_tasks is not None and task in owned_tasks
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)

    assert response.status_code == 302
    assert was_owned
    assert task not in eval_routes._eval_tasks
    assert "eval_background_failed run_id=53 error_type=EvalBackgroundError" in caplog.text
    assert sentinel not in caplog.text
    assert exception_contexts == []


@pytest.mark.asyncio
async def test_eval_cli_dataset_output_contains_only_safe_metadata(
    monkeypatch, capsys
):
    provider_sentinel = "https://user:password@provider dataset-user-sentinel"
    input_sentinel = "private-input-sentinel"
    missing_sentinel = "missing-word-sentinel"
    calls = 0

    monkeypatch.setattr(
        run_evals,
        "_load_dataset",
        lambda _name: [
            {"id": "unsafe-id", "input": input_sentinel, "expected_contains": []},
            {"id": 62, "input": input_sentinel, "expected_contains": [missing_sentinel]},
        ],
    )
    monkeypatch.setattr(run_evals, "init_llm", lambda: None)

    async def generate_response(_input, context):
        nonlocal calls
        assert context == []
        calls += 1
        if calls == 1:
            raise CliProviderError(provider_sentinel)
        return SimpleNamespace(text="safe response")

    monkeypatch.setattr(run_evals, "generate_response", generate_response)

    assert await run_evals._run_dataset() == (0, 2)
    output = capsys.readouterr().out

    assert "case=1 status=error error_type=CliProviderError" in output
    assert "case=62 status=failed missing_count=1" in output
    assert provider_sentinel not in output
    assert input_sentinel not in output
    assert missing_sentinel not in output
    assert "unsafe-id" not in output


@pytest.mark.asyncio
async def test_eval_cli_adversarial_output_hides_dataset_and_guardrail_values(
    monkeypatch, capsys
):
    input_sentinel = "adversarial-input-sentinel"
    technique_sentinel = "technique-sentinel"
    reason_sentinel = "guardrail-reason-sentinel"
    monkeypatch.setattr(run_evals, "GUARDRAILS_INPUT_ENABLED", True)
    monkeypatch.setattr(run_evals, "GUARDRAILS_INPUT_CATEGORIES", ["configured"])
    monkeypatch.setattr(
        run_evals,
        "_load_dataset",
        lambda _name: [
            {
                "id": 71,
                "input": input_sentinel,
                "technique": technique_sentinel,
                "expected": "input_blocked",
            }
        ],
    )
    monkeypatch.setattr(run_evals, "check_input", lambda _text: (False, reason_sentinel))

    assert await run_evals._run_adversarial() == (1, 0)
    output = capsys.readouterr().out

    assert "case=71 status=blocked" in output
    assert input_sentinel not in output
    assert technique_sentinel not in output
    assert reason_sentinel not in output
