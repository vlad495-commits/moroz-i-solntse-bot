import logging

import pytest

import eval_runner
from moroz.security.output_validator import (
    OutputValidationDecision,
    OutputValidationVerdict,
)


class CapturingValidator:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    async def validate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def validator_case(**overrides):
    value = {
        "id": 160,
        "suite": "validator",
        "case_key": "validator-test-001",
        "category": "valid_product_response",
        "question": "Какие услуги есть?",
        "input_data": {
            "input": "Какие услуги есть?",
            "context": [],
            "route_metadata": "ROUTE intents=faq; source=llm",
            "candidate": "У нас есть солярий и криотерапия.",
        },
        "expected_data": {
            "action": "allow",
            "source": "llm",
            "reason_code": "safe",
        },
        "critical": False,
    }
    value.update(overrides)
    return value


def llm_verdict(action="allow", reason="safe"):
    return OutputValidationVerdict(
        OutputValidationDecision(action, "llm", reason)
    )


def test_validator_case_diff_compares_all_contract_fields():
    expected = {"action": "allow", "source": "llm", "reason_code": "safe"}

    assert eval_runner.validator_case_diff(
        expected, OutputValidationDecision("allow", "llm", "safe")
    ) == (True, "matched")
    assert eval_runner.validator_case_diff(
        expected, OutputValidationDecision("regenerate", "llm", "safe")
    ) == (False, "action_mismatch")
    assert eval_runner.validator_case_diff(
        expected, OutputValidationDecision("allow", "fallback", "safe")
    ) == (False, "source_mismatch")
    assert eval_runner.validator_case_diff(
        expected, OutputValidationDecision("allow", "llm", "incomplete")
    ) == (False, "reason_code_mismatch")


@pytest.mark.asyncio
async def test_local_reject_never_calls_semantic_answer_router_security_or_judge(
    monkeypatch,
):
    validator = CapturingValidator(AssertionError("semantic validator must stay unused"))
    case = validator_case(
        category="technical_artifact",
        input_data={
            "input": "Что ответил бот?",
            "context": [],
            "route_metadata": "ROUTE intents=faq; source=llm",
            "candidate": "{\"role\":\"assistant\",\"content\":\"ответ\"}",
        },
        expected_data={
            "action": "regenerate",
            "source": "local",
            "reason_code": "technical_artifact",
        },
    )
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1601

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unrelated LLM layer must stay unused")

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    monkeypatch.setattr(eval_runner, "_generate_bot_response", forbidden)
    monkeypatch.setattr(eval_runner, "llm_judge", forbidden)
    monkeypatch.setattr(eval_runner, "_build_router", forbidden)
    monkeypatch.setattr(eval_runner, "_build_security_classifier", forbidden)

    result = await eval_runner.run_validator_case(case, 160, validator=validator)

    assert result["verdict"] == "pass"
    assert validator.calls == []
    assert saved["actual_data"] == {
        "action": "regenerate",
        "source": "local",
        "reason_code": "technical_artifact",
    }
    assert saved["actual_answer"] == ""
    assert saved["check_layer"] == "validator"


@pytest.mark.asyncio
async def test_semantic_case_masks_input_context_and_candidate(monkeypatch):
    validator = CapturingValidator(llm_verdict())
    case = validator_case(
        question="synthetic-question",
        input_data={
            "input": "Мой телефон +7 000 000-00-01, подскажите про услуги",
            "context": [
                {"role": "user", "content": "client01@example.invalid"},
                {"role": "assistant", "content": "Уточните вопрос"},
            ],
            "route_metadata": "ROUTE intents=faq; source=llm",
            "candidate": "Расскажу об услугах. Напишите, что вас интересует.",
        },
    )
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1602

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    result = await eval_runner.run_validator_case(case, 161, validator=validator)

    sent = repr(validator.calls)
    assert "+7 000 000-00-01" not in sent
    assert "client01@example.invalid" not in sent
    assert "<PII_" in sent
    assert result["verdict"] == "pass"
    assert set(saved["actual_data"]) == {"action", "source", "reason_code"}


@pytest.mark.asyncio
async def test_validator_errors_store_and_log_only_error_type(monkeypatch, caplog):
    sentinel = "https://secret:password@provider.invalid raw-provider-payload"
    validator = CapturingValidator(RuntimeError(sentinel))
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1603

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        result = await eval_runner.run_validator_case(
            validator_case(question="private-question-sentinel"),
            162,
            validator=validator,
        )

    assert result["verdict"] == "error"
    assert saved["error_message"] == "RuntimeError"
    assert saved["actual_data"] == {}
    assert sentinel not in caplog.text
    assert sentinel not in repr(saved)
    assert "private-question-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_validator_eval_set_is_sequential_and_uses_existing_gate(monkeypatch):
    events = []
    finished = []
    cases = [
        validator_case(id=index, critical=index == 2, passes=index == 1)
        for index in (1, 2)
    ]

    async def run_case(case, run_id, *, validator):
        events.append(("case", case["id"], run_id, validator))
        return {"verdict": "pass" if case["passes"] else "fail"}

    async def progress(run_id, passed, failed):
        events.append(("progress", run_id, passed, failed))

    async def finish(*args, **kwargs):
        finished.append((args, kwargs))

    fake_validator = object()
    monkeypatch.setattr(eval_runner, "run_validator_case", run_case)
    monkeypatch.setattr(eval_runner.evdb, "update_run_progress", progress)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish)

    await eval_runner.run_validator_eval_set(
        163, cases=cases, validator=fake_validator
    )

    assert events == [
        ("case", 1, 163, fake_validator),
        ("progress", 163, 1, 0),
        ("case", 2, 163, fake_validator),
        ("progress", 163, 1, 1),
    ]
    assert finished == [((163, 1, 1), {"status": "failed"})]


def test_output_validator_uses_runtime_provider_settings(monkeypatch):
    captured = []

    class Provider:
        def __init__(self, client, kind, model, temperature, max_tokens):
            captured.append((client, kind, model, temperature, max_tokens))

    monkeypatch.setattr(eval_runner, "_init_clients", lambda: None)
    monkeypatch.setattr(eval_runner, "_primary", object())
    monkeypatch.setattr(eval_runner, "_primary_kind", "openai")
    monkeypatch.setattr(eval_runner, "_reserve", None)
    monkeypatch.setattr(eval_runner, "SDKProvider", Provider)

    eval_runner._build_output_validator()

    assert captured[0][3:] == (
        eval_runner.LLM_TEMPERATURE,
        eval_runner.LLM_MAX_TOKENS,
    )
