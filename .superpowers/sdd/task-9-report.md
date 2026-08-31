# Task 9 implementation report

Status: DONE

## Scope delivered

- Added one durable `record_inbound(channel, user_id, occurred_at, kind)` path.
- Every private Telegram message advances `last_meaningful_inbound_at` monotonically, including STOP, paused-bot and non-text paths, after the deletion fence.
- An attributable inbound or `reactivation:book` / `reactivation:ask` button closes the open journey as `responded`, stores `replied_at`, cancels scheduled/reserved reminder work and terminalizes a still-pending reminder outbound.
- Attribution is limited to seven days after the confirmed main send; closed journeys are not rewritten.
- Fixed `Записаться`, `Задать вопрос`, `Не писать` inline buttons are attached to real reactivation outbounds.
- Book/ask callbacks answer Telegram first, persist the transition, then send one idempotent static prompt. They create no inbound message or LLM task; the next real message continues through the existing flow.
- `marketing:disable` and STOP retain Task 3 revoke/suppress semantics and close as `suppressed`; the known earlier step reason `consent_revoked` is left unchanged.
- No queue, process, callback layer, campaign builder, dependency or external provider call was added.

## TDD and regression evidence

- RED real-webhook matrix after rebuilding the test image: `7 failed, 1 passed`; failures were the absent inbound transition and book/ask handlers, while the closed-journey control already passed.
- First GREEN: `7 passed, 1 failed`; the remaining assertion expected `suppressed` on the step although the existing revoke-before-suppress transaction intentionally preserves `consent_revoked` there. Production behavior was not changed.
- Final focused webhook/privacy matrix: `43 passed in 128.03s`.
- Planner/policy regression: `76 passed in 109.14s`.
- One final broad Task 7–9/reactivation, messaging, Rabbit worker, consent/STOP, booking and admin run: `632 passed, 1 failed in 1135.07s`. Its only failure was test-only JSONB decoding in the newly added keyboard assertion; all production scenarios, including all 24 lock-order cases, passed without `40P01`.
- Affected assertion after test-only `json.loads` correction: `1 passed in 10.22s`. The wide run was not repeated, per owner directive.

## Static checks

- Docker `python -m compileall -q -f /app/src /app/admin /app/llm /app/worker /app/tests`: passed.
- `docker compose ... config --quiet`: passed.
- `git diff --check`: passed (only Windows LF/CRLF notices).

## Safety review

- Client deletion is checked before and again inside the customer advisory transaction, so inbound handling cannot recreate state behind a completed deletion.
- Canonical customer/control/journey/step locking is reused; no new lock order was introduced.
- A pending reserved reminder outbound becomes `cancelled`; a provider attempt already in `sending` keeps the conservative Task 8 delivery contract and is not overwritten.
- Duplicate callbacks reuse the existing outbound idempotency key and cannot inject a fake user message into the LLM path.
- No real Telegram, YCLIENTS, LLM, staging or production call was made.

## Deliberate deferrals

- No absolute exactly-once mechanism, new recovery infrastructure, campaign abstraction, manual broadcast, runtime LLM copy generation, discounts or extra follow-up was added.
- Funnel analytics remain Task 10; privacy reuse remains Task 11.
