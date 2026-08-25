# Task 1 Report: Strict deterministic + LLM router contract

## Scope

- Worktree: `D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.worktrees\llm-router-evaluations`
- Branch: `codex/llm-router-evaluations`
- No real LLM, Telegram, YCLIENTS, staging, or production request was made.

## Delivered

- `deterministic_route()` resolves exactly one explicit intent; unresolved and multi-intent input falls back to `unknown` without a confidence value.
- `LLMIntentRouter` receives only the bounded masked current message and recent user/assistant context, asks the provider only for strict JSON, validates an allowlist, and has no tools or side effects.
- Added `RouterVerdict`, shared `build_untrusted_input`, OpenAI-only `response_format`, and per-call `LLMUsage` on SDK responses.
- Removed `medical_risk` from the router; medical escalation remains the local Security responsibility.

## TDD evidence

RED Docker command:

```powershell
docker compose -p moroz-router-task1 --env-file D:/AI_Projects/moroz_i_solntse/moroz-i-solntse-bot/.env run --rm test pytest -q tests/unit/messaging/test_router.py tests/unit/security/test_llm_gateway.py
```

Result: expected collection failures for missing `ROUTER_RESPONSE_FORMAT` and `LLMUsage`.

Focused GREEN after implementation:

```powershell
docker compose -p moroz-router-task1 --env-file D:/AI_Projects/moroz_i_solntse/moroz-i-solntse-bot/.env run --build --rm test pytest -q tests/unit/messaging/test_router.py tests/unit/security/test_llm_gateway.py tests/unit/security/test_guardrails.py tests/unit/security/test_pipeline.py
```

Result: `153 passed in 4.55s`.

Final full Docker suite used the required read-only `/docs` and architecture HTML mounts. Result: `1278 passed in 791.31s`, exit `0`.

## Downstream contract correction

The first complete suite found one stale expectation outside the original Task 1 file list: `tests/unit/security/test_pipeline.py` expected the historical deterministic multi-intent value `booking_cancel,faq`. The approved Task 1 plan/spec requires deterministic multi-intent fallback to `unknown`; only that assertion changed. Production code was not changed to preserve obsolete behavior.

## Self-review and concerns

- Confirmed strict schema, intent allowlist, non-finite/bool confidence rejection, cancellation propagation, provider failure sanitization, and no confidence on fallback.
- No concerns remain. The retained stopped `moroz-router-task1-full-verify` container is synthetic verification evidence in the isolated Compose namespace.
