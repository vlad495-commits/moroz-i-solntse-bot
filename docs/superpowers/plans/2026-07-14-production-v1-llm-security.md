# LLM Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить scripts-first защиту, PII-маскирование, primary/reserve LLM, роутер и ограниченный output validator.

**Architecture:** Один `SecurityPipeline` оборачивает каждый внешний LLM-вызов. Детерминированные проверки идут первыми; облачный guard получает только обезличенный текст; единый `LLMPort` управляет fallback.

**Tech Stack:** Python, regex, existing OpenAI/Anthropic clients, pytest, current eval runner.

## Global Constraints

- ПД маскируются до любой внешней модели.
- Один output retry максимум.
- Цены, слоты и связи программ берутся из структурированных источников, не из LLM.
- Голос возвращает текстовый шаблон, SpeechKit не подключается.

---

### Task 1: PII masking and session restore

**Files:** Create `project/src/moroz/security/pii.py`; Test `project/tests/unit/security/test_pii.py`.

- [ ] Write tests for phone, email, full name placeholder stability and refusal to restore unknown placeholders.
- [ ] Run test; expect import failure.
- [ ] Implement:

```python
@dataclass(frozen=True)
class MaskedText:
    text: str
    mapping: dict[str, str]

def restore_validated(text: str, mapping: Mapping[str, str]) -> str:
    unknown = set(re.findall(r"<PII_[A-Z]+_\d+>", text)) - mapping.keys()
    if unknown:
        raise UnknownPlaceholder(sorted(unknown))
    return reduce(lambda value, item: value.replace(*item), mapping.items(), text)
```

- [ ] Run tests; expect pass.
- [ ] Commit `feat: добавлено маскирование ПД`.

### Task 2: Primary/reserve LLM port

**Files:** Create `project/src/moroz/security/llm_gateway.py`; Modify `project/llm/llm.py`; Test `project/tests/unit/security/test_llm_gateway.py`.

- [ ] Test primary success, retryable primary failure → reserve, both fail → `LLMUnavailable`.
- [ ] Run red.
- [ ] Implement:

```python
async def complete(self, request: LLMRequest) -> LLMResponse:
    try:
        return await self.primary.complete(request)
    except RetryableLLMError:
        if self.reserve is None:
            raise LLMUnavailable
        return await self.reserve.complete(request)
```

- [ ] Run tests; expect pass and exactly one call per provider.
- [ ] Commit `feat: добавлен резервный LLM gateway`.

### Task 3: Scripts-first guardrails and router

**Files:** Create `project/src/moroz/security/guardrails.py`, `project/src/moroz/messaging/router.py`; Test `project/tests/unit/security/test_guardrails.py`, `project/tests/unit/messaging/test_router.py`.

- [ ] Test length/rate/prompt-leak/stop commands locally and deterministic routing for booking, cancellation, complaint, FAQ.
- [ ] Run red.
- [ ] Implement ordered rules returning typed `GuardDecision` and `RouteDecision`; call guard LLM only when local result is `uncertain` and pass `MaskedText.text`.
- [ ] Run tests; expect pass and zero guard-LLM calls for deterministic inputs.
- [ ] Commit `feat: добавлены scripts-first guardrails и router`.

### Task 4: Output validator with one retry

**Files:** Create `project/src/moroz/security/validator.py`, `project/src/moroz/messaging/orchestrator.py`; Test `project/tests/unit/security/test_validator.py`.

- [ ] Test leaked canary, invented price, medical guarantee and valid answer; test exactly one retry.
- [ ] Run red.
- [ ] Implement:

```python
for attempt in range(2):
    response = await llm.complete(request.with_feedback(feedback))
    verdict = validator.validate(response.text, facts)
    if verdict.ok:
        return response
    feedback = verdict.short_reason
raise UnsafeOutput(verdict.code)
```

- [ ] Run tests; expect pass and safe fallback after two invalid outputs.
- [ ] Commit `feat: добавлен ограниченный output validator`.

### Task 5: Security eval gate

**Files:** Modify `project/llm/eval/dataset.json`, `project/llm/eval/adversarial_dataset.json`, `project/admin/eval_runner.py`; Test `project/tests/e2e/test_security_pipeline.py`.

- [ ] Add concrete cases for consent, PII, prompt leak, medical promise, booking hallucination, fallback and voice template.
- [ ] Run targeted evals; expected red before pipeline wiring.
- [ ] Wire `SecurityPipeline` into worker orchestrator and eval runner using the same entrypoint.
- [ ] Run Docker tests and evals; expect 100% critical and ≥95% total.
- [ ] Commit `test: добавлен security eval gate`; update roadmap/changelog checkpoint.
