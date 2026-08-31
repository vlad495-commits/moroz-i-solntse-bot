# Task 8 implementation report

Status: DONE

## Scope delivered

- Added an optional generic pre-send guard and atomic delivery hook without importing reactivation into messaging.
- Added explicit Telegram outcome classification: permanent recipient failures, retry-after, ambiguous network/timeout/cancellation, and unchanged generic-message retry semantics.
- Added a shared/exclusive program advisory fence. Delivery holds the shared lock through the network seam; emergency stop takes the exclusive lock and cannot overtake an in-flight send.
- Rechecked program mode, legal/version state, consent, suppression, inbound activity, booking freshness, deletion, human mode, escalation, journey and step immediately before the fake Telegram seam.
- Terminalized linked outbound, step and journey outcomes atomically. Forbidden/NotFound suppress the recipient; BadRequest and ambiguous delivery auto-pause with allowlisted audit fields only.
- Kept `TelegramRetryAfter` on the existing broker retry path and never requeued ambiguous delivery.
- Made test sends version/checksum/chat-bound and allowed `test_sent_at` only after confirmed success.
- Made emergency stop idempotent and limited cancellation to not-started work. If stop wins after network success but before the sent hook, the actual send is recorded and no reminder is created.
- Added startup reconciliation for the generic stale-claim crash window. Only still-reserved linked steps are reconciled, so a repeated startup is a no-op.
- Preserved the Task 7 canonical order: program → customer → controls → journey → step → outbound.

## TDD evidence

- Baseline selected Docker regression: `38 passed in 83.89s`.
- RED: focused collection failed with `ImportError: cannot import name 'classify_delivery_error'`.
- RED: repeated startup reconciliation returned `1` instead of the required idempotent `0`.
- GREEN: final Task 8 delivery/messaging/worker suite: `172 passed in 66.93s`.
- GREEN: broad reactivation/messaging/e2e/worker regression before the final count-only refinement: `366 passed in 388.85s`.
- GREEN: final count refinement probe: `1 passed, 22 deselected in 6.26s`.

All provider behavior was exercised with fake Telegram transports and PostgreSQL-backed tests. No real Telegram, YCLIENTS, LLM, staging or production call was made.

## Concurrency and idempotency evidence

- A blocking fake proves stop waits for an in-flight network call and cancels the next delivery.
- The opposite start order proves an exclusive transition blocks the sender and the final guard observes the new mode.
- Crash/redelivery probes prove stale `sending` becomes `delivery_unknown`, is projected exactly once, and is never resent.
- Repeated stop, delivery hooks, startup reconciliation and outbox relay do not duplicate state transitions, reminders, audits or queue effects.
- Exact once is not claimed across an ambiguous external Telegram response; the safe terminal state is deliberately `delivery_unknown` with no retry.

## Static/runtime checks

- `docker compose ... config --quiet`: passed.
- Docker `python -m compileall -q -f /app/src /app/admin /app/llm /app/worker`: passed.
- `git diff --check`: passed (only Windows LF/CRLF notices).

## Changed files

- `project/src/moroz/messaging/repository.py`
- `project/src/moroz/messaging/telegram.py`
- `project/src/moroz/reactivation/repository.py`
- `project/src/moroz/reactivation/service.py`
- `project/worker/main.py`
- `project/tests/unit/reactivation/test_delivery.py`
- `project/tests/integration/reactivation/test_delivery_fence.py`
- `project/tests/integration/messaging/test_outbox.py`
- `project/tests/unit/reactivation/test_service.py`
- `changelog.md`

## Residual risks and deferrals

- Telegram cannot provide transactional exactly-once delivery across a lost response; ambiguous outcomes are intentionally terminal and pause the program for operator review.
- Real-provider and staging/prod verification is intentionally deferred because Task 8 forbids external calls.
- Task 9+ outcome analytics and admin presentation were not changed.
