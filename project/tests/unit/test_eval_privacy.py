import asyncio
import json
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

import eval_routes
import eval_runner
from eval import run_evals


class EvalInitError(RuntimeError):
    pass


class EvalFinalizeError(RuntimeError):
    pass


class EvalBackgroundError(RuntimeError):
    pass


class CliProviderError(RuntimeError):
    pass


class CliDatasetError(RuntimeError):
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
    monkeypatch.setattr(
        run_evals,
        "_load_guardrail_checker",
        lambda: lambda _text: (False, reason_sentinel),
    )
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
    assert await run_evals._run_adversarial() == (1, 0)
    output = capsys.readouterr().out

    assert "case=71 status=blocked" in output
    assert input_sentinel not in output
    assert technique_sentinel not in output
    assert reason_sentinel not in output


@pytest.mark.parametrize(
    ("error", "sentinel"),
    [
        (OSError("unreadable-dataset-sentinel"), "unreadable-dataset-sentinel"),
        (
            json.JSONDecodeError("malformed", "malformed-dataset-sentinel", 0),
            "malformed-dataset-sentinel",
        ),
    ],
)
@pytest.mark.asyncio
async def test_eval_cli_dataset_load_error_is_nonzero_and_redacted(
    monkeypatch, capsys, error, sentinel
):
    def fail_load(_name):
        raise error

    monkeypatch.setattr(run_evals, "_load_dataset", fail_load)

    assert await run_evals._run_dataset() == (0, 1)
    output = capsys.readouterr().out
    assert f"status=error error_type={type(error).__name__}" in output
    assert sentinel not in output


@pytest.mark.asyncio
async def test_eval_cli_adversarial_load_error_is_nonzero_and_redacted(
    monkeypatch, capsys
):
    sentinel = "C:/private/adversarial.json malformed-user-sentinel"
    monkeypatch.setattr(
        run_evals, "_load_guardrail_checker", lambda: lambda _text: (True, "")
    )

    def fail_load(_name):
        raise CliDatasetError(sentinel)

    monkeypatch.setattr(run_evals, "_load_dataset", fail_load)

    assert await run_evals._run_adversarial() == (0, 1)
    output = capsys.readouterr().out
    assert "status=error error_type=CliDatasetError" in output
    assert sentinel not in output


@pytest.mark.asyncio
async def test_eval_cli_init_failure_is_nonzero_and_redacted(monkeypatch, capsys):
    sentinel = "https://user:password@provider init-user-sentinel"
    monkeypatch.setattr(
        run_evals,
        "_load_dataset",
        lambda _name: [{"id": 1, "input": "safe", "expected_contains": []}],
    )

    def fail_init():
        raise CliProviderError(sentinel)

    monkeypatch.setattr(run_evals, "init_llm", fail_init)

    assert await run_evals._run_dataset() == (0, 1)
    output = capsys.readouterr().out
    assert "status=error error_type=CliProviderError" in output
    assert sentinel not in output


def test_eval_cli_real_foundation_module_imports_without_stubs():
    result = subprocess.run(
        [sys.executable, "-c", "import eval.run_evals"],
        cwd="/app/llm",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_eval_cli_real_adversarial_mode_skips_safely_without_guardrails():
    result = subprocess.run(
        [sys.executable, "-m", "eval.run_evals", "--only", "adversarial"],
        cwd="/app/llm",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "status=unavailable" in result.stdout
    assert "ImportError" not in result.stderr


def test_eval_cli_enabled_but_missing_guardrails_is_safely_unavailable():
    code = """
import asyncio
import config
import sys

config.GUARDRAILS_INPUT_ENABLED = True
config.GUARDRAILS_INPUT_CATEGORIES = ["configured"]
from eval import run_evals

sys.argv = ["run_evals", "--only", "adversarial"]
raise SystemExit(asyncio.run(run_evals.main()))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/app/llm",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "status=unavailable" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("guardrails_source", "error_type", "sentinel"),
    [
        (
            "import missing_guardrail_dependency_sentinel\n",
            "ModuleNotFoundError",
            "missing_guardrail_dependency_sentinel",
        ),
        ("BROKEN_GUARDRAILS = True\n", "ImportError", "missing-check-sentinel"),
        ("def syntax_error_sentinel(:\n", "SyntaxError", "syntax_error_sentinel"),
        (
            'raise RuntimeError("runtime-error-sentinel")\n',
            "RuntimeError",
            "runtime-error-sentinel",
        ),
    ],
)
def test_eval_cli_existing_broken_guardrails_is_nonzero_and_redacted(
    tmp_path, guardrails_source, error_type, sentinel
):
    (tmp_path / "guardrails.py").write_text(guardrails_source, encoding="utf-8")
    code = f"""
import asyncio
import config
import sys

config.GUARDRAILS_INPUT_ENABLED = True
config.GUARDRAILS_INPUT_CATEGORIES = ["configured"]
sys.path.insert(0, {str(tmp_path)!r})
from eval import run_evals

sys.argv = ["run_evals", "--only", "adversarial"]
raise SystemExit(asyncio.run(run_evals.main()))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/app/llm",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"status=error error_type={error_type}" in result.stdout
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    assert str(tmp_path) not in result.stdout
    assert str(tmp_path) not in result.stderr
    assert "Traceback" not in result.stderr


def test_eval_cli_real_dataset_error_exits_nonzero_without_raw_exception():
    sentinel = "C:/private/dataset.json subprocess-user-sentinel"
    code = f"""
import asyncio
import sys
from eval import run_evals

class DatasetError(RuntimeError):
    pass

def fail_load(_name):
    raise DatasetError({sentinel!r})

run_evals._load_dataset = fail_load
sys.argv = ["run_evals", "--only", "dataset"]
raise SystemExit(asyncio.run(run_evals.main()))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/app/llm",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "status=error error_type=DatasetError" in result.stdout
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


@pytest.mark.asyncio
async def test_eval_cli_empty_dataset_is_explicit_noop(monkeypatch, capsys):
    monkeypatch.setattr(run_evals, "_load_dataset", lambda _name: [])

    assert await run_evals._run_dataset() == (0, 0)
    assert "пустой" in capsys.readouterr().out
