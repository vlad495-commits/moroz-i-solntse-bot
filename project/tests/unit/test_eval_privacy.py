import asyncio
import ast
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import eval_routes
import eval_runner
from eval import run_evals
from moroz.security.guardrails import GuardDecision
from moroz.security.llm_gateway import LLMResponse


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
    calls = []

    def check_input(text, *, recent_message_count):
        calls.append((text, recent_message_count))
        return GuardDecision("block", "test_block")

    monkeypatch.setattr(run_evals, "check_input", check_input)
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
    assert calls == [(input_sentinel, 1)]


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


def test_eval_cli_imports_typed_phase5_guard_directly():
    source = Path("/workspace/llm/eval/run_evals.py").read_text(encoding="utf-8")

    assert (
        "from moroz.security.guardrails import GuardDecision, check_input"
        in source
    )
    assert "_load_guardrail_checker" not in source


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.asyncio
async def test_eval_judge_masks_every_interpolated_field(
    monkeypatch,
    kind,
):
    captured = []

    async def create(**kwargs):
        captured.append(kwargs)
        if kind == "openai":
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"score": 1.0, "reasoning": "ok"}'
                        )
                    )
                ]
            )
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text='{"score": 1.0, "reasoning": "ok"}',
                )
            ]
        )

    client = (
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        if kind == "openai"
        else SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(eval_runner, "_judge", client)
    monkeypatch.setattr(eval_runner, "_judge_kind", kind)

    raw_values = (
        "Меня зовут Анна Иванова, телефон +7 999 111-22-33",
        "Ответ отправлен на expected@example.ru",
        "Адрес: улица Секретная, 1",
    )
    assert await eval_runner.llm_judge(*raw_values) == (1.0, "ok")

    sent = repr(captured)
    assert captured
    assert "<PII_" in sent
    assert all(
        sentinel not in sent
        for sentinel in (
            "Анна Иванова",
            "+7 999 111-22-33",
            "expected@example.ru",
            "улица Секретная, 1",
        )
    )


def test_admin_client_factory_disables_sdk_retries(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(eval_runner, "AsyncOpenAI", Client)
    monkeypatch.setattr("anthropic.AsyncAnthropic", Client)

    eval_runner._create_client("configured", None, "openai")
    eval_runner._create_client("configured", None, "anthropic")

    assert [call["max_retries"] for call in calls] == [0, 0]


@pytest.mark.asyncio
async def test_admin_bot_response_uses_shared_security_pipeline(monkeypatch):
    captured = {}

    class Provider:
        def __init__(self, client, kind, model, temperature, max_tokens):
            self.values = (client, kind, model, temperature, max_tokens)

    class Gateway:
        def __init__(self, primary, reserve):
            captured["providers"] = (primary, reserve)

    class Pipeline:
        def __init__(self, gateway, system_prompt, facts):
            captured["pipeline"] = (gateway, system_prompt, facts)

        async def respond(self, question, context, *, recent_message_count):
            captured["request"] = (question, context, recent_message_count)
            return LLMResponse("safe", 1, 1, 0, 2, "fake")

    primary = object()
    reserve = object()
    monkeypatch.setattr(eval_runner, "_primary", primary)
    monkeypatch.setattr(eval_runner, "_primary_kind", "openai")
    monkeypatch.setattr(eval_runner, "_reserve", reserve)
    monkeypatch.setattr(eval_runner, "_reserve_kind", "anthropic")
    monkeypatch.setattr(eval_runner, "SDKProvider", Provider)
    monkeypatch.setattr(eval_runner, "PrimaryReserveGateway", Gateway)
    monkeypatch.setattr(eval_runner, "SecurityPipeline", Pipeline)

    assert await eval_runner._generate_bot_response("Вопрос", "Цена 2400 руб.") == "safe"
    primary_provider, reserve_provider = captured["providers"]
    assert primary_provider.values[:3] == (
        primary,
        "openai",
        eval_runner.LLM_MODEL,
    )
    assert reserve_provider.values[:3] == (
        reserve,
        "anthropic",
        eval_runner.RESERVE_MODEL,
    )
    _, source_prompt, facts = captured["pipeline"]
    assert source_prompt == "Цена 2400 руб."
    assert "2400" in facts.prices
    assert captured["request"] == ("Вопрос", [], 1)


def test_external_sdk_calls_are_limited_to_provider_and_masked_judge_adapter():
    root = Path("/workspace")
    found = set()
    openai_call = ".chat." + "completions." + "create"
    anthropic_call = ".messages." + "create"

    for relative in (
        "src/moroz/security/llm_gateway.py",
        "llm/llm.py",
        "admin/eval_runner.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
            if not isinstance(node, ast.Call):
                continue
            expression = ast.unparse(node.func)
            if not (
                expression.endswith(openai_call)
                or expression.endswith(anthropic_call)
            ):
                continue
            parent = node
            while parent in parents and not isinstance(
                parent,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                parent = parents[parent]
            found.add((relative, getattr(parent, "name", "")))

    assert found == {
        ("src/moroz/security/llm_gateway.py", "complete"),
        ("admin/eval_runner.py", "_invoke_masked_judge"),
    }


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
