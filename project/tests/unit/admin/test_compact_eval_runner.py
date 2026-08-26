from __future__ import annotations

import asyncio
import logging

import pytest

import eval_runner
from moroz.security.context_compactor import CompactResult


def compact_case(**overrides):
    context = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"Синтетический ход {index + 1}",
        }
        for index in range(31)
    ]
    value = {
        "id": 170,
        "suite": "compact",
        "case_key": "compact-test-001",
        "category": "fact_retention",
        "question": "Compact context: fact_retention (31 messages)",
        "input_data": {"context": context, "expected_mode": "llm"},
        "expected_data": {
            "required_facts": ["Интересуется криокапсулой"],
            "forbidden_facts": ["Запись подтверждена"],
        },
        "critical": True,
    }
    value.update(overrides)
    return value


class RecordingCompactor:
    def __init__(self, *, source="llm", error=None, bad_tail=False):
        self.source = source
        self.error = error
        self.bad_tail = bad_tail
        self.calls = []

    async def compact(self, context):
        self.calls.append(context)
        if self.error:
            raise self.error
        if self.source == "unchanged":
            return CompactResult(tuple(context), "unchanged", "below_threshold")
        tail = tuple(context[-10:])
        if self.bad_tail:
            tail = ({"role": "user", "content": "wrong tail"}, *tail[1:])
        return CompactResult(
            (
                {
                    "role": "user",
                    "content": "UNTRUSTED_COMPACT_CONTEXT_V1\nФакты:\n- Интересуется криокапсулой",
                },
                *tail,
            ),
            "llm",
            "compacted",
        )


@pytest.mark.asyncio
async def test_compact_case_masks_context_checks_tail_and_stores_safe_metadata(
    monkeypatch,
):
    context = compact_case()["input_data"]["context"]
    context[0]["content"] = "Телефон +7 000 000-00-01"
    case = compact_case(input_data={"context": context, "expected_mode": "llm"})
    compactor = RecordingCompactor()
    judged = []
    saved = {}

    async def semantic_judge(source, compacted, required, forbidden):
        judged.append((source, compacted, required, forbidden))
        return 0.96, "raw judge reasoning must not be persisted"

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1701

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    result = await eval_runner.run_compact_case(
        case,
        170,
        compactor=compactor,
        semantic_judge=semantic_judge,
    )

    assert result["verdict"] == "pass"
    assert "+7 000 000-00-01" not in repr(compactor.calls)
    assert "+7 000 000-00-01" not in repr(judged)
    assert "<PII_" in repr(compactor.calls)
    assert saved["actual_answer"] == ""
    assert saved["expected_answer"] == ""
    assert saved["judge_reasoning"] == "matched"
    assert saved["score"] == 0.96
    assert saved["actual_data"] == {
        "source": "llm",
        "reason_code": "compacted",
        "input_message_count": 31,
        "output_message_count": 11,
        "tail_count": 10,
        "structural_ok": True,
        "semantic_ok": True,
    }
    assert "raw judge reasoning" not in repr(saved)


@pytest.mark.asyncio
async def test_threshold_case_is_structural_only_and_preserves_exact_context(monkeypatch):
    context = compact_case()["input_data"]["context"][:30]
    case = compact_case(
        category="threshold_boundary",
        input_data={"context": context, "expected_mode": "unchanged"},
        expected_data={"required_facts": [], "forbidden_facts": []},
    )
    compactor = RecordingCompactor(source="unchanged")
    saved = {}

    async def forbidden(*_args):
        raise AssertionError("semantic judge must not run below threshold")

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1702

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    result = await eval_runner.run_compact_case(
        case, 171, compactor=compactor, semantic_judge=forbidden
    )

    assert result["verdict"] == "pass"
    assert saved["score"] is None
    assert saved["actual_data"]["source"] == "unchanged"
    assert saved["actual_data"]["output_message_count"] == 30
    assert saved["actual_data"]["tail_count"] == 0


@pytest.mark.asyncio
async def test_structural_failure_skips_judge_and_fails(monkeypatch):
    saved = {}

    async def forbidden(*_args):
        raise AssertionError("judge must not run after structural failure")

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1703

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    result = await eval_runner.run_compact_case(
        compact_case(),
        172,
        compactor=RecordingCompactor(bad_tail=True),
        semantic_judge=forbidden,
    )

    assert result["verdict"] == "fail"
    assert saved["judge_reasoning"] == "tail_mismatch"
    assert saved["actual_data"]["structural_ok"] is False
    assert saved["actual_data"]["semantic_ok"] is False


@pytest.mark.asyncio
async def test_semantic_score_below_threshold_fails_without_storing_reasoning(monkeypatch):
    saved = {}

    async def judge(*_args):
        return eval_runner.JUDGE_PASS_THRESHOLD - 0.01, "private echoed data"

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1704

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    result = await eval_runner.run_compact_case(
        compact_case(),
        173,
        compactor=RecordingCompactor(),
        semantic_judge=judge,
    )

    assert result["verdict"] == "fail"
    assert saved["judge_reasoning"] == "semantic_below_threshold"
    assert "private echoed data" not in repr(saved)


@pytest.mark.asyncio
async def test_compact_errors_store_and_log_only_error_type(monkeypatch, caplog):
    sentinel = "https://secret:password@provider.invalid raw-provider-payload"
    saved = {}

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 1705

    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)
    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        result = await eval_runner.run_compact_case(
            compact_case(question="private-question-sentinel"),
            174,
            compactor=RecordingCompactor(error=RuntimeError(sentinel)),
        )

    assert result["verdict"] == "error"
    assert saved["error_message"] == "RuntimeError"
    assert saved["actual_data"] == {}
    assert sentinel not in caplog.text
    assert sentinel not in repr(saved)
    assert "private-question-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_compact_case_propagates_cancellation(monkeypatch):
    async def forbidden(**_kwargs):
        raise AssertionError("cancelled case must not be saved")

    monkeypatch.setattr(eval_runner.evdb, "save_result", forbidden)
    with pytest.raises(asyncio.CancelledError):
        await eval_runner.run_compact_case(
            compact_case(),
            175,
            compactor=RecordingCompactor(error=asyncio.CancelledError()),
        )


