import inspect

import llm
from worker.main import _supervise
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts, validate_output


def test_no_hot_reload_listener_or_supervisor_hook():
    assert not hasattr(llm, "prompt_reload_listener")
    assert "prompt_listener" not in inspect.signature(_supervise).parameters


def test_loading_file_updates_prompt_and_tariffs_together(monkeypatch, tmp_path):
    path = tmp_path / "system.md"
    monkeypatch.setattr(llm, "SYSTEM_PROMPT_PATH", path)
    monkeypatch.setattr(llm, "_system_prompt", "")
    monkeypatch.setattr(llm, "_pipeline", SecurityPipeline(
        object(), "", extract_structured_facts("")
    ))
    for rate, total, stale in [(42, 546, 559), (43, 559, 546)]:
        prompt = f"Солярий — {rate} ₽ за минуту."
        path.write_text(prompt, encoding="utf-8")
        llm._load_prompt()
        assert llm._system_prompt == llm._pipeline.system_prompt == prompt
        assert validate_output(
            f"13 минут солярия — {total} ₽.", llm._pipeline.facts, frozenset()
        ).ok
        assert not validate_output(
            f"13 минут солярия — {stale} ₽.", llm._pipeline.facts, frozenset()
        ).ok
