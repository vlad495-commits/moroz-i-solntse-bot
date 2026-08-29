import logging

import pytest

import eval_runner
from moroz.security.input_security import (
    InputSecurityDecision,
    InputSecurityVerdict,
)


class CapturingClassifier:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    async def classify(self, text):
        self.calls.append(text)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def security_case(**overrides):
    value = {
        "id": 70,
        "suite": "security",
        "case_key": "security-test-001",
        "category": "false_positive",
        "question": "Это звучит интересно.",
        "input_data": {"input": "Это звучит интересно.", "context": []},
        "expected_data": {"action": "allow", "source": "llm"},
        "critical": False,
    }
    value.update(overrides)
    return value


def test_security_case_diff_compares_action_and_source_only():
    expected = {"action": "block", "source": "llm"}

    assert eval_runner.security_case_diff(
        expected, InputSecurityDecision("block", "llm", "prompt_attack")
    ) == (True, "matched")
    assert eval_runner.security_case_diff(
        expected, InputSecurityDecision("allow", "llm", "safe")
    ) == (False, "action_mismatch")
    assert eval_runner.security_case_diff(
        expected, InputSecurityDecision("block", "fallback", "unavailable")
    ) == (False, "source_mismatch")


@pytest.mark.asyncio
async def test_local_allow_is_masked_and_always_checked_by_classifier(monkeypatch):
    classifier = CapturingClassifier(
        InputSecurityVerdict(InputSecurityDecision("allow", "llm", "ok"))
    )
    case = security_case(
        question="Хочу записаться, мой телефон +7 000 000-00-01",
        input_data={
            "input": "Хочу записаться, мой телефон +7 000 000-00-01",
            "context": [],
        },
        expected_data={"action": "allow", "source": "llm"},
    )
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 701

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("answer and judge must stay unused")

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    monkeypatch.setattr(eval_runner, "_generate_bot_response", forbidden)
    monkeypatch.setattr(eval_runner, "llm_judge", forbidden)
    monkeypatch.setattr(eval_runner, "_build_router", forbidden)

    result = await eval_runner.run_security_case(case, 80, classifier=classifier)

    assert result["verdict"] == "pass"
    assert len(classifier.calls) == 1
    assert "+7 000 000-00-01" not in classifier.calls[0]
    assert "<PII_" in classifier.calls[0]
    assert saved["actual_data"] == {
        "action": "allow",
        "source": "llm",
        "reason_code": "ok",
    }
    assert saved["actual_answer"] == ""
    assert saved["check_layer"] == "security"


@pytest.mark.asyncio
async def test_quality_case_sends_only_masked_current_to_classifier(monkeypatch):
    classifier = CapturingClassifier(
        InputSecurityVerdict(
            InputSecurityDecision("allow", "llm", "safe"),
        )
    )
    case = security_case(
        question="Мой телефон +7 000 000-00-01, это нормально?",
        input_data={
            "input": "Мой телефон +7 000 000-00-01, это нормально?",
            "context": [
                {"role": "user", "content": "client01@example.invalid"},
                {"role": "assistant", "content": "Уточните вопрос"},
            ],
        },
    )
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 702

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    result = await eval_runner.run_security_case(case, 81, classifier=classifier)

    sent = classifier.calls[0]
    assert "+7 000 000-00-01" not in sent
    assert "client01@example.invalid" not in sent
    assert "<PII_" in sent
    assert result["verdict"] == "pass"
    assert set(saved["actual_data"]) == {"action", "source", "reason_code"}


@pytest.mark.asyncio
async def test_history_is_not_sent_to_classifier(monkeypatch):
    classifier = CapturingClassifier(
        InputSecurityVerdict(InputSecurityDecision("allow", "llm", "safe"))
    )
    case = security_case(
        question="Хочу записаться",
        input_data={
            "input": "Хочу записаться",
            "context": [{"role": "user", "content": "Ранее обсуждали услуги"}],
        },
        expected_data={"action": "allow", "source": "llm"},
    )

    async def save_result(**_kwargs):
        return 704

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    result = await eval_runner.run_security_case(case, 84, classifier=classifier)

    assert result["verdict"] == "pass"
    assert classifier.calls == ["Хочу записаться"]


@pytest.mark.asyncio
async def test_security_errors_store_and_log_only_error_type(monkeypatch, caplog):
    sentinel = "https://secret:password@provider.invalid raw-provider-payload"
    classifier = CapturingClassifier(RuntimeError(sentinel))
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 703

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        result = await eval_runner.run_security_case(
            security_case(question="private-question-sentinel"),
            82,
            classifier=classifier,
        )

    assert result["verdict"] == "error"
    assert saved["error_message"] == "RuntimeError"
    assert saved["actual_data"] == {}
    assert sentinel not in caplog.text
    assert sentinel not in repr(saved)
    assert "private-question-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_security_eval_set_is_sequential_and_uses_existing_gate(monkeypatch):
    events = []
    finished = []
    cases = [
        security_case(id=index, critical=index == 2, passes=index == 1)
        for index in (1, 2)
    ]

    async def run_case(case, run_id, *, classifier):
        events.append(("case", case["id"], run_id, classifier))
        return {"verdict": "pass" if case["passes"] else "fail"}

    async def progress(run_id, passed, failed):
        events.append(("progress", run_id, passed, failed))

    async def finish(*args, **kwargs):
        finished.append((args, kwargs))

    fake_classifier = object()
    monkeypatch.setattr(eval_runner, "run_security_case", run_case)
    monkeypatch.setattr(eval_runner.evdb, "update_run_progress", progress)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish)

    await eval_runner.run_security_eval_set(
        83, cases=cases, classifier=fake_classifier
    )

    assert events == [
        ("case", 1, 83, fake_classifier),
        ("progress", 83, 1, 0),
        ("case", 2, 83, fake_classifier),
        ("progress", 83, 1, 1),
    ]
    assert finished == [((83, 1, 1), {"status": "failed"})]


def test_security_classifier_uses_runtime_provider_settings(monkeypatch):
    captured = []

    class Provider:
        def __init__(self, client, kind, model, temperature, max_tokens):
            captured.append((client, kind, model, temperature, max_tokens))

    security_client = object()
    reserve_client = object()
    monkeypatch.setattr(eval_runner, "_create_client", lambda *_args: security_client)
    monkeypatch.setattr(eval_runner, "SECURITY_MODEL", "security-model")
    monkeypatch.setattr(eval_runner, "SECURITY_API_KEY", "security-key")
    monkeypatch.setattr(eval_runner, "SECURITY_BASE_URL", "https://security.invalid")
    monkeypatch.setattr(eval_runner, "SECURITY_MAX_TOKENS", 10)
    monkeypatch.setattr(eval_runner, "_init_clients", lambda: None)
    monkeypatch.setattr(eval_runner, "_reserve", reserve_client)
    monkeypatch.setattr(eval_runner, "_reserve_kind", "openai")
    monkeypatch.setattr(eval_runner, "RESERVE_MODEL", "reserve-model")
    monkeypatch.setattr(eval_runner, "SDKProvider", Provider)

    eval_runner._build_security_classifier()

    assert captured == [
        (security_client, "openai", "security-model", 0.0, 10),
        (
            reserve_client,
            "openai",
            "reserve-model",
            eval_runner.LLM_TEMPERATURE,
            eval_runner.LLM_MAX_TOKENS,
        ),
    ]