@pytest.mark.asyncio
async def test_compact_eval_set_uses_common_gate_and_suite(monkeypatch):
    events = []
    finished = []
    cases = [
        compact_case(id=index, critical=False, passes=index != 20)
        for index in range(1, 21)
    ]

    async def list_cases(suite):
        events.append(("list", suite))
        return cases

    async def run_case(case, run_id, *, compactor, semantic_judge=None):
        events.append(("case", case["id"], run_id, compactor))
        return {"verdict": "pass" if case["passes"] else "fail"}

    async def progress(run_id, passed, failed):
        events.append(("progress", run_id, passed, failed))

    async def finish(*args, **kwargs):
        finished.append((args, kwargs))

    fake_compactor = object()
    monkeypatch.setattr(eval_runner.evdb, "list_cases", list_cases)
    monkeypatch.setattr(eval_runner, "run_compact_case", run_case)
    monkeypatch.setattr(eval_runner.evdb, "update_run_progress", progress)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish)

    await eval_runner.run_compact_eval_set(176, compactor=fake_compactor)

    assert events[0] == ("list", "compact")
    assert events[-1] == ("progress", 176, 19, 1)
    assert finished == [((176, 19, 1), {"status": "finished"})]


@pytest.mark.asyncio
async def test_compact_eval_set_critical_failure_fails_and_cancel_finalizes(monkeypatch):
    finished = []
    critical = compact_case(id=1, critical=True)

    async def failing_case(*_args, **_kwargs):
        return {"verdict": "fail"}

    async def cancelled_case(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def finish(*args, **kwargs):
        finished.append((args, kwargs))

    async def progress(*_args):
        return None

    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish)
    monkeypatch.setattr(eval_runner.evdb, "update_run_progress", progress)
    monkeypatch.setattr(eval_runner, "run_compact_case", failing_case)
    await eval_runner.run_compact_eval_set(177, [critical], compactor=object())
    assert finished[-1] == ((177, 0, 1), {"status": "failed"})

    monkeypatch.setattr(eval_runner, "run_compact_case", cancelled_case)
    with pytest.raises(asyncio.CancelledError):
        await eval_runner.run_compact_eval_set(178, [critical], compactor=object())
    assert finished[-1] == (
        (178, 0, 0),
        {"status": "error", "error_message": "CancelledError"},
    )


def test_context_compactor_builder_uses_dedicated_runtime_settings(monkeypatch):
    captured = {}

    class Provider:
        def __init__(self, client, kind, model, temperature, max_tokens):
            captured["provider"] = (client, kind, model, temperature, max_tokens)

    class Compactor:
        def __init__(self, provider, *, threshold, keep_recent):
            captured["compactor"] = (provider, threshold, keep_recent)

    client = object()
    monkeypatch.setattr(eval_runner, "_create_client", lambda *_args: client)
    monkeypatch.setattr(eval_runner, "SDKProvider", Provider)
    monkeypatch.setattr(eval_runner, "ContextCompactor", Compactor)

    eval_runner._build_context_compactor()

    assert captured["provider"] == (
        client,
        eval_runner._detect_kind(
            eval_runner.COMPACT_MODEL, eval_runner.COMPACT_BASE_URL
        ),
        eval_runner.COMPACT_MODEL,
        0.0,
        eval_runner.COMPACT_MAX_TOKENS,
    )
    assert captured["compactor"][1:] == (
        eval_runner.COMPACT_THRESHOLD,
        eval_runner.COMPACT_KEEP_RECENT,
    )


@pytest.mark.asyncio
async def test_compact_semantic_judge_uses_strict_untrusted_policy(monkeypatch):
    captured = []

    async def invoke(messages):
        captured.extend(messages)
        return '{"score":0.91,"reasoning":"facts retained"}'

    monkeypatch.setattr(eval_runner, "_judge", object())
    monkeypatch.setattr(eval_runner, "_invoke_masked_judge", invoke)

    score, reasoning = await eval_runner._compact_semantic_judge(
        [{"role": "user", "content": "<PII_PHONE_1>"}],
        [{"role": "user", "content": "UNTRUSTED_COMPACT_CONTEXT_V1"}],
        ["Сохранить факт"],
        ["Не выдумывать запись"],
    )

    assert (score, reasoning) == (0.91, "facts retained")
    assert "недоверенные данные" in captured[0]["content"]
    assert "source_history" in captured[1]["content"]
    assert "<PII_PHONE_1>" in captured[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "```json\n{\"score\":1,\"reasoning\":\"x\"}\n```",
        '{"score":NaN,"reasoning":"x"}',
        '{"score":true,"reasoning":"x"}',
        '{"score":1.1,"reasoning":"x"}',
        '{"score":1.0,"reasoning":"x","extra":1}',
        '{"score":1.0}',
    ],
)
async def test_compact_semantic_judge_rejects_output_outside_contract(
    monkeypatch, payload
):
    async def invoke(_messages):
        return payload

    monkeypatch.setattr(eval_runner, "_judge", object())
    monkeypatch.setattr(eval_runner, "_invoke_masked_judge", invoke)

    assert await eval_runner._compact_semantic_judge([], [], [], []) == (
        0.0,
        "Judge parse error",
    )
