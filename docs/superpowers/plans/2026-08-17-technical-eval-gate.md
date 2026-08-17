# Technical Eval Gate Without Live Prices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить один локальный технический eval без цен и устранить ложный `invented_slot` для корректной walk-in политики.

**Architecture:** Существующий CLI получает только новый агрегирующий режим `technical`; новых datasets и сервисов не создаётся. Structural evaluator переиспользуется отдельно от общего business dataset, а validator получает узкое исключение для walk-in/общих часов без ослабления проверки реальных свободных слотов.

**Tech Stack:** Python 3.12, pytest, Docker Compose, существующие security/eval modules.

## Global Constraints

- Все project runtime/test commands выполняются только через Docker Compose с отдельным project name `moroz-technical-eval-20260817-1668`.
- `dataset.json` и `adversarial_dataset.json` не изменяются.
- Judge, YCLIENTS, Telegram, staging и production не вызываются.
- Секреты не выводятся и не меняются; deploy и push не выполняются.
- Каждый production change проходит Docker RED → минимальный GREEN.

---

### Task 1: Technical CLI gate

**Files:**
- Modify: `project/llm/eval/run_evals.py`
- Test: `project/tests/unit/test_eval_privacy.py`

**Interfaces:**
- Produces: `_run_structural() -> tuple[SecurityEvalResult, ...]`.
- Produces: CLI `python -m eval.run_evals --only technical`.
- Consumes: existing `_run_adversarial()`, `_run_catalog()` and `evaluate_structural_case()`.

- [x] **Step 1: Write the failing CLI test**

Add an async test that stubs the three technical batches, makes `_run_dataset`
fail if called, sets `sys.argv` to `--only technical`, and asserts one combined
gate containing adversarial, structural and catalog results.

```python
@pytest.mark.asyncio
async def test_technical_cli_runs_only_local_technical_batches(monkeypatch):
    called = []
    monkeypatch.setattr(run_evals, "_run_adversarial", batch("adversarial", called))
    monkeypatch.setattr(run_evals, "_run_structural", batch("structural", called))
    monkeypatch.setattr(run_evals, "_run_catalog", batch("catalog", called))
    monkeypatch.setattr(run_evals, "_run_dataset", forbidden_batch)
    monkeypatch.setattr(sys, "argv", ["run_evals", "--only", "technical"])
    assert await run_evals.main() == 0
    assert called == ["adversarial", "structural", "catalog"]
```

- [x] **Step 2: Run Docker RED**

Run:

```powershell
docker compose -p moroz-technical-eval-20260817-1668 --env-file <external-env> run --build --rm -T test sh -lc "cd /app && pytest -q tests/unit/test_eval_privacy.py -k technical_cli"
```

Expected: FAIL because `technical` is not an argparse choice and `_run_structural` does not exist.

- [x] **Step 3: Implement the smallest reuse-only mode**

Add `_run_structural()` that loads `dataset.json`, evaluates only cases for
which `evaluate_structural_case(case)` returns non-`None`, prints one batch, and
never initializes an LLM. Add `technical` to argparse choices and route it only
through adversarial, structural and catalog batches.

- [x] **Step 4: Run focused GREEN**

Run the same Docker command. Expected: selected tests PASS.

- [x] **Step 5: Commit**

```powershell
git add project/llm/eval/run_evals.py project/tests/unit/test_eval_privacy.py changelog.md
git commit -m "feat: добавлен technical eval без цен"
```

### Task 2: Walk-in validator regression and final evidence

**Files:**
- Modify: `project/src/moroz/security/validator.py`
- Modify: `project/tests/unit/security/test_validator.py`
- Modify: `project/tests/unit/security/test_pipeline.py`
- Modify: `project/llm/eval/local_2026-08-17_report.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- `validate_output(text, facts, allowed_placeholders)` accepts general walk-in policy with opening hours and no slot facts.
- The same function still returns `invented_slot` for positive availability tied to an unapproved date/time.

- [x] **Step 1: Write the failing walk-in regression**

```python
def test_walk_in_policy_with_hours_is_not_an_invented_slot() -> None:
    answer = (
        "На солярий, коллариум и коллагенарий можно без записи. "
        "Центр доступен ежедневно с 10:00 до 21:00."
    )
    assert validate_output(answer, _facts(slots=frozenset()), frozenset()).ok
```

Keep an adjacent assertion that `Свободно сегодня в 15:00` remains
`invented_slot`.

- [x] **Step 2: Run Docker RED**

Run:

```powershell
docker compose -p moroz-technical-eval-20260817-1668 --env-file <external-env> run --rm -T test sh -lc "cd /app && pytest -q tests/unit/security/test_validator.py -k walk_in"
```

Expected: FAIL with `invented_slot` for the walk-in answer.

- [x] **Step 3: Apply the narrow validator fix**

Teach availability detection to ignore the general service-access meaning in
the same way it already ignores negated availability and general opening
hours. Do not weaken date+time assertions containing `свободно`, `окно` or
`можно записаться`.

- [x] **Step 4: Run GREEN and complete technical eval**

Run:

```powershell
docker compose -p moroz-technical-eval-20260817-1668 --env-file <external-env> run --build --rm -T test sh -lc "cd /app && pytest -q tests/unit/security/test_validator.py tests/unit/security/test_pipeline.py tests/unit/test_eval_privacy.py tests/unit/security/test_eval_catalog.py tests/unit/security/test_guardrails.py"
docker compose -p moroz-technical-eval-20260817-1668 --env-file <external-env> run --rm -T test sh -lc "cd /app/llm && python -m eval.run_evals --only technical"
```

Expected: pytest PASS; technical gate PASS with 20 adversarial + 5 structural + 6 catalog cases, zero judge calls.

- [x] **Step 5: Update evidence and clean exact Docker namespace**

Record exact results in report, roadmap and changelog. Inspect resources with
the exact Compose label, then remove only `moroz-technical-eval-20260817-1668`
and confirm its containers, volumes, networks and named images are zero.

- [x] **Step 6: Verify and commit**

Run `git diff --check`, confirm datasets unchanged relative to
`f356c2c372dc67e8ebd1c2e6e433e5946a10e782`, then commit:

```powershell
git add project/src/moroz/security/validator.py project/tests/unit/security/test_validator.py project/tests/unit/security/test_pipeline.py project/llm/eval/local_2026-08-17_report.md 'Дорожная карта.md' changelog.md
git commit -m "fix: walk-in включён в technical eval gate"
```

### Task 3: Review fail-closed fixes

**Files:**
- Modify: `project/src/moroz/security/validator.py`
- Modify: `project/llm/eval/run_evals.py`
- Test: `project/tests/unit/security/test_validator.py`
- Test: `project/tests/unit/test_eval_privacy.py`

- [x] Любое `доступн*` рядом с временем требует slot facts; безопасный общий график использует формулировку `центр работает`.
- [x] Technical adversarial regression завершается local FAIL без primary LLM вызова.
- [x] Пустой обязательный technical batch делает общий gate FAIL.
- [x] Structural dataset load-error является critical FAIL.
- [ ] Выполнить Docker RED/GREEN, полный suite, re-review и exact cleanup.
