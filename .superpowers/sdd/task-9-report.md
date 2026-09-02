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

## Limited review fix

- Independent review reproduced a real same-second precision gap: Telegram's
  `callback.message.date` described the original bot message and could precede
  durable `first_sent_at`, so a later click did not close the journey.
- The webhook now captures one UTC server-receipt timestamp before parsing the
  accepted update and reuses it for every callback transition. The realistic
  250 ms boundary RED was `2 failed`; exact GREEN was `2 passed`.
- Duplicate delivery can still call `answer_callback_query` again. Durable
  business effects and the static reply remain idempotent; per the owner rule,
  this non-safety Minor is documented as technical debt and not expanded.
- The first limited gate after the contract change was `107 passed / 2 failed`;
  both failures were stale privacy assertions for the former message-date and
  epoch semantics. A second gate was `108 passed / 1 failed`; the remaining
  failure proved the test had placed `first_sent_at` briefly in the future.
  No production change followed either diagnostic run.
- With the deterministic boundary (message timestamp 500 ms before durable
  send, both before receipt), the final client-flow/privacy plus canonical
  outcome/delivery lock gate was `45 passed in 138.13s`, without `40P01`.
- Docker compileall, Compose config and `git diff --check` passed after the
  final test-only correction. The broad suite was not repeated.
